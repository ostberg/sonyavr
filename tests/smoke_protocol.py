"""Protocol smoke test against a fake AVR on 127.0.0.1. No Home Assistant needed.

sonyavr.py does not import Home Assistant, so this script copies it (with
const.py and feedback_frames.py) into a temporary package and runs it against
a local TCP server that acts as the AVR.

Needs asyncping3 (and anyio) installed, for example in a venv:
    python3 -m venv /tmp/venv
    /tmp/venv/bin/pip install "asyncping3==5.1.3" anyio
    /tmp/venv/bin/python -I tests/smoke_protocol.py
"""

import asyncio
import importlib
import pathlib
import shutil
import sys
import tempfile

SRC = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "sonyavr"

tmp = pathlib.Path(tempfile.mkdtemp())
pkg = tmp / "sonyavr_smoke"
pkg.mkdir()
(pkg / "__init__.py").write_text("")
for name in ("sonyavr.py", "const.py", "feedback_frames.py"):
    shutil.copy(SRC / name, pkg / name)
sys.path.insert(0, str(tmp))
s = importlib.import_module("sonyavr_smoke.sonyavr")

# Packets as the AVR sends them (taken from the constants in sonyavr.py)
SOURCE_GAME_ON_UNMUTED = (
    s.FEEDBACK_SOURCE_MAP["game"][0] + s.FEEDBACK_SOURCE_MAP["game"][2]
)
BLUETOOTH_ON_UNMUTED = (
    s.FEEDBACK_SOURCE_MAP["bluetooth"][0] + s.FEEDBACK_SOURCE_MAP["bluetooth"][2]
)


def volume_packet(vol):
    return bytearray([0x02, 0x06, 0xA8, 0x92, 0x00, 0x03, 0x00, vol, 0x00])


class FakeConfigEntry:
    def __init__(self):
        self.options = {}


class FakeAVR:
    def __init__(self):
        self.clients = []
        self.received = []

    async def handle(self, reader, writer):
        self.clients.append(writer)
        try:
            while data := await reader.read(1024):
                self.received.append(bytes(data))
        except OSError:
            pass
        finally:
            writer.close()

    async def send_all(self, data):
        for w in self.clients:
            w.write(data)
            await w.drain()


async def main():
    fake = FakeAVR()
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    avr = s.SonyAVR(None, FakeConfigEntry(), "127.0.0.1", "test", "STR-DN1040", port)
    avr.set_update_cb(lambda *a: None)
    avr.set_remote_update_cb(lambda *a: None)
    avr.set_sensor_update_cb(lambda *a: None)
    avr.volume_max = 45

    notifier = asyncio.get_running_loop().create_task(avr.run_notifier())
    await avr.command_service.async_connect()
    await asyncio.sleep(0.3)
    assert len(fake.clients) == 2, fake.clients

    # 1. Two packets in one TCP write: source + volume must both apply
    await fake.send_all(SOURCE_GAME_ON_UNMUTED + volume_packet(30))
    await asyncio.sleep(0.4)
    assert avr.state_service.source == "game", avr.state_service.source
    assert avr.state_service.power is True
    assert avr.state_service.volume == 30, avr.state_service.volume
    assert avr.state_service.volume_model == 3
    print("PASS coalesced packets")

    # 2. Bluetooth feedback: source property must not raise KeyError
    await fake.send_all(BLUETOOTH_ON_UNMUTED)
    await asyncio.sleep(0.4)
    assert avr.source == "Bluetooth", avr.source
    print("PASS bluetooth source name")

    # 3. Volume above Max Volume (jump of 5): integration sets it back to 45
    fake.received.clear()
    await fake.send_all(volume_packet(50))
    await asyncio.sleep(0.4)
    sent = b"".join(fake.received)
    assert bytes([0x02, 0x06, 0xA0, 0x52, 0x00, 0x03, 0x00, 45]) in sent, sent.hex()
    print("PASS volume cap")

    # 4. Set Volume from the service (text value), clamped to max
    fake.received.clear()
    await avr.async_send_command("Set Volume", "60")
    await asyncio.sleep(0.2)
    sent = b"".join(fake.received)
    assert bytes([0x02, 0x06, 0xA0, 0x52, 0x00, 0x03, 0x00, 45]) in sent, sent.hex()
    await avr.async_send_command("Set Volume", "abc")  # logged, not sent
    await avr.async_send_command("Byte Array String", "zz")  # logged, not sent
    print("PASS set volume text value")

    # 5. Ping interval default when the option was never saved
    assert avr.ping_watcher._interval() == 60
    print("PASS ping interval default")

    await avr.stop_notifier()
    notifier.cancel()
    await avr.command_service.async_disconnect()
    server.close()
    await server.wait_closed()


asyncio.run(main())
shutil.rmtree(tmp)
print("ALL PASS")
