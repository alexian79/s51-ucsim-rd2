#!/usr/bin/env python3
"""T2MOD.T2OE (C9h bit 1, doc4235 Table 12-2): Timer 2 clock out on P1.0.

The mode is only reachable if the device asks cl_timer2 for the t2_clock_out feature,
which uc52.cc does not do for any 8052, so the 89C51R adds it to its own timer 2 while
building its hardware.  Nothing else in the tree exercises the output, so the rate is
established here from scratch.

Every image is the same six setup MOVs and then SJMP $:

    MOV RCAP2L,#lo      timer 2 reload value: period P = 65536 - RCAP2
    MOV RCAP2H,#hi
    MOV TH2,#hi         start the counter where the reload will put it
    MOV TL2,#lo
    MOV T2MOD,#t2mod    the one bit under test (0x02 = T2OE)
    MOV T2CON,#0x04     TR2 = 1, C/T2 = 0, CP_RL2 = 0, EXEN2 = 0, RCLK = TCLK = 0
    SJMP $              2 machine cycles each

P1.0 is read out of the P1 latch with `dump sfr[0x90]`, NOT polled with JNB: a port bit
read on this model returns 0 whatever the latch holds, so `JNB P1.0,$` falls through
immediately even when the clock out never moved, and would prove nothing.  Scanning the
step count at which the latched bit first goes low gives the first overflow to within one
SJMP (24 clocks).

doc4235 12.2: clock out increments TL2 at FCLK_PERIPH/2, which on this part is 3 counts
per machine cycle, and the output toggles on every overflow, so the first toggle costs
4 * P clocks and f_clkout = FCLK_PERIPH / (4 * P).  The clock budget of a run of k
instructions is 12 * (1 + 2 * (k - 6)): the MOV that sets TR2 is the first machine cycle
timer 2 may use and every SJMP is 2, so the toggle is expected at step 6 + P / 6 within a
step.  The slope rows difference two reload values, which cancels the fixed overhead.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.path.expanduser(
  "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
SJMP = bytes([0x80, 0xFE])
RCAP2L, RCAP2H, TL2, TH2 = 0xCA, 0xCB, 0xCC, 0xCD
T2MOD, T2CON = 0xC9, 0xC8
T2OE, TR2 = 0x02, 0x04
P1 = 0x90
SETUP = 6                       # setup MOVs in image(), one machine cycle each
SJMP_CLKS = 24                  # 2 machine cycles at 12 clocks


def pre(pairs):
    b = bytearray()
    for a, v in pairs:
        b += bytes([0x75, a, v])
    return bytes(b)


def image(rcap, t2mod, t2con=TR2):
    lo, hi = rcap & 0xff, (rcap >> 8) & 0xff
    return pre([(RCAP2L, lo), (RCAP2H, hi), (TH2, hi), (TL2, lo),
                (T2MOD, t2mod), (T2CON, t2con)]) + SJMP


def measure(rcap, t2mod, t2con, steps):
    """P1 latch after `steps` instructions; None if the dump has no P1 row."""
    img = "/tmp/t2oe_%x_%02x_%02x_%d.hex" % (rcap, t2mod, t2con, steps)
    write_hex(img, image(rcap, t2mod, t2con))
    cmds = ['file "%s"' % img, "step %d" % steps, "dump sfr[0x90]", "exit"]
    p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=300)
    m = re.search(r"^0x90 P1:\s+\S+\s+(0x[0-9a-f]+)", p.stdout + p.stderr, re.M)
    return int(m.group(1), 16) if m else None


def first_toggle(rcap, t2mod, t2con=TR2, span=8):
    """Step at which P1.0 is first low, nearest `span` steps to the expectation."""
    guess = SETUP + (0x10000 - rcap) // 6
    for k in range(max(SETUP + 1, guess - span), guess + span + 1):
        v = measure(rcap, t2mod, t2con, k)
        if v is not None and not v & 1:
            return k
    return None


results = []


def case(name, expect, got, unit, note=""):
    if isinstance(expect, (int, float)) and isinstance(got, (int, float)):
        ok = abs(expect - got) <= max(1.0, abs(expect) * 0.02)
    else:
        ok = expect == got
    results.append((name, expect, got, ok, note, unit))


hx = lambda v: "no dump" if v is None else "0x%02x" % v
period = lambda rcap: 0x10000 - rcap

# --- the toggle lands where FCLK_PERIPH/2 says it should ---------------------------
k256 = first_toggle(0xff00, T2OE)
k512 = first_toggle(0xfe00, T2OE)
k1024 = first_toggle(0xfc00, T2OE)
for rcap, k in ((0xff00, k256), (0xfe00, k512), (0xfc00, k1024)):
    case("first toggle, P=%d" % period(rcap), SETUP + period(rcap) / 6.0,
         -1 if k is None else k, "step", "4 clks per TL2 count, 1 step of slack")

# --- slope: differencing two reload values cancels the setup overhead ---------------
case("toggle step 512 vs 256", 4 * (period(0xfe00) - period(0xff00)),
     SJMP_CLKS * (k512 - k256) if k256 and k512 else -1, "clks",
     "same slope at double the period")
case("toggle step 1024 vs 512", 4 * (period(0xfc00) - period(0xfe00)),
     SJMP_CLKS * (k1024 - k512) if k512 and k1024 else -1, "clks",
     "still linear at four times the period")

# --- A/B: the bit under test, and the TR2 the mode still needs ----------------------
case("T2OE clear: P1 never moves", "0xff", hx(measure(0xff00, 0x00, TR2, 200)),
     "P1", "200 steps = 8x the shortest period above")
case("T2OE set, TR2 clear: static", "0xff", hx(measure(0xff00, T2OE, 0x00, 200)),
     "P1", "clock out still needs TR2")

print("%-33s %12s %12s %6s  %s" % ("case", "expected", "measured", "unit", "verdict"))
bad = 0
for name, expect, got, ok, note, unit in results:
    bad += 0 if ok else 1
    e = "%.1f" % expect if isinstance(expect, float) else str(expect)
    print("%-33s %12s %12s %6s  %s  %s" % (name, e, got, unit,
                                           "PASS" if ok else "FAIL", note))
print("\n%s: %d/%d" % ("PASS" if not bad else "FAIL", len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
