#!/usr/bin/env python3
"""D2: CCAPMn must reset to X000 0000b, i.e. with ECOM (bit 6) clear.

uc89c51r::reset() set bmECOM in CCAPM0..4, which doc4235 table 3-11 declares as 0.  The
fork does that for every part built on the class, so the fix leaves it in place for the
AT89C51-series members that want it and skips it for the RD2 (rd2_mem()).  It matters
because ECOM is the comparator output enable: pca.cc only drives the CEX pin inside
`if (ccapm[nr] & bmECOM)`, so with ECOM preset the PCA owns P1 pins straight out of
reset, and a PWM module with CCAPnL below CL pulls its pin low before the program has
said anything about the PCA at all.

The behavioural images are the same six setup MOVs plus SJMP $:

    MOV CCAPM0,#mode    0x00 / 0x02 (PWM, ECOM clear) / 0x42 (PWM, ECOM set)
    MOV CCAP0H,#hi      PWM duty, reloaded into CCAP0L on every CL wrap
    MOV CCAP0L,#lo
    MOV CL,#0xfe        one count from a CL wrap, which is where the reload happens
    MOV CH,#0xff        ... and the next wrap overflows CH, which calls do_pca_module()
    MOV CCON,#0x40      CR = 1: start the PCA counter (CMOD is left at its reset value)
    SJMP $

CEX0 is P1.3 in this model (bmCEX0), so a PWM module whose CL is below CCAP0L pulls P1
to 0xf7 and leaves it at the reset value 0xff otherwise.  Note the pin is read from the
P1 latch (`dump sfr[0x90]`); a port bit read on this model returns 0 either way.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.path.expanduser(
  "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
CCAPM = (0xDA, 0xDB, 0xDC, 0xDD, 0xDE)
CL, CH, CCON = 0xE9, 0xF9, 0xD8
CCAP0L, CCAP0H = 0xEA, 0xFA
P1, CEX0 = 0x90, 0x08
ECOM, PWM = 0x40, 0x02
SJMP = bytes([0x80, 0xFE])


def pre(pairs):
    b = bytearray()
    for a, v in pairs:
        b += bytes([0x75, a, v])
    return bytes(b)


def sim(cmds, chip="89C51R"):
    p = subprocess.run([SIM, "-t", chip, "-c", "-"], input="\n".join(cmds + ["exit"]) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=300)
    return p.stdout + p.stderr


def peek(out, addr):
    m = re.search(r"^0x%02x \S+:\s+\S+\s+(0x[0-9a-f]+)" % addr, out, re.M)
    return m.group(1) if m else None


def run(mode, ccaph=0xff, ccapl=0xff, steps=100):
    """P1 and friends after `steps` instructions of the image above."""
    code = pre([(CCAPM[0], mode), (CCAP0H, ccaph), (CCAP0L, ccapl),
                (CL, 0xFE), (CH, 0xFF), (CCON, 0x40)]) + SJMP
    img = "/tmp/ccapm_%02x_%02x_%02x_%d.hex" % (mode, ccaph, ccapl, steps)
    write_hex(img, code)
    out = sim(['file "%s"' % img, "step %d" % steps] +
              ["dump sfr[0x%x]" % a for a in (P1, CL, CH, CCAP0L, CCON)])
    return {a: peek(out, a) for a in (P1, CL, CH, CCAP0L, CCON)}


results = []


def case(name, expect, got, unit, note=""):
    ok = expect == got
    results.append((name, expect, got, ok, note, unit))


# --- reset values -------------------------------------------------------------------
fresh = sim(["dump sfr[0x%x]" % a for a in CCAPM])
for i, a in enumerate(CCAPM):
    case("CCAPM%d resets to" % i, "0x00", peek(fresh, a), "value",
         "doc4235 table 3-11: X000 0000b")

# --- table 3-11 also pins CCON and CMOD, and both have reset-0 bits the fork set -----
# CCON 00X0 0000b (CF, CR and CCF4..0 clear), CMOD 00XX X000b (WDTE, CPS1:0, CIDL
# clear).  Masking keeps the one don't-care bit of each out of the comparison.
fresh2 = sim(["dump sfr[0x%x]" % a for a in (CCON, 0xD9)])
for addr, mask, label in ((CCON, 0xDF, "CCON reset, 00X0 0000b"),
                          (0xD9, 0xC7, "CMOD reset, 00XX X000b")):
    raw = peek(fresh2, addr)
    got = "0x%02x" % (int(raw, 16) & mask) if raw and raw != "?" else raw
    case(label, "0x00", got, "value", "table 3-11 p.7; reserved bits masked")

# --- the comparator really is ECOM-gated, so the reset bit was doing damage ---------
untouched = run(0x00)
case("reset CCAPM0: CEX0 untouched", "0xff", untouched[P1], "P1",
     "CR running, CH overflows, no software touched the PCA")
case("PWM without ECOM: CEX0 untouched", "0xff", run(PWM)[P1], "P1",
     "mode bits alone must not drive the pin")
case("PWM with ECOM: CEX0 pulled low", "0x%02x" % (0xff & ~CEX0),
     run(ECOM | PWM)[P1], "P1", "one bit (0x42 vs 0x02) is the whole difference")

# --- the PWM period still reloads, so ECOM being clear is not killing the module ----
# do_pca_counter reloads the duty byte on every CL wrap, but only for a module with the
# PWM bit set, so this runs with PWM enabled and ECOM still clear.
reload = run(PWM, ccaph=0x80, ccapl=0x11)
case("CCAP0L reloads from CCAP0H", "0x80", reload[CCAP0L], "CCAP0L",
     "PWM module, CL wrapped, ECOM still clear")
case("PCA counter ran from reset", True, untouched[CL] not in (None, "0xfe"), "CL",
     "CMOD was never written; clk_source must follow its reset value")

print("%-32s %12s %12s %8s  %s" % ("case", "expected", "measured", "unit", "verdict"))
bad = 0
for name, expect, got, ok, note, unit in results:
    bad += 0 if ok else 1
    print("%-32s %12s %12s %8s  %s  %s" % (name, expect, got, unit,
                                           "PASS" if ok else "FAIL", note))
print("\n%s: %d/%d" % ("PASS" if not bad else "FAIL", len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
