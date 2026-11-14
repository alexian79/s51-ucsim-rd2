#!/usr/bin/env python3
"""Timer 2 counter/capture edge counting: one count per falling T2 edge.

Authority (doc4235K, AT89C51RD2/ED2):
  * 12 "Timer 2" (printed p.31): "C/T2 selects FOSC/12 (timer operation) or external
    pin T2 (counter operation) as the timer clock input.  Setting TR2 allows TL2 to
    increment by the selected input."  Same page: "Timer 2 operation is similar to
    Timer 0 and Timer 1."
  * Table 12-1 T2CON (printed p.33-34): bit 1 C/T2# - "Set for counter operation
    (input from T2 input pin, falling edge trigger)"; bit 3 EXEN2 and bit 6 EXF2 -
    capture/reload "when a negative transition on T2EX pin is detected"; bit 0
    CP/RL2# - "Set to capture on negative transitions on T2EX pin if EXEN2 = 1";
    bit 2 TR2 - "Cleared to turn off Timer 2."
  * Port 1 alternate functions (printed p.12): "T2 (P1.0): Timer/Counter 2 external
    count input/Clockout", "T2EX: Timer/Counter 2 Reload/Capture/Direction Control",
    "ECI: External Clock for the PCA".  Port 3 (printed p.13): "T0 (P3.4): Timer 0
    external input", "T1 (P3.5): Timer 1 external input".

So a falling edge on T2 is worth exactly ONE TL2 increment: neither zero, nor two.

How an edge is produced here.  ucsim represents a pin transition two ways:
  (a) external stimulus - `set memory port_1_cfg 1 <val>` writes the port's pin set
      (cl_port::set_pin, port.cc:232).  This is the electrically honest route.
  (b) a latch write - a program's MOV P1,#data.  Every 51-family subscriber decides
      the pin level as `pins & latch` (cl_timer2::happen, timer2.cc:402-405, and the
      identical idiom in timer0.cc, pca.cc, interrupt.cc), so under that convention a
      latch write that clears a bit the pins are holding high IS the falling edge, and
      must be worth one count.  Route (b) is what the legs below drive, except where a
      leg name says "pin-drive".
Route (a) is measured too (the "pin-drive" legs): it is the invariant the fix must
preserve, and it is also the reason a fix that simply suppressed latch events outright
would not pass this file.

A latch write must produce exactly one EV_PORT_CHANGED.  Two means every counter
subscriber (timer 2, timer 0/1, PCA ECI) counts each edge twice, which is what the
Table 12-1 and p.31 legs catch on the vendor binary.

Control-binary audit: on a binary that carries the same double emit (/tmp/s51-ref-b) the
9 legs that still pass are exactly the negative ones - static level, raising-only, TR2=0,
T2EX-is-not-T2, C/T2=0-adds-nothing, T0-with-no-toggle - plus the three pin-drive legs.
They stay because they are the invariants a fix must not destroy: drop the port event
instead of deduplicating it and the pin-drive legs fall to 0; count every latch write
instead of every edge and the static/raising legs stop being 0.  Every leg that actually
measures the defect fails on the control binary.

Runs in a few seconds per binary; honours S51_SIM.  NOT wired into t/floor.sh.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.path.expanduser(
  "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
TMP = "/tmp/t2cnt_%d.hex" % os.getpid()

TCON, TMOD = 0x88, 0x89
TL0, TL1, TH0 = 0x8A, 0x8B, 0x8C
P1, P3 = 0x90, 0xB0
T2CON, T2MOD, RCAP2L, RCAP2H, TL2, TH2 = 0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD
CCON, CMOD, CL = 0xD8, 0xD9, 0xE9
CP_RL2, C_T2, TR2, EXEN2, EXF2, TF2 = 0x01, 0x02, 0x04, 0x08, 0x40, 0x80
CRBIT = 0x40                                       # CCON.CR (regs51.h bmCR)
T2_PIN, T2EX_PIN, ECI_PIN = 0x01, 0x02, 0x04       # P1.0, P1.1, P1.2
T0_PIN, T1_PIN = 0x10, 0x20                        # P3.4, P3.5
LBL = {TL0: "TL0", TL1: "TL1", TH0: "TH0", P1: "P1", CCON: "CCON", CL: "CL",
       CMOD: "CMOD", T2CON: "T2CON", RCAP2L: "RCAP2L", TL2: "TL2", TH2: "TH2"}
SLED = bytes([0x00] * 4) + bytes([0x80, 0xFE])     # 4 NOPs, then SJMP $

results = []


def mov(a, v):
    return bytes([0x75, a, v])


def console(cmds, device="89C51RD2"):
    """One short scripted run; commands go on stdin, never on argv.  Refuses to
    score a run whose image did not load (a zero image steps happily)."""
    p = subprocess.run([SIM, "-t", device, "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=120)
    out = p.stdout + p.stderr
    if "words read" not in out:
        raise RuntimeError("image did not load: %s" % out.splitlines()[:6])
    for ln in out.splitlines():
        if ln.startswith("Error:") or "Unknown" in ln:
            raise RuntimeError("console rejected a command: %s" % ln)
    return out


def read_sfrs(img, steps, addrs, device="89C51RD2", pre=(), extra_steps=()):
    write_hex(TMP, img)
    cmds = ['file "%s"' % TMP, "step %d" % steps] + list(pre)
    for s in extra_steps:
        cmds.append("step %d" % s)
    cmds += ["dump sfr[0x%x]" % a for a in addrs] + ["exit"]
    out = console(cmds, device)
    got = []
    for a in addrs:
        m = re.search(r"^0x[0-9a-f]{2} %s:\s+\S+\s+(0x[0-9a-f]+)" % LBL[a], out, re.M)
        if m is None:
            raise RuntimeError("no %s row in dump" % LBL[a])
        got.append(int(m.group(1), 16))
    return tuple(got)


def case(name, img, steps, addrs, expect, device="89C51RD2", note=""):
    try:
        got = read_sfrs(img, steps, addrs, device)
    except RuntimeError as e:
        got = ("error", str(e)[:40])
    ok = got == expect
    results.append((name, expect, got, ok, note))
    return ok


# ---------------------------------------------------------------- image builders
def t2_img(t2con, npairs, mask=T2_PIN, port=P1, pre=None, sled=4):
    """RCAP2/TL2/TH2 = 0, T2CON, latch high, then npairs falling+raising writes."""
    b = bytearray(mov(RCAP2L, 0) + mov(RCAP2H, 0) + mov(TL2, 0) + mov(TH2, 0))
    for a, v in (pre or ()):
        b += mov(a, v)
    b += mov(T2CON, t2con) + mov(port, 0xFF)
    n = 4 + len(pre or ()) + 2
    for _ in range(npairs):
        b += mov(port, 0xFF & ~mask) + mov(port, 0xFF)
        n += 2
    b += SLED
    return bytes(b), n + sled


def t2_flat_img(t2con, writes, first=0xFF):
    """same instruction count whatever `writes` holds: setup, latch, then writes"""
    b = bytearray(mov(RCAP2L, 0) + mov(RCAP2H, 0) + mov(TL2, 0) + mov(TH2, 0) +
                  mov(P1, first) + mov(T2CON, t2con))
    for v in writes:
        b += mov(P1, v)
    b += SLED
    return bytes(b), 6 + len(writes) + 4


def t2_bit_img(t2con, ops):
    """CLR/SETB on bit address 0x90 (P1.0): the bit-addressable branch of the latch."""
    b = bytearray(mov(RCAP2L, 0) + mov(RCAP2H, 0) + mov(TL2, 0) + mov(TH2, 0) +
                  mov(T2CON, t2con) + mov(P1, 0xFF))
    for o in ops:
        b += bytes([o, 0x90])
    b += SLED
    return bytes(b), 6 + len(ops) + 4


def tn_img(tmod, tr, npairs, mask, cell):
    """T0/T1 as event counters: mode 1 + C/T + TR, then npairs full toggles of `mask`."""
    b = bytearray(mov(TMOD, tmod) + mov(cell, 0) + mov(cell + 2, 0) + mov(P3, 0xFF))
    b += mov(TCON, tr)
    for _ in range(npairs):
        b += mov(P3, 0xFF & ~mask) + mov(P3, 0xFF)
    b += SLED
    return bytes(b), 5 + 2 * npairs + 4


# ------------------------------------------------------------------ timer 2 legs
# Table 12-1 C/T2# (p.34) + 12 (p.31): capture mode, C/T2=1, TR2=1.
CAP = CP_RL2 | C_T2 | TR2
for n in (1, 2, 3, 4):
    img, st = t2_img(CAP, n)
    case("T12-1 C/T2#: capture mode %d falling T2 edge(s) -> TL2" % n,
         img, st, (TL2,), (n,),
         note="one count per falling edge")

img, st = t2_img(CAP, 4)
case("T12-1 C/T2#: 4 edges stay inside TL2 (TH2 untouched)", img, st, (TL2, TH2),
     (4, 0), note="TH2 must not move before TL2 wraps")

# Table 12-1 C/T2# (p.34): falling edge only.
img, st = t2_flat_img(CAP, [0xFF, 0xFF, 0xFF], first=0x00)
case("T12-1 C/T2#: 3 raising-only writes -> TL2", img, st, (TL2,), (0,),
     note="latch low -> high is not a falling edge")
img, st = t2_flat_img(CAP, [0xFF, 0xFF, 0xFF, 0xFF])
case("T12-1 C/T2#: static level (4 identical writes) -> TL2", img, st, (TL2,), (0,),
     note="a steady level is not an edge at all")
img, st = t2_bit_img(CAP, [0xC2, 0xC2, 0xC2])
case("T12-1 C/T2#: CLR P1.0 x3 (1 edge, 2 static) -> TL2", img, st, (TL2,), (1,),
     note="bit-addressable latch: only the first CLR is an edge")
img, st = t2_bit_img(CAP, [0xC2, 0xD2, 0xC2, 0xD2])
case("T12-1 C/T2#: CLR/SETB P1.0, 2 falling edges -> TL2", img, st, (TL2,), (2,),
     note="bit-addressable latch route")

# Port 1 alternate functions (p.12): the count input is P1.0, not P1.1.
img, st = t2_img(CAP | EXEN2, 3, mask=T2EX_PIN)
case("p.12 pin table: T2EX(P1.1) edges do not clock TL2", img, st, (TL2,), (0,),
     note="T2 is P1.0; P1.1 is T2EX")

# Table 12-1 TR2 (p.34): counter disabled while TR2 = 0.
img, st = t2_img(CP_RL2 | C_T2, 3)
case("T12-1 TR2: C/T2=1 with TR2=0 -> TL2 stays 0", img, st, (TL2,), (0,),
     note="TR2 clears the counter input")

# Table 12-1 EXEN2 / EXF2 / CP-RL2 (p.34): capture on the T2EX falling edge.
img, st = t2_img(CAP | EXEN2, 3, mask=T2_PIN)
case("T12-1 EXEN2: EXEN2=1, no T2EX transition -> EXF2 clear", img, st,
     (TL2, T2CON), (3, CAP | EXEN2), note="EXF2 must not follow T2 edges")
img, st = t2_img(CAP | EXEN2, 3, mask=T2_PIN | T2EX_PIN)
case("T12-1 EXEN2+EXF2: 3 T2 edges + T2EX capture -> TL2", img, st, (TL2,), (3,),
     note="capture must not add counts")
img, st = t2_img(CAP | EXEN2, 3, mask=T2_PIN | T2EX_PIN)
case("T12-1 CP/RL2#: capture latches TL2 into RCAP2L", img, st, (RCAP2L, T2CON),
     (2, CAP | EXEN2 | EXF2),
     note="last capture saw the count of the 2 earlier edges; EXF2 set")

# Table 12-1 TF2 (p.34) "Set by hardware on Timer 2 overflow": the overflow boundary is
# a second, scale-independent check on the same edges - from FFFFh exactly ONE edge must
# land on 0000h, not 0001h.
img, st = t2_img(CAP, 1, pre=((TL2, 0xFF), (TH2, 0xFF)))
case("T12-1 TF2: 1 edge from FFFFh -> 0000h + TF2", img, st, (TL2, TH2, T2CON),
     (0, 0, CAP | TF2), note="overflow flag instead of a count magnitude")

# 12.1 (p.31): auto-reload mode is a 16-bit event counter too (CP/RL2#=0, C/T2=1).
for n in (1, 2, 3):
    img, st = t2_img(C_T2 | TR2, n)
    case("12.1 p.31 reload mode C/T2=1: %d edge(s) -> TL2" % n, img, st, (TL2,), (n,),
         note="event counter with automatic reload")

# Table 12-1 C/T2# (p.34) cleared: internal clock only, pin edges must be invisible.
# Differenced pair: same length, only the pin writes differ.
img_a, st_a = t2_flat_img(TR2, [0xFF & ~T2_PIN, 0xFF] * 3)
img_b, st_b = t2_flat_img(TR2, [0xFF] * 6)
try:
    d = read_sfrs(img_a, st_a, (TL2,))[0] - read_sfrs(img_b, st_b, (TL2,))[0]
    shown, leg_ok = "delta %+d" % d, d == 0
except RuntimeError as e:
    shown, leg_ok = str(e)[:24], False
results.append(("T12-1 C/T2#: C/T2=0 -> pin edges add 0 counts", "delta 0", shown, leg_ok,
                "two runs differing only in the pin writes"))

# ----------------------------------------------------------------- pin-drive legs
# 12 (p.31) external stimulus: driving the pin through the port's pin config must
# count 1:1.  These legs also pass on the unfixed binary ON PURPOSE - they are the
# invariant the latch route has to be measured against, and they fail if a "fix"
# drops events instead of deduplicating them.
spin = bytearray(mov(RCAP2L, 0) + mov(RCAP2H, 0) + mov(TL2, 0) + mov(TH2, 0) +
                 mov(T2CON, CAP) + mov(P1, 0xFF)) + bytes([0x80, 0xFE])
for n in (1, 2, 3):
    drives = []
    for _ in range(n):
        drives += ["set memory port_1_cfg 1 0x00", "set memory port_1_cfg 1 0xff"]
    try:
        got = read_sfrs(bytes(spin), 6, (TL2,), pre=drives, extra_steps=(3,))
    except RuntimeError as e:
        got = ("error", str(e)[:24])
    results.append(("p.31 pin-drive route: %d falling pin edge(s) -> TL2" % n,
                    (n,), got, got == (n,),
                    "external stimulus; invariant the latch route must match"))

# ------------------------------------------- same core under a second device model
img, st = t2_img(CAP, 2)
case("T12-1 C/T2# on -t C52: 2 edges -> TL2", img, st, (TL2,), (2,), device="C52",
     note="shared core/port layer, not the RD2 device file")
img, st = t2_img(C_T2 | TR2, 2)
case("12.1 p.31 reload mode on -t C52: 2 edges -> TL2", img, st, (TL2,), (2,),
     device="C52", note="shared core/port layer")

# ---------------------------------- other EV_PORT_CHANGED subscribers (blast radius)
# 12 (p.31) "Timer 2 operation is similar to Timer 0 and Timer 1" + p.13 pin table:
# one complete toggle of T0/T1 is worth one count, whatever polarity is sampled.
for (nm, tmod, tr, mask, cell) in (("T0 (P3.4)", 0x05, 0x10, T0_PIN, TL0),
                                   ("T1 (P3.5)", 0x50, 0x40, T1_PIN, TL1)):
    img, st = tn_img(tmod, tr, 1, mask, cell)
    case("12 p.31 + p.13: %s counter, 1 full toggle -> TL" % nm, img, st, (cell,), (1,),
         note="one edge of the sampled polarity per toggle")
    img, st = tn_img(tmod, tr, 2, mask, cell)
    case("12 p.31 + p.13: %s counter, 2 full toggles -> TL" % nm, img, st, (cell,), (2,),
         note="linear in the number of toggles")
img, st = tn_img(0x05, 0x10, 0, T0_PIN, TL0)
case("p.13: T0 counter, static P3 (no toggle) -> TL0", img, st, (TL0,), (0,),
     note="no toggle, no count")

# Table 13-1 CPS=11: the PCA counter runs off ECI (P1.2) falling edges.
img = bytearray(mov(CL, 0) + mov(0xEA, 0) + mov(P1, 0xFF) + mov(CMOD, 0x06) +
                mov(CCON, CRBIT))          # CPS=11 -> ECI clock, CCON.CR -> run
n = 5
for _ in range(2):
    img += mov(P1, 0xFF & ~ECI_PIN) + mov(P1, 0xFF)
    n += 2
img += SLED
case("T13-1 CPS=11: 2 falling ECI(P1.2) edges -> CL", bytes(img), n, (CL,), (2,),
     note="PCA counts ECI edges too (pca.cc:368 happen)")

# ------------------------------------------------------------------------ summary
print("%-56s %-12s %-12s %-4s %s" % ("leg", "expected", "measured", "verdict", "note"))
bad = 0
for name, expect, got, ok, note in results:
    bad += 0 if ok else 1
    print("%-56s %-12s %-12s %-4s %s" %
          (name, expect, got, "ok" if ok else "BAD", note))
print("binary: %s" % SIM)
print("t2cnttest: %s %d/%d legs" % ("FAIL" if bad else "PASS", len(results) - bad,
                                    len(results)))
if bad:
    print("t2cnttest: %d leg(s) over-count/under-count edges" % bad)
sys.exit(1 if bad else 0)
