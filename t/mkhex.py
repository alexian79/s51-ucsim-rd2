#!/usr/bin/env python3
"""Build an Intel-HEX file from a flat byte image at a base address."""
import sys


def rec(t, a, d):
    b = bytes([len(d), (a >> 8) & 0xFF, a & 0xFF, t]) + d
    return ":%02X%04X%02X%s%02X" % (len(d), a & 0xFFFF, t, d.hex().upper(), (-sum(b)) & 0xFF)


def write_hex(path, image, base=0, entry=None):
    lines = []
    for off in range(0, len(image), 16):
        lines.append(rec(0, base + off, image[off:off + 16]))
    if entry is not None:
        lines.append(rec(5, 0, bytes([entry >> 24 & 255, entry >> 16 & 255, entry >> 8 & 255,
                                      entry & 255])))
    lines.append(":00000001FF")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    # self-check every record we just emitted
    for ln in open(path):
        b = bytes.fromhex(ln.strip()[1:])
        assert sum(b) & 0xFF == 0, "bad checksum in %r" % ln
    return path


if __name__ == "__main__":
    prog = bytes.fromhex(sys.argv[2])
    base = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0
    write_hex(sys.argv[1], prog, base)
    print("wrote %s (%d bytes @ 0x%x)" % (sys.argv[1], len(prog), base))
