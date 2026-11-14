#!/usr/bin/env python3
"""Three-leg check of the dynamic dual-DPTR bank case and the Bank51 override.

gh/dyn.hex seeds DPTR0=0x0200 holding 0xAA and DPTR1=0x0100 holding 0x55, then selects the bank
with `MOV AUXR1,A` - a write whose value is not knowable while parsing, so the language cannot
resolve it. Reading through the selected pointer tells us which bank the hardware used: 0xAA is
DPTR0, 0x55 is DPTR1.

  leg 1  ucsim executes it and reports the truth                       -> bank 0
  leg 2  Ghidra without help keeps the last statically known bank       -> bank 1, WRONG
  leg 3  Ghidra after Bank51 asserts the bank at the MOVX               -> bank 0

Leg 2 is the point of the whole test: it pins that the gap is real, so leg 3 cannot pass by
accident and leg 3 cannot silently stop doing anything useful. It also documents that the correct
override value must come from the simulator, not from a guess.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

S51 = os.environ.get("S51") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# S51_SIM=<path> aims the simulator legs at another build (t/floor.sh passes the binary
# under test that way); the Ghidra legs are unaffected by it.
SIM = os.environ.get("S51_SIM") or os.path.join(
  S51, "vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
HEADLESS = os.path.join(
    os.path.expanduser(os.environ.get("GHIDRA", "~/ghidra_12.1.4_PUBLIC")),
    "support", "analyzeHeadless")
RD2 = "AT89C51RD2:BE:16:default"
IMG = os.path.join(S51, "gh", "dyn.hex")
MOVX = 0x16          # the MOVX A,@DPTR whose bank is not statically knowable
STEPS = 12           # retired instructions up to and including MOV 0x40,A
TMP = "/tmp/banktest"

# DPTR0=0x0200<-0xAA, DPTR1=0x0100<-0x55, then A=0 selects bank 0 through AUXR1
PROG = bytes([
    0x75, 0xA2, 0x00, 0x90, 0x02, 0x00, 0x74, 0xAA, 0xF0,
    0x75, 0xA2, 0x01, 0x90, 0x01, 0x00, 0x74, 0x55, 0xF0,
    0x74, 0x00, 0xF5, 0xA2, 0xE0, 0xF5, 0x40, 0x80, 0xFE])


def sim_bank():
    cmds = ['file "%s"' % IMG] + ["step"] * STEPS + ["dump iram 0x40 0x41", "exit"]
    p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=180)
    m = [l.split()[1] for l in (p.stdout + p.stderr).splitlines() if re.match(r"^0x40\s", l)]
    return {"55": 1, "aa": 0}.get(m[0].lower()) if m else None


def ghidra_bank(bank_override):
    root = os.path.join(TMP, "p%s" % bank_override)
    subprocess.run(["rm", "-rf", root], check=True)
    os.makedirs(root)
    env = dict(os.environ)
    if bank_override is not None:
        env["BANK51_ADDR"] = "0x%x" % MOVX
        env["BANK51_BANK"] = str(bank_override)
    else:
        env.pop("BANK51_ADDR", None)
        env.pop("BANK51_BANK", None)
    p = subprocess.run([HEADLESS, root, "p", "-import", IMG, "-processor", RD2, "-noanalysis",
                        "-scriptPath", os.path.join(S51, "gh"), "-postScript", "Bank51.java"],
                       capture_output=True, text=True, timeout=900, env=env)
    for l in (p.stdout + p.stderr).splitlines():
        if "Bank51.java> " not in l:
            continue
        line = l.split("Bank51.java> ", 1)[1].strip()
        if line.startswith("CODE:%04x " % MOVX):
            m = re.search(r"bank=(\S+)", line)
            return None if m is None else int(m.group(1))
    return None


def main():
    os.makedirs(TMP, exist_ok=True)
    write_hex(IMG, PROG)
    truth = sim_bank()
    default = ghidra_bank(None)
    forced = ghidra_bank(truth)

    bad = 0
    checks = [
        ("simulator ground truth is bank 0", truth == 0, truth),
        ("language alone gets it wrong, so the override is needed", default == 1, default),
        ("Bank51 override matches the simulator", forced == truth, forced),
    ]
    for label, ok, got in checks:
        bad += 0 if ok else 1
        print("%s %-52s got %s" % ("PASS" if ok else "FAIL", label, got))

    print("\n%s: %d/%d" % ("PASS" if not bad else "FAIL", len(checks) - bad, len(checks)))
    print("note: leg 2 failing means the language learned to resolve MOV AUXR1,A on its own,")
    print("      in which case delete this test - the override would no longer be needed.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
