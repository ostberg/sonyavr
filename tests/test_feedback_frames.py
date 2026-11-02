"""Standalone tests for feedback_frames.split_feedback.

Run from the repo root:  python3 -I tests/test_feedback_frames.py
No Home Assistant install is necessary.
"""

import importlib.util
import pathlib
import sys

MODULE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "sonyavr"
    / "feedback_frames.py"
)
spec = importlib.util.spec_from_file_location("feedback_frames", MODULE)
feedback_frames = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feedback_frames)
split = feedback_frames.split_feedback

# Packets taken from the constants in sonyavr.py
SOURCE_FM_ON = bytearray(
    [0x02, 0x07, 0xA8, 0x82, 0x00, 0x2E, 0x00, 0x2B, 0x00, 0x48]
)  # N=7, with checksum (10 bytes)
VOLUME_25 = bytearray(
    [0x02, 0x06, 0xA8, 0x92, 0x00, 0x03, 0x00, 0x19, 0x9A]
)  # N=6, with checksum (9 bytes)
AUTO_STANDBY_ON = bytearray([0x02, 0x03, 0xA8, 0xA4, 0xCC])  # N=3, no checksum
PHASE_AUTO = bytearray([0x2, 0x4, 0xAB, 0x97, 0x48, 0x2])  # N=4, no checksum


def test_single_packets_unchanged():
    for pkt in (SOURCE_FM_ON, VOLUME_25, AUTO_STANDBY_ON, PHASE_AUTO):
        assert split(pkt) == [pkt], pkt


def test_two_packets_with_checksum():
    assert split(SOURCE_FM_ON + VOLUME_25) == [SOURCE_FM_ON, VOLUME_25]


def test_three_packets():
    data = VOLUME_25 + SOURCE_FM_ON + VOLUME_25
    assert split(data) == [VOLUME_25, SOURCE_FM_ON, VOLUME_25]


def test_mixed_with_and_without_checksum():
    assert split(SOURCE_FM_ON + AUTO_STANDBY_ON) == [SOURCE_FM_ON, AUTO_STANDBY_ON]
    assert split(AUTO_STANDBY_ON + VOLUME_25) == [AUTO_STANDBY_ON, VOLUME_25]


def test_packet_that_ends_in_stx_byte():
    # PHASE_AUTO ends in 0x02, so a careless splitter could cut it wrongly.
    assert split(PHASE_AUTO + VOLUME_25) == [PHASE_AUTO, VOLUME_25]


def test_unknown_data_unchanged():
    junk = bytearray([0x10, 0x20, 0x30])
    assert split(junk) == [junk]
    # Valid packet followed by junk: return the whole read unchanged
    assert split(SOURCE_FM_ON + junk) == [SOURCE_FM_ON + junk]


def test_partial_packet_unchanged():
    data = SOURCE_FM_ON + VOLUME_25[:4]
    assert split(data) == [data]


def test_empty():
    assert split(b"") == []


def test_no_exponential_backtracking():
    import time

    data = bytearray([0x02] * 1024) + bytearray([0x10])
    t0 = time.monotonic()
    assert split(data) == [data]
    assert time.monotonic() - t0 < 1.0


def test_many_packets_limit():
    data = VOLUME_25 * 100  # more than _MAX_FRAMES: returned unchanged
    assert split(data) == [data]


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"PASS {name}")
            except AssertionError as err:
                failures += 1
                print(f"FAIL {name}: {err}")
    sys.exit(1 if failures else 0)
