#!/usr/bin/env python3
"""
AT89C51RD2 enhanced UART: the internal baud rate generator (BRL 0x9a, BDRCON 0x9b) and
CKCON0.SIX2 (bit 4 of CKCON0 0x8f).

Ground truth is Atmel doc4235, digested in ~/s51/doc4235-spec.md:

  14 BAUD RATE GENERATOR, table 14-2 / 14-13 and figure 14-5:
      BDRCON 0x9b = - - - BRR(4) TBCK(3) RBCK(2) SPD(1) SRC(0), reset XXX0 0000b.
      BRL is the reload value of an 8-bit down accumulator (256-BRL counts per overflow).
      SRC  mode 0 clock: 0 = fixed rate (fosc/12), 1 = internal BRG /32.
      SPD  1 = BRG clock used directly (fast), 0 = divided by 6; reset value 0.
      TBCK/RBCK  1 = Tx/Rx clock from the BRG (T2CON.TCLK/RCLK then don't care), 0 = T1.
      BRR  0 = the BRG stops (no baud clock at all).
      Baud = 2^SMOD1 * FCLK_PERIPH / (6^(1-SPD) * 32 * (256-BRL)), FCLK_PERIPH = Fosc/2.
  p.19: "SIX2: Enhanced UART Clock (Mode 0 and 2). ... SIX2=0 selects 6 and SIX2=1 selects
      12 clock periods per peripheral clock cycle. The SIX2 bit is only valid if the X2 bit
      is set to '1'."  So in X2 a fixed-rate bit costs Fosc/6 with SIX2=0 and Fosc/12 with
      SIX2=1 -- identical to standard mode, which is what the X2 acceptance rows assert.

Everything is measured on transmitter bytes only.  Reception from a batch (-R in=...) file is
not dependable in this build (serconf_check_often is off by default, see SIMULATION-PLAN.md
step 13), so every leg drives SBUF and waits on TI.

Rates are measured by DIFFERENCING two runs of the same program that differ only in how many
bytes they send, which cancels the launch cost and the per-byte JNB/CLR polling overhead to
within one poll (2 machine cycles).  Bit periods are quoted in oscillator clocks, which is
what the datasheet's own worked examples give (table 14-5: 9600 -> 144 clocks at BRL=247), so
the same number must hold at any -X frequency; the baud column is the same number read back
at that frequency and must match the datasheet's printed baud to within its own % error.

Traps this file defends against, each of which produced a plausible wrong number earlier:
  - `file` must be quoted, or the loader reports "File name is missing" and runs zeros (NOPs).
  - an Intel HEX record with a bad checksum is not rejected: the bytes stay zero, so the run
    still produces clock counts -- just not the ones you wrote.  _hex_bytes asserts.
  - the SCON mode bits are SM0=bit7 and SM1=bit6: MODE1 is 0x40, not 0x10 (0x10 is TB8, and
    the part is still in mode 0).
  - `go` without a breakpoint wedges the console until the subprocess timeout.
  - BRR (BDRCON.4) has to be set for the BRG to overflow at all; a leg that forgets it
    measures a stopped generator and looks like a timer fallback.

Run:  S51_SIM=/path/to/ucsim_51 python3 euarttest.py
"""

import os
import re
import subprocess
import sys

# Default to the tree under development: a suite whose default points at some other build
# silently measures that build instead (that is exactly how a BRG change got "verified" against
# a tree that had it on the wrong gate).  S51_SIM still overrides for control runs.
SIM = os.environ.get("S51_SIM", os.path.expanduser(
    "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51"))
WORKDIR = "/tmp/euart"

ACCURACY = 0.03          # datasheet tables print 0.1%..5.7% errors; 3% is generous here
TOLERANCE = 0.02         # for "the same rate as" checks: 2%
SJMP = bytes([0x80, 0xFE])

BRL, BDRCON = 0x9a, 0x9b
PCON, SCON, SBUF, TI_BIT = 0x87, 0x98, 0x99, 0x99
CKCON0, TCON, TMOD, TH1, TL1 = 0x8f, 0x88, 0x89, 0x8d, 0x8b
BM_BRR, BM_TBCK, BM_RBCK, BM_SPD, BM_SRC = 0x10, 0x08, 0x04, 0x02, 0x01
# BRG/BRG6: the generator switched on (BRR set, or nothing ever overflows)
BRG = BM_BRR | BM_TBCK | BM_RBCK
BRG6 = BRG | BM_SPD

MODE0, MODE1, MODE2 = 0x00, 0x40, 0x80   # SCON: SM0=bit7, SM1=bit6
X2, SIX2 = 0x01, 0x10
# Timer 2 and AUXR, for the leg that proves neither of them may drive the timer-1 baud
# counters.  AUXR bit 0 is AUXR.AO on this part (doc4235 §9), NOT the STC baud select.
AUXR = 0x8e
RCAP2L, RCAP2H, TL2, TH2 = 0xca, 0xcb, 0xcc, 0xcd
T2MOD, T2CON = 0xc9, 0xc8
T2OE, TR2 = 0x02, 0x04

_checks = 0
_failures = []


def check(name, ok, detail=""):
    global _checks
    _checks += 1
    print("%-58s %s%s" % (name, "PASS" if ok else "FAIL",
                          "  %s" % detail if detail else ""))
    if not ok:
        _failures.append(name)
    return ok


def mov(a, v):
    """MOV direct,#imm."""
    return bytes([0x75, a, v])


def or_imm(addr, v):
    """ORL direct,#imm."""
    return bytes([0x43, addr, v])


def send_and_wait(nbytes, first=0x30):
    """nbytes SBUF writes, each closed by JNB TI,$ ; CLR TI."""
    out = bytearray()
    for i in range(nbytes):
        out += bytes([0x74, (first + i) & 0xff, 0xf5, SBUF])
        out += bytes([0x30, TI_BIT, 0xfd])          # JNB TI, .   (2 MC poll)
        out += bytes([0xc2, TI_BIT])                # CLR TI
    return bytes(out)


def tx_prog(scon, ckcon0, brl, bdrcon, nbytes, smod1=False, timer1=False):
    """Set the UART/BRG state, then send nbytes and park.  timer1 starts T1 in mode 2 with
    TH1=0xfd, i.e. the classic 9600-baud overflow source, for the fallback checks."""
    code = bytearray()
    code += mov(SCON, scon)
    code += mov(CKCON0, ckcon0)
    code += mov(BRL, brl)
    code += mov(BDRCON, bdrcon)
    if smod1:
        code += or_imm(PCON, 0x80)
    if timer1:
        # TMOD is NOT optional here: without it timer 1 is in mode 0 (13-bit), whose overflow
        # period is ~8192 counts instead of 3, and every rate measured through this branch is
        # then off by three orders of magnitude while still being a real measurement.  That is
        # how a "786432 clocks/bit" figure came out of leg 3 and looked like a model defect.
        code += mov(TMOD, 0x20)
        code += mov(TH1, 0xfd) + mov(TL1, 0xfd) + mov(TCON, 0x40)
    code += send_and_wait(nbytes)
    return bytes(code) + SJMP


def _hex_bytes(code, base=0):
    """Intel HEX, the shape t/mkhex.py emits (checksum included, verified).  A record with a
    bad checksum is not rejected loudly: the loader leaves the skipped bytes at zero, zero is
    NOP, so the program still runs and still reports clock counts -- just not the ones you
    wrote.  The assert below is what keeps that from being invisible."""
    lines = []
    for off in range(0, len(code), 16):
        chunk = code[off:off + 16]
        b = bytes([len(chunk), ((base + off) >> 8) & 0xff, (base + off) & 0xff, 0]) + chunk
        lines.append(":%02X%04X%02X%s%02X" %
                     (len(chunk), (base + off) & 0xffff, 0, chunk.hex().upper(),
                      (-sum(b)) & 0xff))
    lines.append(":00000001FF")
    text = "\n".join(lines) + "\n"
    for ln in text.splitlines():
        assert sum(bytes.fromhex(ln[1:])) & 0xFF == 0, "bad checksum in %r" % ln
    return text.encode()


def _image(name, code):
    os.makedirs(WORKDIR, exist_ok=True)
    img = os.path.join(WORKDIR, "%s_%d_%x.hex" % (name, len(code), sum(code) & 0xffff))
    with open(img, "wb") as f:
        f.write(_hex_bytes(code))
    return img


def clks_at(code, freq, timeout=900):
    """Run `code` to its final SJMP and return the elapsed oscillator clocks, or None if it
    never got there (a direction with no clock never sets TI, and the poll loop is endless)."""
    img = _image("e", code)
    cmds = ['file "%s"' % img, "break 0x%x" % (len(code) - 2), "go", "state"]
    args = [SIM, "-t", "89C51RD2", "-c", "-"]
    if freq:
        args += ["-X", freq]
    try:
        p = subprocess.run(args, input="\n".join(cmds) + "\n", capture_output=True,
                           text=True, cwd=WORKDIR, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", p.stdout)
    return int(m.group(1)) if m else None


def tx_bytes(code, freq, steps, timeout=900, sim=None, extra=()):
    """Run `code` for a bounded number of instructions with the UART output captured to a
    file, and return the bytes that actually went out plus the console transcript."""
    out = os.path.join(WORKDIR, "out_%d_%x.bin" % (len(code), sum(code) & 0xffff))
    if os.path.exists(out):
        os.unlink(out)
    img = _image("s", code)
    cmds = (['file "%s"' % img, "step %d" % steps, "dump sfr[0x98]"] + list(extra) +
            ["info hardware brg", "exit"])
    args = [sim or SIM, "-t", "89C51RD2", "-c", "-", "-S", "out=%s" % out]
    if freq:
        args += ["-X", freq]
    p = subprocess.run(args, input="\n".join(cmds) + "\n", capture_output=True,
                       text=True, cwd=WORKDIR, timeout=timeout)
    data = open(out, "rb").read() if os.path.exists(out) else b""
    return data, p.stdout


def rate(code_small, code_big, n, freq, per_bit):
    """Clocks per bit, differenced over n bytes, or None if either run never finished."""
    big = clks_at(code_big, freq)
    small = clks_at(code_small, freq)
    if big is None or small is None:
        return None
    return (big - small) / float(n) / float(per_bit)


# Table 14-5 (X2, SMOD1=1, SPD=1) and table 14-6 (standard, SMOD1=0, SPD=0) rows of doc4235.
# (label, -X freq, SCON, CKCON0, smod1, BDRCON, BRL, expected bit in OSC CLOCK periods,
#  datasheet baud, datasheet % error)
VECTORS = [
    ("t14-5 9600   16.384MHz BRL=247", "16.384M", MODE1, X2, True,
     BRG6, 247, 144.0, 113778, -1.23),
    ("t14-5 9600   24MHz BRL=243", "24M", MODE1, X2, True,
     BRG6, 243, 208.0, 115385, 0.16),
    ("t14-5 8000   24MHz BRL=100", "24M", MODE1, X2, True,
     BRG6, 100, 2496.0, 9615, 0.15),
    ("t14-6 4800   16.384MHz BRL=247", "16.384M", MODE1, 0x00, False,
     BRG, 247, 3456.0, 4741, -1.23),
    ("t14-6 1200   24MHz BRL=202", "24M", MODE1, 0x00, False,
     BRG, 202, 20736.0, 1157, -3.55),
    ("t14-6 600    24MHz BRL=152", "24M", MODE1, 0x00, False,
     BRG, 152, 39936.0, 601, -0.15),
]


def fit(points):
    """Least squares T_meas = a*T_formula + b over (formula, measured) pairs."""
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    a = (n * sxy - sx * sy) / float(n * sxx - sx * sx)
    return a, (sy - a * sx) / float(n)


def leg_vectors():
    print("== leg 1: the BRG reproduces the datasheet worked vectors ==")
    print("   Baud = 2^SMOD1*Fosc/(2*32*(256-BRL)) standard; BDRCON.SPD takes another /6 off")
    print("   the BRG clock, so the bit period must shrink by exactly 6.")
    print("   %-36s %10s %10s %11s %9s %8s" %
          ("configuration", "bit meas", "bit spec", "baud meas", "spec", "err"))
    points = []
    for (name, freq, scon, ck, smod1, bdrcon, brl, want, want_baud,
         want_err) in VECTORS:
        bit = rate(tx_prog(scon, ck, brl, bdrcon, 3, smod1),
                   tx_prog(scon, ck, brl, bdrcon, 13, smod1), 10, freq, 10)
        if bit is None:
            check(name, False, "no transmission (BRG never overflowed)")
            continue
        mhz = float(freq[:-1])
        baud = mhz * 1e6 / bit
        err = 100.0 * (baud - want_baud) / want_baud
        points.append((want, bit))
        print("   %-36s %10.2f %10.1f %11.0f %9.0f %+7.2f%%" %
              (name, bit, want, baud, want_baud, err))
        check(name + " bit period",
              abs(bit - want) / want <= ACCURACY + abs(want_err) / 100.0,
              "%.2f vs %.1f clocks" % (bit, want))
    if len(points) >= 3:
        a, b = fit(points)
        check("vector fit is the BRG law, not a lucky constant (slope ~1)",
              abs(a - 1.0) < 0.01 and abs(b) < 8.0,
              "T_meas = %.4f*T_spec + %.2f" % (a, b))


def leg_brr_off():
    print("== leg 2: BRR=0 stops the BRG, and a BRG-driven direction must not fall back ==")
    print("   Table 14-13: \"BRR = 0 -> BRG not running\".  Timer 1 is left running at the")
    print("   classic 9600 rate, so a sloppy implementation would keep sending from it.")
    off_prog = tx_prog(MODE1, X2, 247, BM_TBCK | BM_RBCK | BM_SPD, 1, True, timer1=True)
    on_prog = tx_prog(MODE1, X2, 247, BRG6, 1, True, timer1=True)
    off, out_off = tx_bytes(off_prog, "16.384M", 4000)
    on, out_on = tx_bytes(on_prog, "16.384M", 4000)
    ti_off = re.search(r"0x98 SCON:\s+\S+\s+(0x[0-9a-fA-F]{2})", out_off)
    running_off = "stopped" in out_off
    running_on = "RUNNING" in out_on
    got = lambda d: d.decode("latin-1").count("0") if d else 0
    check("BRR=0: BRG reports stopped", running_off,
          [l for l in out_off.splitlines() if "brg[" in l][:1])
    check("BRR=0: nothing transmitted even though T1 overflows", got(off) == 0,
          "%d byte(s)" % got(off))
    check("BRR=0: TI stays clear",
          ti_off is not None and not int(ti_off.group(1), 16) & 1,
          "SCON=%s" % (ti_off.group(1) if ti_off else "?"))
    check("BRR=1: BRG runs", running_on)
    check("BRR=1: the byte goes out", got(on) >= 1, "%d byte(s)" % got(on))


def leg_tbck_select():
    print("== leg 3: TBCK=1 replaces timer 1, TBCK=0 keeps it ==")
    print("   Table 14-3: TBCK=1 -> \"BRG generates the Tx clock (TCLK is don't care)\".")
    print("   Same BRL and CKCON0 in all three runs; only TR1 and TBCK move.")
    # Both programs in a pair come from the same builder with only the byte count changed:
    # splicing extra send blocks onto the end of a finished program used to make this leg print
    # ~786000 clocks/bit for the timer-1 row (a figure nobody asserted, and which measured
    # 289.8 clocks/bit on three different binary/device combinations when built the same way as
    # its siblings).  Never print a rate without asserting it.
    specs = {                       # key -> (BDRCON, timer1)
        "brg_tr1": (BRG6, True),    # BRG drives Tx (TBCK=1) while T1 also overflows
        "brg_no": (BRG6, False),    # BRG alone
        "t1_tr1": (BM_BRR | BM_RBCK | BM_SPD, True),   # TBCK=0: T1 clocks Tx, Rx stays on BRG
    }
    c = {}
    for k, (bdrcon, timer1) in specs.items():
        c[k] = rate(tx_prog(MODE1, X2, 247, bdrcon, 4, True, timer1),
                    tx_prog(MODE1, X2, 247, bdrcon, 20, True, timer1),
                    16, "16.384M", 10)
    if None in list(c.values()):
        check("TBCK select measured at all", False, str(c))
        return
    print("   bit periods: BRG+TR1=%.2f  BRG alone=%.2f  TBCK=0 with TR1=%.2f clocks" %
          (c["brg_tr1"], c["brg_no"], c["t1_tr1"]))
    check("TBCK=1: the rate is the BRG's whether or not T1 overflows",
          abs(c["brg_tr1"] - c["brg_no"]) / c["brg_no"] <= TOLERANCE,
          "%.2f vs %.2f" % (c["brg_tr1"], c["brg_no"]))
    check("TBCK=1: that rate is the 144-clock vector, not T1's",
          abs(c["brg_no"] - 144.0) / 144.0 <= ACCURACY, "%.2f clocks" % c["brg_no"])
    check("TBCK=0: timer 1 clocks the UART instead (rate changes)",
          abs(c["t1_tr1"] - c["brg_no"]) / c["brg_no"] > 0.5,
          "%.2f vs %.2f clocks/bit" % (c["t1_tr1"], c["brg_no"]))
    # The classic mode-1 bit with SMOD1=1 is 2^1/32 x timer-1 overflow rate, timer 1 in mode 2
    # with TH1=0xfd overflowing every 3 machine cycles -> 48 machine cycles, i.e. 288 oscillator
    # clocks under X2.  Asserting the number is what keeps this leg from becoming a rumour.
    check("TBCK=0: that rate is the classic 48-machine-cycle bit (288 clocks in X2)",
          abs(c["t1_tr1"] - 288.0) / 288.0 <= ACCURACY, "%.2f clocks" % c["t1_tr1"])
    # And the TBCK=1 number cannot be timer 1 in disguise: stop the timer and bytes still go.
    data, _ = tx_bytes(tx_prog(MODE1, X2, 247, BRG6, 4, True), "16.384M", 8000)
    check("TBCK=1 with T1 stopped: bytes still leave",
          data.decode("latin-1").count("0") >= 1, repr(data[:8]))


def leg_six2_fixed():
    print("== leg 4: CKCON0.SIX2 scales the fixed rates of modes 0 and 2 only ==")
    print("   p.19: SIX2=0 -> 6, SIX2=1 -> 12 clock periods per peripheral cycle, valid only")
    print("   when X2=1.  Fosc/12 (standard) == Fosc/6 * X2 * SIX2=1, so the X2+SIX2 column")
    print("   must land on the standard one, and SIX2 must be inert without X2.  Ratios, not")
    print("   absolute counts: a measured bit carries the poll term, so the absolute window for")
    print("   mode 2 lives in leg_fixed_rate_mode2 and this leg owns only the X2/SIX2 shape.")
    for mode, label in ((MODE0, "mode0"), (MODE2, "mode2")):
        per_bit = 8 if mode == MODE0 else 11
        v = {}
        for tag, ck in (("std", 0x00), ("x2", X2), ("x2six", X2 | SIX2), ("six", SIX2)):
            v[tag] = rate(tx_prog(mode, ck, 0, 0, 4), tx_prog(mode, ck, 0, 0, 22),
                          18, "16.384M", per_bit)
        if None in list(v.values()):
            check("%s measured at all" % label, False, str(v))
            continue
        print("   %-6s per byte: std=%.2f  X2=%.2f  X2+SIX2=%.2f  SIX2 alone=%.2f clocks"
              % (label, v["std"], v["x2"], v["x2six"], v["six"]))
        check("%s: X2 with SIX2=0 is faster than standard" % label,
              v["x2"] < v["std"] * 0.9, "%.2f < %.2f" % (v["x2"], v["std"]))
        # The pure bit periods obey bit(X2,SIX2=1) == bit(std) == 2*bit(X2,SIX2=0), but a
        # measured bit carries a per-byte poll term (2 MC, i.e. 24 clocks in std and only 12
        # in X2), so equality with std cannot be read off directly.  What is exact is the
        # ratio the bit itself produces: SIX2=1 must nearly double the X2 fixed bit.
        check("%s: SIX2=1 nearly doubles the X2 fixed bit period" % label,
              v["x2six"] >= 1.5 * v["x2"],
              "%.2f / %.2f = %.2f (poll overhead dilutes the theoretical 2.00)" %
              (v["x2six"], v["x2"], v["x2six"] / v["x2"]))
        check("%s: SIX2 is inert while X2=0" % label,
              abs(v["six"] - v["std"]) / v["std"] <= TOLERANCE,
              "%.2f vs %.2f" % (v["six"], v["std"]))


def leg_six2_not_the_brg():
    print("== leg 5: a BRG-driven rate is untouched by SIX2 ==")
    print("   SIX2 is documented for modes 0 and 2 only; the BRG is derived from")
    print("   FCLK_PERIPH and X2 already cancels in the baud formula, so flipping SIX2 must")
    print("   not move a BRG clocked byte by so much as one count.")
    for scon, bdrcon, label, bits in (
            (MODE1, BRG6, "mode1 TBCK=1", 10),
            (MODE0, BM_BRR | BM_SRC | BM_TBCK | BM_RBCK | BM_SPD, "mode0 SRC=1", 8)):
        no = rate(tx_prog(scon, X2, 247, bdrcon, 2, True),
                  tx_prog(scon, X2, 247, bdrcon, 12, True), 10, "16.384M", bits)
        yes = rate(tx_prog(scon, X2 | SIX2, 247, bdrcon, 2, True),
                   tx_prog(scon, X2 | SIX2, 247, bdrcon, 12, True), 10, "16.384M", bits)
        if no is None or yes is None:
            check("%s measured at all" % label, False, "%s / %s" % (no, yes))
            continue
        print("   %-16s per bit: SIX2=0 %.2f  SIX2=1 %.2f clocks" % (label, no, yes))
        check("%s: SIX2 does not move the BRG rate" % label, no == yes,
              "%.2f vs %.2f" % (no, yes))


def leg_spd_over_six():
    print("== leg 6: BDRCON.SPD is a /6, in the fast direction ==")
    print("   Table 14-2: SPD=1 bypasses the /6.  Same BRL, same SMOD1, standard mode: the")
    print("   bit period must be six times shorter, and it must still be exact at the")
    print("   256-count reload (figure 14-5 reloads from BRL; the prescaler never touches it).")
    v = {}
    for tag, brl, bdrcon in (("slow", 247, BRG), ("fast", 247, BRG6),
                             ("full_slow", 0, BRG), ("full_fast", 0, BRG6)):
        v[tag] = rate(tx_prog(MODE1, 0x00, brl, bdrcon, 2),
                      tx_prog(MODE1, 0x00, brl, bdrcon, 12), 10, "16.384M", 10)
    if None in list(v.values()):
        check("SPD ratio measured at all", False, str(v))
        return
    print("   BRL=247: SPD=0 %.1f  SPD=1 %.1f clocks/bit (ratio %.3f)" %
          (v["slow"], v["fast"], v["slow"] / v["fast"]))
    print("   BRL=0  : SPD=0 %.1f  SPD=1 %.1f clocks/bit (ratio %.3f)" %
          (v["full_slow"], v["full_fast"], v["full_slow"] / v["full_fast"]))
    check("SPD=1 is a /6 on the bit period", abs(v["slow"] / v["fast"] - 6.0) <= 0.3,
          "%.3f" % (v["slow"] / v["fast"]))
    check("the /6 is exact at the 256-count reload too",
          abs(v["full_slow"] / v["full_fast"] - 6.0) <= 0.3,
          "%.3f" % (v["full_slow"] / v["full_fast"]))
    check("SPD=1 is faster, not slower", v["fast"] < v["slow"])


def leg_reset():
    print("== leg 7: reset leaves BDRCON = XXX0 0000b and the BRG stopped ==")
    print("   Table 14-2 reset value; cl_hw::reset() runs before the SFRs are cleared, so the")
    print("   BRG must take its state from the register's reset constants, not from garbage.")
    code = mov(SCON, MODE1) + SJMP          # BDRCON never written
    _, out = tx_bytes(code, "16.384M", 100)
    line = [l for l in out.splitlines() if "brg[" in l]
    bdrcon = re.search(r"0x9b BDRCON:\s+\S+\s+(\S+)", out)
    check("cold start: BDRCON reads 0x00",
          bdrcon is not None and int(bdrcon.group(1), 16) == 0,
          bdrcon.group(1) if bdrcon else "?")
    check("cold start: BRG is stopped", bool(line) and "stopped" in line[0],
          line[0] if line else "no brg element")


def leg_reference_proof(sim):
    print("== leg 8: the same vector on another binary (%s) ==" % sim)
    print("   Without the BRG there is no clock behind TBCK=1: timer 1 is deliberately")
    print("   stopped here, so the program must wedge in its JNB TI loop and send nothing.")
    if not os.path.exists(sim):
        check("reference binary present", False, sim)
        return
    code = tx_prog(MODE1, X2, 247, BRG6, 1, True)
    data, out = tx_bytes(code, "16.384M", 8000, sim=sim)
    brg_line = [l for l in out.splitlines() if "brg[" in l]
    check("that binary sends nothing with TBCK=1 and T1 stopped",
          len(data) == 0, "%r" % data[:8])
    check("and it has no BRG hardware element at all", not brg_line,
          brg_line[0] if brg_line else "none")


def leg_fixed_rate_mode2():
    """doc4235 table 14-4, printed p. 53 (`/tmp/ds/p052.txt`), fixes mode 2: "9-bit UART
    FXTAL/64 or FXTAL/32".  FXTAL is the crystal, so a bit is 64 oscillator clock periods with
    SMOD=0 and 32 with SMOD=1 -- the same units `clks_at` reports, so no clock_per_cycle factor
    belongs anywhere in this expectation.

    Differencing a 1-frame against an 8-frame build cancels the launch cost, but NOT the CPU
    cost of `send_and_wait`: MOV A,#d + MOV SBUF,A + JNB TI,$ + CLR TI is 5 machine cycles = 60
    oscillator clocks per byte, i.e. 60/11 = 5.45 clocks per bit, plus the up-to-one-poll
    (2 MC = 24 clocks = 2.18 per bit) quantisation of when the poll notices TI.  The window is
    therefore the table number plus that arithmetic -- it is a derived bound, not a fitted one.
    The 2x-too-fast divisor this check was written against measured 37.0 and 21.0, which sit far
    outside it, so a regression cannot slip through the top or bottom of either window."""
    print("\n-- mode 2 fixed rate: table 14-4 (p.53) says FXTAL/64 and FXTAL/32")
    freq = "11.0592M"
    std = rate(tx_prog(MODE2, 0, 0, 0, 1), tx_prog(MODE2, 0, 0, 0, 8), 7, freq, 11)
    dbl = rate(tx_prog(MODE2, 0, 0, 0, 1, smod1=True),
               tx_prog(MODE2, 0, 0, 0, 8, smod1=True), 7, freq, 11)
    check("mode 2 SMOD=0 bit costs 64 osc clocks + poll",
          std is not None and 64.0 <= std <= 78.0,
          ("%.2f clocks/bit" % std) if std else "TI never set")
    check("mode 2 SMOD=1 halves it to 32 clocks + poll",
          dbl is not None and 32.0 <= dbl <= 46.0,
          ("%.2f clocks/bit" % dbl) if dbl else "TI never set")

def leg_baud_source_isolation():
    """Two ways something else can steal the timer-1 baud clock.

    (a) AUXR bit 0.  On STC12 that bit is S1BRS, on STC15 it is S1ST2, and both mean
        "timer 1 is NOT the baud source"; the model tested it on every part.  Here the
        same address and bit is AUXR.AO, the ALE control used to cut EMI (doc4235 §9),
        which firmware sets for reasons that have nothing to do with the UART -- and AO=1
        threw away every timer-1 overflow, so TI never rose.
    (b) T2MOD.T2OE.  Clock out publishes its reload overflow as EV_OVERFLOW exactly like
        the baud mode does, and the handler counted it unconditionally, so an active clock
        out drove the EUart fast.
    """
    print("\n== leg 9: only timer 1 may drive the timer-1 baud counters ==")
    print("   Each variant is one bit away from its control and the same length: the")
    print("   control writes the same registers with the bit clear (ORL #0, MOV #0).")

    def prog(auxr_bit, t2_on):
        c = bytearray()
        c += (mov(RCAP2L, 0xf0) + mov(RCAP2H, 0xff) + mov(TL2, 0x00) + mov(TH2, 0xff))
        c += mov(T2MOD, T2OE if t2_on else 0x00)
        c += mov(T2CON, TR2 if t2_on else 0x00)
        c += or_imm(AUXR, auxr_bit)
        c += tx_prog(MODE1, 0x00, 0x00, 0x00, 2, timer1=True)
        return bytes(c)

    STEPS = 900
    ctl, _ = tx_bytes(prog(0x00, False), None, STEPS)
    ao, _ = tx_bytes(prog(0x01, False), None, STEPS)
    t2, _ = tx_bytes(prog(0x00, True), None, STEPS)

    check("control: timer-1 baud sends bytes", len(ctl) >= 1, "%r" % ctl)
    check("AUXR.AO=1 sends exactly the same bytes", ao == ctl and len(ao) >= 1,
          "%r vs %r" % (ao, ctl))
    check("T2 clock out sends exactly the same bytes", t2 == ctl,
          "%r vs %r" % (t2, ctl))

    # Sensitivity, measured where nothing can wrap.  Reading TL2 after the runs above
    # would prove nothing: RCAP2 is deliberately close there so that clock out overflows
    # constantly, and a one-bit pin check depends on how many times it happened to
    # toggle.  With RCAP2 = 0 the period is the full 65536 counts, so the 16-bit value the
    # element reports is the raw clock-out count -- 3 per machine cycle, ~92 over the 20
    # instructions below -- and it cannot wrap.
    def t2_only(on):
        return (mov(RCAP2L, 0x00) + mov(RCAP2H, 0x00) +
                mov(TL2, 0x00) + mov(TH2, 0x00) +
                mov(T2MOD, T2OE if on else 0x00) +
                mov(T2CON, TR2 if on else 0x00) + SJMP)

    _, live_on = tx_bytes(t2_only(True), None, 20, extra=["info hardware timer2"])
    _, live_off = tx_bytes(t2_only(False), None, 20, extra=["info hardware timer2"])

    def running(out):
        m = re.search(r"timer2\[\d+\]\s+0x([0-9a-f]{4})\b(.*)", out)
        return None if m is None else (int(m.group(1), 16), "ON" in m.group(2))

    on_v, off_v = running(live_on), running(live_off)
    check("sensitivity: clock out runs, and only when it is asked to",
          on_v is not None and off_v is not None and on_v[0] > 0x10 and on_v[1] and
          off_v == (0, False), "on=%s off=%s" % (on_v, off_v))


if __name__ == "__main__":
    print("SIM =", SIM)
    leg_vectors()
    leg_brr_off()
    leg_tbck_select()
    leg_six2_fixed()
    leg_fixed_rate_mode2()
    leg_six2_not_the_brg()
    leg_spd_over_six()
    leg_reset()
    # The control must be a binary that HAS the 89C51RD2 identity but NOT the BRG, otherwise it
    # fails because the device name is unknown rather than because the generator is missing.
    # /tmp/s51-ref-b is steps 1-6 + PCA fixes (identity yes, BRG no); /tmp/s51-ref predates the
    # identity and would pass these two checks vacuously.
    leg_reference_proof(os.environ.get("S51_REF", "/tmp/s51-ref-b"))
    leg_baud_source_isolation()
    print("\nChecks run: %d  Failed: %d" % (_checks, len(_failures)))
    if _failures:
        print("FAILURES: " + ", ".join(_failures))
        sys.exit(1)
    print("ALL EUART / BRG / SIX2 CHECKS PASSED")
