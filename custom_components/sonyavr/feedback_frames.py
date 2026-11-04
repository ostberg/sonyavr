"""Split a TCP read from the AVR into separate feedback packets.

A packet is: 0x02 (STX), a length byte N, N data bytes and, usually, one
checksum byte. Some feedback packets known to this integration have no
checksum byte. TCP can deliver two or more packets in one read, so the read
must be split before it is matched against the known packets.

The splitter is careful: it changes a read only when the whole read divides
exactly into valid packets. In all other cases it returns the read unchanged,
which is the behavior before this module was added.

This module has no Home Assistant imports, so it can be tested on its own.
"""

STX = 0x02
# Packet length = N + 3 (with checksum) or N + 2 (without). Try N + 3 first.
_EXTRA_BYTES = (3, 2)
# A single read rarely holds more than a few packets. This limit stops a
# pathological read from causing deep recursion.
_MAX_FRAMES = 64


def _split(data: bytes, start: int, depth: int, failed: set[int]) -> list | None:
    if start == len(data):
        return []
    if (
        start in failed
        or depth >= _MAX_FRAMES
        or data[start] != STX
        or start + 1 >= len(data)
    ):
        return None
    length = data[start + 1]
    for extra in _EXTRA_BYTES:
        end = start + length + extra
        if end > len(data):
            continue
        if end < len(data) and data[end] != STX:
            continue
        rest = _split(data, end, depth + 1, failed)
        if rest is not None:
            return [data[start:end], *rest]
    # Remember dead ends, so that data such as many 0x02 bytes in a row
    # cannot cause exponential backtracking.
    failed.add(start)
    return None


def split_feedback(data: bytes | bytearray) -> list[bytes | bytearray]:
    """Return the packets in data, or [data] if it cannot be split exactly."""
    if not data:
        return []
    frames = _split(data, 0, 0, set())
    if not frames or len(frames) == 1:
        return [data]
    return frames
