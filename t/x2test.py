#!/usr/bin/env python3
"""CKCON0 X2 mode: clock periods per machine cycle and per peripheral cycle.

f_xtal is ucsim's default 11.0592 MHz.  doc4235 section 7 gives two rules:

  CKCON0.X2=1     CPU runs at 6 clock periods per machine cycle instead of 12.  The number of
                  machine cycles an instruction takes does not change, so a timer counting one
                  tick per machine cycle still does - it just costs half the clocks.
  CKCON0.TnX2=1   that peripheral stays at 12 clock periods per tick while the CPU is fast, so
                  it ticks once every two machine cycles.  Inactive unless X2 is set.

Each case is measured by differencing two runs that only differ in the timer reload, which
cancels the fixed instruction overhead and leaves the cost of a known number of ticks.
The PCA rows difference a fixed instruction budget instead and read CH:CL, which leaves the
number of counts that a known number of machine cycles produced (doc4235 Table 13-1: CPS=00
is FCLK_PERIPH/6 = 1 count per MC and CPS=01 is FCLK_PERIPH/2 = 3, so both are machine-cycle
derived and both must answer to CKCON0.PCAX2).
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
CKCON0 = 0x8F
COUNTS = 128                 # reload span used by every differencing pair


def run(code):
    code = bytes(code)
    img = "/tmp/x2_%d_%d.hex" % (len(code), sum(code) & 0xFFFF)
    write_hex(img, code)
    cmds = ['file "%s"' % img, "break 0x%x" % (len(code) - 2), "go", "state", "exit"]
    p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=300)
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", p.stdout + p.stderr)
    return int(m.group(1)) if m else None


def pre(pairs):
    b = bytearray()
    for a, v in pairs:
        b += bytes([0x75, a, v])
    return b


def wait(bit):
    return bytes([0x30, bit, 0xFD])


results = []


def case(name, expect, got, unit, note=""):
    ok = abs(expect - got) <= max(1.0, abs(expect) * 0.02) if isinstance(expect, (int, float)) \
        else expect == got
    results.append((name, expect, got, ok, note, unit))


# --- CKCON0 is a real register -------------------------------------------------
code = pre([(CKCON0, 0x0B)]) + SJMP
write_hex("/tmp/x2_rd.hex", code)
p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"],
                   input='file "/tmp/x2_rd.hex"\nstep 3\ndump sfr 0x8f 0x90\nexit\n',
                   capture_output=True, text=True, cwd="/tmp", timeout=180)
m = [l for l in (p.stdout + p.stderr).splitlines() if re.match(r"^0x8f\s", l)]
# the line prints the address, an optional name, then binary and hex forms of the value
got = re.findall(r"0x[0-9a-f]+", m[0])[1] if m else None
case("CKCON0 holds what was written", "0x0b", got, "", "0x8f is not special")


# --- timer 2, baud mode: 128 ticks between the two reloads ---------------------
def t2_period(ckcon, rcap_lo):
    return run(pre([(CKCON0, ckcon), (0xCB, 0xFF), (0xCA, rcap_lo), (0xCD, 0xFF),
                    (0xCC, rcap_lo), (0xC8, 0x04)]) + wait(0xCF) + SJMP)


def t2_span(ckcon):
    return t2_period(ckcon, 0x00) - t2_period(ckcon, 0x80)


case("T2 standard mode", COUNTS * 12, t2_span(0x00), "clks", "1 tick/MC at 12 clks")
case("T2 X2, T2X2 clear", COUNTS * 6, t2_span(0x01), "clks", "1 tick/MC at 6 clks")
case("T2 X2, T2X2 set", COUNTS * 12, t2_span(0x09), "clks", "1 tick per 2 MC at 6 clks")
case("T2X2 inert without X2", COUNTS * 12, t2_span(0x08), "clks", "bit only active in X2")


# --- timer 0, mode 1: 65536 ticks from 0x0000 to overflow ----------------------
def t0_ticks(ckcon):
    return run(pre([(CKCON0, ckcon), (0x89, 0x01), (0x8C, 0x00), (0x8A, 0x00)]) +
               bytes([0xD2, 0x8C]) + wait(0x8D) + SJMP)


base = t0_ticks(0x00)
case("T0 standard mode", 65536 * 12, base, "clks", "1 tick/MC at 12 clks")
case("T0 X2, T0X2 clear", base / 2, t0_ticks(0x01), "clks", "same MC, half the clocks")
case("T0 X2, T0X2 set", base * 1, t0_ticks(0x03), "clks", "half rate cancels the faster CPU")

# --- PCA counter: the two machine-cycle clock selects of doc4235 Table 13-1 ------
CMOD = 0xD9
CCON = 0xD8
CR = 0x40                      # CCON.6 on this part; CCON.7 is the CF flag
CPS6 = 0x00                    # FCLK_PERIPH/6 = 1 count per machine cycle
CPS2 = 0x02                    # FCLK_PERIPH/2 = 3 counts per machine cycle


def pca_count(ckcon, cps, steps, filler=b""):
    """CH:CL after `steps` instructions: 3 setup MOVs, optional filler, then SJMP $."""
    code = pre([(CKCON0, ckcon), (CMOD, cps), (CCON, CR)]) + filler + SJMP
    img = "/tmp/x2_pca_%02x_%02x_%d_%d.hex" % (ckcon, cps, steps, len(filler))
    write_hex(img, code)
    cmds = ['file "%s"' % img, "step %d" % steps,
            "dump sfr[0xe9]", "dump sfr[0xf9]", "exit"]
    p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=300)
    v = {}
    for ln in (p.stdout + p.stderr).splitlines():
        m = re.match(r"^0x(e9|f9)\s+\S+:\s+\S+\s+(0x[0-9a-f]+)", ln)
        if m:
            v[m.group(1)] = int(m.group(2), 16)
    return 256 * v.get("f9", 0) + v.get("e9", 0)


def pca_span(ckcon, cps, n=500):
    """Counts over `n` SJMPs = 2n machine cycles; the setup MOVs difference out."""
    return pca_count(ckcon, cps, 2 * n) - pca_count(ckcon, cps, n)


MC = 2 * 500                   # machine cycles inside every differenced span below
case("PCA CPS=00 standard", MC, pca_span(0x00, CPS6), "counts", "1 tick/MC at 12 clks")
case("PCA X2, PCAX2 clear", MC, pca_span(0x01, CPS6), "counts", "same MC, half the clocks")
case("PCA X2, PCAX2 set", MC // 2, pca_span(0x21, CPS6), "counts", "1 tick per 2 MC")
case("PCAX2 inert without X2", MC, pca_span(0x20, CPS6), "counts", "bit only active in X2")
case("PCA CPS=01 standard", 3 * MC, pca_span(0x00, CPS2), "counts", "FCLK_PERIPH/2 = 3 ticks/MC")
case("PCA CPS=01 PCAX2 set", 3 * MC // 2, pca_span(0x21, CPS2), "counts", "fast timebase halved too")


# --- the skipped half cycle is carried, not dropped ------------------------------
# NOP is one machine cycle, so replacing 101 SJMPs with 101 NOPs shortens the same
# 500-instruction run from 1000 MC to 899 MC.  If the half cycle a slowed PCAX2 skips
# were dropped at every instruction boundary instead of being carried into the next
# instruction, those 101 MC would contribute nothing and this would read 101 counts.
phase = pca_count(0x21, CPS6, 500) - pca_count(0x21, CPS6, 500, b"\x00" * 101)
case("PCA PCAX2 keeps phase", 50.5, phase, "counts", "101-MC span of NOPs")


print("%-28s %12s %12s %6s  %s" % ("case", "expected", "measured", "unit", "verdict"))
bad = 0
for name, expect, got, ok, note, unit in results:
    bad += 0 if ok else 1
    e = "%d" % expect if isinstance(expect, (int, float)) else str(expect)
    g = "%d" % got if isinstance(got, (int, float)) else str(got)
    print("%-28s %12s %12s %6s  %s  %s" % (name, e, g, unit,
                                           "PASS" if ok else "FAIL", note))
print("\n%s: %d/%d" % ("PASS" if not bad else "FAIL", len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
