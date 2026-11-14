#!/usr/bin/env python3
"""Cross-check Ghidra's static DPS resolution against the simulator for RMW bank writes.

Each case seeds both pointers (DPTR0=0x0200 holding 0xAA, DPTR1=0x0100 holding 0x55), selects a
starting bank, applies one RMW form to AUXR1, then reads through the selected pointer. The byte
that comes back names the bank: 0x55 means DPTR1, 0xAA means DPTR0.

The same bytes are run twice:

  * ucsim, which executes and therefore knows the real answer (t/dpstest.py already pins that it
    honours MOV AUXR1,#imm and INC AUXR1);
  * Ghidra under AT89C51RD2:BE:16:default, which must resolve the bank statically at parse time.

Agreement is the point. A case where Ghidra picks a bank the hardware would not is exactly the
class of bug this language exists to remove, and a one-sided test would not catch a simulator
regression either.

Forms and their effect on AUXR1.0:
  ORL #imm  sets DPS when imm&1        ANL #imm  clears DPS when !(imm&1)
  XRL #imm  toggles DPS when imm&1     DEC       always toggles (bit 0 flips on a decrement)
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

S51 = os.environ.get("S51") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.environ.get("S51_SIM") or os.path.join(S51, "vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
HEADLESS = os.path.join(
    os.path.expanduser(os.environ.get("GHIDRA", "~/ghidra_12.1.4_PUBLIC")),
    "support", "analyzeHeadless")
RD2 = "AT89C51RD2:BE:16:default"
TMP = "/tmp/rmwtest"

FORMS = {  # name -> (bytes, length)
    "ORL": lambda d: (bytes([0x43, 0xA2, d]), 3),
    "ANL": lambda d: (bytes([0x53, 0xA2, d]), 3),
    "XRL": lambda d: (bytes([0x63, 0xA2, d]), 3),
    "DEC": lambda d: (bytes([0x15, 0xA2]), 2),
    # rel=0 makes the branch target the very next instruction, so the taken and not-taken paths
    # both land on the same MOVX and only the toggled bank differs.
    "DJNZ": lambda d: (bytes([0xD5, 0xA2, 0x00]), 3),
}

# CASES: (label, start_bank, form, imm, expected_bank_after)
CASES = [
    ("ORL sets", 0, "ORL", 0x01, 1),
    ("ORL no-op", 0, "ORL", 0x04, 0),
    ("ANL clears", 1, "ANL", 0xFE, 0),
    ("ANL no-op", 1, "ANL", 0xFF, 1),
    ("XRL toggle 0", 0, "XRL", 0x01, 1),
    ("XRL toggle 1", 1, "XRL", 0x01, 0),
    ("DEC toggle 0", 0, "DEC", 0, 1),
    ("DEC toggle 1", 1, "DEC", 0, 0),
    ("DJNZ taken", 0, "DJNZ", 0, 1),
    ("DJNZ skip", 1, "DJNZ", 0, 0),
]


def build(start, form, imm):
    fb, flen = FORMS[form](imm)
    p = bytearray([
        0x75, 0xA2, 0x00,                       # 0  force DPS=0
        0x90, 0x02, 0x00, 0x74, 0xAA, 0xF0,     # 3  DPTR0=0x0200; xram[0x200]=0xAA
        0x75, 0xA2, 0x01,                       # 9  DPS=1
        0x90, 0x01, 0x00, 0x74, 0x55, 0xF0,     # 12 DPTR1=0x0100; xram[0x100]=0x55
        0x75, 0xA2, start])                     # 18 start bank
    p += fb                                     # 21 the form under test
    p += bytes([0xE0, 0xF5, 0x40, 0x80, 0xFE])  #    MOVX A,@DPTR; MOV 0x40,A; SJMP $
    return bytes(p), 21 + flen


def sim_bank(img, steps=12):
    # 12 retired instructions lands just past `MOV 0x40,A`; 11 leaves the store pending and the
    # dump then reads uninitialized IRAM, which looks like a wrong bank rather than a bug.
    cmds = ['file "%s"' % img] + ["step"] * steps + ["dump iram 0x40 0x41", "exit"]
    p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=180)
    m = [l.split()[1] for l in (p.stdout + p.stderr).splitlines() if re.match(r"^0x40\s", l)]
    if not m:
        return None
    return {"55": 1, "aa": 0}.get(m[0].lower())


def ghidra_bank(img, movx_addr):
    root = os.path.join(TMP, "proj")
    subprocess.run(["rm", "-rf", root], check=True)
    os.makedirs(root)
    p = subprocess.run([HEADLESS, root, "p", "-import", img, "-processor", RD2, "-noanalysis",
                        "-scriptPath", os.path.join(S51, "gh"), "-postScript", "Pcode51.java"],
                       capture_output=True, text=True, timeout=900)
    want = "CODE:%04x" % movx_addr
    for l in (p.stdout + p.stderr).splitlines():
        if "Pcode51.java> " not in l:
            continue
        line = l.split("Pcode51.java> ", 1)[1]
        # match the instruction's own address, not a branch target named in its pcode
        if not line.startswith(want + " "):
            continue
        if "DPTR1" in line:
            return 1
        if "DPTR0" in line:
            return 0
        return None
    return None


def main():
    os.makedirs(TMP, exist_ok=True)
    bad = 0
    print("%-14s %-6s %-6s %-6s %s" % ("case", "sim", "ghidra", "want", "verdict"))
    for label, start, form, imm, want in CASES:
        img_bytes, movx_addr = build(start, form, imm)
        img = os.path.join(TMP, "c.hex")
        write_hex(img, img_bytes)
        s, g = sim_bank(img), ghidra_bank(img, movx_addr)
        ok = s == want and g == want
        bad += 0 if ok else 1
        print("%-14s %-6s %-6s %-6s %s" %
              (label, s, g, want, "PASS" if ok else "FAIL"))
    print("\n%s: %d/%d cases agree with the simulator" %
          ("PASS" if not bad else "FAIL", len(CASES) - bad, len(CASES)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
