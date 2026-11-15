#!/usr/bin/env python3
"""
AT89C51RD2/ED2 Serial Peripheral Interface: SPCON (0xc3), SPSTA (0xc4), SPDAT (0xc5),
the master clock divider of doc4235 tables 16-1 and 16-3 with CKCON1.SPIX2, the four
Port 1 pins (P1.1 SS, P1.5 MISO, P1.6 SCK, P1.7 MOSI -- table 4-1, printed pp. 11-12)
and the two interrupt requests of figure 16-7 (printed p. 69) behind IEN1.ESPI, vector
004Bh (table 17-2, printed p. 73).

Ground truth is Atmel doc4235K, digested in ~/s51/doc4235-spec.md; every check cites the
printed page it comes from.  The one number this suite can really falsify is the master
bit time.  Table 16-3 (printed pp. 69-70) gives SPR2:SPR0 = 000..110 as FCLK PERIPH /2,
/4, /8, /16, /32, /64, /128 and 111 as Invalid, and FCLK PERIPH is 6 periods per machine
cycle in BOTH clock states (section 6, table 7-2), so a bit costs 2*BD oscillator clocks
in standard mode and BD in X2 mode, and a byte of 8 bits costs 16*BD.  Rates are measured
by DIFFERENCING two runs of one program that differ only in how many bytes it sends, so
the launch cost cancels.

Probe order, stated because it matters here: `info hardware spi' FIRST, `dump sfr' only
afterwards.  Reading SPSTA arms the flag-clearing sequence and reading SPDAT performs it
(sections 16.3.1.1 and 16.3.3, printed pp. 66-68), so `dump sfr 0xc3 0xc5' consumes the
very flags the legs are about to measure; the instrument prints the same numbers through
get() and disturbs nothing.

Traps this file defends against; each one produced a plausible wrong answer while the
block was written:
  - `file' must be quoted or the loader prints "File name is missing" and runs an
    all-zero image, and 0x00 is NOP, so a dead image still advances PC and reports ticks.
  - a relative jump built from `len(code)` BEFORE the rest of the instruction sequence is
    appended aims at itself: `MOV A,SPSTA / ANL A,#SPIF / JZ <itself>' spins forever and
    every timing leg then measures a spin.  poll_spif() is the only place offsets are
    computed and it appends the test before computing the target.
  - `go' needs a breakpoint it will actually reach.  A leg whose transfer never completes
    (SPR=111 has no clock generator) must use `step N', and every console run is wrapped
    in a subprocess timeout that names the leg instead of hanging the floor.
  - SPCON and SPSTA are NOT bit addressable (printed pp. 69-70), so SPSTA cannot be
    polled with JNB, and a bare "SPSTA reads 0x00 out of reset" proves nothing because SFR
    space no element owns also reads 0x00.  Every reset-value leg therefore has to show
    the element's own `info hardware spi' output in the same breath.
  - P2 resets to 0xff, so a marker port incremented by an ISR has to be zeroed by the
    program first; otherwise one entry reads back as 0x00 and looks like no interrupt.

Run:  S51_SIM=/path/to/ucsim_51 python3 spitest.py
      S51_REF=/tmp/s51-ref-b (default) is the pre-SPI control used by leg_reference_proof.
"""

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM", "/tmp/ucsim-spi/ucsim/src/sims/s51.src/ucsim_51")
WORK = tempfile.mkdtemp(prefix="spitest-")

SPCON, SPSTA, SPDAT = 0xc3, 0xc4, 0xc5                       # table 16-3, 16-4, p. 69-71
P1, P2, CKCON0, CKCON1 = 0x90, 0xa0, 0x8f, 0xaf
IE, IEN1 = 0xa8, 0xb1                                        # tables 17-3, 17-4
EXIF, T2CON, TH2, TL2, RCAP2H, RCAP2L = 0x91, 0xc8, 0xcd, 0xcc, 0xcb, 0xca
SS, MISO, SCK, MOSI = 0x02, 0x20, 0x40, 0x80                 # bits of P1, table 4-1
BM_SPEN, BM_SSDIS, BM_MSTR, BM_CPOL, BM_CPHA = 0x40, 0x20, 0x10, 0x08, 0x04
BM_SPIF, BM_WCOL, BM_SSERR, BM_MODF = 0x80, 0x40, 0x20, 0x10
BM_ESPI, BM_SPIX2, BM_X2, BM_EA = 0x04, 0x01, 0x01, 0x80
MARKER = P2                # the ISR counts entries here; nothing else in these images can
SPI_VECTOR = 0x004b        # table 17-2, printed p. 73

_checks = 0
_failures = []


def check(name, ok, detail=""):
    global _checks
    _checks += 1
    if not ok:
        _failures.append(name)
        print("FAIL  %s%s" % (name, ("  [%s]" % detail) if detail else ""))
    else:
        print("ok    %s%s" % (name, ("  [%s]" % detail) if detail else ""))


# ---------------------------------------------------------------- opcodes
# JZ is 0x60 and JNZ 0x70 here; 0x72 is ORL C,bit.  MOV direct,#imm costs 2 machine
# cycles and every accumulator move 1, which is what the timing windows are built from.
def d(a, v): return [0x75, a, v & 0xff]
def ai(v): return [0x74, v & 0xff]
def da(a): return [0xf5, a]
def ad(a): return [0xe5, a]
def anl(v): return [0x54, v & 0xff]
def r0(v): return [0x78, v]
def r1(v): return [0x79, v]
def jz(rel): return [0x60, rel & 0xff]
def djnz0(rel): return [0xd8, rel & 0xff]
def djnz1(rel): return [0xd9, rel & 0xff]
def inc(a): return [0x05, a]
def sjmp(): return [0x80, 0xfe]
def reti(): return [0x32]
def nop(): return [0x00]


def poll_spif(code):
    """`MOV A,SPSTA / ANL A,#SPIF / JZ <back to the move>'.  The target is computed only
    after the test has been appended, because a self-aiming JZ is invisible in every
    result except a hang."""
    poll = len(code)
    code += ad(SPSTA) + anl(BM_SPIF)
    code += jz(poll - (len(code) + 2))
    return poll


def pad_to(code, addr):
    while len(code) < addr:
        code.append(0x00)              # NOP; these images never run past 0x20
    return code


def image(code, name):
    path = os.path.join(WORK, name + ".hex")
    write_hex(path, bytes(code))
    return path


# ---------------------------------------------------------------- console
def run(path, cmds, device="89C51RD2", sim=None,
        timeout=int(os.environ.get("S51_TIMEOUT", "30"))):
    args = [sim or SIM, "-t", device, "-c", "-"]
    script = "\n".join(['file "%s"' % path] + cmds + ["exit"]) + "\n"
    try:
        p = subprocess.run(args, input=script, capture_output=True, text=True,
                           timeout=timeout, cwd=WORK)
    except subprocess.TimeoutExpired:
        return "<<TIMEOUT console wedged>>"
    return p.stdout


def spi_info(text):
    """Parse `info hardware spi' into a dict, or None when there is no such element."""
    m = re.search(r"^spi\[\d+\] .*$", text, re.M)
    if not m:
        return None
    blk = text[m.start():]
    cut = blk.find("Configuration memory")
    if cut > 0:
        blk = blk[:cut]
    state = {"text": blk}
    for k, v in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=0x([0-9a-fA-F]{2,})", blk):
        state[k] = int(v, 16)
    for k, v in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(-?\d+)\b", blk):
        state[k] = int(v)
    return state


def dump_sfr(text, addr):
    """Value of one SFR from a transcript that already contains its `dump sfr' line."""
    m = re.search(r"^0x%02x\s+(?:\S+\s+)?0b[01]+\s+0x([0-9a-f]{2})" % addr, text, re.M)
    return int(m.group(1), 16) if m else None


def probed(path, steps=0, device="89C51RD2", sim=None):
    """One console run: instrument first, then the marker and T2CON dumps (neither is an
    SPI register, so neither can consume a clearing sequence).  Returns
    (state, marker, t2con)."""
    cmds = (["step %d" % steps] if steps else []) + [
        "info hw spi",
        "dump sfr 0x%02x 0x%02x" % (MARKER, MARKER),
        "dump sfr 0x%02x 0x%02x" % (T2CON, T2CON)]
    t = run(path, cmds, device=device, sim=sim)
    return spi_info(t), dump_sfr(t, MARKER), dump_sfr(t, T2CON)


def clocks(path, spin, device="89C51RD2", sim=None):
    """Oscillator clocks until the program parks itself at `spin'.  Taken from `state':
    the `go' path prints no per-stop tick total the way `step' does."""
    t = run(path, ["break 0x%x" % spin, "go", "state"], device=device, sim=sim)
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", t)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- programs
def master_tx(spcn, n, name, pre=()):
    """Send n copies of 0x5a as master, polling SPSTA, then park.  (path, spin)."""
    code = list(pre) + d(SPCON, spcn) + r0(n)
    loop = len(code)
    code += ai(0x5a) + da(SPDAT)
    poll_spif(code)
    code += djnz0(loop - (len(code) + 2))
    spin = len(code)
    code += sjmp()
    return image(code, name), spin


def rate_per_byte(spcn, tag, pre=(), n_lo=1, n_hi=9):
    """Oscillator clocks per transferred byte, by differencing two byte counts."""
    lo, spin_lo = master_tx(spcn, n_lo, "lo" + tag, pre)
    hi, spin_hi = master_tx(spcn, n_hi, "hi" + tag, pre)
    c_lo, c_hi = clocks(lo, spin_lo), clocks(hi, spin_hi)
    if c_lo is None or c_hi is None:
        return None
    return (c_hi - c_lo) / float(n_hi - n_lo)


def spcn(spr, cpha=BM_CPHA, ssdis=BM_SSDIS, mstr=BM_MSTR, cpol=0):
    """SPCON with SPR2 in bit 7 and SPR1:SPR0 in bits 1:0 (table 16-3, printed p. 69)."""
    return (BM_SPEN | ssdis | mstr | cpol | cpha |
            ((spr & 0x04) << 5) | (spr & 0x03))


# ===================================================================== legs
def leg_rate_table():
    """Table 16-3 (printed pp. 69-70): SPR=000..110 divides FCLK PERIPH by 2..128, and
    FCLK PERIPH is 6 periods per machine cycle in both clock states (section 6), i.e. one
    FCLK period is 2 oscillator clocks at 12 clocks per cycle.  A bit is BD FCLK periods
    and a byte 8 bits, so a byte costs 16*BD oscillator clocks."""
    measured = {}
    for spr, bd in ((0x00, 2), (0x02, 8), (0x04, 32), (0x06, 128)):
        per = rate_per_byte(spcn(spr), "bd%d" % bd)
        measured[bd] = per
        # The loop around each byte costs a fixed 8 machine cycles (96 clocks) and the
        # poll can only notice the finished byte at a pass boundary (up to 48 clocks
        # more), so the window is the datasheet figure plus that overhead.  What the
        # window cannot see -- a constant offset faking a slope -- is caught below.
        want = 16 * bd
        check("SPR=%d (FCLK PERIPH/%d): byte = %d oscillator clocks" % (spr, bd, want),
              per is not None and want <= per <= want + 144,
              ("%.1f clocks/byte, window %d..%d" % (per, want, want + 144))
              if per is not None else "transfer never completed")
    # Differencing ACROSS dividers cancels the program's own cost exactly: the loop is
    # the same 8 cycles per byte in both runs, so only the clock differs.
    lo, hi = measured.get(2), measured.get(128)
    check("BD=128 less BD=2 is exactly 16*(128-2) = 2016 clocks/byte (the program's own "
          "cost cancels, so this is the divider and nothing else)",
          lo is not None and hi is not None and abs((hi - lo) - 2016) <= 48,
          "%.1f - %.1f = %.1f, want 2016 +/- 48" % (hi, lo, hi - lo)
          if None not in (lo, hi) else "one of the two runs never completed")


def leg_x2_spix2():
    """CKCON1.SPIX2 (table 7-2, printed p. 20) holds the SPI at its standard rate while
    the CPU doubles, and is validated only when CKCON0.X2 is set.  So X2 alone halves the
    byte time in oscillator clocks, X2+SPIX2 restores the standard value, and SPIX2 with
    X2 clear does nothing at all."""
    spr = 0x06                                   # BD=128: long enough that the fixed
    want = 16 * 128                              # cost of a poll pass cannot hide a 2x
    std = rate_per_byte(spcn(spr), "x2std")
    x2 = rate_per_byte(spcn(spr), "x2only", pre=d(CKCON0, BM_X2))
    both = rate_per_byte(spcn(spr), "x2both",
                         pre=d(CKCON0, BM_X2) + d(CKCON1, BM_SPIX2))
    spix = rate_per_byte(spcn(spr), "spixonly", pre=d(CKCON1, BM_SPIX2))
    check("CKCON0.X2 clear: byte costs %d oscillator clocks" % want,
          std is not None and want <= std <= want + 144,
          "%.1f" % std if std is not None else "no transfer")
    check("X2 set, SPIX2 clear: byte halves to %d clocks (FCLK PERIPH = Fosc)"
          % (want // 2),
          x2 is not None and want // 2 <= x2 <= want // 2 + 72,
          "%.1f" % x2 if x2 is not None else "no transfer")
    check("X2 with SPIX2 set: back to the standard %d clocks" % want,
          both is not None and want <= both <= want + 144,
          "%.1f" % both if both is not None else "no transfer")
    check("SPIX2 with X2 clear changes nothing (table 7-2: 'when X2 is low, this bit "
          "has no effect')",
          spix is not None and std is not None and abs(spix - std) < 5,
          "%.1f vs %.1f" % (spix, std) if None not in (spix, std) else "no transfer")


def leg_no_clock_setting():
    """SPR=111 is printed Invalid (table 16-3, printed p. 70), so there is no clock to
    divide: the byte must sit in the shift register for ever and SPIF must never set."""
    p, _ = master_tx(spcn(0x07), 1, "nobrg")
    st, _, _ = probed(p, steps=60)
    check("SPR=111: no baud divider, so no SCK edge and no SPIF ever",
          st is not None and st.get("BD") == 0 and st.get("edge") == 0
          and st.get("SPIF") == 0 and st.get("bytes") == 0,
          st["text"].splitlines()[0] if st else "no spi element")
    # The contrast control for that verdict: the identical program with SPR=110 does
    # transfer, so "no clock ever" cannot be an artifact of a loop that never let the
    # transfer finish.
    p2, _ = master_tx(spcn(0x06), 1, "brg")
    st2, _, _ = probed(p2, steps=400)
    check("same program with SPR=110: the clock exists, the byte transfers and SPIF "
          "sets (so the SPR=111 verdict is the divider, not a broken probe)",
          st2 is not None and st2.get("BD") == 128 and st2.get("bytes") == 1
          and st2.get("SPIF") == 1,
          st2["text"].splitlines()[0] if st2 else "no spi element")


def leg_cpol():
    """CPOL 'defines the default SCK line level in idle state' (table 16-3 bit 3, printed
    p. 70): the master holds it from the moment SPEN is set -- before any byte has been
    written -- and returns to it when the byte is done."""
    for cpol, name in ((0, "CPOL=0"), (BM_CPOL, "CPOL=1")):
        idle = 1 if cpol else 0
        # BD=128: one half period is 64 FCLK periods, so the first edge is still more
        # than 10 machine cycles away when the program parks after the SPDAT write.
        code = (d(P1, 0xff) + d(SPCON, spcn(0x06, cpol=cpol)) + ai(0x5a) + da(SPDAT))
        code += sjmp()
        p = image(code, "cpolrun%d" % idle)
        st, _, _ = probed(p, steps=4)
        check("%s: SCK idles at %d (P1.6 in the latch) while the transfer runs"
              % (name, idle),
              st is not None and st.get("running") == 1 and st.get("edge") == 0
              and st.get("SCK") == idle,
              "edge=%s SCK=%s" % (st.get("edge"), st.get("SCK")) if st else "no element")
        st, _, _ = probed(p, steps=400)
        check("%s: the byte runs its eight full SCK cycles and ends back at the idle "
              "level (SPIF set, generator returned to edge 0)" % name,
              st is not None and st.get("SPIF") == 1 and st.get("SCK") == idle
              and st.get("edge") == 0 and st.get("running") == 0
              and st.get("bytes") == 1,
              "edge=%s SCK=%s SPIF=%s bytes=%s" % (st.get("edge"), st.get("SCK"),
                                                   st.get("SPIF"), st.get("bytes"))
              if st else "no element")


def leg_cpha():
    """Figure 16-4 against figure 16-5 (printed p. 67).  With CPHA=0 the data is sampled
    as SCK leaves the idle level, so the sender must already be driving MOSI when the
    byte is loaded; with CPHA=1 it is sampled as SCK returns, and the sender starts
    driving on the first edge.  0x0f is chosen so that bit 7 is 0 while P1 resets to 1:
    an idle-high line and a driven-low line are then distinguishable."""
    for cpha, name in ((0, "CPHA=0"), (BM_CPHA, "CPHA=1")):
        code = (d(P1, 0xff) + d(SPCON, spcn(0x06, cpha=cpha)) + ai(0x0f) + da(SPDAT)
                + sjmp())
        p = image(code, "cpha%d" % (1 if cpha else 0))
        st, _, _ = probed(p, steps=4)
        if cpha:
            check("CPHA=1: MOSI still holds the idle level with no edge taken "
                  "(figure 16-5: driving starts on the first SCK edge)",
                  st is not None and st.get("edge") == 0 and st.get("MOSI") == 1,
                  "edge=%s MOSI=%s" % (st.get("edge"), st.get("MOSI")) if st else "none")
        else:
            check("CPHA=0: the MSB is already on MOSI before any edge (figure 16-4: the "
                  "first SCK edge is the MSB capture strobe)",
                  st is not None and st.get("edge") == 0 and st.get("MOSI") == 0,
                  "edge=%s MOSI=%s" % (st.get("edge"), st.get("MOSI")) if st else "none")


def leg_spif_and_receive():
    """Section 16.3.1.1 (printed p. 66): SPIF is set at the end of the transfer and the
    received byte lands in the SPDAT receive register at the same moment.  MISO is P1.5,
    so the byte received is the level the program left on that bit."""
    for bit, want in ((1, 0xff), (0, 0x00)):
        p1 = 0xff if bit else (0xff & ~MISO)
        code = d(P1, p1) + d(SPCON, spcn(0x00)) + ai(0x5a) + da(SPDAT)
        poll_spif(code)
        code += sjmp()                    # park without reading SPDAT: SPIF stays set
        p = image(code, "rx%d" % bit)
        st, _, _ = probed(p, steps=40)
        check("MISO held %d: the byte received is 0x%02x and SPIF is set" % (bit, want),
              st is not None and st.get("SPIF") == 1 and st.get("SPDAT") == want,
              "SPIF=%s SPDAT=0x%02x" % (st.get("SPIF"), st.get("SPDAT", 0))
              if st else "no element")

    # A read of SPDAT answers with the receive buffer, not with what was written there
    # (section 16.3.5.3, printed p. 71): the program moves SPDAT into the marker port.
    code = d(SPCON, spcn(0x00)) + ai(0x5a) + da(SPDAT)
    poll_spif(code)
    code += ad(SPSTA) + ad(SPDAT) + da(MARKER)          # the clearing sequence
    code += sjmp()
    p = image(code, "rxbuf")
    st, marker, _ = probed(p, steps=40)
    # The byte counter is part of the assertion deliberately: without it this leg passes
    # on a binary that has no SPI at all, because the poll loop never exits, the move to
    # the marker port never executes, and P2 still holds its reset image of 0xff -- which
    # is the value the leg is looking for.  Requiring the model's own count of completed
    # bytes means only a part that really transferred something can satisfy it.
    check("SPDAT reads back the received byte 0xff, not the 0x5a written to it",
          marker == 0xff and st is not None and st.get("bytes") == 1,
          "marker=0x%02x bytes=%s" % (marker if marker is not None else 0,
                                      st.get("bytes") if st else None))
    check("the SPSTA-then-SPDAT sequence clears SPIF (section 16.3.1.1)",
          st is not None and st.get("SPIF") == 0,
          "SPIF=%s" % (st.get("SPIF") if st else None))


def leg_flag_clearing():
    """Each flag has its own clearing sequence (section 16.3.3, printed p. 68) and only
    the pair counts: reading SPSTA alone, or SPDAT alone, must leave SPIF set."""
    base = d(SPCON, spcn(0x06)) + ai(0x5a) + da(SPDAT)
    for name, tail in (("SPSTA", ad(SPSTA)), ("SPDAT", ad(SPDAT))):
        # A burn loop, not a poll: polling SPSTA is itself the first half of the
        # clearing sequence, so a polled leg could never tell 'SPDAT alone' from the
        # real pair.  320 machine cycles is past the 171 a BD=128 byte takes.
        code = list(base) + r1(0xa0) + djnz1(-2) + tail + sjmp()
        p = image(code, "clear-" + name)
        st, _, _ = probed(p, steps=180)
        check("reading %s alone does not clear SPIF (and the byte did complete, so the "
              "flag was there to clear)" % name,
              st is not None and st.get("SPIF") == 1 and st.get("bytes") == 1,
              "SPIF=%s bytes=%s" % (st.get("SPIF"), st.get("bytes"))
              if st else "no element")

    # MODF clears on SPSTA followed by a WRITE to SPCON (section 16.3.3.1, printed p. 68).
    # The same image is replayed at two step counts so both halves are observed.
    code = (d(P1, 0xff & ~SS)                          # SS driven low, SS handling on
            + d(SPCON, BM_SPEN | BM_MSTR | BM_CPHA)    # SSDIS cleared
            + r1(0x10) + djnz1(-2)                     # let the fault settle a while
            + ad(SPSTA) + d(SPCON, BM_SSDIS) + sjmp())
    p = image(code, "clear-modf")
    st, _, _ = probed(p, steps=4)
    check("a mode fault is live before the sequence: MODF set with SS low and SSDIS=0, "
          "and the fault itself has cleared SPEN",
          st is not None and st.get("MODF") == 1 and st.get("SPEN") == 0
          and st.get("MSTR") == 0,
          "MODF=%s SPEN=%s MSTR=%s" % (st.get("MODF"), st.get("SPEN"),
                                       st.get("MSTR")) if st else "no element")
    st, _, _ = probed(p, steps=21)
    check("SPSTA then a write to SPCON clears MODF (and SSDIS=1 stops the fault "
          "re-asserting on the still-low pin)",
          st is not None and st.get("MODF") == 0,
          "MODF=%s SPCON=0x%02x" % (st.get("MODF"), st.get("SPCON", 0))
          if st else "no element")


def leg_wcol():
    """Section 16.3.3.2 (printed p. 68): WCOL is set when SPDAT is written during a
    transmit sequence, it causes no interruption, and the transfer continues
    uninterrupted -- which here is a testable claim, because the byte on the wire has to
    survive the colliding write."""
    # BD=128 so the byte is still on the wire when the second write lands: the burn loop
    # is 32 x 2 machine cycles, well inside the 170 cycles of a 128-divider byte.
    code = (d(SPCON, spcn(0x06)) + ai(0x0f) + da(SPDAT)
            + r1(0x20) + djnz1(-2) + ai(0xff) + da(SPDAT) + sjmp())
    p = image(code, "wcol")
    st, _, _ = probed(p, steps=40)
    check("a write to SPDAT during a transmit sequence sets WCOL",
          st is not None and st.get("WCOL") == 1,
          "WCOL=%s edge=%s" % (st.get("WCOL"), st.get("edge")) if st else "no element")
    check("the colliding write is dropped, not swapped in: MOSI is still carrying a 0 "
          "from 0x0f where the rejected 0xff has a 1",
          st is not None and st.get("WCOL") == 1 and st.get("MOSI") == 0
          and st.get("edge", 0) >= 1,
          "MOSI=%s edge=%s shift=0x%02x" % (st.get("MOSI"), st.get("edge"),
                                            st.get("shift", 0)) if st else "no element")
    st, _, _ = probed(p, steps=400)
    check("the interrupted transfer still completes: SPIF set, exactly one byte counted",
          st is not None and st.get("SPIF") == 1 and st.get("bytes") == 1
          and st.get("SPDAT") == 0xff,
          "SPIF=%s bytes=%s SPDAT=0x%02x" % (st.get("SPIF"), st.get("bytes"),
                                             st.get("SPDAT", 0)) if st else "no element")


def isr(counts=True, clear="flag"):
    """Service routine body for 004Bh: count entries in the marker port, then clear the
    flag that brought us here.  `flag' runs the SPSTA/SPDAT pair of section 16.3.1.1;
    `modf' runs SPSTA followed by a WRITE to SPCON, which is the only sequence that
    clears a mode fault (section 16.3.3.1, printed p. 68) -- using the pair there leaves
    MODF set and the routine re-enters for ever, which is correct behaviour being
    mistaken for a defect, so the two must not be conflated."""
    code = inc(MARKER) if counts else []
    if clear == "modf":
        return code + ad(SPSTA) + d(SPCON, BM_SSDIS) + reti()
    if clear:
        code += ad(SPSTA) + ad(SPDAT)
    return code + reti()


def leg_interrupt():
    """Figure 16-7 (printed p. 69) routes SPIF to the SPI Transmitter request and table
    17-2 (p. 73) gives vector 004Bh.  The enable is IEN1.ESPI (table 17-3), one of the
    sources whose enable lives outside IEN0 and whose priority is in the second pair."""
    def prog(ien1, ie=BM_EA, name="it"):
        code = (d(MARKER, 0) + d(SPCON, spcn(0x00)) + d(IE, ie) + d(IEN1, ien1)
                + ai(0x5a) + da(SPDAT) + sjmp())
        pad_to(code, SPI_VECTOR)
        code += isr()
        return image(code, name)

    p = prog(BM_ESPI, BM_EA, "it-on")
    st, marker, _ = probed(p, steps=60)
    check("EA+ESPI: the routine at 004Bh runs exactly once and its SPSTA/SPDAT sequence "
          "clears SPIF",
          marker == 1 and st is not None and st.get("SPIF") == 0,
          "marker=%s SPIF=%s" % (marker, st.get("SPIF") if st else None))

    p = prog(0x00, BM_EA, "it-noespi")
    st, marker, _ = probed(p, steps=60)
    check("ESPI cleared: no interrupt, but SPIF still sets (it is a flag first)",
          marker == 0 and st is not None and st.get("SPIF") == 1,
          "marker=%s SPIF=%s" % (marker, st.get("SPIF") if st else None))

    p = prog(BM_ESPI, 0x00, "it-noea")
    st, marker, _ = probed(p, steps=60)
    check("EA cleared: no interrupt even with ESPI set",
          marker == 0 and st is not None and st.get("SPIF") == 1,
          "marker=%s SPIF=%s" % (marker, st.get("SPIF") if st else None))

    # IEN1 bit 7 is Reserved but readable, and it is also the marker the second priority
    # pair uses inside the simulator.  Loading it must not switch the SPI off.
    p = prog(BM_ESPI | 0x80, BM_EA, "it-reserved")
    st, marker, _ = probed(p, steps=60)
    check("MOV IEN1,#0x84 (ESPI plus the reserved bit 7) still enables the SPI "
          "interrupt: the enable mask is tested as ESPI alone, not as the whole mask",
          marker == 1, "marker=%s" % marker)

    # WCOL raises no request (section 16.3.3.2, printed p. 68), so a transfer that takes
    # a collision still enters the service routine exactly once.
    code = (d(MARKER, 0) + d(SPCON, spcn(0x06)) + d(IE, BM_EA) + d(IEN1, BM_ESPI)
            + ai(0x0f) + da(SPDAT) + r1(0x20) + djnz1(-2) + ai(0xff) + da(SPDAT)
            + sjmp())
    pad_to(code, SPI_VECTOR)
    code += isr()
    p = image(code, "it-wcol")
    st, marker, _ = probed(p, steps=600)
    check("a colliding write raises no interrupt of its own (one entry, SPIF cleared)",
          marker == 1 and st is not None and st.get("SPIF") == 0,
          "marker=%s WCOL=%s SPIF=%s" % (marker, st.get("WCOL") if st else None,
                                         st.get("SPIF") if st else None))


def leg_modf():
    """Section 16.3.3.1 (printed p. 68): a mode fault raises the receiver/error request,
    clears SPEN and clears MSTR.  With SSDIS set, SS is a general purpose pin and no MODF
    request is generated at all (table 16-3 bit 5, printed p. 69)."""
    for ssdis, name in ((0, "SSDIS=0"), (BM_SSDIS, "SSDIS=1")):
        code = (d(MARKER, 0) + d(P1, 0xff & ~SS) + d(SPCON, BM_SPEN | BM_MSTR | BM_CPHA
                | ssdis) + d(IE, BM_EA) + d(IEN1, BM_ESPI) + ai(0x5a) + da(SPDAT)
                + sjmp())
        pad_to(code, SPI_VECTOR)
        code += isr(clear="modf") if not ssdis else isr()
        p = image(code, "modf%d" % (1 if ssdis else 0))
        st, marker, _ = probed(p, steps=40)
        if ssdis:
            check("SSDIS=1 with SS driven low: no mode fault at all, the transfer goes "
                  "ahead and completes (the single service-routine entry below is the "
                  "transmitter request at the end of that byte, not a fault)",
                  st is not None and st.get("MODF") == 0 and st.get("SPEN") == 1
                  and st.get("MSTR") == 1 and st.get("bytes") == 1
                  and st.get("modf_events") == 0 and marker == 1,
                  "MODF=%s modf_events=%s SPEN=%s bytes=%s marker=%s"
                  % (st.get("MODF"), st.get("modf_events"), st.get("SPEN"),
                     st.get("bytes"), marker) if st else "no element")
        else:
            check("SSDIS=0 with SS low: the fault fires (modf_events=1), clears SPEN and "
                  "clears MSTR, and the byte is dropped; the routine's own SPSTA+SPCON "
                  "write then clears the flag, which is why the flag reads 0 here",
                  st is not None and st.get("modf_events") == 1 and st.get("SPEN") == 0
                  and st.get("MSTR") == 0 and st.get("bytes") == 0
                  and st.get("MODF") == 0,
                  st["text"].replace("\n", " | ") if st else "no element")
            check("the mode fault is the receiver/error request: 004Bh runs",
                  marker == 1, "marker=%s" % marker)


def leg_modf_rearm():
    """Footnote 1 on printed p. 64 together with section 16.3.3.1 (printed p. 68): a mode
    fault is the SS *level*, and the SPSTA read plus the SPCON write only clears the *flag*
    -- "SPEN may then be restored to its prior state" restores the fault with it.  Two
    service routines restore SPEN under an SS that is still held low: one runs the
    documented clear sequence first, one writes SPCON straight away and so leaves MODF set.
    The second is where a model that treats "MODF is already set" as "this pin has already
    been reported" goes wrong: the byte shifts out under an SS the part is still calling an
    inappropriate logic level.  Final SPEN/MSTR are deliberately not asserted -- where
    `steps' stops inside the routine decides those; the counters do not depend on it."""
    for clears_flag, name in ((True, "rearm-clear"), (False, "rearm-dirty")):
        body = (inc(MARKER) + (ad(SPSTA) if clears_flag else [])
                + d(SPCON, BM_SPEN | BM_MSTR | BM_CPHA) + reti())
        code = (d(MARKER, 0) + d(P1, 0xff & ~SS) + d(SPCON, BM_SPEN | BM_MSTR | BM_CPHA)
                + d(IE, BM_EA) + d(IEN1, BM_ESPI) + ai(0x5a) + da(SPDAT) + sjmp())
        pad_to(code, SPI_VECTOR)
        code += body
        p = image(code, name)
        st, marker, _ = probed(p, steps=60)
        check("%s: restoring SPEN with SS still low faults again (%s): the receiver/error "
              "request is raised more than once, the routine is entered more than once, and "
              "no byte ever completes" % (name, "flag cleared first" if clears_flag
                                          else "MODF left set"),
              st is not None and st.get("modf_events", 0) >= 2 and marker >= 2
              and st.get("bytes") == 0,
              st["text"].replace("\n", " | ") if st else "no element")


def leg_pins_are_shared():
    """P1 is one latch shared with T2/T2EX, ECI and CEX0..CEX4 (table 4-1, printed
    pp. 11-12), so every SPI pin write must be a read-modify-write: the five bits that
    are not the SPI's keep the values the program gave them, and SS (P1.1, which is also
    T2EX) must never be written by the SPI at all.  The T2 half runs a capture-armed
    timer 2 (EXEN2) through the same transfer, so a SPI that wrote the whole port would
    be caught stealing its pin."""
    keep = 0x15 & 0x1d                        # P1.4 and P1.2 and P1.0 high, P1.3 low
    p1_init = keep | SS | MISO | SCK | MOSI
    code = (d(P1, p1_init)
            + d(T2CON, 0x00) + d(RCAP2H, 0x12) + d(RCAP2L, 0x34) + d(TH2, 0x00)
            + d(TL2, 0x00) + d(T2CON, 0x08 | 0x04)          # EXEN2 | TR2: capture armed
            + d(SPCON, spcn(0x00)) + ai(0x5a) + da(SPDAT))
    poll_spif(code)
    code += d(P1, p1_init & ~SS)              # the program drives T2EX low itself
    code += sjmp()
    p = image(code, "shared")
    st, _, t2con = probed(p, steps=140)
    if st is None:
        check("the shared-port legs ran (no spi element)", False, "no element")
        return
    check("the transfer ran, so these pin checks measure a live bus",
          st.get("SPIF") == 1 and st.get("bytes") == 1,
          "SPIF=%s bytes=%s" % (st.get("SPIF"), st.get("bytes")))
    check("P1.0/P1.2/P1.3/P1.4 keep the levels the program wrote (read-modify-write, "
          "not a whole-port write)",
          (st.get("P1", 0) & 0x1d) == keep,
          "P1=0x%02x masked 0x%02x want 0x%02x" % (st.get("P1", 0),
                                                   st.get("P1", 0) & 0x1d, keep))
    check("P1.1 is still the level the program gave it; the SPI never writes SS",
          st.get("SS") == 0, "SS=%s" % st.get("SS"))
    check("the program's P1.1 falling edge still reaches T2EX while the SPI is enabled "
          "(T2CON.EXF2, bit 6, which is where this model raises it)",
          t2con is not None and (t2con & 0x40) == 0x40,
          "T2CON=0x%02x" % (t2con if t2con is not None else 0))


def leg_spen_off_is_inert():
    """SPCON resets to 0001 0100b, MSTR set and SPEN clear (table 16-3, printed p. 69),
    so a cold part must not move a single pin; and 'clearing SPEN would immediately
    disable the SPI' (printed p. 71) must drop an in-flight byte and return SCK to idle."""
    code = d(SPCON, BM_MSTR | BM_CPHA) + ai(0x5a) + da(SPDAT) + sjmp()
    p = image(code, "spen0")
    st, _, _ = probed(p, steps=40)
    check("at the SPCON reset value nothing moves: P1 stays at its reset level, SPIF "
          "never sets, no byte is transferred",
          st is not None and st.get("SPEN") == 0 and st.get("P1") == 0xff
          and st.get("SPIF") == 0 and st.get("bytes") == 0 and st.get("running") == 0,
          st["text"].replace("\n", " | ")[-150:] if st else "no element")

    code = (d(SPCON, spcn(0x06)) + ai(0x5a) + da(SPDAT) + sjmp())
    p = image(code, "spen1")
    st_on, _, _ = probed(p, steps=20)
    code = (d(SPCON, spcn(0x06)) + ai(0x5a) + da(SPDAT)
            + d(SPCON, BM_MSTR | BM_CPHA) + sjmp())     # SPEN cleared mid-transfer
    p = image(code, "spenoff")
    st_off, _, _ = probed(p, steps=60)
    check("clearing SPEN mid-transfer drops the byte: running=0, no byte counted, SCK "
          "back at the idle level",
          st_on is not None and st_on.get("running") == 1
          and st_off is not None and st_off.get("running") == 0
          and st_off.get("SCK") == 0 and st_off.get("bytes") == 0,
          "before running=%s after running=%s SCK=%s"
          % (st_on and st_on.get("running"), st_off and st_off.get("running"),
             st_off and st_off.get("SCK")))


def slave_image(spcn_, edges, data, name, pre=(), ss_release=None, with_isr=False):
    """Bit-bang the pins of a slave SPI from the program: SS (P1.1) asserted first as
    figure 16-6 (printed p. 67) requires, then one explicit P1 write per half period of
    SCK (P1.6) with MSB-first data on MOSI (P1.7).  SCK idles low (CPOL=0), so even
    transitions raise it and odd ones drop it.  `ss_release' names the transition at
    which SS is driven high instead, which is how section 16.3.3.4's mid-byte abort is
    produced.  The bit on MOSI before transition e is e//2 with CPHA=1 and (e+1)//2 with
    CPHA=0, because the samples are the transitions returning to idle and leaving idle
    respectively (figures 16-4 and 16-5, printed p. 67)."""
    cpha = spcn_ & BM_CPHA
    base = 0xff & ~SS & ~MISO            # SS asserted low, MISO left high in the latch
    # P2 resets to 0xff, so an entry-counting service routine needs the port zeroed
    # first; otherwise one entry reads back as 0x00 and looks exactly like none.
    code = list(pre) + ([0x75, MARKER, 0x00] if with_isr else [])
    val = base & ~SCK
    val = (val & ~MOSI) if (data & 0x80) else (val | MOSI)
    # The pins are set up BEFORE the module is enabled.  P1 resets to 0xff, so a write
    # that drops SCK is a real falling edge: done after the enable it would be counted
    # as the first edge of the byte and every sample would land one transition late.
    # The enable samples the levels instead (write() takes pin_sck/pin_ss from the
    # pins), so an arrangement made while SPEN is clear invents no edge at all.
    code += [0x75, P1, val]              # SS asserted low, SCK at its idle level
    code += [0x75, SPCON, spcn_ & 0xff]
    sck = False
    for e in range(edges):
        if e == ss_release:
            code += [0x75, P1, (val | SS) | (SCK if sck else 0)]
            continue
        k = (e // 2) if cpha else ((e + 1) // 2)
        k = min(k, 7)
        val = (val & ~MOSI) | (MOSI if (data >> (7 - k)) & 1 else 0)
        sck = not sck
        code += [0x75, P1, val | (SCK if sck else 0)]
    code += sjmp()
    if with_isr:
        # P2 resets to 0xff, so the counter has to be zeroed or one entry reads back as
        # 0x00 and looks exactly like no interrupt at all.
        code += [0x75, IE, BM_EA, 0x75, IEN1, BM_ESPI]
        pad_to(code, SPI_VECTOR)
        code += isr()
    return image(code, name)


def leg_slave_mode():
    """Slave mode, where the clock arrives from outside.  Sixteen half periods carry
    eight samples; with CPHA=0 they are the transitions leaving the idle level, so the
    eighth sample is transition 15, and with CPHA=1 the transitions returning to it, so
    transition 16 (figures 16-4 and 16-5, printed p. 67)."""
    data = 0x5a                          # 0b01011010, MSB first
    for cpha, name in ((0, "CPHA=0"), (BM_CPHA, "CPHA=1")):
        spcn_ = BM_SPEN | cpha           # MSTR cleared -> slave
        p = slave_image(spcn_, 15, data, "sl-%s-15" % name)
        st, _, _ = probed(p, steps=90)
        want = 1 if not cpha else 0
        check("%s: the byte is not complete after 15 SCK edges (SPIF %d)" % (name, want),
              st is not None and st.get("SPIF") == want,
              "SPIF=%s edge=%s captures=%s" % (st.get("SPIF"), st.get("edge"),
                                               st.get("captures")) if st else "no element")
        p = slave_image(spcn_, 16, data, "sl-%s-16" % name)
        st, _, _ = probed(p, steps=100)
        check("%s: 16 edges complete the byte and it is received MSB-first as 0x%02x"
              % (name, data),
              st is not None and st.get("SPIF") == 1 and st.get("SPDAT") == data,
              "SPDAT=0x%02x want 0x%02x SPIF=%s edge=%s"
              % (st.get("SPDAT", 0), data, st.get("SPIF"), st.get("edge"))
              if st else "no element")

    # SSERR (section 16.3.3.4, printed p. 68): SS going high before the end of a received
    # byte aborts it, raises SSERR, requests nothing, and is cleared only by clearing SPEN.
    p = slave_image(BM_SPEN, 6, data, "sserr", ss_release=4, with_isr=True)
    st, marker, _ = probed(p, steps=40)
    check("SS released mid-byte sets SSERR",
          st is not None and st.get("SSERR") == 1,
          "SSERR=%s captures=%s edge=%s" % (st.get("SSERR"), st.get("captures"),
                                            st.get("edge")) if st else "no element")
    check("SSERR requests nothing (section 16.3.3.4) with EA and ESPI set",
          marker == 0, "marker=%s" % marker)
    check("an aborted byte is not counted as received",
          st is not None and st.get("bytes") == 0 and st.get("SPIF") == 0,
          "bytes=%s SPIF=%s" % (st.get("bytes"), st.get("SPIF")) if st else "no element")
    # same image with SPEN cleared afterwards: SSERR must go
    code = []
    code += [0x75, SPCON, BM_SPEN, 0x75, P1, 0xff & ~SS, 0x75, P1, 0xff]
    code += [0x75, SPCON, BM_SSDIS | BM_CPHA]      # SPEN cleared
    code += sjmp()
    p = image(code, "sserr-clear")
    st, _, _ = probed(p, steps=10)
    check("clearing SPEN clears SSERR (nothing else does)",
          st is not None and st.get("SSERR") == 0,
          "SSERR=%s" % (st.get("SSERR") if st else None))


def leg_deselected_slave_is_quiet():
    """'A high level on the SS pin puts the MISO line of a Slave SPI in a high-impedance
    state' (section 16.2.4, printed p. 64): a deselected slave must not answer the clock,
    so it neither counts edges nor sets SPIF nor loads a byte."""
    data = 0x5a
    code = [0x75, P1, 0xff, 0x75, SPCON, BM_SPEN]   # SS stays HIGH the whole time
    val, sck = 0xff, False
    for e in range(16):
        k = min((e + 1) // 2, 7)
        val = (val & ~MOSI) | (MOSI if (data >> (7 - k)) & 1 else 0)
        sck = not sck
        code += [0x75, P1, (val & ~SCK) | (SCK if sck else 0)]
    code += sjmp()
    p = image(code, "deselected")
    st, _, _ = probed(p, steps=90)
    check("a slave whose SS stays high ignores the clock: no edges, no SPIF, no byte",
          st is not None and st.get("edge") == 0 and st.get("SPIF") == 0
          and st.get("bytes") == 0,
          "edge=%s SPIF=%s bytes=%s" % (st.get("edge"), st.get("SPIF"), st.get("bytes"))
          if st else "no element")


def leg_reference_proof(ref):
    """The absence half, in the only shape that can be written honestly on this console.

    `info hardware' with no category and `info hw' with a category the CPU does not have
    both print nothing but the usage line, so a byte-for-byte diff over 'the hardware
    list' compares two identical usage strings and can never fail.  ident89c51r.py covers
    `info hw wdt' and `info hw pca' only, so it cannot see an SPI element leaking onto
    the AT89C51RB2/RC2 either.  What can be shown is (a) the element is absent under
    -t 89C51R on this tree, and (b) the same SPI-driving image is inert on that part:
    SPCON reads back what was written (no hardware clears SPEN or MSTR), SPSTA stays
    0x00 (no hardware sets SPIF), Port 1 never moves and the 004Bh vector is never
    entered.  Both are expected to hold on the pre-SPI control binary as well -- that is
    what makes the control useful."""
    a = run(image([0x00], "empty"), ["info hw spi"], device="89C51R", sim=SIM)
    b = run(image([0x00], "empty"), ["info hw spi"], device="89C51R", sim=ref)
    check("-t 89C51R on this tree reports no `spi' element, and its output is "
          "byte-identical to the pre-SPI control's (both are the bare usage line, which "
          "is why the behavioural checks below are the real guard)",
          "spi[" not in a and a == b,
          "equal=%s spi_line=%s" % (a == b, "spi[" in a))

    code = (d(P1, 0xff) + d(MARKER, 0) + d(SPCON, BM_SPEN | BM_MSTR | BM_CPHA)
            + d(IE, BM_EA) + d(IEN1, BM_ESPI) + ai(0x5a) + da(SPDAT) + sjmp())
    pad_to(code, SPI_VECTOR)
    code += isr()
    p = image(code, "absent-89c51r")

    def inert(sim):
        t = run(p, ["step 40", "info hw spi", "dump sfr 0xc3 0xc5",
                    "dump sfr 0x%02x 0x%02x" % (P1, P1),
                    "dump sfr 0x%02x 0x%02x" % (MARKER, MARKER)],
                device="89C51R", sim=sim)
        return spi_info(t), {a: dump_sfr(t, a) for a in (SPCON, SPSTA, SPDAT, P1, MARKER)}

    st, regs = inert(SIM)
    check("-t 89C51R here: the three addresses are inert -- writing SPCON leaves them "
          "reading 0x00 (the part has no register there at all, so nothing cleared "
          "SPEN and nothing set SPIF)",
          st is None and all(regs[a] in (None, 0) for a in (SPCON, SPSTA, SPDAT)),
          "spi_element=%s SPCON=%r SPSTA=%r SPDAT=%r" % (st is not None, regs[SPCON],
                                                         regs[SPSTA], regs[SPDAT]))
    check("-t 89C51R here: Port 1 never moved and the 004Bh routine never ran",
          regs[P1] == 0xff and regs[MARKER] == 0,
          "P1=0x%02x marker=0x%02x" % (regs[P1] if regs[P1] is not None else 0,
                                       regs[MARKER] or 0))
    st_ref, regs_ref = inert(ref)
    check("the pre-SPI control binary behaves identically -- this check is expected to "
          "PASS there too, it is the control for the two above",
          st_ref is None and regs_ref[SPSTA] == regs[SPSTA]
          and regs_ref[SPCON] == regs[SPCON] and regs_ref[P1] == regs[P1]
          and regs_ref[MARKER] == regs[MARKER],
          "ref SPCON=0x%02x SPSTA=0x%02x P1=0x%02x marker=0x%02x"
          % (regs_ref[SPCON] or 0, regs_ref[SPSTA] or 0, regs_ref[P1] or 0,
             regs_ref[MARKER] or 0))

    # Ownership half: on the AT89C51RD2 the same three addresses answer through a named
    # element that decodes them, which a binary without the model cannot say.
    p, _ = master_tx(spcn(0x00), 1, "own")
    st, _, _ = probed(p, steps=30)
    check("on the AT89C51RD2 those SFRs belong to the model: SPCON decodes to "
          "SPEN/MSTR/CPHA with BD=2 and the byte has transferred",
          st is not None and st.get("SPEN") == 1 and st.get("MSTR") == 1
          and st.get("CPHA") == 1 and st.get("BD") == 2 and st.get("bytes") == 1,
          st["text"].splitlines()[0] if st else "no spi element on the AT89C51RD2")


if __name__ == "__main__":
    print("SIM =", SIM)
    print("REF =", os.environ.get("S51_REF", "/tmp/s51-ref-b"))
    leg_rate_table()
    leg_x2_spix2()
    leg_no_clock_setting()
    leg_cpol()
    leg_cpha()
    leg_spif_and_receive()
    leg_flag_clearing()
    leg_wcol()
    leg_interrupt()
    leg_modf()
    leg_modf_rearm()
    leg_pins_are_shared()
    leg_spen_off_is_inert()
    leg_slave_mode()
    leg_deselected_slave_is_quiet()
    leg_reference_proof(os.environ.get("S51_REF", "/tmp/s51-ref-b"))
    print("\nChecks run: %d  Failed: %d" % (_checks, len(_failures)))
    if _failures:
        print("FAILURES: " + ", ".join(_failures))
        sys.exit(1)
    print("ALL SPI CHECKS PASSED")
