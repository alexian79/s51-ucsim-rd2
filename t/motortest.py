#!/usr/bin/env python3
"""
motortest.py - closed-loop rotary plant test for the AT89C51RD2 ucsim fork.

Firmware running INSIDE the simulator drives the plant (PCA 8-bit PWM on
CEX0/P1.3), counts the tach wheel on a real RD2 timer input (T1 in counter mode
on P3.5) and derives speed over a T0 16-bit timer window; the plant's own
print_info report is then compared against what the firmware measured.  The
plant does not know the firmware exists, so agreement between the two is
evidence rather than a tautology, and disagreement means one of them is wrong.

Legs (MOTOR-CONTRACT.md):
  A  registration, cfg defaults, reset state, the WARN contradiction; GUARD:
     the 251 family must not have gained the element.
  B  open-loop spin: the 8051's own T1 count == model pulses (within 1) and the
     firmware's cycles/rev == the model's (+-2 %).
  C  stiction holds against a fixed-direction load with zero drive (held=yes,
     zero edges); above breakaway the shaft runs backwards and the firmware's
     quadrature decode on P3.5/P3.4 agrees with the model's direction.
  D  gear + backlash: 4:1 -> output = motor/4; after a reversal the output
     stays frozen until the configured slack is taken up, then follows.
  E  duty sweep 25/50/75 %: speed monotone in duty, each firmware-derived speed
     within +-2 % of the model's, and zero duty a hard off.

Time base: machine cycles only.  The console `step N` command is the only thing
that advances time (N cycles; the transcript prints 12*N oscillator ticks), so
every console write lands between cycles and nothing drifts while the harness is
thinking.  `go` is asynchronous and is never used, and breakpoints re-hit in this
console, so the firmware waits on an arm flag in IRAM instead.

Register map note: this fork follows regs51.h, which puts the timers at the
STC-style addresses (TMOD 0x89, TL0 0x8a, TL1 0x8b, TH0 0x8c, TH1 0x8d, TCON
0x88 with TR0 0x8c / TR1 0x8e / TF1 0x8f as bit addresses) -- NOT the positions
doc4235 prints.  `dump sfr 0x88 0x8f` confirms it.

Firmware is hand-assembled below (kbdtest style, no SDCC), loaded through an
Intel-Hex file because the console truncates a long `flash code 0x0 <hex>` line.

Run:  python3 t/motortest.py            (repo root; t/floor.sh runs it too)
Env:  S51_SIM        simulator binary (default below)
      MOTORT_DEBUG=1 echo each transcript
Exit: 0 all green, 1 otherwise.  Floor-safe: the last line is "Checks run: <n>"
      and FAIL is printed only for a real failure.
"""

import os
import re
import subprocess
import sys

SIM = (os.environ.get("S51_SIM") or
       os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/"
                          "s51.src/ucsim_51"))
DEVICE = "89C51RD2"
WORKDIR = "/tmp/motortest-%d" % os.getpid()   # per-process: concurrent runs
                                              # must not clobber each other
DEBUG = os.environ.get("MOTORT_DEBUG")

# ---------------------------------------------------------------- SFR map ---
# sims/s51.src/regs51.h of this fork (uc51.cc:168 names 0x89 TMOD).
TMOD, TCON = 0x89, 0x88
TL0, TL1, TH0, TH1 = 0x8A, 0x8B, 0x8C, 0x8D
P1, P3 = 0x90, 0xB0
CCON, CMOD, CCAPM0, CL, CCAP0L, CCAP0H = 0xD8, 0xD9, 0xDA, 0xE9, 0xEA, 0xFA
TR0_B, TR1_B, TF1_B = 0x8C, 0x8E, 0x8F         # TCON.4/.6/.7 bit addresses
TF0_B = 0x8D                                    # TCON.5, the T0 window clock flag
CR_B = 0xDE                                     # CCON.6 bit address (inert, see D16)
TACH_A_BIT, TACH_B_BIT = 0xB5, 0xB4             # P3.5 / P3.4 bit addresses: the
                                                # plant's default tach pins, and
                                                # the ONLY way firmware can see
                                                # them (see quad_decode)
TMOD_T1CNT_T0TMR = 0x51                         # bmC_T1|bmM01 | bmM00
PWM_PERIOD = 256                                # PCA 8-bit PWM period in CL
                                                # counts == machine cycles
PWM_WINDOW = 4096                               # the plant's default duty
                                                # averaging window, in cycles


def duty_ok(got, want, window=PWM_WINDOW):
    """Duty within one unfinished PWM period of the setting.

    The plant meters the drive over the last `pwm_window` cycles, and the run is
    sampled on the cycle the measurement window closes -- never on a PWM
    boundary -- so the average can be missing part of one period at the tail.
    One period out of the window is the strict bound on that error: 256 / 4096 =
    6.25 points, which still separates 25 / 50 / 75 by 12.5 points on either
    side.  The old `== 75.00' only passed when an overshooting `step' happened
    to land the sample on a period boundary.

    Widening pwm_window to shrink this bound is NOT the answer: the meter is the
    plant's drive input, so a longer window lags the torque and every speed
    comparison in legs B and E drifts (measured 5.4 % at 65536, 0.5 % at 4096).
    """
    return got is not None and abs(got - want) <= 100.0 * PWM_PERIOD / window + 1e-9

# ------------------------------------------------- firmware data memory ----
BUD_LO, BUD_HI = 0x07, 0x03                     # firmware deadline (2 bytes)
R_OVS = 0x05                                    # overflow budget (R5)

O_PL, O_PH = 0x20, 0x21                         # T1 count at the window end
O_TL0, O_TH0 = 0x22, 0x23                       # window clock snapshot
O_OVS = 0x24                                    # window clock overflows
O_END = 0x25                                    # 0 = budget, 2 = deadline
O_FWD, O_FWDH = 0x26, 0x27                      # quadrature counters
O_REV, O_REVH = 0x28, 0x29
O_SKP, O_SKPH = 0x2A, 0x2B
O_T1OV = 0x2F                                   # T1 counter wraps (saturation)
O_S, O_PREV, O_D = 0x2C, 0x2D, 0x2E             # quadrature scratch
O_POLL, O_POLLH = 0x0A, 0x0B                    # poll iterations (bank 1 R2/R3,
                                                # never addressed as registers)

ARM_ADDR = 0x30                                 # `set mem iram 0x30 1` opens it

CFG_NAME = "motor_20_cfg"
CFG = {"on": 0, "pwm_port": 1, "pwm_bit": 2, "pwm_invert": 3, "pwm_window": 4,
       "slots": 5, "tach_a_port": 6, "tach_a_bit": 7, "tach_b_port": 8,
       "tach_b_bit": 9, "torque_stall": 10, "wmax_q": 11, "inertia_motor": 12,
       "inertia_load": 13, "gear_x1000": 14, "backlash_x1000": 15,
       "coulomb": 16, "static_friction": 17, "viscous_x1000": 18,
       "load_torque": 19, "load_mode": 20, "period_min": 21}
CFG_DEFAULT = {"motor20_on": 1, "motor20_pwm_port": 1, "motor20_pwm_bit": 3,
               "motor20_pwm_invert": 0, "motor20_pwm_window": 4096,
               "motor20_slots": 60, "motor20_tach_a_port": 3,
               "motor20_tach_a_bit": 5, "motor20_tach_b_port": 3,
               "motor20_tach_b_bit": 4, "motor20_torque_stall": 1000,
               "motor20_wmax_q": 10, "motor20_inertia_motor": 1000,
               "motor20_inertia_load": 0, "motor20_gear_x1000": 1000,
               "motor20_backlash_x1000": 0, "motor20_coulomb": 40,
               "motor20_static_friction": 60, "motor20_viscous_x1000": 100,
               "motor20_load_torque": 0, "motor20_load_mode": 0,
               "motor20_period_min": 4}
SLOTS = 60                                      # contract default, channel A

LABELS = {}                                     # labels of the last build():
                                                # `stop', plus `mark' when the
                                                # image carries a window
_checks = 0
_failures = []


def check(name, ok, detail=""):
    """One floor-visible assertion, phrased like t/kbdtest.py."""
    global _checks
    _checks += 1
    line = "  %-44s %s" % (name, "PASS" if ok else "FAIL")
    if detail:
        line += "  " + detail
    print(line)
    if not ok:
        _failures.append(name)
    return ok


# ------------------------------------------------------ 8051 assembler -----
class As:
    """Enough 8051 to write a measurement loop; addresses are code offsets."""

    def __init__(self):
        self.items = []
        self.errors = []

    def op(self, *b):
        self.items.append(("b", list(b)))
        return self

    def label(self, n):
        self.items.append(("lbl", n))
        return self

    def rel(self, n):
        self.items.append(("rel", n))
        return self

    def ljmp(self, n):                          # LJMP abs16: 0x02 + label addr
        self.items.append(("ljmp", n))
        return self

    def lcall(self, n):                         # LCALL abs16: 0x12 + label addr
        self.items.append(("lcall", n))
        return self

    def ret(self):                              # RET, 1 byte (glob.cc:74)
        return self.op(0x22)

    def mov_d(self, a, v):                       # MOV direct,#data
        return self.op(0x75, a, v)

    def mov_r(self, n, v):                       # MOV Rn,#data
        return self.op(0x78 | n, v)

    def mov_a_d(self, a):
        return self.op(0xE5, a)

    def mov_d_a(self, a):
        return self.op(0xF5, a)

    def mov_a_r(self, n):
        return self.op(0xE8 | n)

    def mov_r_a(self, n):
        return self.op(0xF8 | n)

    def clr_a(self):
        return self.op(0xE4)

    def xrl_a_r(self, n):
        return self.op(0x68 | n)

    def anl_a(self, v):
        return self.op(0x54, v)

    def subb_a_d(self, a):                       # SUBB A,direct (CLR C first)
        return self.op(0x95, a)

    def inc_d(self, a):
        return self.op(0x05, a)

    def clr_c(self):
        return self.op(0xC3)

    def swap(self):
        return self.op(0xC4)                    # 0xC0 is PUSH direct -- see A0

    def rr_a(self):
        return self.op(0x03)

    def clr_bit(self, a):
        return self.op(0xC2, a)

    def setb_bit(self, a):
        return self.op(0xD2, a)

    def mov_c_bit(self, a):                      # MOV C,bit: samples the PIN,
        return self.op(0xA2, a)                  # a byte read reads the latch

    def rlc_a(self):
        return self.op(0x33)

    def djnz_d(self, a, lbl):
        return self.op(0xD5, a).rel(lbl)

    def djnz_r(self, n, lbl):
        return self.op(0xD8 | n).rel(lbl)

    def jnz_a(self, lbl):
        return self.op(0x70).rel(lbl)            # JNZ rel: there is NO JNZ
                                                 # direct in the 8051 (and 0x25
                                                 # is ADD, 0xB5 CJNE A,direct)

    def cjne_a_im(self, v, lbl):
        return self.op(0xB4, v).rel(lbl)

    def jb_bit(self, a, lbl):
        return self.op(0x20, a).rel(lbl)

    def sjmp(self, lbl):
        return self.op(0x80).rel(lbl)

    def jnb_bit(self, a, lbl):
        return self.op(0x30, a).rel(lbl)

    def asm(self):
        """Two passes: label addresses, then bytes.

        A label is ZERO WIDTH -- it is a position, not a byte, so it must not
        advance the address map.  A relative branch is resolved against the
        address of the byte following the rel byte, which is the reason the
        `DJNZ R7,$' of t/kbdtest.py:175 encodes as 0x7d ff d7 fe: with the rel
        byte at pc the next instruction is pc+1 and the target is the opcode at
        pc-1, so rel = (pc-1) - (pc+1) = -2 = 0xfe.

        Nothing here raises.  An out-of-range or undefined branch target is
        recorded in self.errors and build() turns it into a failed check: a leg
        must never take the whole suite down with a traceback, because
        floor.sh reads the final `Checks run:' line."""
        addr, labels = 0, {}
        for k, v in self.items:
            if k == "lbl":
                if v in labels:
                    # a redefined label used to win silently: window_body's poll
                    # loop and the quadrature subroutine both ended theirs with
                    # `done'/`out', so the poll's `jz done' landed inside the
                    # subroutine and ran off through its RET on an empty stack
                    self.errors.append("duplicate label: %s" % v)
                labels[v] = addr
            elif k == "rel":
                addr += 1
            elif k in ("ljmp", "lcall"):
                addr += 3
            else:
                addr += len(v)
        self.labels = labels                    # build() reads `stop' from here
        out, pc = bytearray(), 0
        self.errors = []
        for k, v in self.items:
            if k == "lbl":
                continue
            if k in ("rel", "ljmp", "lcall"):
                to = labels.get(v)
                if to is None:
                    self.errors.append("undefined label: %s" % v)
                    out.extend(b"\x00" * (1 if k == "rel" else 3))
                    pc += 1 if k == "rel" else 3
                    continue
                if k in ("ljmp", "lcall"):
                    out += bytes([0x02 if k == "ljmp" else 0x12,
                                  (to >> 8) & 0xFF, to & 0xFF])
                    pc += 3
                    continue
                rel = to - (pc + 1)
                if not -128 <= rel <= 127:
                    self.errors.append("branch out of range: %s -> %d"
                                       % (v, rel))
                    rel = 0
                out.append(rel & 0xFF)
                pc += 1
                continue
            out.extend(v)
            pc += len(v)
        return bytes(out)


def jmp_self(p):
    p.op(0x80, 0xFE)                             # SJMP $


def settle(p, outer=48):
    """Busy wait of about outer * 514 machine cycles."""
    p.label("st_outer")
    p.mov_r(4, outer)
    p.label("st_mid")
    p.mov_r(5, 0xFF)
    p.label("st_in")
    p.djnz_r(5, "st_in")
    p.djnz_r(4, "st_mid")


def arm_gate(p):
    """Spin until the console writes `set mem iram 0x30 1`, then consume it.
    Breakpoints re-hit in this console, so the wait is firmware-side."""
    p.label("gate")
    p.mov_a_d(ARM_ADDR)
    p.cjne_a_im(1, "gate")                       # rel = -5
    p.mov_d(ARM_ADDR, 0)


def quad_decode(p):
    """Classify every P3.5(A)/P3.4(B) transition into the fwd/rev/skip counters.

    s = (A<<1)|B is a 2-bit Gray code; its binary value q = s ^ (s>>1) advances
    by +1 per tach pulse one way and -1 the other, so d = (q - prev_q) & 3 is 3
    forward, 1 reverse, 2 when both channels moved between two polls (a missed
    sample) and 0 for no change.

    They are sampled one bit at a time with JNB.  Not with a byte read of P3,
    not with MOV C,bit, and not with JB: measured inside the very routine the
    poll calls (probeG), JNB P3.5 found the pin low in 39 % of 27420 samples
    while the JB-based sampler ahead of it saw the wheel as still.  On an 8051
    a byte read of a port returns the output LATCH and only a bit-addressed
    read samples the pin -- which is why read-modify-write on a port is taught
    as destructive -- and this core follows that literally: inst_e5 (mov.cc:484)
    does get_direct()->read(), which hands back the raw cell, while the
    jump-on-clear bit test goes through the port's bit-addressed cells and so
    reaches cl_port::read(), the one place that ANDs the cell with port_pins
    (port.cc:136-147).  Side by side over one wheel (probeD): JNB read P3.5 low
    16297 times in 32617 samples, MOV A,P3 only 23 times.  JNB is also the
    honest 8051 way to watch an input pin, so the leg is not leaning on a
    simulator quirk.  The latch effect is NOT what explains the count deficit
    in C2c: this sampler sits in the hot loop and the leg still reports 21
    transitions for the 646 that T1 counted, so the two are separate things
    and the deficit is still open (SIMULATION-PLAN.md, open items).

    Called with LCALL and ending in RET, because two places need it: the poll
    loop, and the window's setup pass that seeds prev_q.  Nothing here writes
    P3 -- a latch write would clock T1 (see window_body).
    """
    p.clr_a()                                    # s = 00
    p.jnb_bit(TACH_A_BIT, "q_a0")                # JNB, not JB: see the note
    p.jnb_bit(TACH_B_BIT, "q_two")               # above -- only the jump-on-
    p.op(0x74, 3)                                # clear path reaches the pins
    p.sjmp("q_s")                                # 11
    p.label("q_two")
    p.op(0x74, 2)                                # 10
    p.sjmp("q_s")
    p.label("q_a0")
    p.jnb_bit(TACH_B_BIT, "q_s")                 # 00: A is already clear
    p.op(0x74, 1)                                # 01
    p.label("q_s")
    p.mov_d_a(O_S)
    p.anl_a(0x02)                                # A, still in place
    p.rr_a()                                     # m = A >> 1
    p.mov_r_a(0)                                 # R0 = m
    p.mov_a_d(O_S)
    p.xrl_a_r(0)                                 # A = q = s ^ m
    p.mov_r_a(1)                                 # R1 = q
    p.clr_c()
    p.subb_a_d(O_PREV)                           # A = q - prev_q
    p.anl_a(0x03)                                # d
    p.mov_d_a(O_D)
    p.mov_a_r(1)
    p.mov_d_a(O_PREV)                            # prev_q = q
    p.mov_a_d(O_D)
    p.cjne_a_im(0, "q_move")
    p.sjmp("q_next")
    p.label("q_move")
    p.cjne_a_im(3, "q_rev")
    p.inc_d(O_FWD)
    p.mov_a_d(O_FWD)                             # INC direct sets no flags
    p.jnz_a("q_next")
    p.sjmp("q_next")
    p.label("q_rev")
    p.cjne_a_im(1, "q_skip")
    p.inc_d(O_REV)
    p.mov_a_d(O_REV)
    p.jnz_a("q_next")
    p.sjmp("q_next")
    p.label("q_skip")
    p.inc_d(O_SKP)
    p.mov_a_d(O_SKP)
    p.jnz_a("q_next")
    p.inc_d(O_SKPH)
    p.label("q_next")
    p.ret()


def window_body(p, n_over, quad=False):
    """T0 (mode 1 timer) is the window clock and T1 (mode 1 counter on T1/P3.5)
    counts the wheel; both are 16 bit and neither is reloaded mid-window, so the
    span is overflows * 65536 + residual machine cycles.

    The window closes on `n_over` REAL T0 overflows: the poll tests TF0
    (TCON.5, bit address 0x8d) and the handler clears it.  TF1 is the COUNTER's
    own overflow and is kept only as a saturation signal -- a wrapped T1 means
    the pulse count rolled over -- so it bumps O_T1OV and is never allowed to
    end the window.  `end' therefore says 0 = the overflow budget of real T0
    wraps, 2 = the iteration deadline; a leg that sees 2 did not measure what it
    thinks it measured.

    Zero pulses is a legitimate answer (a shaft held by stiction), so the loop
    also carries a two-byte iteration deadline, the kbdtest idiom generalized:
    no leg can hang even on a simulator with no tach source at all.  The
    deadline is per overflow (0xFFFF passes over a ~25-cycle body, far longer
    than one 65536-cycle span) so a working window always ends on its own clock.

    The quadrature decoder is OUT OF LINE at the tail, reached by LJMP and
    returning by LJMP: its body alone is longer than the +-127 reach of a
    relative branch, so with it inlined every `djnz ...,"poll"' blew the range.
    With `quad' the body emits its own terminating SJMP $ and returns True so
    build() does not append a second one.

    P3 is written ONCE, here, before TR1 is set.  Never afterwards:
    cl_port::write() raises EV_PORT_CHANGED for any latch change
    (port.cc:194-195) and cl_timer0::happen() counts (new_pins & new_value)
    (timer0.cc:393-408), so an ANL/ORL/MOV to P3 while T1 runs clocks the
    counter with the firmware's own store -- the plant would then be measured
    against a number the test itself generated.  Channel B is therefore read
    only, with MOV A,P3 (see quad_decode).
    """
    p.mov_d(P3, 0xFF)                           # latch, once, before TR1 (above)
    p.clr_a()
    for a in (O_PL, O_PH, O_TL0, O_TH0, O_OVS, O_END, O_PREV, O_T1OV,
              O_FWD, O_FWDH, O_REV, O_REVH, O_SKP, O_SKPH, O_POLL, O_POLLH):
        p.mov_d_a(a)
    if quad:
        # Seed prev_q from the level the wheel actually sits at.  With prev_q
        # left at 0 the first pass always sees a two-channel jump and charges
        # the skip counter for the firmware's own power-up state, not for an
        # edge -- and a "2" is reserved as a real missed-sample indicator.
        p.lcall("qdec")                         # seed prev_q from live pins
        p.clr_a()                               # the seed pass left q in A
        for a in (O_FWD, O_FWDH, O_REV, O_REVH, O_SKP, O_SKPH):
            p.mov_d_a(a)                        # drop the seed's counters
    p.mov_d(TMOD, TMOD_T1CNT_T0TMR)
    p.mov_d(TH0, 0)
    p.mov_d(TL0, 0)
    p.mov_d(TH1, 0)
    p.mov_d(TL1, 0)
    p.mov_d(BUD_LO, 0xFF)
    p.mov_d(BUD_HI, 0x20)                       # 0x20ff poll passes per span:
                                                # ~6x the minimum for 65536
                                                # cycles, yet bounded so a
                                                # binary that never sets TF0
                                                # still reaches `stop'
    p.mov_d(R_OVS, n_over)
    p.label("mark")                             # window boundary: the plant may
                                                # already have turned through
                                                # the settle, so a leg that
                                                # wants the pulses emitted
                                                # INSIDE the window breaks here
                                                # (see leg C) and subtracts
    p.setb_bit(TR0_B)
    p.setb_bit(TR1_B)
    p.sjmp("poll")

    p.label("ov")                               # T0 wrapped: next window
    p.clr_bit(TF0_B)
    p.mov_a_d(TL0).mov_d_a(O_TL0)
    p.mov_a_d(TH0).mov_d_a(O_TH0)
    p.mov_a_d(TL1).mov_d_a(O_PL)
    p.mov_a_d(TH1).mov_d_a(O_PH)
    p.inc_d(O_OVS)
    p.clr_a()
    p.mov_d_a(TL0)                              # restart the window clock
    p.mov_d_a(TH0)
    p.mov_d(BUD_LO, 0xFF)
    p.mov_d(BUD_HI, 0x20)
    p.djnz_r(R_OVS, "poll")                     # overflow budget
    p.mov_d(O_END, 0)                           # closed on its own clock
    p.sjmp("fin")

    p.label("t1ov")                             # counter wrapped: saturation
    p.clr_bit(TF1_B)
    p.inc_d(O_T1OV)
    p.sjmp("poll")

    p.label("poll")
    p.jb_bit(TF0_B, "ov")                       # the window's own clock
    p.jb_bit(TF1_B, "t1ov")
    p.mov_a_d(TL0).mov_d_a(O_TL0)
    p.mov_a_d(TH0).mov_d_a(O_TH0)
    p.mov_a_d(TL1).mov_d_a(O_PL)
    p.mov_a_d(TH1).mov_d_a(O_PH)
    p.inc_d(O_POLL)                              # how often the poll really ran
    p.mov_a_d(O_POLL)
    p.jnz_a("poll_no_carry")
    p.inc_d(O_POLLH)
    p.label("poll_no_carry")
    if quad:
        p.lcall("qdec")                         # out of line, see the docstring
    p.djnz_d(BUD_LO, "poll")
    p.mov_d(BUD_LO, 0xFF)
    p.djnz_d(BUD_HI, "poll")                    # two-byte deadline, inner then
                                                # outer: 0x20ff passes total
    p.mov_d(O_END, 2)                           # deadline: nothing to wait for
    p.label("fin")
    p.clr_bit(TR1_B)
    p.clr_bit(TR0_B)
    if not quad:
        return False
    p.sjmp("stop")
    p.label("qdec")
    quad_decode(p)
    p.label("stop")
    jmp_self(p)
    return True


def build(setup=None, gate=None, window=None, settle_outer=48):
    """Park the plant, settle, wait for the arm gate, run the gate actions,
    measure, then park at a label the leg can break on.

    P1 resets to 0xFF and P1.3 is the drive line, so the setup either clears the
    latch or sets it deliberately: an untouched latch is full throttle.

    Returns (image, stop).  `stop' is the address of the final SJMP $, the first
    instruction the firmware reaches when its work is done.  A leg that compares
    its own counters against the plant prints `break stop' + `go', which halts
    on the cycle the window closes; stepping a fixed count past the close cannot
    work, because the plant keeps integrating while the CPU spins and the two
    counters then measure different spans (that is what produced 530 vs 831).

    A branch that does not fit is a FAILED CHECK, never an exception: floor.sh
    reads the suite's last line, so a traceback in one leg must not cost the
    other four their evidence.
    """
    p = As()
    (setup or (lambda q: None))(p)
    settle(p, settle_outer)
    arm_gate(p)
    (gate or (lambda q: None))(p)
    terminated = bool(window) and window_body(p, **window)
    if not terminated:
        p.label("stop")
        jmp_self(p)
    code = p.asm()
    if p.errors:                                 # one line, not one per branch
        check("image assembles (%d bad branch/es: %s)"
              % (len(p.errors), p.errors[0]), False)
    pad = (-len(code)) % 16
    LABELS.update(p.labels)                      # `mark' too; see leg C
    return code + bytes([0x00] * pad), p.labels.get("stop")


def park(p):
    p.mov_d(P1, 0xF7)                            # drive line low: duty 0


def drive(p):
    p.mov_d(P1, 0x08)                            # P1.3 high, no PCA: duty 100 %


def cut(p):
    p.mov_d(P1, 0xF7)                            # dynamic braking through 0


def pca_setup(p, ccap):
    """PCA module 0 in 8-bit PWM on CEX0/P1.3, counter still stopped.  The PCA
    drives the P1 latch itself (pca.cc:297-307), which is what the plant
    measures; the line is low while CL < CCAP0L, so duty = (256-CCAP0L)/256."""
    park(p)
    p.mov_d(CCAP0H, ccap)
    p.mov_d(CCAP0L, ccap)
    p.mov_d(CCAPM0, 0x42)                        # bmECOM0 | bmPWM0
    p.mov_d(CMOD, 0x00)                          # CPS = 0


def pca_gate(p):
    """Start the PCA counter.  This model ignores `SETB 0xDE': pca.cc:80-81
    registers the CR bit cell at 0xda instead of 0xde, so the bit write lands on
    CCON.2/CCAPM0 and CR stays 0 (SIMULATION-PLAN.md defect D16); a real
    AT89C51RD2 accepts either spelling, so the workaround belongs to the
    simulator, not to the firmware.  The byte write works: CL runs."""
    p.mov_d(CCON, 0x40)


# ------------------------------------------------------------- simulator ---
def _hex(code):
    """Intel-Hex of a code image starting at 0 (kbdtest.py:132-143)."""
    lines = []
    for off in range(0, len(code), 16):
        chunk = code[off:off + 16]
        b = bytes([len(chunk), (off >> 8) & 0xFF, off & 0xFF, 0]) + chunk
        lines.append(":%02X%04X%02X%s%02X" % (len(chunk), off & 0xFFFF, 0,
                                              chunk.hex().upper(),
                                              (-sum(b)) & 0xFF))
    lines.append(":00000001FF")
    text = "\n".join(lines) + "\n"
    for ln in text.splitlines():
        assert sum(bytes.fromhex(ln[1:])) & 0xFF == 0, "bad checksum %r" % ln
    return text.encode()


def run(code, cmds, device=DEVICE, name="m", timeout=120):
    """Load `code` at 0, reset, feed `cmds`, return the transcript.

    The simulator is spawned by argv, never through a shell, so a timeout kills
    the child itself instead of orphaning it (t/simproc.py:run_pipe exists for
    pipeline-shaped spawns).  A wedged console -- the `go' of a leg whose
    breakpoint was never reached -- returns a marker rather than raising, in the
    kbdtest.py:152 idiom, so one wedged leg cannot cost the suite its summary.
    """
    os.makedirs(WORKDIR, exist_ok=True)
    img = os.path.join(WORKDIR, "%s_%x_%x.hex" %
                       (device, len(code), sum(code) & 0xFFFF))
    with open(img, "wb") as f:
        f.write(_hex(code))
    stdin = ('file "%s"\nreset\n' % img) + "\n".join(cmds) + "\n"
    try:
        p = subprocess.run([SIM, "-t", device, "-c", "-"], input=stdin,
                           capture_output=True, text=True, cwd=WORKDIR,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "<<TIMEOUT: console wedged (unreached breakpoint?)>>"
    out = p.stdout + "\n" + p.stderr
    if DEBUG:
        print("---- %s %s" % (device, cmds))
        print(out)
    return out


ARM = "set mem iram 0x%02x 1" % ARM_ADDR


def at_stop(stop):
    """Console commands that run the firmware to its park point and halt there.

    `go' is the only way to stop ON the cycle the measurement window closes:
    every `step N' overshoots by however much of the budget was left, and the
    plant keeps integrating during that overshoot, so the model's cumulative
    counters would describe a longer span than the firmware's window.  One
    `break' + one `go' per run, and never a `step' after it: a second `go' with
    no reachable breakpoint wedges the console (measured), which run() reports
    as a wedge rather than a hang.
    """
    return ["break 0x%04x" % stop, "go"]


def set_cfg(**kw):
    """Console commands moving cfg slots by name, e.g. load_torque=900."""
    return ["set mem %s 0x%02x %d" % (CFG_NAME, CFG[k], v) for k, v in
            kw.items()]


# ------------------------------------------------------------- reporting ---
FIELD = (r"([A-Za-z_][A-Za-z_0-9]*)=\s*("
         r"[\d.]+%|[\d.]+:[\d.]+|[\d.]+|yes|no|fwd|rev|stop|drag|gravity|n/a|\S+)")


def blocks(out):
    """Every print_info report in the transcript, as (fields, raw), in order."""
    res, raw = [], []
    for ln in out.splitlines():
        m = re.match(r"^motor\[(\d+)\] (.*)$", ln)
        if not m:
            continue
        txt = m.group(2)
        if txt.startswith("duty=") and raw:
            res.append((dict(re.findall(FIELD, "\n".join(raw))),
                        "\n".join(raw)))
            raw = []
        raw.append(txt)
    if raw:
        res.append((dict(re.findall(FIELD, "\n".join(raw))), "\n".join(raw)))
    return res


def last(out):
    b = blocks(out)
    return b[-1] if b else ({}, "")


def cycles_per_rev(raw):
    m = re.search(r"\(\s*(-?[\d.]+)\s+cycles/rev", raw)
    return float(m.group(1)) if m else None


def cfg_dump(out):
    """`dump motor_20_cfg 0x00 0x15` -> {motor20_name: value}.  Slot 0 (`on`) is
    published by cl_hw::init and prints as a flag row, not a 0b/0x row."""
    res = {}
    for ln in out.splitlines():
        m = re.match(r"^\s*0x([0-9a-f]{2})\s+(\w+):\s+0b[01]+\s+0x[0-9a-f]+"
                     r"\s+'[^']*'\s+(-?\d+)\s*$", ln)
        if m:
            res[m.group(2)] = int(m.group(3))
            continue
        m = re.match(r"^\s*0x([0-9a-f]{2})\s+(\w+):\s+([01]{8})\s+\.+\s*$", ln)
        if m:
            res[m.group(2)] = int(m.group(3), 2)
    return res


def row(out, addr):
    """One IRAM byte from the `dump iram` row that contains it."""
    for m in re.finditer(r"^0x([0-9a-f]{2,4})\s+((?:[0-9a-f]{2} ){1,16})", out,
                         re.M):
        base = int(m.group(1), 16)
        vals = [int(v, 16) for v in m.group(2).split()]
        if base <= addr < base + len(vals):
            return vals[addr - base]
    return None


def fw(out):
    """The firmware's own measurement block."""
    def b(a):
        return row(out, a) or 0
    return {"pulses": b(O_PL) | (b(O_PH) << 8),
            "TL0": b(O_TL0), "TH0": b(O_TH0), "overflows": b(O_OVS),
            "end": b(O_END), "t1ov": b(O_T1OV),
            "elapsed": b(O_OVS) * 65536 + (b(O_TH0) << 8 | b(O_TL0)),
            "fwd": b(O_FWD) | (b(O_FWDH) << 8),
            "rev": b(O_REV) | (b(O_REVH) << 8),
            "skip": b(O_SKP) | (b(O_SKPH) << 8),
            "polls": b(O_POLL) | (b(O_POLLH) << 8)}


def sfr(out, addr):
    return (sfr_all(out, addr) or [None])[0]


def sfr_all(out, addr):
    """Every dumped value of one SFR byte, in transcript order."""
    return [int(m.group(1), 16) for m in
            re.finditer(r"^0x%02x \w+:\s+0b[01]+\s+0x([0-9a-f]{2})" % addr, out,
                        re.M)]


def fnum(f, k, default=None):
    v = f.get(k)
    if v is None:
        return default
    try:
        return float(v.rstrip("%"))
    except ValueError:
        return default


def dev_has_motor(device):
    out = run(b"\x80\xfe", ["conf", "exit"], device=device, name="probe")
    m = re.search(r"Controller has (\d+) hardware element", out)
    return (re.search(r"\bon motor\[(\d+)\]", out) is not None,
            int(m.group(1)) if m else None, out)


def speed_of(f, m, count):
    """Cycles per revolution from the firmware's own window and counters."""
    if not count or not m["elapsed"]:
        return None
    revs = count / (fnum(f, "slots", SLOTS) or SLOTS)
    return m["elapsed"] / revs if revs else None


# ------------------------------------------------------------------ legs ---
def leg_a():
    print("--- leg A: registration, defaults, reset state")
    has, nel, _ = dev_has_motor(DEVICE)
    check("A1 conf lists the plant on %s" % DEVICE, has,
          "%s elements" % nel)
    if not has:
        return False

    # GUARD: an RD2 element only; the 251 family must not have inherited it.
    guard = None
    for alt in ("251", "SABRENET", "C521"):
        h2, n2, _o = dev_has_motor(alt)
        if n2 is None:
            continue                             # unknown device spelling
        guard = (alt, h2, n2)
        break
    if guard:
        check("A2 GUARD: -t %s has no motor element" % guard[0], not guard[1],
              "%s elements" % guard[2])
    else:
        check("A2 GUARD: -t 251 has no motor element", False,
              "no 251-family device spelling accepted")

    code, _stop = build()
    out = run(code, ["info hw motor", "exit"])
    f, raw = last(out)
    check("A3 `info hw motor` prints the report", raw.count("\n") >= 4,
          "%d line(s)" % (raw.count("\n") + 1))

    d = cfg_dump(run(code, ["dump %s 0x00 0x15" % CFG_NAME, "exit"],
                     name="cfg"))
    missing = sorted(set(CFG_DEFAULT) - set(d))
    check("A4 all 22 cfg slots published", not missing, ",".join(missing[:6]))
    wrong = {k: (d.get(k), v) for k, v in CFG_DEFAULT.items()
             if d.get(k) != v}
    check("A5 slot defaults match the contract", not wrong,
          " ".join("%s=%s!=%s" % (k[7:], v[0], v[1])
                   for k, v in list(wrong.items())[:5]))

    want = {"duty": "0.00%", "pwm": "P1.3", "tach": "P3.5/P3.4", "slots": "60",
            "dir": "stop", "pos": "0.0000", "out": "0.0000", "slack": "0.000",
            "gear": "1.000:1", "held": "yes", "pulses": "0", "emitted": "0",
            "clamped": "0", "J_eff": "1000", "T_stall": "1000", "wmax_q": "10",
            "Tc": "40", "load": "0", "mode": "drag", "window": "4096",
            "pin_a": "1", "pin_b": "1", "floor": "yes", "substeps": "8"}
    bad = {k: f.get(k) for k, v in want.items() if f.get(k) != v}
    check("A6 reset-state report matches the contract", not bad, str(bad))
    check("A7 no WARN while the tach latch bit is high", "WARN" not in raw,
          raw.splitlines()[-1][:56] if raw else "")

    code2, _s2 = build(setup=lambda p: (p.mov_d(P1, 0xF7), p.mov_d(P3, 0xDF)))
    raw2 = last(run(code2, ["step 5000", "info hw motor", "exit"],
                    name="warn"))[1]
    warn = [ln for ln in raw2.splitlines() if "WARN" in ln]
    check("A8 WARN reported when the tach latch bit is 0",
          bool(warn) and "P3.5" in warn[0],
          warn[0][:72] if warn else "no WARN line")
    code3, stop3 = build(setup=lambda p: (park(p), pca_setup(p, 64)),
                         gate=pca_gate, window={"n_over": 4}, settle_outer=24)
    out = run(code3, set_cfg(gear_x1000=4000) + [ARM] + at_stop(stop3) +
              ["info hw motor", "reset", "info hw motor", "exit"],
              name="reset")
    bl = blocks(out)
    check("A9 plant ran before the reset", len(bl) == 2 and
          fnum(bl[0][0], "pulses", 0) > 0,
          "pulses=%s" % (bl[0][0].get("pulses") if bl else "no report"))
    if len(bl) == 2:
        r = bl[1][0]
        check("A10 reset zeroes mechanics, keeps parameters",
              fnum(r, "pulses") == 0 and fnum(r, "emitted") == 0 and
              fnum(r, "pos") == 0 and fnum(r, "duty") == 0 and
              fnum(r, "clamped") == 0 and r.get("gear") == "4.000:1",
              "pulses=%s pos=%s duty=%s gear=%s" % (r.get("pulses"),
                                                    r.get("pos"),
                                                    r.get("duty"),
                                                    r.get("gear")))
    else:
        check("A10 reset zeroes mechanics, keeps parameters", False,
              "%d report(s)" % len(bl))

    # ---- A-CRBIT: pin the D16 pair, do not smooth it over ------------------
    # pca.cc:80-81 registers the CR bit cell at 0xda, so `SETB 0xDE' lands on
    # the byte the SFR map calls CCAPM4 (see `dump sfr 0xd8 0xdf') and CR stays
    # 0, while `MOV CCON,#0x40' starts the counter.  pca_gate() uses the byte
    # write; this leg keeps the asymmetry on the record, in the open, so nobody
    # reads the workaround as a firmware requirement.  A real AT89C51RD2 accepts
    # both spellings, so both halves here describe the simulator only.
    def cl_pair(gate, tag):
        c, _st = build(setup=lambda p: (park(p), pca_setup(p, 64)), gate=gate,
                       settle_outer=8)
        o = run(c, ["step 4000", ARM, "dump sfr 0xe8 0xef", "step 4000",
                    "dump sfr 0xe8 0xef", "info hw motor", "exit"],
                name="crbit_" + tag)
        cl = sfr_all(o, CL)
        return (cl[0], cl[-1], fnum(last(o)[0], "duty", -1.0)) \
            if len(cl) >= 2 else (None, None, -1.0)

    s0, s1, sduty = cl_pair(lambda p: p.setb_bit(CR_B), "setb")
    b0, b1, bduty = cl_pair(pca_gate, "byte")
    check("A-CRBIT CR bit write inert, CR byte write runs (D16)",
          s0 == s1 == 0 and sduty == 0.0 and b1 != b0 and bduty > 60.0,
          "SETB: CL %s->%s duty=%s | MOV CCON: CL %s->%s duty=%s -- once pca.cc "
          "registers CR at 0xde the SETB half stops holding; DELETE this leg "
          "then" % (s0, s1, sduty, b0, b1, bduty))
    return True


def leg_b():
    print("--- leg B: open-loop spin, the 8051's own counter vs the model")
    # 75 % duty (PCA holds the line low for 64 of every 256 counts) and a
    # 20-overflow window: 1_310_720 machine cycles.
    code, stop = build(setup=lambda p: (park(p), pca_setup(p, 64)),
                       gate=pca_gate, window={"n_over": 20}, settle_outer=32)
    out = run(code, [ARM] + at_stop(stop) +
              ["dump iram 0x20 0x2f", "dump sfr 0x88 0x95",
               "info hw motor", "exit"], name="speed")
    f, raw = last(out)
    m = fw(out)
    cpr_model = cycles_per_rev(raw)
    perm = re.search(r"\((\d+)/1000\)", raw)
    check("B0 PCA duty reaches the plant (75.00 %)",
          duty_ok(fnum(f, "duty"), 75.00, fnum(f, "window", PWM_WINDOW)) and
          perm and abs(int(perm.group(1)) - 750) <=
          1000 * PWM_PERIOD / fnum(f, "window", PWM_WINDOW) + 1,
          "duty=%s (%s/1000)" % (f.get("duty"), perm.group(1) if perm else "?"))
    check("B1 window closed on its own overflow budget",
          m["end"] == 0 and m["overflows"] == 20 and m["t1ov"] == 0,
          "end=%d ovs=%d t1ov=%d elapsed=%d" % (m["end"], m["overflows"],
                                                m["t1ov"], m["elapsed"]))
    check("B2 8051 T1 count == model pulses (within 1)",
          m["pulses"] > 0 and abs(m["pulses"] - fnum(f, "pulses", -9)) <= 1,
          "TL1:TH1=%d pulses=%s" % (m["pulses"], f.get("pulses")))
    check("B3 rising edges emitted == T1 count (within 1)",
          abs(m["pulses"] - fnum(f, "emitted", -9)) <= 1,
          "emitted=%s" % f.get("emitted"))
    cpr_fw = speed_of(f, m, m["pulses"])
    ok = bool(cpr_fw and cpr_model and
              abs(cpr_fw - cpr_model) / cpr_model <= 0.02)
    check("B4 firmware cycles/rev within 2 %% of the model%s" %
          ("" if not (cpr_fw and cpr_model)
           else " (%.0f vs %.1f)" % (cpr_fw, cpr_model)), ok,
          "fw=%s model=%s" % (("%.1f" % cpr_fw) if cpr_fw else "n/a",
                              cpr_model))
    check("B5 no edge clamping at this speed", fnum(f, "clamped") == 0,
          "clamped=%s" % f.get("clamped"))
    check("B6 turns forward, not held, floor off",
          f.get("dir") == "fwd" and f.get("held") == "no" and
          f.get("floor") == "no" and fnum(f, "pos", 0) > 0,
          "dir=%s held=%s floor=%s pos=%s" % (f.get("dir"), f.get("held"),
                                              f.get("floor"), f.get("pos")))
    check("B7 T1 count respects the period clamp",
          not m["elapsed"] or m["pulses"] <= m["elapsed"] / 4.0,
          "%d edges in %d cycles" % (m["pulses"], m["elapsed"]))
    check("B8 TCON shows TR0/TR1 cleared after the window",
          sfr(out, TCON) in (0x00, 0x20, 0x80, 0xA0),
          "TCON=%s" % hx(sfr(out, TCON)))
    return f, m


def hx(v):
    return "0x%02x" % v if v is not None else "None"


def leg_c():
    print("--- leg C: stiction holds, gravity above breakaway reverses")
    # C1: duty 0, fixed-direction load 30 below the 60 breakaway -> dead stop.
    code, stop = build(setup=park, window={"n_over": 4, "quad": True},
                       settle_outer=32)
    out = run(code, set_cfg(load_mode=1, load_torque=30) + [ARM] +
              at_stop(stop) + ["dump iram 0x20 0x2f", "info hw motor",
                               "exit"], name="hold")
    f, raw = last(out)
    m = fw(out)
    check("C1 held below breakaway: held=yes dir=stop",
          f.get("held") == "yes" and f.get("dir") == "stop" and
          fnum(f, "load") == 30 and f.get("mode") == "gravity",
          "held=%s dir=%s load=%s mode=%s" % (f.get("held"), f.get("dir"),
                                              f.get("load"), f.get("mode")))
    check("C1b zero edges from the model and from T1",
          fnum(f, "pulses") == 0 and fnum(f, "emitted") == 0 and
          m["pulses"] == 0 and m["rev"] == 0 and m["fwd"] == 0 and
          m["skip"] == 0,
          "model=%s TL1=%d fwd=%d rev=%d skip=%d" % (f.get("pulses"),
                                                     m["pulses"], m["fwd"],
                                                     m["rev"], m["skip"]))
    check("C1c window still closed on its clock, not on the deadline",
          m["end"] == 0 and m["overflows"] == 4 and m["elapsed"] > 4 * 65536,
          "end=%d ovs=%d elapsed=%d" % (m["end"], m["overflows"],
                                        m["elapsed"]))
    check("C1d pins stay released while held",
          f.get("floor") == "yes" and fnum(f, "pin_a") == 1 and
          fnum(f, "pin_b") == 1,
          "floor=%s pin_a=%s pin_b=%s" % (f.get("floor"), f.get("pin_a"),
                                          f.get("pin_b")))

    # C2: load 900 above breakaway with zero drive -> the shaft runs backwards.
    # The load is set before the run starts, so the shaft reaches its terminal
    # reverse speed during the settle and the window measures steady motion.
    # The plant's `pulses' is cumulative and the settle here is NOT motionless
    # (the load is already pulling), so the model side of every comparison is
    # the DIFFERENCE between a report taken on the cycle the window OPENS and
    # one taken on the cycle it closes.  That needs two breakpoints -- and it
    # has to be the window's opening: the engine only ticks peripherals while
    # the CPU runs, so a report issued before `go` always reads pulses=0 and
    # would silently credit the settle's turn to the window.
    code, stop = build(setup=park, window={"n_over": 20, "quad": True},
                       settle_outer=32)
    out = run(code, set_cfg(load_mode=1, load_torque=900) + [ARM] +
              ["break 0x%04x" % LABELS["mark"], "break 0x%04x" % stop,
               "go", "info hw motor", "go"] +
              ["dump iram 0x20 0x2f", "info hw motor", "exit"], name="rev")
    bl = blocks(out)
    f, raw = bl[-1]
    m = fw(out)
    cpr_model = cycles_per_rev(raw)
    delta = fnum(f, "pulses", 0) - fnum(bl[0][0], "pulses", 0)
    check("C2a gravity above breakaway runs it backwards",
          f.get("dir") == "rev" and fnum(f, "pulses", 0) > 0 and
          f.get("held") == "no",
          "dir=%s pulses=%s speed=%s" % (f.get("dir"), f.get("pulses"),
                                         f.get("speed")))
    check("C2b decoder saw reverse transitions only",
          m["rev"] > 0 and m["fwd"] == 0 and m["skip"] == 0,
          "fwd=%d rev=%d skip=%d polls=%d" % (m["fwd"], m["rev"], m["skip"],
                                              m["polls"]))
    # Direction purity is what is proven, and it is proven: fwd and skip stay
    # at zero through a whole reverse window and the state machine never
    # reports a two-bit jump.  The COUNT is not proven and must never be read
    # as a fraction of the wheel: the decoder reported 21 transitions while the
    # T1 hardware counter and the model agreed at 646 for the same window (C2d,
    # C2e).  Nothing in this suite explains that deficit.  Ruled out: the plant
    # (C2d agrees to one edge), pin visibility (probeG, inside this routine,
    # sees the level low in 39% of samples), placement (the LCALL is the last
    # thing before the deadline djnz, so it runs every poll), and the port
    # latch-vs-pin effect (quad_decode note above).  What is left is between
    # the sample and the counter in this image; it is carried as an open item
    # in SIMULATION-PLAN.md instead of being dressed up as a diagnosis.  Keep
    # the ratio visible so a later run cannot quietly make it worse.
    check("C2c decoder direction is pure; its count is unvalidated, not a floor",
          m["pulses"] > 0 and m["rev"] > 0 and m["rev"] <= 4 * m["pulses"],
          "T1=%d rev=%d (%.1f%% of 4x)" % (m["pulses"], m["rev"],
                                           100.0 * m["rev"] /
                                           max(1, 4 * m["pulses"])))
    check("C2d window pulses: 8051 counter == model's own delta",
          abs(m["pulses"] - delta) <= 2,
          "TL1:TH1=%d model delta=%s (%s->%s)" % (m["pulses"], delta,
                                                  bl[0][0].get("pulses"),
                                                  f.get("pulses")))
    cpr_fw = speed_of(f, m, m["pulses"])
    ok = bool(cpr_fw and cpr_model and
              abs(cpr_fw - cpr_model) / cpr_model <= 0.02)
    check("C2e reverse speed within 2 %% of the model%s" %
          ("" if not (cpr_fw and cpr_model)
           else " (%.0f vs %.1f)" % (cpr_fw, cpr_model)), ok,
          "fw=%s model=%s" % (("%.1f" % cpr_fw) if cpr_fw else "n/a",
                              cpr_model))
    return f, m


def leg_d():
    print("--- leg D: gear reduction and backlash")
    # D1: 4:1 with no slack -> output angle is exactly the motor angle / 4.
    code, stop = build(setup=park, gate=drive, window={"n_over": 10},
                       settle_outer=32)
    out = run(code, set_cfg(gear_x1000=4000) + [ARM] + at_stop(stop) +
              ["dump iram 0x20 0x2f", "info hw motor", "exit"], name="gear")
    f, raw = last(out)
    m = fw(out)
    pos, out_pos = fnum(f, "pos"), fnum(f, "out")
    check("D1 gear 4000 published as 4.000:1", f.get("gear") == "4.000:1",
          str(f.get("gear")))
    check("D1b output angle == motor angle / 4",
          pos and out_pos and abs(out_pos * 4.0 - pos) <= 0.001 * pos,
          "pos=%s out=%s" % (f.get("pos"), f.get("out")))
    check("D1c no slack with backlash off", fnum(f, "slack") == 0,
          str(f.get("slack")))
    check("D1d firmware counts every edge through the gear",
          m["pulses"] > 0 and abs(m["pulses"] - fnum(f, "pulses", -9)) <= 1,
          "TL1:TH1=%d pulses=%s" % (m["pulses"], f.get("pulses")))

    # D2: 3.6 degrees of slack at the output.  Drive forward at 100 % with a
    # gravity load above breakaway; the gate cuts the drive, dynamic braking
    # plus gravity reverse the shaft, and the output must hold still until the
    # slack is taken up.  This leg deliberately samples across time with `step'
    # -- there is no window and no counter to line up, only the report, and a
    # breakpoint would give one sample instead of a reversal trace.  The first
    # samples are read BEFORE the arm so the forward run is on the record too.
    code, _stop = build(setup=drive, gate=cut, settle_outer=32)
    cmds = (set_cfg(gear_x1000=4000, backlash_x1000=3600, load_mode=1,
                   load_torque=100) +
            ["step 60000", "info hw motor", "step 8000", "info hw motor",
             ARM] +
            sum([["step 8000", "info hw motor"] for _ in range(16)], []) +
            ["step 120000", "info hw motor", "exit"])
    out = run(code, cmds, name="backlash")
    bl = blocks(out)
    fwd = [b for b in bl if b[0].get("dir") == "fwd"]
    rev = [b for b in bl if b[0].get("dir") == "rev"]
    peak = max([fnum(b[0], "out", 0.0) for b in bl] or [None])
    check("D2a drove forward before the cut", len(fwd) >= 2,
          "%d fwd sample(s)" % len(fwd))
    check("D2b reversed after the drive was cut", len(rev) >= 3,
          "%d rev sample(s)" % len(rev))
    lag = None
    for b in rev:
        if fnum(b[0], "slack", 0.0) > 2.5:
            lag = b
            break
    check("D2c the reversal regenerated the slack", lag is not None,
          "max slack=%s" % (max([fnum(b[0], "slack", 0.0) for b in rev])
                            if rev else "none"))
    if lag is not None:
        check("D2d output frozen while the slack is open",
              abs(fnum(lag[0], "out", 9e9) - peak) <= 0.0005,
              "out=%s peak=%s slack=%s" % (lag[0].get("out"), peak,
                                           lag[0].get("slack")))
    else:
        check("D2d output frozen while the slack is open", False,
              "no open-slack sample")
    lf = bl[-1][0] if bl else {}
    lpos, lout = fnum(lf, "pos"), fnum(lf, "out")
    check("D2e output follows once the slack is gone",
          fnum(lf, "slack", 9) == 0 and peak is not None and lout is not None
          and lout < peak - 0.0005 and lpos is not None and
          abs(lout * 4.0 - lpos) <= 0.001 * abs(lpos),
          "out=%s peak=%s slack=%s pos=%s" % (lf.get("out"), peak,
                                              lf.get("slack"), lf.get("pos")))
    check("D2f slack never exceeded the setting",
          all(fnum(b[0], "slack", 0) <= 3.601 for b in bl),
          "max=%s" % max([fnum(b[0], "slack", 0) for b in bl]))
    return bl


def leg_e():
    print("--- leg E: duty sweep")
    # 25 / 50 / 75 %: the PCA holds CEX0 low while CL < CCAP0L, so the drive is
    # high for (256-CCAP0L) of every 256 counts.
    rows = []
    for ccap, want in ((192, 25.00), (128, 50.00), (64, 75.00)):
        code, stop = build(setup=lambda p: (park(p), pca_setup(p, ccap)),
                           gate=pca_gate, window={"n_over": 10},
                           settle_outer=32)
        out = run(code, [ARM] + at_stop(stop) +
                  ["dump iram 0x20 0x2f", "info hw motor", "exit"],
                  name="duty%d" % ccap)
        f, raw = last(out)
        m = fw(out)
        rows.append((want, fnum(f, "duty"), cycles_per_rev(raw),
                     speed_of(f, m, m["pulses"]), m["pulses"],
                     fnum(f, "pulses"), fnum(f, "window", PWM_WINDOW)))
    bad = ["got %s want %s" % (r[1], r[0]) for r in rows
           if not duty_ok(r[1], r[0], r[6])]
    check("E1 duty sweep tracks 25/50/75 %% within one PWM period", not bad,
          " ".join(bad))
    speed = [(1.0 / r[2]) if r[2] else None for r in rows]
    check("E2 speed is monotone in duty",
          all(s is not None for s in speed) and
          speed[0] < speed[1] < speed[2],
          " ".join("%.2e" % s if s else "n/a" for s in speed))
    worst = None
    for r in rows:
        if r[2] and r[3]:
            e = abs(r[3] - r[2]) / r[2]
            worst = e if worst is None else max(worst, e)
    check("E3 every firmware speed within 2 %% of the model (worst %.2f %%)"
          % ((worst if worst is not None else 9.99) * 100),
          worst is not None and worst <= 0.02,
          " ".join("fw=%s/model=%s" % (("%.0f" % r[3]) if r[3] else "n/a",
                                       r[2]) for r in rows))
    check("E4 counts agree at every duty",
          all(r[4] and r[5] is not None and abs(r[4] - r[5]) <= 1 for r in rows),
          " ".join("%s/%s" % (r[4], r[5]) for r in rows))

    # Zero drive is a hard off: nothing moves and nothing is emitted with no
    # load, and a load above breakaway runs away backwards instead of braking.
    code, stop = build(setup=park, window={"n_over": 10}, settle_outer=32)
    a = last(run(code, [ARM] + at_stop(stop) +
                 ["dump iram 0x20 0x2f", "info hw motor", "exit"],
                 name="off0"))
    b = last(run(code, set_cfg(load_mode=1, load_torque=900) + [ARM] +
                 at_stop(stop) + ["dump iram 0x20 0x2f", "info hw motor",
                                  "exit"], name="off1"))
    fa, mb, fb = a[0], fw(a[1]), b[0]
    check("E5 duty 0: dead with no load, runaway backwards with gravity",
          fnum(fa, "duty") == 0 and fnum(fa, "pulses") == 0 and
          mb["pulses"] == 0 and fa.get("dir") == "stop" and
          fnum(fa, "pos") == 0 and fb.get("dir") == "rev" and
          fnum(fb, "pulses", 0) > 0,
          "dir=%s/%s pulses=%s/%s duty=%s" % (fa.get("dir"), fb.get("dir"),
                                              fa.get("pulses"),
                                              fb.get("pulses"), fa.get("duty")))
    return rows


# ------------------------------------------------------------------ leg 0 ---
# The deadline this suite builds its own legs with, pinned to the bed's decoder:
# {0xdd, "DJNZ R5,%r"} at sims/s51.src/glob.cc:261.
DEADLINE_BYTES = bytes([0x7D, 0xFF, 0xDD, 0xFE])  # MOV R5,#0xff ; DJNZ R5,$
# t/kbdtest.py:175 used to carry 7d ff d7 fe labelled "MOV R7,#0xff ; DJNZ R7,$".  This
# bed decodes 0x7d as MOV R5,#imm (glob.cc:165) and 0xd7 as XCHD (glob.cc:255), so those
# bytes were a swap plus a fall-through and the "~510 machine cycle" deadline never
# waited -- it reached the landing point in about 4 cycles.  It now carries the R7 form
# below, which is what its comments always named; A0a pins the encoding and A0d is the
# real oracle, being the simulator's own disassembler.
KBD_DEADLINE = bytes([0x7F, 0xFF, 0xDF, 0xFE])
COOKBOOK = ["MOV", "MOV", "MOV", "MOV", "MOV", "MOV", "CLR", "XRL", "ANL",
            "SUBB", "INC", "CLR", "SWAP", "RR", "CLR", "SETB", "MOV", "RLC",
            "DJNZ", "DJNZ", "JNZ", "CJNE", "JB", "JNB", "SJMP", "LCALL", "RET"]


def disasm(out):
    """Mnemonics from the console's own `dc` listing, in address order.

    The last row on each listing line carries an erase-to-end-of-line escape
    glued onto the mnemonic (`RET\\x1b[0K'), so the escapes come off before the
    tokens are split -- otherwise the final instruction of any listing never
    compares equal to anything.
    """
    got = []
    for ln in out.splitlines():
        ln = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", ln)
        m = re.match(r"^0x[0-9a-f]{4}\s+\S+\s+", ln)
        if not m:
            continue
        rest = ln[m.end():].split()
        n = 0
        while n < len(rest) and re.fullmatch(r"[0-9a-f]{2}", rest[n]):
            n += 1
        if n and n < len(rest):
            got.append(rest[n])
    return got


def leg_tooling():
    """The instrument before the specimen.

    Every byte the legs measure with is hand-assembled here, so the assembler is
    part of the measurement and no C++ gate covers it.  Three fictions lived in
    it: 0xC0 is PUSH direct (not SWAP A), 0x25 is ADD A,direct, and there is no
    `JNZ direct' at all in the 8051 -- a PUSH then swallows the byte after it and
    everything from there on runs misaligned, which is exactly the shape of the
    `TL1 counted while pulses=0' confusion.  The only decoder that matters is
    the simulator's own, so A0d feeds it one of every idiom and reads back the
    mnemonics; A0a pins the one instruction the house deadline idiom turns on.
    """
    print("--- leg 0: the mini-assembler, pinned against the bed's decoder")
    p = As()
    p.mov_r(5, 0xFF)
    p.label("spin")
    p.djnz_r(5, "spin")
    check("A0a deadline idiom encodes MOV R5,#0ff / DJNZ R5,$",
          p.asm() == DEADLINE_BYTES and not p.errors,
          "%s (kbdtest.py:175 carries the R7 form %s)"
          % (p.asm().hex(" "), KBD_DEADLINE.hex(" ")))

    q = As()
    q.label("far")
    for _ in range(200):
        q.op(0x00)                               # NOP
    q.sjmp("far")
    qbytes = q.asm()                             # must not raise
    check("A0b branch out of range is recorded, not raised",
          len(q.errors) == 1 and "far" in q.errors[0] and len(qbytes) == 202,
          q.errors[0] if q.errors else "no error recorded")

    r = As()
    r.ljmp("t")
    r.op(0x22)
    r.label("t")
    r.op(0x80, 0xFE)
    check("A0c LJMP emits 0x02 and the label's 16-bit address",
          r.asm().startswith(b"\x02\x00\x04") and not r.errors,
          r.asm().hex(" "))

    c = As()
    c.mov_d(P1, 0x08)
    c.mov_r(5, 0xFF)
    c.mov_a_d(TL0)
    c.mov_d_a(O_TL0)
    c.mov_a_r(1)
    c.mov_r_a(0)
    c.clr_a()
    c.xrl_a_r(0)
    c.anl_a(0x03)
    c.subb_a_d(O_PREV)
    c.inc_d(O_OVS)
    c.clr_c()
    c.swap()
    c.rr_a()
    c.clr_bit(TF0_B)
    c.setb_bit(TR0_B)
    c.mov_c_bit(TACH_A_BIT)                      # the pin-sampling idiom itself
    c.rlc_a()
    c.label("one")
    c.djnz_d(BUD_LO, "one")
    c.djnz_r(5, "one")
    c.jnz_a("one")
    c.cjne_a_im(1, "one")
    c.jb_bit(TF1_B, "one")
    c.jnb_bit(TF1_B, "one")
    c.sjmp("one")
    c.lcall("two")
    c.label("two")
    c.ret()                                      # end of the listing
    code = c.asm()
    got = disasm(run(code, ["dc 0x0 0x%x" % (len(code) - 1), "exit"],
                     name="cookbook"))
    bad = [(i, g, w) for i, (g, w) in enumerate(zip(got, COOKBOOK))
           if g.split()[:2] != w.split()[:2]]
    check("A0d simulator decodes every idiom as emitted",
          not bad,
          "want %d insn, got %d%s" % (len(COOKBOOK), len(got),
                                      "" if not bad else
                                      " first diff @%d: %r vs %r" % bad[0]))


# ------------------------------------------------------------------ main ---
def main():
    print("s51 rotary plant test")
    print("SIM    = %s" % SIM)
    print("device = %s" % DEVICE)
    if not os.path.exists(SIM):
        print("FAIL simulator binary missing: %s" % SIM)
        print("Checks run: %d" % _checks)
        return 1

    # The suite's own instrument first: if the hand-assembler is wrong, every
    # other line here is noise, and that verdict must not depend on which
    # binary is under test.
    leg_tooling()

    # A pre-change binary has no plant at all.  Say so once, fast, instead of
    # letting every leg sit out its deadline: that is the gate-before-fix A/B,
    # and "no motor element" must not be confused with "plant present, stuck".
    has, nel, _ = dev_has_motor(DEVICE)
    if not has:
        print("FAIL %s exposes no motor element (%s hardware element(s)): "
              "this simulator predates the rotary plant" % (SIM, nel))
        for name in ("A registration", "B speed", "C hold and direction",
                     "D gear and backlash", "E duty sweep"):
            check("plant present for leg %s" % name, False)
        print("Checks run: %d" % _checks)
        return 1

    if leg_a():
        leg_b()
        leg_c()
        leg_d()
        leg_e()

    if _failures:
        print("Failed: %s" % ", ".join(_failures))
    print("Checks run: %d" % _checks)
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
