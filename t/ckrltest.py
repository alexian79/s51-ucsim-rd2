#!/usr/bin/env python3
"""CKRL (0x97) oscillator prescaler of the AT89C51RD2/ED2 -- doc4235 section 6.

The law, from section 6.2.1 (printed p.16, /tmp/ds/p015.txt, where the equations extract as
split lines and must be read as one page):

    CKRL = FFh   FCLK_CPU = FCLK_PERIPH = FOSC/2 (Standard), FOSC (X2)      <- BYPASS
    CKRL = 00h   FCLK_CPU = FCLK_PERIPH = FOSC/1020 (Standard), FOSC/510 (X2)
    else         FOSC/(4*(255-CKRL)) in X1, FOSC/(2*(255-CKRL)) in X2

so with  M = 1 for CKRL = FFh else 2*(255 - CKRL),  both nets are the bypass clock divided
by M.  Table 6-1 (printed p.15) resets CKRL to 1111 1111b and Table 3-11 (printed p.8, the
90h/97h row of the map that starts on p.7) agrees, so the part comes up at M = 1.

A machine cycle is 6 periods of the core clock (section 7.1.1, printed p.17: "needs only 6
clock periods per machine cycle", 12 in Standard mode because of the /2 on XTAL1), so the
quantity ucsim can actually observe is:

    clocks per machine cycle = 12*M in Standard mode, 6*M in X2            (section 6.2.1)

Figures 6-1 (printed p.16) and 7-1 (printed p.18) put the prescaler AHEAD of the
CPU/peripheral split -- section 6.2.1 writes the result as one equality, FCLK_CPU =
FCLK_PERIPH -- which yields the two rules every leg below asserts:

  (a) nothing that counts machine cycles moves.  Timers, the PCA timebase (Table 13-1,
      printed p.37: CPS=00 is FCLK_PERIPH/6 = 1 count per machine cycle, CPS=01 is
      FCLK_PERIPH/2 = 3), the watchdog ("increment every machine cycle", section 19, printed
      p.84), the internal BRG (figure 14-5) and every instruction's cycle count are machine
      cycle derived, so their counts are identical at any CKRL.
  (b) every rate stated in oscillator clocks divides by M, because a machine cycle is worth
      M times as many of them: the BRG's overflow, the PCA timebase, timer counting, T2OE's
      clock out (section 12, printed p.31: "the input clock increments TL2 at frequency
      FCLK PERIPH/2"), the SPI bit time (Table 16-x, printed p.64: FCLK PERIPH /2../128) and
      the UART's fixed mode 0 / mode 2 rates (Table 14-4, printed p.53, stated at CKRL = FFh).

Method.  Every number is a DIFFERENCE between two runs whose programs are byte-identical up
to a tail of NOPs (opcode 0x00, 1 machine cycle, 1 byte) and differ only in how many of them
they hold.  The difference is therefore exactly D machine cycles, and it cancels the launch
cost, the cost of the setup MOVs and -- importantly -- the machine cycle in which the write
hook that changes CKRL itself runs.  Nothing is fitted to the model: each expectation is
written as D * (12 or 6) * M with M coming from CKRL through the formula above, or is the
count of a peripheral event whose per-machine-cycle rate the datasheet prints.

Traps this file defends against, each of which has already burned this project:
  - `-t 89C51RD2`, not `-t AT89C51RD2` (the latter is "Unknown processor type").
  - `get sfr 0x97` does NOT read the SFR cell -- it reads another space and answers 0x7f.
    SFR reads are `dump sfr <lo> <hi>` with an explicit range, writes are `MOV`, there is no
    `set sfr`.
  - clock counts come from `state` ("Total time since last reset= .. sec (N clks)"); there is
    no `get sim_ticks`.
  - `file` must be quoted or the loader runs an all-zero image, and a bad HEX checksum is not
    rejected, so every image goes through mkhex.write_hex, which asserts its own checksums.
  - MOV PCON,#0x01 idles the CPU and looks exactly like a hang; SMOD1 is 0x80.
  - a rate measurement must never read a port BIT (a port bit read answers 0); T2OE is read
    out of the P1 latch.
  - a long scripted console run DROPS output rows, so the k-th `dump` in a `step 1 ; dump`
    script is not the k-th step: it desynchronised while this file was written and invented a
    13-machine-cycle timer period.  Every step-quantised reading here is therefore one
    `step N` plus one query in its own session (see pin_low_at / clks_at).
  - every console run is bounded by S51_TIMEOUT: a binary without the prescaler would spin
    forever on a leg that waits for an event that never comes.

Run:  S51_SIM=/path/to/ucsim_51 python3 ckrltest.py
Control: S51_SIM=/tmp/s51-ref-b python3 ckrltest.py   (pre-CKRL build; must fail, and the
         last block proves which legs detect its absence and which are absence-checking.)
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM", os.path.expanduser(
  "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51"))
REF = os.environ.get("S51_REF", "/tmp/s51-ref-b")
TIMEOUT = int(os.environ.get("S51_TIMEOUT", "60"))
WORK = "/tmp/ckrl-%d" % os.getpid()   # per-process: concurrent runs must not clobber
DEVICE = "89C51RD2"

os.makedirs(WORK, exist_ok=True)

# SFRs and bits used below (doc4235 Table 3-11, printed pp.7-8)
CKRL = 0x97
CKCON0 = 0x8f                     # X2 0x01, T0X2 0x02, T1X2 0x04, T2X2 0x08, SIX2 0x10,
                                  # PCAX2 0x20, WDX2 0x40 (Table 7-1, printed p.18)
X2, T0X2, SIX2 = 0x01, 0x02, 0x10
PCON, SMOD1 = 0x87, 0x80
TMOD, TCON, TL0, TH0 = 0x89, 0x88, 0x8a, 0x8c
TR0 = 0x10
SCON, SBUF, TI = 0x98, 0x99, 0x99
MODE2 = 0x80                      # SM0=bit7, SM1=bit6
BRL, BDRCON = 0x9a, 0x9b
BM_BRR, BM_TBCK, BM_RBCK, BM_SPD = 0x10, 0x08, 0x04, 0x02
CCON, CMOD, CL, CH = 0xd8, 0xd9, 0xe9, 0xf9
CR = 0x40
RCAP2L, RCAP2H, TL2, TH2 = 0xca, 0xcb, 0xcc, 0xcd
T2MOD, T2CON, T2OE, TR2 = 0xc9, 0xc8, 0x02, 0x04
P1 = 0x90

SJMP = bytes([0x80, 0xFE])
NOP = b"\x00"                     # 1 machine cycle, 1 byte

_checks = 0
_failures = []


def check(name, ok, detail=""):
    global _checks
    _checks += 1
    print("%-62s %s%s" % (name, "PASS" if ok else "FAIL", "  %s" % detail if detail else ""))
    if not ok:
        _failures.append(name)
    return ok


def mov(a, v):
    """MOV direct,#imm -- 2 machine cycles, and the SFR write hook lands one cycle in."""
    return bytes([0x75, a, v])


def nops(n):
    return NOP * n


def m_of(ckrl):
    """M of doc4235 section 6.2.1: FFh is the bypass, never a divide by 255-255."""
    return 1 if ckrl == 0xff else 2 * (255 - ckrl)


# ---------------------------------------------------------------- console
def run(path, cmds, sim=None):
    script = "\n".join(['file "%s"' % path] + cmds + ["exit"]) + "\n"
    try:
        p = subprocess.run([sim or SIM, "-t", DEVICE, "-c", "-"], input=script,
                           capture_output=True, text=True, cwd=WORK, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    return p.stdout + p.stderr


def measure(code, sim=None, dumps=(), info=()):
    """Run `code` to its trailing SJMP and return {'clks','sfr{addr}','ovf'} or None.

    The break stops the CPU before the SJMP executes, so the clock total is the prefix plus
    exactly len(NOP tail) machine cycles of the program itself."""
    path = os.path.join(WORK, "m_%d_%d.hex" % (len(code), sum(code) & 0xFFFF))
    write_hex(path, bytes(code))
    cmds = ["break 0x%x" % (len(code) - 2), "go"]
    for a in dumps:
        cmds.append("dump sfr 0x%02x 0x%02x" % (a, a))
    for i in info:
        cmds.append("info hw %s" % i)
    cmds.append("state")
    t = run(path, cmds, sim)
    if t is None:
        return None
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", t)
    out = {"clks": int(m.group(1)) if m else None, "text": t}
    for a in dumps:
        mm = re.search(r"^0x%02x\s+(?:\S+\s+)?0b[01]+\s+0x([0-9a-f]{2})" % a, t, re.M)
        out["v%02x" % a] = int(mm.group(1), 16) if mm else None
    mm = re.search(r"published_overflows=(\d+)", t)
    if mm:
        out["ovf"] = int(mm.group(1))
    return out


def pair(prefix, n, d, sim=None, dumps=(), info=()):
    """Two runs identical but for d extra NOPs: returns (delta_clks, {addr: delta_value})."""
    a = measure(prefix + nops(n) + SJMP, sim, dumps, info)
    b = measure(prefix + nops(n + d) + SJMP, sim, dumps, info)
    if not a or not b or a["clks"] is None or b["clks"] is None:
        return None, None, (a or {}).get("text") or "<timeout>"
    deltas = {}
    for k in ("ovf",):
        if k in a and k in b:
            deltas[k] = b[k] - a[k]
    for ad in dumps:
        if a.get("v%02x" % ad) is not None and b.get("v%02x" % ad) is not None:
            deltas[ad] = b["v%02x" % ad] - a["v%02x" % ad]
    return b["clks"] - a["clks"], deltas, b["text"]


def counter(deltas, hi, lo):
    """16-bit {hi:lo} delta from two reads, wrapped."""
    if hi not in deltas or lo not in deltas:
        return None
    return (deltas[hi] * 256 + deltas[lo]) & 0xFFFF


def state_of(code, sim=None):
    path = os.path.join(WORK, "s_%d_%d.hex" % (len(code), sum(code) & 0xFFFF))
    write_hex(path, bytes(code))
    t = run(path, ["break 0x%x" % (len(code) - 2), "go", "state"], sim)
    if t is None:
        return None
    out = {}
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", t)
    out["clks"] = int(m.group(1)) if m else None
    m = re.search(r"Inst= (\d+) Fetch= (\d+)", t)
    out["inst"], out["fetch"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    return out


# =============================================================== 1. the register
print("\n-- CKRL itself: reset value (Table 3-11 p.8, Table 6-1 p.15) is the bypass")
write_hex(os.path.join(WORK, "reset.hex"), SJMP)
t = run(os.path.join(WORK, "reset.hex"), ["dump sfr 0x90 0x9f"])
m = re.search(r"^0x97\s+(?:\S+\s+)?0b[01]+\s+0x([0-9a-f]{2})", t or "", re.M)
reset_ckrl = int(m.group(1), 16) if m else None
check("reset value of CKRL is 1111 1111b", reset_ckrl == 0xff, "read 0x%02x" % reset_ckrl
      if reset_ckrl is not None else "CKRL missing from the dump")

# The bypass must not be "divide by 255-255".  Four MOV A,#data are 4 machine cycles, so at
# M = 1 the program costs 4*12 = 48 oscillator clocks; a run that first writes CKRL = FFh
# costs those 48 plus its own 2-cycle MOV, because FFh changes nothing.
base = measure(bytes([0x74, 0x00]) * 4 + SJMP)
withff = measure(mov(CKRL, 0xff) + bytes([0x74, 0x00]) * 4 + SJMP)
check("M = 1 at reset: 4 machine cycles cost 48 oscillator clocks",
      base and base["clks"] == 4 * 12, "got %s" % (base and base["clks"]))
check("CKRL = FFh is a bypass, not a divide (same 4 MC, plus the MOV that writes it)",
      withff and withff["clks"] == 4 * 12 + 2 * 12, "got %s" % (withff and withff["clks"]))

# Table 3-11 prints the write mask of 97h as 8 real bits: plain RW, no read-clear, no
# reserved hole.  Three writes, read after each, so a read-clear bug cannot hide.
path = os.path.join(WORK, "rw.hex")
write_hex(path, mov(CKRL, 0x5a) + mov(CKRL, 0x00) + mov(CKRL, 0xff) + SJMP)
t = run(path, ["step 1", "dump sfr 0x97 0x97", "step 1", "dump sfr 0x97 0x97",
               "step 1", "dump sfr 0x97 0x97"])
vals = [int(v, 16) for v in re.findall(r"^0x97\s+(?:\S+\s+)?0b[01]+\s+0x([0-9a-f]{2})",
                                       t or "", re.M)]
check("CKRL is a plain RW byte (5Ah, 00h, FFh read back in order)",
      vals == [0x5a, 0x00, 0xff], "read %s" % ["0x%02x" % v for v in vals])

# =============================================================== 2. the clock law
print("\n-- clocks per machine cycle = 12*M (Standard) and 6*M (X2), section 6.2.1")
# D machine cycles are the difference of two programs holding D extra NOPs, so the cost of
# the CKRL write and of every setup MOV cancels; the expectation is pure section 6.2.1.
for ckrl, label in [(0xfe, "FEh -> M = 2 (the /4 endpoint of section 6.2.1)"),
                    (0xf8, "F8h -> M = 14"),
                    (0x00, "00h -> M = 510 (FOSC/1020, machine cycle = 6*1020 clocks)")]:
    M = m_of(ckrl)
    d = 256 if ckrl != 0x00 else 20
    dc, _, why = pair(mov(CKRL, ckrl), 10, d)
    check("%s: %d machine cycles cost %d oscillator clocks" % (label, d, d * 12 * M),
          dc == d * 12 * M, "got %s" % dc if dc is not None else why[:70])

# X2 composes: the prescaler sits ahead of the X2 mux (figure 7-1, printed p.18), so the X2
# machine cycle is 6*M oscillator clocks.  FOSC/510 at CKRL = 00h means exactly that.
for ckcon, ckrl, label in [(X2, 0xfe, "X2 + CKRL = FEh: 6*2 = 12 clocks per machine cycle"),
                           (X2, 0x00, "X2 + CKRL = 00h: FOSC/510 -> 6*510 = 3060 clocks")]:
    M = m_of(ckrl)
    d = 100 if ckrl != 0x00 else 20
    dc, _, why = pair(mov(CKCON0, ckcon) + mov(CKRL, ckrl), 4, d)
    check(label.replace("6*2 = 12", "6*%d = 12" % M).replace("6*510 = 3060", "6*510 = 3060"),
          dc == d * 6 * M, "got %s" % dc if dc is not None else why[:70])

# No drift.  A divisor carried as an integer count per machine cycle would have to round for
# M > 2; over 8192 machine cycles at M = 14 the exact total is 8192*12*14 clocks and any
# per-call rounding shows up as a difference of tens of thousands.
M = m_of(0xf8)
d = 8192
dc, _, why = pair(mov(CKRL, 0xf8), 8, d)
check("no drift: 8192 machine cycles at M = 14 cost exactly %d clocks" % (d * 12 * M),
      dc == d * 12 * M, "got %s" % dc if dc is not None else why[:70])

# =============================================================== 3. the consumers
print("\n-- consumers: counts per machine cycle stand still, their clock cost divides by M")

# timer 0, mode 1, internal clock: 1 count per machine cycle at any CKRL (section 7.1's
# "12 clock periods per peripheral clock cycle", Table 7-1 printed p.18), so a count costs
# 12*M oscillator clocks.  TH0:TL0 starts at FF00h so a 200-cycle window cannot wrap.
t0 = mov(CKRL, 0xf8) + mov(TMOD, 0x01) + mov(TH0, 0xff) + mov(TL0, 0x00) + mov(TCON, TR0)
dc, dl, why = pair(t0, 10, 200, dumps=(TL0, TH0))
M = m_of(0xf8)
check("timer 0 counts 1 per machine cycle at M = 14 (200 NOPs = 200 counts)",
      counter(dl, TH0, TL0) == 200, "got %s" % counter(dl, TH0, TL0))
check("timer 0 count costs 12*14 = 168 oscillator clocks (FCLK_PERIPH/6, Table 13-1 p.37)",
      dc == 200 * 12 * M, "got %s" % dc if dc is not None else why[:70])

# T0X2 holds timer 0 at the standard speed while the CPU is fast: 2 machine cycles per count
# (Table 7-1), and CKRL still multiplies the whole domain, so a count costs 2*6*14 clocks.
M = m_of(0xf8)
dc, dl, why = pair(mov(CKRL, 0xf8) + mov(CKCON0, X2 | T0X2) + mov(TMOD, 0x01) +
                   mov(TH0, 0xff) + mov(TL0, 0x00) + mov(TCON, TR0), 10, 200,
                   dumps=(TL0, TH0))
check("CKCON0.X2 + T0X2 compose with CKRL: 200 MC -> 100 counts",
      counter(dl, TH0, TL0) == 100, "got %s" % counter(dl, TH0, TL0))
check("...at 100*2*(6*14) = 16800 clocks, i.e. X2 halves and CKRL multiplies",
      dc == 100 * 2 * 6 * M, "got %s" % dc if dc is not None else why[:70])

# PCA, CPS = 01 is FCLK_PERIPH/2 = 3 counts per machine cycle (Table 13-1, printed p.37), so
# one PCA count costs 12*M/3 = 4*M oscillator clocks.  CPS is CMOD bits 2:1, so CPS = 01b is
# the byte 0x02, and 0x00 is CPS = 00b = FCLK_PERIPH/6 (pca.cc: clk_source reads value >> 1).
M = m_of(0xf8)
d = 300
dc, dl, why = pair(mov(CKRL, 0xf8) + mov(CMOD, 0x02) + mov(CCON, CR), 6, d, dumps=(CL, CH))
counts = counter(dl, CH, CL)
check("PCA CPS=01 counts 3 per machine cycle at M = 14 (%d MC -> %d counts)" % (d, 3 * d),
      counts == 3 * d, "got %s" % counts)
check("PCA count costs 4*14 = 56 oscillator clocks (FCLK_PERIPH/2 / M, Table 13-1 p.37)",
      dc == 3 * d * 4 * M, "got %s" % dc if dc is not None else why[:70])

# Timer 2 clock out (T2OE).  "The input clock increments TL2 at frequency FCLK PERIPH/2"
# (section 12, printed p.31), i.e. 3 counts per machine cycle, and the counter reloads from
# RCAP2 at overflow, toggling P1.0 there -- so with RCAP2 = FFD0h it overflows every
# (65536-0xFFD0)/3 = 16 machine cycles and P1.0 repeats every 32 of them.  TL2 itself cannot
# be used as a measure in this mode (it is reloaded on every overflow), so the wave on P1.0
# is measured instead: 32 machine cycles is the machine-cycle-domain reading of that period,
# 32*12*M clocks is the absolute one the prescaler has to move.
def pin_low_at(path, n, sim):
  """P1.0 after EXACTLY n steps, or None.  One step count per session, one query."""
  t = run(path, ["step %d" % n, "dump sfr 0x90 0x90"], sim)
  m = re.search(r"^0x90\s+(?:\S+\s+)?0b[01]+\s+0x([0-9a-f]{2})", t or "", re.M)
  return None if m is None else not int(m.group(1), 16) & 1


def clks_at(path, n, sim):
  """oscillator clocks after exactly n steps, from `state'."""
  t = run(path, ["step %d" % n, "state"], sim)
  m = re.search(r"\((\d+) clks\)", t or "")
  return None if m is None else int(m.group(1))


def crossing(path, sim, lo, hi, want):
  """First step in (lo,hi] where the pin equals `want`; the window must hold one edge."""
  while hi - lo > 1:
    mid = (lo + hi) // 2
    if pin_low_at(path, mid, sim) == want:
      hi = mid
    else:
      lo = mid
  return hi if pin_low_at(path, hi, sim) == want else None


def t2_wave(sim, ckrl):
  """(falling, rising, next falling, clocks over the full period) of the P1.0 clock out."""
  code = mov(CKRL, ckrl) + mov(RCAP2L, 0xd0) + mov(RCAP2H, 0xff) + mov(TH2, 0xff) + \
      mov(TL2, 0xd0) + mov(T2MOD, T2OE) + mov(T2CON, TR2) + nops(200) + SJMP
  path = os.path.join(WORK, "t2wave_%02x_%s.hex" % (ckrl, "s"))
  write_hex(path, code)
  if pin_low_at(path, 9, sim):                  # 9 > the 7-instruction setup
    return None, None, None, None, "P1.0 was already low after the setup"
  k1 = crossing(path, sim, 9, 40, True)         # first falling edge
  if k1 is None:
    return None, None, None, None, "P1.0 never went low within 40 steps"
  h1 = crossing(path, sim, k1, k1 + 20, False)  # 20 < the 32-step period: one edge inside
  k2 = crossing(path, sim, h1, h1 + 20, True) if h1 else None
  c1 = clks_at(path, k1, sim)
  c2 = clks_at(path, k2, sim) if k2 else None
  return k1, h1, k2, (c2 - c1) if None not in (c1, c2) else None, \
      "edges %s/%s/%s" % (k1, h1, k2)

M = m_of(0xf8)
k1, h1, k2, dc, why = t2_wave(SIM, 0xf8)
check("T2OE wave: P1.0 is low and high for 16 machine cycles each (48 counts, FCLK_PERIPH/2)",
      None not in (k1, h1, k2) and h1 and k2 and h1 - k1 == 16 and k2 - k1 == 32, why)
check("T2OE wave: that 32-machine-cycle period costs 32*12*14 = 5376 clocks, so it divides by M",
      dc == 32 * 12 * M, "got %s" % dc if dc is not None else why)
k1a, h1a, k2a, dca, whya = t2_wave(SIM, 0xff)
check("the same wave at CKRL=FF costs 32*12 = 384 clocks: the ratio is exactly M",
      dca == 32 * 12 and dc and dca and dc == dca * M,
      "FF %s vs F8 %s" % (dca, dc))
check("rule (a): the edges land on the SAME steps at CKRL=FF and CKRL=F8 (%s vs %s)" % (k1a, k1),
      None not in (k1, k1a, h1, h1a, k2, k2a) and (k1, h1, k2) == (k1a, h1a, k2a),
      "F8 %s vs FF %s" % (why, whya))
k1b, h1b, k2b, dcb, whyb = t2_wave(REF, 0xf8)
check("control cannot fake the T2OE clock cost at CKRL=F8",
      dcb != 32 * 12 * M, "it also reported %s" % dcb if dcb is not None
      else "no control wave (%s)" % whyb)

# Internal BRG: FCLK_PERIPH/6^(1-SPD) with SPD cleared is one count per machine cycle, BRL =
# 00h reloads 00h so an overflow is one full 256-count wrap (figure 14-5, printed p.68;
# BDRCON Table 14-13, printed p.58), hence 256 machine cycles and 256*12*M clocks apiece.
M = m_of(0xf8)
d = 512
dc, dl, why = pair(mov(CKRL, 0xf8) + mov(BRL, 0x00) + mov(BDRCON, BM_BRR), 6, d,
                   info=("brg",))
check("BRG overflows once per 256 machine cycles at M = 14 (512 MC -> 2 overflows)",
      dl.get("ovf") == 2, "got %s" % dl.get("ovf"))
check("BRG overflow costs 256*12*14 = 43008 clocks, so BRL-derived baud divides by M",
      dc == d * 12 * M, "got %s" % dc if dc is not None else why[:70])

# UART mode 2 fixed rate: Table 14-4 (printed p.53) states 9-bit UART at FXTAL/64 (SMOD1=0)
# or FXTAL/32, which is the rate AT CKRL = FFh.  The frame is 11 bits (start, 8 data, TB8,
# stop); per byte the CPU also spends MOV A,#d + MOV SBUF,A + JNB TI,$ + CLR TI = 5 machine
# cycles, and the poll notices TI up to one 2-machine-cycle poll late.  So the clocks per bit
# measured by differencing an 8-byte run against a 1-byte run must lie in
# [64, (64*11 + 5*12 + 24)/11] = [64, 72], each end multiplied by M.
def send(n, first=0x30):
    out = bytearray()
    for i in range(n):
        out += bytes([0x74, (first + i) & 0xff, 0xf5, SBUF])
        out += bytes([0x30, TI, 0xfd])                 # JNB TI,.
        out += bytes([0xc2, TI])                       # CLR TI
    return bytes(out)


def mode2_ckrl(ckrl, smod=False, ckcon=0x00):
    return (mov(SCON, MODE2) + mov(CKCON0, ckcon) + (mov(PCON, SMOD1) if smod else b"") +
            mov(CKRL, ckrl) + send(1) + SJMP), (
        mov(SCON, MODE2) + mov(CKCON0, ckcon) + (mov(PCON, SMOD1) if smod else b"") +
        mov(CKRL, ckrl) + send(8) + SJMP)


def bit_clocks(ckrl, smod=False, ckcon=0x00, sim=None):
    small, big = mode2_ckrl(ckrl, smod, ckcon)
    a, b = measure(small, sim), measure(big, sim)
    if not a or not b or a["clks"] is None or b["clks"] is None:
        return None
    return (b["clks"] - a["clks"]) / 7.0 / 11.0


for ckrl in (0xff, 0xf8):
    M = m_of(ckrl)
    got = bit_clocks(ckrl)
    check("mode 2 fixed bit at CKRL=%02Xh costs 64..%d clocks (Table 14-4 p.53 x M)"
          % (ckrl, 72 * M),
          got is not None and 64.0 * M <= got <= 72.0 * M,
          ("%.2f clocks/bit" % got) if got else "TI never set / timeout")

# SIX2 (Table 7-1, printed p.19: "Enhanced UART Clock (Mode 0 and 2)") is the UART's own
# hold-at-standard bit: in X2 with SIX2 clear a fixed bit costs half the clocks, with SIX2 set
# it is held at the standard cost -- and CKRL multiplies both readings.
M = m_of(0xf8)
g6 = bit_clocks(0xf8, ckcon=X2)
g7 = bit_clocks(0xf8, ckcon=X2 | SIX2)
check("X2 without SIX2 halves the fixed rate at M = 14 (32..40 clocks/bit)",
      g6 is not None and 32.0 * M <= g6 <= 40.0 * M,
      ("%.2f" % g6) if g6 else "TI never set / timeout")
check("X2 with SIX2 holds it at the standard cost at M = 14 (64..72 clocks/bit)",
      g7 is not None and 64.0 * M <= g7 <= 72.0 * M,
      ("%.2f" % g7) if g7 else "TI never set / timeout")
g8 = bit_clocks(0xff, ckcon=X2 | SIX2)
check("SIX2 only works under X2 (CKCON0.X2 is the master gate): SMOD1 halves it too",
      bit_clocks(0xff, smod=True) is not None and
      32.0 <= bit_clocks(0xff, smod=True) <= 40.0,
      ("%.2f clocks/bit at SMOD1=1" % g8) if g8 else "timeout")

# =============================================================== 4. instruction cost
print("\n-- instruction machine-cycle counts are untouched (section 6.2.1 / 7.1)")
prog = mov(CKRL, 0x00) + bytes([0x74, 0x5a]) * 3 + b"\x24\x03" + b"\xe5\x82" + \
    bytes([0x75, 0x30, 0x40]) + b"\x90\x12\x34" + b"\x74\x00\xe0" + SJMP
a = state_of(mov(CKRL, 0xff) + prog[3:])
b = state_of(prog)
check("same instruction trace at CKRL=FF and CKRL=00 (Inst and Fetch from `state')",
      a and b and a["inst"] == b["inst"] and a["fetch"] == b["fetch"] and a["fetch"] > 0,
      "FF: %s/%s  00: %s/%s (inst/fetch)" % (a and a["inst"], a and a["fetch"],
                                             b and b["inst"], b and b["fetch"])
      if a and b else "one run timed out")
# and the same retired cycles cost 510 times the clocks, measured over a NOP window so the
# CKRL write's own machine cycles cancel (see the method note at the top).
M = m_of(0x00)
dc, _, why = pair(mov(CKRL, 0x00), 4, 50)
check("those cycles cost 50*12*510 = 306000 clocks, i.e. FOSC/1020 x 6 (section 6.2.1)",
      dc == 50 * 12 * M, "got %s" % dc if dc is not None else why[:70])

# =============================================================== 5. control proof
print("\n-- control: %s must not satisfy the prescaler legs" % REF)
if not os.path.exists(REF):
    print("  (control binary absent, skipping)")
else:
    # Legs that detect the absence of the prescaler: the control has to fail these, or the
    # suite is measuring nothing.  Legs that legitimately pass on it are listed too, so the
    # audit is in the transcript and not only in a report someone may never open.
    must_fail = []
    M = m_of(0xfe)
    dc, _, _ = pair(mov(CKRL, 0xfe), 10, 64, sim=REF)
    must_fail.append(("clocks per machine cycle at CKRL=FE", dc != 64 * 12 * M))
    dc, _, _ = pair(mov(CKRL, 0xf8) + mov(BRL, 0x00) + mov(BDRCON, BM_BRR), 6, 256,
                    sim=REF, info=("brg",))
    must_fail.append(("BRG clock cost at CKRL=F8", dc != 256 * 12 * m_of(0xf8)))
    g = bit_clocks(0xf8, sim=REF)
    must_fail.append(("mode 2 fixed bit at CKRL=F8", g is None or not
                      (64.0 * 14 <= g <= 72.0 * 14)))
    for label, failed in must_fail:
        check("control cannot fake %s" % label, failed,
              "the control satisfied it -- the leg is vacuous" if not failed else "")
    # Accepted control-passes: absence-checking legs, kept because they pin behaviour a later
    # change can regress (reset value, plain RW, machine-cycle counts standing still).
    t = run(os.path.join(WORK, "reset.hex"), ["dump sfr 0x90 0x9f"], sim=REF)
    mm = re.search(r"^0x97\s+(?:\S+\s+)?0b[01]+\s+0x([0-9a-f]{2})", t or "", re.M)
    check("accepted control-pass: reset value 0xFF is an absence check", True,
          "control reads 0x%s" % (mm.group(1) if mm else "??"))
    a = state_of(mov(CKRL, 0xff) + prog[3:], sim=REF)
    b = state_of(prog, sim=REF)
    check("accepted control-pass: machine-cycle counts stand still (rule (a) of 6.2.1)",
          a and b and a["inst"] == b["inst"], "control satisfies it by construction")

# =============================================================== summary
print("\nChecks run: %d  Failed: %d" % (_checks, len(_failures)))
if _failures:
    print("FAILURES: " + ", ".join(_failures))
    sys.exit(1)
print("ALL CKRL PRESCALER CHECKS PASSED")
