#!/usr/bin/env python3
"""Interrupt arbitration on the AT89C51RD2.

D1 (the first eight cases): the STC12 PCA interrupt sources must not exist on the
AT89C51RD2.

interrupt.cc registers three STC-only sources (PCA_CCF0, PCA_CCF1, PCA_CF) at vector
0x003B, enabled by CCAPMn bit 0 and CMOD.ECF, because the STC12 has no global PCA enable
bit in IE.  On the AT89C51RD2 0x003B is the keyboard vector (doc4235 table 17-2) and the
PCA is enabled by IEN0.EC with vector 0x0033, which pca.cc models itself.  Registering
the STC sources unconditionally therefore let a set CCAPM0 bit 0 plus CCON.CCF0 vector a
RD2 program to the keyboard address with EC clear.  The block is now gated on CPU_STC12.

The behavioural images differ only in IE bit 6 (IEN0.EC):

    MOV CCAPM0,#0x01    STC: ECCF0 enable.  Atmel: MAT, harmless without ECOM
    MOV CMOD,#0x01      STC: ECF (counter overflow interrupt enable)
    MOV IE,#0x80        EA only  (0xC0 in the second image: EA + EC)
    MOV CCON,#0x41      CR = 1 and CCF0 = 1, i.e. a PCA request is pending
    SJMP $

with ISRs planted at both candidate vectors, each writing its own address into P1:

    0x0033: MOV P1,#0x33 ; RETI     PCA, per doc4235
    0x003B: MOV P1,#0x5B ; RETI     keyboard, per doc4235

So EC clear must leave P1 at 0xff (nothing vectors) and EC set must give 0x33.

The rest of the file covers the priority registers (doc4235K section 17, pp.72-79).
Table 17-1 (p.73) makes every source's level two bits wide: the L bit in its IPLx
register is the +1 bit, the H bit in its IPHx register is the +2 bit, giving levels 0
(lowest) to 3 (highest).  Section 17.1 adds the two arbitration rules -- a request of a
higher level wins over a simultaneously pending one of a lower level, and within one
level the polling sequence of table 17-2 decides; a running handler yields only to a
strictly higher level.  IPL0/IPH0 (0xB8/0xB7) carry INT0, T0, INT1, T1, the UART, T2 and
the PCA; IPL1/IPH1 (0xB2/0xB3, tables 17-7 and 17-8, pp.78-79) carry the same four
levels for the keyboard (bit 0) and the SPI (bit 2), with bit 1 reserved -- `TWIL' is
the only trace of a TWI peripheral in the whole datasheet and this part has none.

Two of those rules were not honoured: `cl_51core::priority_of()` read IP alone, so the
whole H half of the pair was inert and every level was 0 or 1, and `do_interrupt()`
accepted the first pending source in polling order whatever its level, so a low-level
source listed early took a vector that a higher-level pending source should have had.
The arbitration images set both request flags from software -- TCON.TF1 for timer 1,
CCON.CCF0 for the PCA -- and every case prints the vector that was actually entered by
writing its own marker into P1 (PC alone cannot tell two handlers apart when both spin
in place).  Timer 1 is polled ahead of the PCA, so an equal level must leave the vector
with timer 1 and only a higher PCA level may take it away.

The keyboard and the SPI have no interrupt source in the model yet, so the IPL1/IPH1
pair has nothing to arbitrate: the legs below pin the two registers (reset value,
readback) and pin that the pair is inert for every source that *does* exist, by running
the arbitration cases again with both registers loaded with 0xff.  Bit 0 of IPL1 sits on
the same bit number as PX0L and bit 2 on PX1L, so a pair that leaked into the IPL0
namespace would show up immediately in the `Pr' column and in the winner.  What cannot
be tested until cl_it_src objects for the keyboard and the SPI exist is listed in
/tmp/irq-report.md.

The fix is gated on CPU_89C51RD2 alone: the AT89C51RB2/RC2 (SIMULATION-PLAN.md D13) and
the plain 8052 keep the two-level answer, which is what cases 29-32 pin.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = (os.environ.get("S51_SIM") or os.environ.get("S51") or
       os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51"))
IE, CCON, CMOD, CCAPM0, P1 = 0xA8, 0xD8, 0xD9, 0xDA, 0x90
SJMP = bytes([0x80, 0xFE])
RETI = bytes([0x32])
EC = 0x40                       # IEN0.EC: doc4235 IEN0 = EA EC ET2 ES ET1 EX1 ET0 EX0
IP, IPH, IPL1, IPH1, TCON = 0xB8, 0xB7, 0xB2, 0xB3, 0x88   # tables 17-4/17-5/17-7/17-8
# IEN0 bits (table 17-4)
EA, ET2, ES, ET1, EX1, ET0, EX0 = 0x80, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01
# IPL0/IPH0 bit positions (identical in both registers: tables 17-4 and 17-5)
PX0, PT0, PX1, PT1, PSL, PT2H = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
PPCL = 0x40                     # PCA: PPCL in IPL0, PPCH in IPH0
TF1, CR, CCF0 = 0x80, 0x40, 0x01        # TCON.TF1, CCON.CR, CCON.CCF0


def pre(pairs):
    b = bytearray()
    for a, v in pairs:
        b += bytes([0x75, a, v])
    return bytes(b)


def image(ie):
    main = pre([(CCAPM0, 0x01), (CMOD, 0x01), (IE, ie), (CCON, 0x41)]) + SJMP
    isr33 = pre([(P1, 0x33)]) + RETI
    isr3b = pre([(P1, 0x5B)]) + RETI
    return main.ljust(0x33, b"\x00") + isr33 + b"\x00" * (0x3b - 0x36) + isr3b


def sim(cmds, chip):
    p = subprocess.run([SIM, "-t", chip, "-c", "-"], input="\n".join(cmds + ["exit"]) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=300)
    return p.stdout + p.stderr


def sources(chip):
    """(vector, name) for every line `info hardware irq` reports."""
    out = sim(["info hardware irq"], chip)
    return [(int(m.group(1), 16), m.group(2))
            for m in re.finditer(r"^  0x([0-9a-f]{6}) \S+\s+\d+ \S+\s+\S+\s+(.+)$",
                                 out, re.M)]


def run(ie, steps=60):
    img = "/tmp/irc_%02x_%d.hex" % (ie, steps)
    write_hex(img, image(ie))
    out = sim(['file "%s"' % img, "step %d" % steps, "dump sfr[0x90]",
               "dump sfr[0xd8]", "dump sfr[0xa8]"], "89C51R")
    v = {}
    for a in (P1, CCON, IE):
        m = re.search(r"^0x%02x \S+:\s+\S+\s+(0x[0-9a-f]+)" % a, out, re.M)
        v[a] = m.group(1) if m else None
    return v


def cell(addr, chip, cmds):
    """Value of one SFR at the end of `cmds': dumps it, then reads it back."""
    out = sim(list(cmds) + ["dump sfr[0x%02x]" % addr], chip)
    m = re.search(r"^0x%02x \S+:\s+\S+\s+(0x[0-9a-f]+)" % addr, out, re.M)
    return m.group(1) if m else None


def levels(chip, ip=0, iph=0, ipl1=0, iph1=0):
    """The `Pr' column of `info hardware irq' after programming all four registers."""
    img = pre([(IP, ip), (IPH, iph), (IPL1, ipl1), (IPH1, iph1)]) + SJMP
    path = "/tmp/irc_levels_%s_%02x_%02x_%02x_%02x.hex" % (chip, ip, iph, ipl1, iph1)
    write_hex(path, img)
    out = sim(['file "%s"' % path, "step 20", "info hardware irq"], chip)
    pr = {}
    for m in re.finditer(r"^  0x([0-9a-f]{6}) \S+\s+(\d+) \S+\s+\S+\s+(.+)$", out, re.M):
        pr.setdefault(m.group(3).strip(), int(m.group(2)))
    return pr


# table 17-2 order, so the tuple reads in polling order
LADDER = [("external #0", "int0"), ("timer #0", "t0"), ("external #1", "int1"),
          ("timer #1", "t1"), ("serial receive", "uart"), ("timer #2 TF2", "t2"),
          ("PCA counter", "pca")]


def ladder(chip, **kw):
    """Level of each of the seven IPL0/IPH0 sources, missing sources as None."""
    pr = levels(chip, **kw)
    return tuple(pr.get(name) for name, _ in LADDER)


def nonzero(levels_map):
    return sorted({v for v in levels_map.values() if v})


def arbiter(chip, ip=0, iph=0, ipl1=0, iph1=0, preempt=False, steps=80):
    """P1 after timer 1 and the PCA compete, both request flags raised by software.

    preempt=False raises TCON.TF1 and CCON.CCF0 together with both enables, so the leg
    measures which of the two pending requests gets the vector.  preempt=False with
    equal levels must leave it with timer 1, which table 17-2 polls first (it_sources
    order), and only a strictly higher PCA level may take it.

    preempt=False raises only TF1; the timer 1 ISR enables EC and sets CCF0 while it
    spins, so the leg measures whether the pending PCA preempts a running handler.
    """
    main = pre([(IP, ip), (IPH, iph), (IPL1, ipl1), (IPH1, iph1),
                (CCON, CR if preempt else (CR | CCF0)),
                (TCON, TF1),
                (IE, EA | ET1 | (0 if preempt else EC))]) + SJMP
    isr1b = pre([(P1, 0x1b)])
    if preempt:
        isr1b += pre([(IE, EA | ET1 | EC), (CCON, CR | CCF0)])
    img = main.ljust(0x1b, b"\x00") + isr1b + SJMP
    img = img.ljust(0x33, b"\x00") + pre([(P1, 0x33)]) + SJMP
    path = "/tmp/irc_arb_%s_%02x_%02x_%02x_%02x_%d.hex" % (chip, ip, iph, ipl1, iph1,
                                                           preempt)
    write_hex(path, img)
    return cell(P1, chip, ['file "%s"' % path, "step %d" % steps])


def t0_t1(chip, ip=0, iph=0, steps=80):
    """Timer 0 and timer 1 pending at once: which one gets the vector."""
    main = pre([(IP, ip), (IPH, iph),
                (TCON, 0x20 | TF1), (IE, EA | ET0 | ET1)]) + SJMP
    img = main.ljust(0x0b, b"\x00") + pre([(P1, 0x0b)]) + SJMP
    img = img.ljust(0x1b, b"\x00") + pre([(P1, 0x1b)]) + SJMP
    path = "/tmp/irc_t0t1_%s_%02x_%02x.hex" % (chip, ip, iph)
    write_hex(path, img)
    return cell(P1, chip, ['file "%s"' % path, "step %d" % steps])


def default_order(chip):
    """Winners with IP and IPH0 at their reset value.

    This is the shape of every program in t/ that never touches IPH0, so all three
    markers must be identical on a binary without the level fix (/tmp/s51-ref-b) --
    the level fix may only reorder sources somebody actually gave a level to.
    """
    img = pre([(CCON, CR | CCF0), (IE, EA | EC)]) + SJMP
    img = img.ljust(0x33, b"\x00") + pre([(P1, 0x33)]) + SJMP
    path = "/tmp/irc_pcaonly_%s.hex" % chip
    write_hex(path, img)
    return (arbiter(chip), t0_t1(chip), cell(P1, chip, ['file "%s"' % path, "step 40"]))


def pair_regs(chip, steps=20):
    """Reset value of IPL1/IPH1, and what they hold after a write."""
    reset = {a: cell(a, chip, ["step 1"]) for a in (IPL1, IPH1)}
    img = pre([(IPL1, 0x05), (IPH1, 0xF5)]) + SJMP
    path = "/tmp/irc_pairrw_%s.hex" % chip
    write_hex(path, img)
    written = {a: cell(a, chip, ['file "%s"' % path, "step %d" % steps])
               for a in (IPL1, IPH1)}
    return reset, written


results = []


def case(name, expect, got, unit, note=""):
    ok = expect == got
    results.append((name, expect, got, ok, note, unit))


r51 = sources("89C51R")
rstc = sources("STC12")
case("89C51R sources", 14, len(r51), "count", "was 17 with the 3 STC rows")
case("89C51R rows at 0x3b", 0, len([s for s in r51 if s[0] == 0x3b]), "count",
     "that vector is the keyboard on this part")
case("89C51R rows at 0x33", 6, len([s for s in r51 if s[0] == 0x33]), "count",
     "PCA counter + 5 modules, from pca.cc")
case("STC12 rows at 0x3b", 3, len([s for s in rstc if s[0] == 0x3b]), "count",
     "gate must not reach the STC parts")
case("STC12 sources", 15, len(rstc), "count", "unchanged by the gate")

off = run(0x80)
on = run(0x80 | EC)
case("EC clear: no vector", "0xff", off[P1], "P1", "CCF0 pending, EA set")
case("EC clear: request pending", "0x41", off[CCON], "CCON",
     "the row above is not vacuous")
case("EC set: vectors to 0x33", "0x33", on[P1], "P1", "same image, one bit different")

# --- table 17-1: the two bits of the IPL0/IPH0 pair ----------------------------
case("RD2 level 00", (0,) * 7, ladder("89C51RD2"), "level",
     "control: the old one-bit code answers this too")
case("RD2 level 01", (1,) * 7, ladder("89C51RD2", ip=0x7F), "level",
     "L bit only")
case("RD2 level 10", (2,) * 7, ladder("89C51RD2", iph=0x7F), "level",
     "H bit alone: IPH0 was inert before the fix")
case("RD2 level 11", (3,) * 7, ladder("89C51RD2", ip=0x7F, iph=0x7F), "level",
     "both bits of every source")
case("RD2 per source", (1, 2, 1, 2, 1, 2, 0),
     ladder("89C51RD2", ip=PX0 | PX1 | PSL, iph=PT0 | PT1 | PT2H), "level",
     "IP=15h IPH=2Ah: each source decodes its own pair")
case("RD2 IPH0 decodes", [2], nonzero(levels("89C51RD2", iph=0x7F)), "levels",
     "the H half of the pair moves the Pr column")

# --- section 17.1, on the wire: which source gets the vector -------------------
# With IP and IPH0 left alone -- the state every program that never touches IPH0
# runs in -- the arbitration order must be exactly the order the model had before
# the level fix.  /tmp/s51-ref-b reports the same three markers for this leg.
case("RD2 default order", ("0x1b", "0x0b", "0x33"), default_order("89C51RD2"), "P1",
     "IP=0 IPH0=0: t1 over pca by poll order, t0 over t1, pca alone vectors")
case("RD2 tie at level 1", "0x1b", arbiter("89C51RD2", ip=PT1 | PPCL), "P1",
     "equal levels never flip the winner")
case("RD2 L1 vs L2", "0x33", arbiter("89C51RD2", ip=PT1, iph=PPCL), "P1",
     "one IPH0 bit takes the vector from the earlier-polled source")
case("RD2 L0 vs L3", "0x33", arbiter("89C51RD2", ip=PPCL, iph=PPCL), "P1",
     "pca at 3, timer 1 at 0")
case("RD2 tie at level 2", "0x1b", arbiter("89C51RD2", iph=PT1 | PPCL), "P1",
     "both at 2: still the polling order")
case("RD2 L2 vs L3", "0x33", arbiter("89C51RD2", ip=PPCL, iph=PT1 | PPCL), "P1",
     "timer 1 at 2 loses to the pca at 3")

# --- section 17.1: what may interrupt a running handler ------------------------
case("RD2 default no preempt", "0x1b", arbiter("89C51RD2", preempt=True), "P1",
     "IP=0 IPH0=0: a level 0 handler still runs to the spin (ref-b same)")
case("RD2 preempt into L1", "0x33",
     arbiter("89C51RD2", ip=PT1, iph=PPCL, preempt=True), "P1",
     "a low-priority handler yields to a higher one")
case("RD2 same level never", "0x1b",
     arbiter("89C51RD2", ip=PT1 | PPCL, preempt=True), "P1",
     "control: equal level must not preempt")
case("RD2 L3 preempts L2", "0x33",
     arbiter("89C51RD2", ip=PPCL, iph=PT1 | PPCL, preempt=True), "P1",
     "handler at 2, request at 3")
case("RD2 L2 never L3", "0x1b",
     arbiter("89C51RD2", ip=PT1, iph=PT1 | PPCL, preempt=True), "P1",
     "section 17.1: the top level is interrupted by nothing")

# --- IPL1/IPH1 must not reach the IPL0 namespace -------------------------------
case("RD2 pair1 ff: levels", (0,) * 7,
     ladder("89C51RD2", ipl1=0xFF, iph1=0xFF), "level",
     "KBDL sits on PX0L's bit number, SPIL on PX1L's: no leak")
case("RD2 pair1 ff: L1 vs L2", "0x33",
     arbiter("89C51RD2", ip=PT1, iph=PPCL, ipl1=0xFF, iph1=0xFF), "P1",
     "the pair must not promote timer 1 (bit 3 of the junk)")
case("RD2 pair1 05: no preempt", "0x1b",
     arbiter("89C51RD2", ip=PT1 | PPCL, ipl1=0x05, iph1=0x05, preempt=True), "P1",
     "KBDL/SPIL/KBDH/SPIH set, two equal levels: nothing happens")

# --- the pair itself: registers exist, nothing arbitrates them yet -------------
_rst, _wr = pair_regs("89C51RD2")
case("IPL1 reset", "0x00", _rst[IPL1], "IPL1", "table 17-7: XXXX X000b")
case("IPH1 reset", "0x00", _rst[IPH1], "IPH1", "table 17-8: XXXX X000b")
case("IPL1 holds 05h", "0x05", _wr[IPL1], "IPL1", "KBDL+SPIL, bit 1 (TWIL) clear")
case("IPH1 holds f5h", "0xf5", _wr[IPH1], "IPH1",
     "reserved bits read back what was written; no arbitration consumer exists")

# --- the parts the fix must not touch -----------------------------------------
case("89C51R L1 vs L2", "0x1b", arbiter("89C51R", ip=PT1, iph=PPCL), "P1",
     "D13: the RB2/RC2 stays on the old one-bit answer")
case("89C51R L1 never preempted", "0x1b",
     arbiter("89C51R", ip=PT1, iph=PPCL, preempt=True), "P1", "same gate")
case("89C51R IPH0 inert", [], nonzero(levels("89C51R", iph=0x7F)), "levels",
     "no H-bit decoding on that part")
case("8052 IPH0 inert", [], nonzero(levels("8052", iph=0x7F)), "levels",
     "nor on the plain 52")
case("8052 tie stays polling", "0x0b", t0_t1("8052", ip=PT0, iph=PT1), "P1",
     "level 0 timer 0 outranks a level 2 timer 1 that cannot exist there")


# --- the keyboard on the SECOND priority pair --------------------------------------
# Everything above this line runs unchanged with the keyboard present: `ladder()' reads
# seven named rows and the keyboard is an eighth, and no image above presses a key
# (KBLS resets to 0 and P1 to 0xff, and a low-detect line on a high pin is not a key),
# which is why the 37 checks that were here still say what they said.
#
# Timer 1 is the first-pair source these legs arbitrate against (PT1/PT1H, IP/IPH bit
# 3), because the keyboard takes its level from IPL1/IPH1 bit 0 (KBDL/KBDH, tables
# 17-7/17-8 pp.78-79) and its enable from IEN1 bit 0 (table 17-6 p.77): a source on the
# other pair is the only thing that can exercise the pair-1 arm of priority_of().
#
# P2 (0xA0) is the marker port here, not P1: P1 is the keyboard's input, so writing a
# marker there would itself change which source is requesting.  P4 (0xC0) is the obvious
# second choice but `dump sfr[0xc0]' carries no name on the AT89C51RB2/RC2 and the last
# case below runs on that part.  Ports reset to 0xff, so that is what `nothing vectored'
# looks like: 0x1b timer 1, 0x3b keyboard, 0xff nothing.
#
# main() has to fit in the 27 bytes between 0x00 and the ISR planted at 0x1b, which is
# why the level registers are written only when they carry something: IP, IPH, IPL1,
# IPH1, KBLS and the ports already reset to the level-0 / no-key state.  A first draft
# that wrote all twelve registers ran to 0x27 -- past 0x1b -- so ljust() padded nothing,
# both ISRs landed behind main, and every case below `measured' that the vector was
# never taken.  A setup that overruns its own vector reports the absence of a fix as the
# fix, so the assert is what makes that shape loud instead of plausible.
IEN1, KBLS, KBE, P2 = 0xB1, 0x9C, 0x9D, 0xA0
KBDEN = 0x01                    # IEN1.KBD, and IPL1.KBDL / IPH1.KBDH: bit 0 in all three


def kbd_t1(kbd=0, t1=0, junk=0, steps=90, chip="89C51RD2"):
    """The keyboard (level `kbd') and timer 1 (level `t1') pending at one instant.

    The press and TCON.TF1 are raised while EA is clear -- EA resets to 0 and is written
    last -- so neither request gets an instruction boundary to itself: they compete,
    which is the situation section 17.1 (p.73) describes.  `junk' is ORed into IPL1 and
    IPH1 to exercise the Reserved bit 7 of the pair.  Levels: L is +1, H is +2.  Returns
    the marker of whoever took the vector.
    """
    pairs = [(IP, PT1)] if t1 & 1 else []
    pairs += [(IPH, PT1)] if t1 & 2 else []
    pairs += [(IPL1, (kbd & 1) | junk), (IPH1, ((kbd >> 1) & 1) | junk),
              (KBE, KBDEN), (IEN1, KBDEN),
              (P1, 0xFE),                        # P1.0 low: a key for a low-detect line
              (TCON, TF1), (IE, EA | ET1)]
    main = pre(pairs) + SJMP
    assert len(main) <= 0x1b, "setup overruns the vector at 0x1b: %d bytes" % len(main)
    img = main.ljust(0x1b, b"\x00") + pre([(P2, 0x1b)]) + SJMP
    img = img.ljust(0x3b, b"\x00") + pre([(P2, 0x3b)]) + SJMP
    path = "/tmp/irc_kbd_%s_%d_%d_%02x.hex" % (chip, kbd, t1, junk)
    write_hex(path, img)
    return cell(P2, chip, ['file "%s"' % path, "step %d" % steps])


case("KBD pair: rows at 0x3b", 1,
     len([s for s in sources("89C51RD2") if s[0] == 0x3b]), "count",
     "one source on the keyboard vector; 89C51R keeps 0 (case 2 above)")
_rd2 = sources("89C51RD2")
_added = sorted(s for s in _rd2 if s not in r51)
case("sources added over the 89C51R",
     sorted([(0x3b, "keyboard"), (0x4b, "spi receiver/error"), (0x4b, "spi transmitter")]),
     _added, "set",
     "the keyboard's own vector plus the SPI pair on SPIIT 0x4b (two rows on one flag, the "
     "same idiom as the serial pair on 0x23 and the two timer 2 rows on 0x2b); names, not a "
     "count, so the next interrupt source has to say what it is rather than move a number")
_vecs = []
for _v, _n in _rd2:
    if _v not in _vecs:
        _vecs.append(_v)
case("poll order is table 17-2", [0x03, 0x0b, 0x13, 0x1b, 0x33, 0x23, 0x2b, 0x3b, 0x4b],
     _vecs, "vectors",
     "printed p.73 polling priorities INT0 1, T0 2, INT1 3, T1 4, PCA 5, UART 6, T2 7, "
     "Keyboard 8, (0043h unused) 9, SPI 10 -- the model registers in exactly that order, and "
     "it is what decides a tie inside one priority level. The keyboard stopped being last when "
     "the SPI landed; these two legs went red at that cutover and the floor hid it (see "
     "SIMULATION-PLAN.md section 5)")

case("KBD vs t1 both L0", "0x1b", kbd_t1(), "P2",
     "equal levels: table 17-2 polls timer 1 (4) before the keyboard (8)")
case("KBD L1 vs t1 L0", "0x3b", kbd_t1(kbd=1), "P2",
     "IPL1.0 alone gives the keyboard the vector: the second pair is live")
case("KBD L1 vs t1 L1", "0x1b", kbd_t1(kbd=1, t1=1), "P2",
     "one pair per source, equal levels, polling order decides")
case("KBD L2 vs t1 L1", "0x3b", kbd_t1(kbd=2, t1=1), "P2",
     "IPH1.0 (+2) outranks PT1 (+1) across the two pairs")
case("KBD L3 vs t1 L2", "0x3b", kbd_t1(kbd=3, t1=2), "P2",
     "both bits of the pair, against a level-2 timer 1")
case("KBD L0 vs t1 L1", "0x1b", kbd_t1(t1=1), "P2",
     "a level-1 timer 1 keeps the vector against an unlevelled keyboard")
case("KBD pair1 bit7 only: tie", "0x1b", kbd_t1(junk=0x80), "P2",
     "TRAVELS WITH THE uc51.cc HUNK: IPL1.7/IPH1.7 are Reserved (tables 17-7/17-8) and "
     "IP_PAIR1 must be stripped from the mask before it meets them, or this answers "
     "0x3b -- the keyboard at a level nobody gave it takes it from timer 1")
case("KBD Pr from its own pair", 3,
     levels("89C51RD2", ipl1=0x01, iph1=0x01).get("keyboard"), "level",
     "KBDL+KBDH -> Pr 3, the same four levels as the first pair")
case("KBD Pr ignores pair1 bit7", 0,
     levels("89C51RD2", ipl1=0x80, iph1=0x80).get("keyboard"), "level",
     "the strip again, on the Pr column rather than on the winner")

case("89C51R: no keyboard arbitration", "0x1b",
     kbd_t1(chip="89C51R"), "P2",
     "D13: same image on the RB2/RC2, which has no keyboard -- IEN1.0 and KBE are inert "
     "there, so timer 1 keeps the vector; 0x3b would be the pre-gating bug")

print("%-34s %22s %22s %6s  %s" % ("case", "expected", "measured", "unit", "verdict"))
bad = 0
for name, expect, got, ok, note, unit in results:
    bad += 0 if ok else 1
    print("%-34s %22s %22s %6s  %s  %s" % (name, expect, got, unit,
                                           "PASS" if ok else "FAIL", note))
print("\n%s: %d/%d" % ("PASS" if not bad else "FAIL", len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
