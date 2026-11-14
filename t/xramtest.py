#!/usr/bin/env python3
"""Verify the AT89C51RD2 expanded-RAM window against doc4235 Table 9-1.

For each XRS selection, probe the address just below and just above the on-chip
boundary. Each probe seeds both views with distinct values (0x22 on-chip, 0x11
off-chip) and reads them back, so a window that is too small or too wide shows up
as the wrong value rather than as uninitialised-RAM noise.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.environ.get("S51") or os.path.expanduser(
    "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
SIZES = {0: 256, 1: 512, 2: 768, 3: 1024, 4: 1792}   # AUXR.XRS2:XRS0 -> bytes


def seq(auxr, addr, op, val=None):
    b = bytes([0x75, 0x8E, auxr, 0x90, addr >> 8, addr & 0xff])
    if op == "w":
        return b + bytes([0x74, val, 0xF0]), 4
    return b + bytes([0xE0]), 3


def build(xrs, addr):
    on, off = xrs << 2, (xrs << 2) | 0x02
    code, steps = bytearray(), 0
    for auxr, val in ((on, 0x22), (off, 0x11)):
        s, n = seq(auxr, addr, "w", val)
        code += s
        steps += n
    for auxr, dst in ((on, 0x40), (off, 0x41)):
        s, n = seq(auxr, addr, "r")
        code += s + bytes([0xF5, dst])
        steps += n + 1
    return bytes(code + bytes([0x80, 0xFE])), steps + 1


def run(img, steps):
    cmds = ['file "%s"' % img] + ["step"] * steps + ["dump iram 0x40 0x41", "exit"]
    p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=120)
    m = [l.split()[1:3] for l in (p.stdout + p.stderr).splitlines()
         if re.match(r"^0x40\s", l)]
    return tuple(m[0]) if m else None


def eecon_does_not_remap():
    """doc4235 puts `EECON` (0d2h) in the family SFR map with reset xxxx xx00b (Table 3-11, printed
    p.7) but gives the 2048 B EEPROM array that `EEE` remaps to the AT89C51ED2 only -- "On-chip
    2048 Bytes EEPROM Block for Data Storage (AT89C51ED2 Only)", printed p.1.  Naming the register
    on the RD2 is therefore only honest if the model also shows the remap is absent: a byte written
    through `MOVX` must still be there after `EEE` is set, or the name would promise an ED2
    behaviour on a part that has no array.  `EEBUSY` (bit 0) is read back too -- it must stay clear,
    since no EEPROM cycle can be in progress here.

    This leg runs -t 89C51RD2 on purpose, unlike the XRS boundary legs above, which are family-wide
    by design and would pass on a part that has no EECON name at all."""
    fails = 0
    code = bytes([
        0x90, 0x01, 0x00,          # MOV  DPTR,#0x0100   (inside the 768 B window AUXR resets to)
        0x74, 0x22, 0xF0,          # MOV  A,#0x22 ; MOVX @DPTR,A
        0x90, 0x01, 0x00, 0xE0,    # MOVX A,@DPTR
        0xF5, 0x40,                # MOV  0x40,A         read before EEE
        0x75, 0xD2, 0x02,          # MOV  EECON,#0x02    EEE = 1
        0xE5, 0xD2, 0xF5, 0x42,    # MOV  A,EECON ; MOV 0x42,A   EEBUSY must read 0
        0x90, 0x01, 0x00, 0xE0,    # MOVX A,@DPTR again, with EEE set
        0xF5, 0x41,                # MOV  0x41,A         read after EEE
        0x80, 0xFE])
    img = "/tmp/eecon_remap.hex"
    write_hex(img, code)
    cmds = ['file "%s"' % img] + ["step"] * 12 + ["dump iram 0x40 0x42", "exit"]
    p = subprocess.run([SIM, "-t", "89C51RD2", "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=120)
    m = re.search(r"^0x40\s+([0-9a-f]{2})\s+([0-9a-f]{2})\s+([0-9a-f]{2})",
                  p.stdout + p.stderr, re.M)
    got = m.groups() if m else None
    for label, value, want in (("0x0100 read before EEE", got and got[0], "22"),
                               ("0x0100 read with EEE set", got and got[1], "22"),
                               ("EECON after MOV #0x02", got and got[2], "02")):
        ok = value == want
        fails += 0 if ok else 1
        print("%-30s %-4s expected %s  %s" % (label, value or "??", want,
                                              "OK" if ok else "MISMATCH"))
    return fails



bad = 0
print("XRS   size  probe   on-chip off-chip  expect        verdict")
for xrs, size in sorted(SIZES.items()):
    for addr, where in ((size - 1, "on-chip"), (size, "off-chip")):
        img = "/tmp/xrs_%d_%x.hex" % (xrs, addr)
        code, steps = build(xrs, addr)
        write_hex(img, code)
        got = run(img, steps)
        want = ("22", "11") if where == "on-chip" else ("11", "11")
        ok = got == want
        bad += 0 if ok else 1
        print("%3d %6d  0x%04x   %-4s   %-4s     %s/%s        %s" %
              (xrs, size, addr, got[0] if got else "??", got[1] if got else "??",
               want[0], want[1], "OK" if ok else "MISMATCH"))
eecon_fails = eecon_does_not_remap()
print("\n%s: %d/10 boundaries correct, EECON remap leg %s" %
      ("PASS" if not bad and not eecon_fails else "FAIL", 10 - bad,
       "OK" if not eecon_fails else "%d FAILED" % eecon_fails))
sys.exit(1 if (bad or eecon_fails) else 0)
