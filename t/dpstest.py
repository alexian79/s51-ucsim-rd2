#!/usr/bin/env python3
"""Acceptance oracle for AT89C51RD2 dual DPTR (AUXR1.0 = DPS).

Builds one program twice: variant A selects banks with `MOV AUXR1,#n`, variant B replaces those
writes with three NOPs each so the bank never changes. Both seed xram[0x100]=0x55 and
xram[0x200]=0xAA through DPTR0, then point DPTR0 at 0x0200 and DPTR1 at 0x0100 and read once
through each bank.

  A -> iram[0x40]=0x55 iram[0x41]=0xaa   two live pointers, DPS honoured
  B -> iram[0x40]=0x55 iram[0x41]=0x55   one pointer, both reads hit the last MOV DPTR

The B control is what makes A meaningful: without it a stray 0xaa could come from anywhere.
NOP is one byte, so variant B executes 12 more instructions than A; the step counts below are
per-variant, not shared. This is the oracle for the deferred Ghidra dual-DPTR language work:
whatever Ghidra decompiles must agree with these two tuples.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.path.expanduser(
  "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
TMP = "/tmp/dpstest"

# (offset, bytes) of the four AUXR1 writes; three bytes each
BANK_WRITES = (12, 18, 24, 30)

A = bytearray([
    0x90, 0x02, 0x00, 0x74, 0xAA, 0xF0,          # 0  DPTR=0x0200; xram[0x200]=0xAA
    0x90, 0x01, 0x00, 0x74, 0x55, 0xF0,          # 6  DPTR=0x0100; xram[0x100]=0x55
    0x75, 0xA2, 0x00, 0x90, 0x02, 0x00,          # 12 AUXR1=0 ; DPTR0=0x0200
    0x75, 0xA2, 0x01, 0x90, 0x01, 0x00,          # 18 AUXR1=1 ; DPTR1=0x0100
    0x75, 0xA2, 0x01, 0xE0, 0xF5, 0x40,          # 24 AUXR1=1 ; read via DPTR1 -> 0x40
    0x75, 0xA2, 0x00, 0xE0, 0xF5, 0x41,          # 30 AUXR1=0 ; read via DPTR0 -> 0x41
    0x80, 0xFE])                                 # 36 SJMP $

# retired-instruction count that lands on the SJMP: 17 for A; B splits four 3-byte writes
# into twelve 1-byte NOPs, so 16 + 12 = 28
STEPS = {"A": 17, "B": 29}


def variant(bank_select):
    img = bytearray(A)
    if not bank_select:
        for off in BANK_WRITES:
            img[off] = img[off + 1] = img[off + 2] = 0x00
    return bytes(img)


def run(img, steps):
    cmds = ['file "%s"' % img] + ["step"] * steps + ["dump iram 0x40 0x41", "exit"]
    p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=180)
    m = [l.split()[1:3] for l in (p.stdout + p.stderr).splitlines()
         if re.match(r"^0x40\s", l)]
    return tuple(m[0]) if m else None


def main():
    os.makedirs(TMP, exist_ok=True)
    expect = {"A": ("55", "aa"), "B": ("55", "55")}
    bad = 0
    for v, want in expect.items():
        img = os.path.join(TMP, "dps%s.hex" % v)
        write_hex(img, variant(v == "A"))
        got = run(img, STEPS[v])
        ok = got == want
        bad += 0 if ok else 1
        print("%s: bank=%s  got=%s want=%s  %s" %
              ("PASS" if ok else "FAIL", "on " if v == "A" else "off", got, want,
               "" if ok else "<-- dual DPTR not modelled as expected"))
    print("\n%s: %d/2  (A proves DPS honoured, B proves it is the cause)" %
          ("PASS" if not bad else "FAIL", 2 - bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
