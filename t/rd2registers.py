#!/usr/bin/env python3
"""Phase 1 steps 7-9: the SFRs the AT89C51RD2/ED2 has and the name table did not know.

Three things are gated here, all of them from doc4235 Tables 3-2..3-11:

  (a) every new SFR name resolves at its documented address under -t 89C51RD2
      (and -t 89C51ED2, which selects the same CPU flag) -- in `dump sfr`, in
      `dump bits` and in the instruction trace;
  (b) -t 89C51R and -t 251 gain NONE of those names: the rows are gated on
      CPU_89C51RD2 alone, not on CPU_ALL_89C51R;
  (c) the Table 3-11 reset values on the RD2 path, with every bit the table
      leaves as X masked out, both at cold start and after a `reset`.

The console echoes a piped stdin into the same stream as its results and the two
interleave, so a `dump` row can begin anywhere on a line: every match here keys on
the adjacency of an address and its name (or of two tokens that the command echo
cannot produce), never on the start of a line.  Each `step` leg gets its own
session, because a session only reports the block of the step it last finished.

Run against another build with S51=<path>: on /tmp/s51-ref (the build before
steps 7-9) the RD2 legs must FAIL, which is what proves the coverage is new.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = (os.environ.get("S51_SIM") or os.environ.get("S51") or
       os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51"))

RD2_CHIPS = ["89C51RD2", "89C51ED2"]
CONTROL_CHIPS = ["89C51R", "251"]
# a control run has to prove it really is the device it claims to be; the 251
# carries no AUXR row, so the probe list is per chip
FAMILY_PROBES = {"89C51R": [(0x8e, "AUXR"), (0xd8, "CCON")],
                 "251": [(0xd8, "CCON"), (0xd0, "PSW")]}

# New sfr-space names, (addr, name).  Addresses are doc4235 Tables 3-2..3-11.
# P5 is byte accessible at 0xc7 and bit accessible at 0xe8, and cl_var_list::add()
# deletes any var that already carries the name, so one name cannot sit at two
# addresses: 0xe8 is labelled P5_BIT.
SFR_NAMES = [
  (0x97, "CKRL"),
  (0x9a, "BRL"),
  (0x9b, "BDRCON"),
  (0x9c, "KBLS"),
  (0x9d, "KBE"),
  (0x9e, "KBF"),
  (0xa7, "WDTPRG"),
  (0xb1, "IEN1"),
  (0xb2, "IPL1"),
  (0xb3, "IPH1"),
  (0xc0, "P4"),
  (0xc3, "SPCON"),
  (0xc4, "SPSTA"),
  (0xc5, "SPDAT"),
  (0xc7, "P5"),
  (0xd1, "FCON"),
  (0xd2, "EECON"),
  (0xe8, "P5_BIT"),
]
# New bit-space names.  P4 (0xc0) and P5 (0xe8) are the only new registers with
# addressable bits: a bit address names the latch of the multiple-of-8 byte it sits
# in, so IEN1/IPL1/IPH1 (0xb1..0xb3), KBLS/KBE/KBF (0x9c..0x9e), SPCON/SPSTA
# (0xc3/0xc4), BDRCON (0x9b) and FCON (0xd1) have no bit names -- their enable and
# flag bits (ESPI, KBD, SPIF, ...) live inside the byte register.
BIT_NAMES = ([(0xc0 + i, "P4.%d" % i) for i in range(8)] +
             [(0xe8 + i, "P5.%d" % i) for i in range(8)])
ALL_NAMES = [n for _, n in SFR_NAMES] + [n for _, n in BIT_NAMES]

# Table 3-11 reset values, masked over the bits the table leaves as X.
# (addr, name, expected, mask, note)
RESET = [
  (0x97, "CKRL",   0xff, 0xff, "prescaler bypass: FFh = Fosc/2"),
  (0x9a, "BRL",    0x00, 0xff, "slowest reload"),
  (0x9b, "BDRCON", 0x00, 0x1f, "XXX0 0000b: 7:5 X, BRG stopped"),
  (0x9c, "KBLS",   0x00, 0xff, "every key senses a low level"),
  (0x9d, "KBE",    0x00, 0xff, "no key may request the interrupt"),
  (0x9e, "KBF",    0x00, 0xff, "read-only flags clear"),
  (0xa7, "WDTPRG", 0x00, 0x07, "XXXX X000b: 7:3 X, base 2^14 period"),
  (0xb1, "IEN1",   0x00, 0x07, "XXXX X000b: ESPI and KBD clear"),
  (0xb2, "IPL1",   0x00, 0x07, "XXXX X000b: SPIL and KBDL clear"),
  (0xb3, "IPH1",   0x00, 0x07, "XXXX X000b: SPIH and KBDH clear"),
  (0xc0, "P4",     0xff, 0xff, "pull-ups, like P0..P3"),
  (0xc3, "SPCON",  0x14, 0xff, "0001 0100b: SSDIS=1 CPHA=1, SPI off"),
  (0xc4, "SPSTA",  0x00, 0xff, "0000 0000b (Table 16-4 says 00X0 XXXXb)"),
  (0xc5, "SPDAT",  0x00, 0xff, "indeterminate in the datasheet; model reads 0"),
  (0xc7, "P5",     0xff, 0xff, "byte access latch, pull-ups"),
  (0xe8, "P5_BIT", 0xff, 0xff, "bit access address of the same latch"),
  (0xd1, "FCON",   0x00, 0x0f, "XXXX 0000b: 7:4 X, 3:0 read 0"),
  (0xd2, "EECON",  0x00, 0x03, "xxxx xx00b: 7:2 X, EEE and EEBUSY clear"),
  (0xa2, "AUXR1",  0x00, 0x85, "0XXX X0X0b: bits 7, 2, 0 definite 0"),
  (0xd9, "CMOD",   0x00, 0xc7, "00XX X000b: CIDL and WDTE clear, CPSx X"),
  (0xd8, "CCON",   0x00, 0xef, "00X0 0000b: CF, CR, CCFn clear, bit 5 X"),
  (0xda, "CCAPM0", 0x00, 0x7f, "X000 0000b: ECOM clear"),
  (0xdb, "CCAPM1", 0x00, 0x7f, "X000 0000b: ECOM clear"),
  (0xdc, "CCAPM2", 0x00, 0x7f, "X000 0000b: ECOM clear"),
  (0xdd, "CCAPM3", 0x00, 0x7f, "X000 0000b: ECOM clear"),
  (0xde, "CCAPM4", 0x00, 0x7f, "X000 0000b: ECOM clear"),
  (0xc9, "T2MOD",  0x00, 0x03, "XXXX XX00b: T2OE and C/T2 clear"),
  (0x87, "PCON",   0x10, 0xdf, "00X1 0000b: POF set by hardware at power-on, bit 5 reserved"),
]

results = []


def case(name, expect, got, ok):
    results.append((name, expect, got, ok))


def sim(cmds, chip):
    """One console session.  Commands go in on a pipe and the console echoes them into
    the same stream as its results, so the two interleave: never anchor a row to the
    start of a line, match the adjacency of an address and its name instead."""
    p = subprocess.run([SIM, "-q", "-b", "-t", chip, "-c", "-"],
                       input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, timeout=60)
    return p.stdout + p.stderr


def bit_row(out, name):
    """Value of a bit-space name; the bits dump prints it indented under its address."""
    m = re.search(r"\s%s:\s+0b([01])\s+0x([0-9a-f]{2})" % re.escape(name), out)
    return int(m.group(1)) if m else None


def row(out, addr, name):
    """Value of `addr` on the row that names it `name`, or None."""
    m = re.search(r"0x%02x %s:\s+0b[01]+\s+0x([0-9a-f]{2})" % (addr, re.escape(name)), out)
    return int(m.group(1), 16) if m else None


def first_row(out, addr):
    """(name or None, value or None) of the dump row that reports `addr`."""
    m = re.search(r"0x%02x ([A-Za-z][A-Za-z0-9_/.]*):\s+0b[01]+\s+0x([0-9a-f]{2})" % addr, out)
    if m:
        return (m.group(1), int(m.group(2), 16))
    m = re.search(r"0x%02x\s+([0-9a-f]{2}) \.", out)
    if m:
        return (None, int(m.group(1), 16))
    return (None, None)


# ----------------------------------------------------------- images for the legs
# MOV A,#v ; MOV <sfr>,A four times.  Stepping 1,2,2,2 stops with the PC on each of
# the four writes, so its disassembly has to name SPCON, P5, IEN1 and CKRL.
NAME_CODE = (bytes([0x74, 0x55, 0xF5, 0xC3, 0x74, 0xAA, 0xF5, 0xC7,
                    0x74, 0x0F, 0xF5, 0xB1, 0x74, 0x3C, 0xF5, 0x97]) +
             bytes([0x80, 0xFE]))
write_hex("/tmp/rd2reg_name.hex", NAME_CODE)
# MOV P3,#0 ; MOV IEN1,#0FFh : the 0xb0 bit band belongs to P3, so bit address 0xb1
# has to stay 0 while IEN1 -- including its ESPI and KBD bits -- is all ones.
BITBAND_CODE = (bytes([0x75, 0xB0, 0x00, 0x75, 0xB1, 0xFF]) + bytes([0x90, 0, 0, 0xE4, 0xF0]))
write_hex("/tmp/rd2reg_bitband.hex", BITBAND_CODE)

for chip in RD2_CHIPS:
    # ------------------------------------------------ (a) names resolve as documented
    cmds = ['file "/tmp/rd2reg_name.hex"']
    cmds += ['dump sfr 0x%02x 0x%02x' % (a, a) for a, _ in SFR_NAMES]
    out = sim(cmds, chip)
    for addr, name in SFR_NAMES:
        got, val = first_row(out, addr)
        case("%s: sfr 0x%02x is %s" % (chip, addr, name), name, got, got == name)
        if val is None:
            case("%s: sfr 0x%02x readable" % (chip, addr), "value", "no row", False)

    cmds = ['file "/tmp/rd2reg_name.hex"']
    cmds += ['dump bits 0x%02x 0x%02x' % (a, a) for a, _ in BIT_NAMES]
    out = sim(cmds, chip)
    for addr, name in BIT_NAMES:
        val = bit_row(out, name)
        case("%s: bit 0x%02x is %s" % (chip, addr, name), name,
             "absent" if val is None else name, val is not None)

    # The disassembly has to print the new names instead of a bare direct operand.
    # Each step stops on one of the four writes, and each needs its own session from
    # reset: a session only reports the block of its last step.
    traced = ""
    for n in (1, 3, 5, 7):
        traced += sim(['file "/tmp/rd2reg_name.hex"', "step %d" % n], chip)
    for addr, name in [(0xc3, "SPCON"), (0xc7, "P5"), (0xb1, "IEN1"), (0x97, "CKRL")]:
        want = "0x%02x <%s>,A" % (addr, name)
        case("%s: trace names 0x%02x as %s" % (chip, addr, name), want,
             "named" if want in traced else "bare direct operand", want in traced)

    # ------------------------------------------------------- (c) Table 3-11 reset state
    cold = ['file "/tmp/rd2reg_name.hex"']
    cold += ['dump sfr 0x%02x 0x%02x' % (a, a) for a, _, _, _, _ in RESET]
    out = sim(cold, chip)
    for addr, name, want, mask, note in RESET:
        val = row(out, addr, name)
        case("%s: reset %s (%s)" % (chip, name, note), "0x%02x" % (want & mask),
             "no row" if val is None else "0x%02x" % val,
             val is not None and (val & mask) == (want & mask))

    # ... and the same values after a warm reset with the registers dirtied first
    warm = ['file "/tmp/rd2reg_name.hex"', "step 8", "reset"]
    warm += ['dump sfr 0x%02x 0x%02x' % (a, a) for a, _, _, _, _ in RESET]
    out = sim(warm, chip)
    for addr, name, want, mask, note in RESET:
        val = row(out, addr, name)
        case("%s: after reset %s" % (chip, name), "0x%02x" % (want & mask),
             "no row" if val is None else "0x%02x" % val,
             val is not None and (val & mask) == (want & mask))

    # The two P5 addresses are two cells in this model: a write to one does not follow
    # the other (documented gap, as for the 89C51R's fourth port), but reset brings both
    # of them up at 0xff.  NAME_CODE step 4 has moved 0xAA into 0xc7 by now.
    out = sim(['file "/tmp/rd2reg_name.hex"', "step 4",
               "dump sfr 0xc7 0xc7", "dump sfr 0xe8 0xe8"], chip)
    c7 = row(out, 0xc7, "P5")
    e8 = row(out, 0xe8, "P5_BIT")
    case("%s: P5 write at 0xc7 lands at 0xc7" % chip, "0xaa",
         "no row" if c7 is None else "0x%02x" % c7, c7 == 0xaa)
    case("%s: 0xe8 keeps its own value (cells are not aliased)" % chip, "0xff",
         "no row" if e8 is None else "0x%02x" % e8, e8 == 0xff)

    # ----------------------------------- bit-address trap: 0xb1 is P3.1, never IEN1
    out = sim(['file "/tmp/rd2reg_bitband.hex"', "step 2",
               "dump sfr 0xb1 0xb1", "dump bits 0xb0 0xb7"], chip)
    ien1 = row(out, 0xb1, "IEN1")
    m = re.search(r"0xb0\s+([01])\s+([01])\s+([01])", out)
    case("%s: IEN1 takes 0xff from code" % chip, "0xff",
         "no row" if ien1 is None else "0x%02x" % ien1, ien1 == 0xff)
    case("%s: bit 0xb1 follows P3, not IEN1" % chip, "P3.1 = 0",
         "no row" if not m else "bit 0xb1 = %s" % m.group(2), bool(m) and m.group(2) == "0")
    for bit in ("ESPI", "KBD", "SPIL", "KBDL", "SPIH", "KBDH"):
        case("%s: no bit name %s (0xb1..0xb3 are not bit addressable)" % (chip, bit),
             "absent", "present" if (bit + ":") in out else "absent", (bit + ":") not in out)

# ------------------------------------------------- (b) the family must gain nothing
for chip in CONTROL_CHIPS:
    cmds = ['file "/tmp/rd2reg_name.hex"']
    cmds += ['dump sfr 0x%02x 0x%02x' % (a, a) for a, _ in SFR_NAMES + FAMILY_PROBES[chip]]
    cmds += ['dump bits 0x%02x 0x%02x' % (a, a) for a, _ in BIT_NAMES]
    out = sim(cmds, chip)
    traced = ""
    for n in (1, 3, 5, 7):
        traced += sim(['file "/tmp/rd2reg_name.hex"', "step %d" % n], chip)
    for addr, name in FAMILY_PROBES[chip]:
        case("%s control: %s at 0x%02x still present" % (chip, name, addr), "present",
             "present" if row(out, addr, name) is not None else "absent",
             row(out, addr, name) is not None)
    for name in ALL_NAMES:
        case("%s gains no %s" % (chip, name), "absent",
             "present" if (name + ":") in out else "absent", (name + ":") not in out)
    for addr, name in [(0xc3, "SPCON"), (0xc7, "P5"), (0xb1, "IEN1"), (0x97, "CKRL")]:
        want = "0x%02x <%s>,A" % (addr, name)
        case("%s trace names no %s" % (chip, name), "absent",
             "present" if want in traced else "absent", want not in traced)

print("%-56s %12s %12s %6s" % ("case", "expected", "measured", "verdict"))
bad = 0
for name, expect, got, ok in results:
    bad += 0 if ok else 1
    if not ok:
        print("%-56s %12s %12s %6s" % (name, expect, got, "FAIL"))
print("%s: %d/%d" % ("PASS" if not bad else "FAIL", len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
