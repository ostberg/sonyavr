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


def source_packet(source, power, muted):
    """Input packet with power and mute, as in FEEDBACK_SOURCE_MAP."""
    fb = s.FEEDBACK_SOURCE_MAP[source]
    if not power:
        return fb[0] + fb[3]
    return fb[0] + (fb[2] if muted else fb[1])


SOURCE_BY_CODE = {cmd[5]: key for key, cmd in s.CMD_SOURCE_MAP.items()}


class FakeAVR:
    """Fake AVR. With state=None it only records (tests 1 to 5).

    With a state it acts like a volume model 3 AVR: it applies power, mute,
    input and volume commands and reports changes on all connections. The
    first volume down only opens the volume display; a second one while the
    display is open lowers the volume. A volume command cancels mute, as on
    the STR-DN1040 (live test, 4 Nov 2026). When status_request is True it
    answers the status request, only on the connection that asked; when False
    it answers with NACK (0xFE). In standby it answers only the status request.
    """

    def __init__(self, state=None, status_request=True):
        self.clients = []
        self.received = []
        self.state = state
        self.status_request = status_request
        self.display_until = 0.0

    async def handle(self, reader, writer):
        self.clients.append(writer)
        try:
            while data := await reader.read(1024):
                self.received.append(bytes(data))
                if self.state is not None:
                    for cmd in s.split_feedback(data):
                        await self.answer(bytes(cmd), writer)
        except OSError:
            pass
        finally:
            writer.close()

    async def send_all(self, data):
        for w in self.clients:
            w.write(data)
            await w.drain()

    def commands(self):
        return [bytes(c) for d in self.received for c in s.split_feedback(d)]

    def count(self, cmd):
        return self.commands().count(bytes(cmd))

    async def report_source(self):
        st = self.state
        await self.send_all(source_packet(st["source"], st["power"], st["muted"]))

    async def answer(self, cmd, writer):
        st = self.state
        now = asyncio.get_running_loop().time()
        if cmd == bytes(s.CMD_STATUS_REQUEST):
            if self.status_request:
                writer.write(source_packet(st["source"], st["power"], st["muted"]))
            else:
                writer.write(b"\xfe")
            await writer.drain()
        elif cmd == bytes(s.CMD_POWER_ON):
            st["power"] = True
            await self.report_source()
        elif not st["power"]:
            return  # standby: no other commands
        elif cmd == bytes(s.CMD_POWER_OFF):
            st["power"] = False
            await self.report_source()
        elif cmd in (bytes(s.CMD_MUTE), bytes(s.CMD_UNMUTE)):
            st["muted"] = cmd == bytes(s.CMD_MUTE)
            await self.report_source()
        elif cmd[:4] == bytes([0x02, 0x04, 0xA0, 0x42]) and cmd[4] == 0:
            st["source"] = SOURCE_BY_CODE.get(cmd[5], st["source"])
            await self.report_source()
        elif cmd in (bytes(s.CMD_VOLUME_DOWN), bytes(s.CMD_VOLUME_UP)):
            if st["muted"]:
                st["muted"] = False
                await self.report_source()
            if now < self.display_until:
                st["volume"] += -1 if cmd == bytes(s.CMD_VOLUME_DOWN) else 1
            self.display_until = now + 5
            await self.send_all(volume_packet(st["volume"]))
        elif cmd[:7] == bytes([0x02, 0x06, 0xA0, 0x52, 0x00, 0x03, 0x00]):
            st["volume"] = cmd[7]
            await self.send_all(volume_packet(st["volume"]))


CHANGING_COMMANDS = [
    s.CMD_MUTE,
    s.CMD_UNMUTE,
    s.CMD_POWER_ON,
    s.CMD_POWER_OFF,
    *s.CMD_SOURCE_MAP.values(),
]


async def startup(label, state, status_request, expect, after=None):
    """Run the startup probe as media_player._async_startup does (power cycle
    off) and check that the fake AVR did not change (or changed to after)."""
    before = dict(after if after is not None else state)
    fake = FakeAVR(state, status_request)
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

    power = await avr.async_get_power_state()
    await avr.async_update_status()
    await asyncio.sleep(0.3)

    st = avr.state_service
    got = {
        "power": power,
        "state_power": st.power,
        "muted": st.muted,
        "source": st.source,
        "volume": st.volume if st.volume_model else None,
        "volume_down": fake.count(s.CMD_VOLUME_DOWN),
    }
    assert got == expect, (label, got, expect)
    assert state == before, (label, "AVR changed", before, state)
    for cmd in CHANGING_COMMANDS:
        assert fake.count(cmd) == 0, (label, "sent", bytes(cmd).hex())

    await avr.stop_notifier()
    notifier.cancel()
    await avr.command_service.async_disconnect()
    server.close()
    await server.wait_closed()
    print("PASS startup:", label)


async def startup_tests():
    # 6. AVR on and muted, status request answered: input, power and mute
    # from the answer; no volume down, because it would cancel mute (fork
    # change 15), so the volume stays unknown; nothing changed
    await startup(
        "on, muted, status request answered",
        {"power": True, "muted": True, "source": "game", "volume": 20},
        True,
        {
            "power": True,
            "state_power": True,
            "muted": True,
            "source": "game",
            "volume": None,
            "volume_down": 0,
        },
    )
    # 7. AVR on and muted, no status request (NACK): the only way to find out
    # that it is on is one volume down, which cancels mute. Unavoidable without
    # the status request (the STR-DN1040 answers it). Only one volume down
    # (a second one would lower the volume).
    await startup(
        "on, muted, no status request",
        {"power": True, "muted": True, "source": "tv", "volume": 12},
        False,
        {
            "power": True,
            "state_power": True,
            "muted": False,
            "source": "tv",
            "volume": 12,
            "volume_down": 1,
        },
        after={"power": True, "muted": False, "source": "tv", "volume": 12},
    )
    # 10. AVR on, not muted, status request answered: volume from one volume
    # down (only opens the display), nothing changed
    await startup(
        "on, not muted, status request answered",
        {"power": True, "muted": False, "source": "tv", "volume": 12},
        True,
        {
            "power": True,
            "state_power": True,
            "muted": False,
            "source": "tv",
            "volume": 12,
            "volume_down": 1,
        },
    )
    # 8. AVR in standby, status request answered: off, no volume down
    await startup(
        "standby, status request answered",
        {"power": False, "muted": False, "source": "tv", "volume": 12},
        True,
        {
            "power": False,
            "state_power": False,
            "muted": None,
            "source": "tv",
            "volume": None,
            "volume_down": 0,
        },
    )
    # 9. AVR in standby, no status request: no answer at all, so off
    await startup(
        "standby, no status request",
        {"power": False, "muted": False, "source": "tv", "volume": 12},
        False,
        {
            "power": False,
            "state_power": None,
            "muted": None,
            "source": None,
            "volume": None,
            "volume_down": 1,
        },
    )


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

    await startup_tests()


asyncio.run(main())
shutil.rmtree(tmp)
print("ALL PASS")
