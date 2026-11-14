#!/usr/bin/env python3
"""D3: the watchdog period must come from WDTPRG, not a hardcoded 0x3fff.

doc4235 19 and Table 19-2: WDTPRG (0A7h) bits S2:S0 select the time-out, and the selected
time-out is (2^(14+S) - 1) machine cycles:

    S2S1S0   000      001      ..       111

with a reset value of XXXX X000, so an untouched part keeps the 2^14-1 = 0x3fff the
constructor already used and no existing test moves.  wdt.cc had never read WDTPRG at
all, so every selection was 16 ms of machine cycles.

Two judgement calls, both recorded in the code comment:

* The table says the select "can not be modified until next reset", so the value is
  latched when the WDTRST 0x1E/0xE1 enable sequence completes; a later WDTPRG write is
  remembered by the SFR but does not move the running period.
* The ms column of Table 19-2 is misprinted for the long rows (2^21-1 machine cycles is
  2.09 s at 12 MHz, not 2.09 ms).  The machine-cycle column is the one implemented.

Every image is the same code, so the only difference between any two rows is WDTPRG:

    MOV WDTPRG,#s       S2:S0 = time-out select
    MOV WDTRST,#0x1E    enable sequence, part 1
    MOV WDTRST,#0xE1    enable sequence, part 2: the period is latched here
    SJMP $              nothing ever services the watchdog

`info hardware wdt` reports the counter and what is left of the period, so the period is
counter + remains.  Whether a reset actually fired is read from `state`: a watchdog reset
restarts the "since last reset" clock, so it is far below the clock budget of the run.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.path.expanduser(
  "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
WDTRST, WDTPRG = 0xA6, 0xA7
SJMP = bytes([0x80, 0xFE])
STEPS = 9000                    # 9000 instructions, 2 clocks each = 216000 clocks


def mov(a, v):
    return bytes([0x75, a, v])


def image(prrg, after=None):
    code = mov(WDTPRG, prrg) + mov(WDTRST, 0x1E) + mov(WDTRST, 0xE1)
    if after is not None:
        code += mov(WDTPRG, after)
    return code + SJMP


def sim(img, cmds, steps=None):
    if steps:
        cmds = ['file "%s"' % img, "step %d" % steps] + cmds
    p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds + ["exit"]) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=600)
    return p.stdout + p.stderr


def period(prrg, after=None, steps=1000):
    """counter + remains as wdt reports them, i.e. the time-out in machine cycles."""
    img = "/tmp/wdt_%02x_%s_%d.hex" % (prrg, after, steps)
    write_hex(img, image(prrg, after))
    out = sim(img, ["info hardware wdt"], steps)
    m = re.search(r"counter=(\d+) \(remains=(\d+)\)", out)
    return int(m.group(1)) + int(m.group(2)) if m else None


def fired(prrg, steps=STEPS):
    """"reset" if a watchdog reset restarted the clock during the run."""
    img = "/tmp/wdtb_%02x_%d.hex" % (prrg, steps)
    write_hex(img, image(prrg))
    out = sim(img, ["state"], steps)
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", out)
    if not m:
        return "no state"
    since, budget = int(m.group(1)), steps * 24
    return "reset (%d of %d clks)" % (since, budget) if since < budget else "no reset"


results = []


def case(name, expect, got, unit, note=""):
    ok = expect == got
    results.append((name, expect, got, ok, note, unit))


# --- Table 19-2 in full -------------------------------------------------------------
periods = []
for s in range(8):
    p = period(s)
    periods.append(p)
    case("S=%d period" % s, (1 << (14 + s)) - 1, p, "MC",
         "2^(14+S)-1 machine cycles")
case("monotone", True, all(b > a for a, b in zip(periods, periods[1:])), "check",
     "differencing eight WDTPRG values")
case("reset select 000", 0x3fff, periods[0], "MC", "old hardcoded value stays put")

# --- the select is S2:S0 only, and it is latched by the enable ----------------------
case("bit 3 of WDTPRG ignored", 0x3fff, period(0x08), "MC", "mask is 0x07, not the byte")
case("latched at the enable", 0x3fff, period(0x00, after=0x07), "MC",
     "WDTPRG written after 0x1E/0xE1 must not move the period")

# --- and the difference shows up as an actual reset ---------------------------------
# 216000 clocks = 18000 machine cycles: past the 16383 of S=0, short of the 32767 of S=1.
f0, f1 = fired(0x00), fired(0x01)
case("S=0 overflows in 18000 MC", True, f0.startswith("reset"), "check", f0)
case("S=1 does not", True, f1 == "no reset", "check", f1)

print("%-28s %12s %12s %7s  %s" % ("case", "expected", "measured", "unit", "verdict"))
bad = 0
for name, expect, got, ok, note, unit in results:
    bad += 0 if ok else 1
    print("%-28s %12s %12s %7s  %s  %s" % (name, expect, got, unit,
                                           "PASS" if ok else "FAIL", note))
print("\n%s: %d/%d" % ("PASS" if not bad else "FAIL", len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
