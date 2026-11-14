#!/usr/bin/env python3
"""CEXn -> module association: the PCA must react on the pin of the module that is armed.

doc4235 §13 p.35 puts all five capture/compare pins on port 1: ECI=P1.2 and
CEX0..4=P1.3..P1.7, which is what `bmCEX0..4` (regs51.h:194-198: 0x08, 0x10, 0x20, 0x40,
0x80) claims.  The masks are a plausible off-by-one site and nothing else in the suite
exercised the *pairing*, so this drives one pin at a time with two modules armed for
positive-edge capture and checks which module's CCF fires:

    MOV CCAPM0,#0x20     CAPP (bit5) only: capture on a rising CEX0, ECOM left CLEAR
    MOV CCAPM4,#0x20     ... likewise on CEX4
    MOV CCAP0H,#0x55     sentinels; a capture overwrites them with CH:CL
    MOV CCAP0L,#0xAA
    MOV CCAP4H,#0x5A
    MOV CCAP4L,#0xA4
    MOV CMOD,#0x00       CPS=00 (FCLK_PERIPH/6), WDTE/ECF clear
    MOV CCON,#0x40       CR = 1: run the counter
    MOV P1,#0x00         CEX0 and CEX4 low (P1 resets to 0xff, so these are falling
    MOV P1,#0x08         ...  edge: only a RISING edge on the armed pin may capture)
    SJMP $

ECOM is deliberately left clear: Table 13-4 marks it don't-care for capture (`X10000X`),
and with ECOM set the comparator *drives* the pin (that is defect D2), so the edge would
no longer belong to the test.  Pins are read from the P1 latch (`dump sfr[0x90]`); a port
bit read on this model always returns 0.

Run B moves the same edge to bit 7 and the expectation swaps to module 4; run C leaves
both CCAPM registers at 0, so no module may claim an event.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.path.expanduser(
  "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
CMOD, CCON = 0xD9, 0xD8
CCAPM0, CCAPM4 = 0xDA, 0xDE
CCAP0H, CCAP0L, CCAP4H, CCAP4L = 0xFA, 0xEA, 0xFE, 0xEE
P1 = 0x90
CAPP, CR = 0x20, 0x40
CCF0, CCF4 = 0x01, 0x10
SJMP = bytes([0x80, 0xFE])
SENT = ((CCAP0H, 0x55), (CCAP0L, 0xAA), (CCAP4H, 0x5A), (CCAP4L, 0xA4))


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
    return int(m.group(1), 16) if m else None


def run(capp, pin_value, steps=60, chip="89C51R"):
    """CCON/CCAP sentinels/P1 after `steps` instructions; `capp` arms modules 0 and 4."""
    code = pre([(CCAPM0, capp), (CCAPM4, capp)] + list(SENT) +
               [(CMOD, 0x00), (CCON, CR), (P1, 0x00), (P1, pin_value)]) + SJMP
    img = "/tmp/pcapin_%02x_%02x_%d_%s.hex" % (capp, pin_value, steps, chip)
    write_hex(img, code)
    out = sim(['file "%s"' % img, "step %d" % steps] +
              ["dump sfr[0x%x]" % a for a in
               (P1, CCON, CCAP0H, CCAP0L, CCAP4H, CCAP4L)], chip=chip)
    return {a: peek(out, a) for a in (P1, CCON, CCAP0H, CCAP0L, CCAP4H, CCAP4L)}, out


fails = []


def check(leg, ok, detail=""):
    print("%-46s %s %s" % (leg, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(leg)


# --- A: rising edge on P1.3 (CEX0) may only fire module 0 --------------------------
a, raw = run(CAPP, 0x08)
ccon = a[CCON]
check("A CEX0=P1.3 edge sets CCF0", ccon is not None and bool(ccon & CCF0), "CCON=%s" % ccon)
check("A CEX0 edge leaves CCF4 clear", ccon is not None and not ccon & CCF4, "CCON=%s" % ccon)
check("A module 0 took CH (sentinel gone)", a[CCAP0H] != 0x55, "CCAP0H=%s" % a[CCAP0H])
check("A module 4 kept both sentinels", a[CCAP4H] == 0x5A and a[CCAP4L] == 0xA4,
      "CCAP4H=%s CCAP4L=%s" % (a[CCAP4H], a[CCAP4L]))
check("A the pin really moved", a[P1] == 0x08, "P1=%s" % a[P1])

# --- B: the same edge on P1.7 (CEX4) may only fire module 4 ------------------------
b, raw = run(CAPP, 0x80)
ccon = b[CCON]
check("B CEX4=P1.7 edge sets CCF4", ccon is not None and bool(ccon & CCF4), "CCON=%s" % ccon)
check("B CEX4 edge leaves CCF0 clear", ccon is not None and not ccon & CCF0, "CCON=%s" % ccon)
check("B module 4 took CH (sentinel gone)", b[CCAP4H] != 0x5A, "CCAP4H=%s" % b[CCAP4H])
check("B module 0 kept both sentinels", b[CCAP0H] == 0x55 and b[CCAP0L] == 0xAA,
      "CCAP0H=%s CCAP0L=%s" % (b[CCAP0H], b[CCAP0L]))

# --- C: nothing armed => no flag, no reload ---------------------------------------
c, raw = run(0x00, 0x08)
ccon = c[CCON]
check("C unarmed modules set no CCF", ccon is not None and not (ccon & (CCF0 | CCF4)),
      "CCON=%s" % ccon)
check("C unarmed modules kept all sentinels",
      (c[CCAP0H], c[CCAP0L], c[CCAP4H], c[CCAP4L]) == (0x55, 0xAA, 0x5A, 0xA4),
      "%s" % [c[k] for k in (CCAP0H, CCAP0L, CCAP4H, CCAP4L)])

# --- D/E/F: the comparator must be evaluated on every count ------------------------
# doc4235 §13.2 p.43: ECOM+MAT is a 16-bit software timer — when {CH,CL} reaches
# {CCAPnH,CCAPnL} the CCFn flag is set, and §13.3 adds TOG to toggle the pin (high-speed
# output).  Both are long-established behaviour that the fork only evaluated once per
# 65536 counts, i.e. never: these images start CL just below the target so the match has
# to happen mid-run, with CH:CL nowhere near a wrap.
CL, CH = 0xE9, 0xF9
MAT, TOG, ECOM = 0x08, 0x04, 0x40


def run_cmp(mode, ccap0l=0x20, cl=0x18, steps=20, chip="89C51R"):
    """CL starts at `cl`, below the CCAP0L target; 7 MOVs then SJMP $."""
    code = pre([(CCAPM0, mode), (CCAP0H, 0x00), (CCAP0L, ccap0l),
                (CL, cl), (CH, 0x00), (CMOD, 0x00), (CCON, CR)]) + SJMP
    img = "/tmp/pcapin_c_%02x_%02x_%02x_%d_%s.hex" % (mode, ccap0l, cl, steps, chip)
    write_hex(img, code)
    out = sim(['file "%s"' % img, "step %d" % steps] +
              ["dump sfr[0x%x]" % x for x in (P1, CCON, CL, CH, CCAP0L)], chip=chip)
    return {x: peek(out, x) for x in (P1, CCON, CL, CH, CCAP0L)}, out


d, raw = run_cmp(ECOM | MAT)
check("D ECOM|MAT fires CCF0 with no counter wrap", d[CCON] is not None and bool(d[CCON] & CCF0),
      "CCON=%s CL=%s" % (d[CCON], d[CL]))
check("D the match point was passed, CH:CL never wrapped",
      d[CL] is not None and d[CH] is not None and 0x20 < d[CL] < 0xf0 and d[CH] == 0,
      "CL=%s CH=%s" % (d[CL], d[CH]))

e, raw = run_cmp(MAT)
check("E MAT without ECOM never fires", e[CCON] is not None and not e[CCON] & CCF0,
      "CCON=%s" % e[CCON])

f, raw = run_cmp(ECOM | MAT | TOG)
check("F high-speed output toggles CEX0 at the match", f[P1] == 0xf7, "P1=%s" % f[P1])
check("F and sets CCF0", f[CCON] is not None and bool(f[CCON] & CCF0), "CCON=%s" % f[CCON])

# --- G/H: the module-4 watchdog must reset the chip, not the PCA -------------------
# §13.5 p.46: with WDTE (CMOD.6) set, a 16-bit match on module 4 generates an internal
# reset that does not drive the RST pin.  The fork called cl_pca::reset(), which only
# clamps the PCA's own state, so the program kept running — the opposite of a watchdog.
WDTE = 0x40


def run_wd(wdte, steps, chip="89C51R"):
    """Module 4 as the watchdog: CCAP4=0x0020, CL starts at 0x18, so it fires soon."""
    code = pre([(CCAPM4, 0x48), (CCAP4H, 0x00), (CCAP4L, 0x20), (CL, 0x18), (CH, 0x00),
                (CMOD, WDTE if wdte else 0x00), (CCON, CR)]) + SJMP
    img = "/tmp/pcapin_wd%d_%d_%s.hex" % (1 if wdte else 0, steps, chip)
    write_hex(img, code)
    out = sim(['file "%s"' % img, "step %d" % steps] +
              ["dump sfr[0x%x]" % x for x in (CCON, CMOD, CL, CCAPM4)], chip=chip)
    return {x: peek(out, x) for x in (CCON, CMOD, CL, CCAPM4)}, out


g, _ = run_wd(True, 10)
check("G WDTE match resets the chip (CCON blank)", g[CCON] == 0, "CCON=%s CL=%s" % (g[CCON], g[CL]))
check("G and clears the mode/period registers", g[CMOD] == 0 and g[CCAPM4] == 0,
      "CMOD=%s CCAPM4=%s" % (g[CMOD], g[CCAPM4]))
g18, _ = run_wd(True, 18)
g20, _ = run_wd(True, 20)
check("G the watchdog fires again after the program re-arms",
      g18[CCON] == 0x40 and g20[CCON] == 0, "CCON@18=%s CCON@20=%s" % (g18[CCON], g20[CCON]))

h10, _ = run_wd(False, 10)
h20, _ = run_wd(False, 20)
check("H no WDTE: no reset, CR survives", h10[CCON] is not None and bool(h10[CCON] & CR),
      "CCON=%s" % h10[CCON])
check("H and the counter is still running later", h20[CCON] is not None and bool(h20[CCON] & CR),
      "CCON=%s CL=%s" % (h20[CCON], h20[CL]))

print()
print("PASS: %d/%d" % (21 - len(fails), 21))
if fails:
    print("failed: " + "; ".join(fails))
    sys.exit(1)
