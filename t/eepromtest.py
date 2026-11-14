#!/usr/bin/env python3
"""AT89C51ED2 on-chip data EEPROM behind EECON (doc4235K section 23, printed pp.89-92).

Everything here runs `-t 89C51ED2` except the absence legs, which run `-t 89C51RD2` on
purpose: doc4235 puts EECON in the *family* SFR map (table 3-11, printed p.7) but gives the
2048-byte array to the ED2 alone ("This feature is available only for the AT89C51ED2 device",
printed p.89; "(AT89C51ED2 Only)", printed p.1), and SIMULATION-PLAN.md D14 deliberately left
the register named-but-inert on the RD2 as the negative control for exactly this suite.  So
half the legs prove the array exists and the other half prove the RD2 gained nothing.

What each group pins, with the page it comes from:

  presence     `info hw eeprom` on the ED2, only the usage line on the RD2.  `info hw`
               resolves elements by NAME, so this is also the leak guard for the id claim.
  registers    EECON xxxx xx00b (table 3-11, printed p.7) on both names, byte-identical
               `dump sfr` for the D0h page, and "EEBUSY ... Can not be set or cleared by
               software" (table 23-1, printed p.91) honoured against `MOV EECON,#0x01`.
  addressing   There is no address or data SFR: "located at addresses 0000h to 07FFh of the
               XRAM/ERAM memory space and is selected by setting control bits in the EECON
               register.  A read or write access ... is done with a MOVX instruction" (printed
               p.89).  The round-trip legs therefore seed the same two addresses in BOTH
               spaces with two different values and check that each space answers with its
               own byte -- a model that merely writes through to XRAM, or that aliases the
               whole window onto one cell, cannot pass those.
  boundary     0x07FF is the last array byte and 0x0800 is not: above 07FFh the bus stays
               whatever AUXR says, even with EEE set.
  busy window  EEBUSY set by hardware at the MOVX, "the EEPROM segment is not available for
               reading or writing" meanwhile (section 23.1, printed p.89), and the end "is
               indicated by a hardware clear of the EEBUSY flag".
  tWRITE       17 ms, the Max column of `tWRITE  Flash or EEdata programming time  7  17 ms`
               (table 25-2, printed p.115), measured as a DIFFERENCE between two runs whose
               images differ in one byte (section 5 of SIMULATION-PLAN.md: never measure a
               rate from absolute elapsed time), and measured in oscillator *clocks* so the
               X2 leg can show it is time and not cycles.
  persistence  The array survives `reset`; its volatile state does not.  Declared deviation,
               see rd2_eepromcl.h: a real EEPROM is non-volatile and this one is RAM, so the
               model gives it the one observable property non-volatility has here.
  security     Nothing in EECON locks the array -- bits 7:2 are Reserved, "The value read
               from this bit is indeterminate.  Do not set this bit" (printed p.90) -- the
               Security the datasheet has is the Software Security Bits of section 24.8.1
               (printed p.103), which gate ISP access, not MOVX access.

S51_SIM=<path> picks the binary (the floor contract; a suite that hard-codes one makes every
green line meaningless).  It FAILS FAST with a named reason if the binary has no `eeprom`
element.  For an audit of what passes on a binary without the feature, set
EEPROMTEST_FORCE=1 to skip that guard: every leg then runs, each subprocess is bounded by
S51_TIMEOUT, and a guest that waits for a flag the model never sets is reported as a timeout
rather than hanging the floor.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or \
  os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
# Optional: a second binary whose -t 89C51RD2 transcripts must match this one's.  Only used
# when the variable is set explicitly -- a leg that quietly depends on whatever /tmp happens to
# contain is a leg that fails for the next person to run the floor.
REF = os.environ.get("S51_REF") or ""
TIMEOUT = float(os.environ.get("S51_TIMEOUT", "180"))
FORCE = os.environ.get("EEPROMTEST_FORCE") == "1"

EECON, EEE, EEBUSY = 0xD2, 0x02, 0x01
CKCON0 = 0x8F
AUXR, EXTRAM = 0x8E, 0x02         # regs51.h: bmEXTRAM 0x02, bmXRS 0x1c (XRS2:XRS0 = bits 4:2)
XTAL = "12M"                      # so tWRITE is an exact 204000 clocks
TWRITE_CLKS = 0.017 * 12e6

results = []


def case(name, expect, got, ok):
    results.append((name, expect, got, ok))


# ------------------------------------------------------------------ two-pass assembler
def assemble(prog):
    """prog: ('label', name) | ('b', [bytes]) | ('rel', [prefix], label, total_len)."""
    addr, addrs = 0, {}
    for item in prog:
        if item[0] == "label":
            addrs[item[1]] = addr
            continue
        addr += item[3] if item[0] == "rel" else len(item[1])
    out, addr = bytearray(), 0
    for item in prog:
        if item[0] == "label":
            continue
        if item[0] == "b":
            out += bytes(item[1])
            addr += len(item[1])
        else:
            rel = (addrs[item[2]] - (addr + item[3])) & 0xff
            out += bytes(item[1]) + bytes([rel])
            addr += item[3]
    return bytes(out)


def mov_a(direct):
    return ["b", [0xE5, direct]]


def mov_direct_imm(direct, val):
    return ["b", [0x75, direct, val]]


def mov_a_imm(val):
    return ["b", [0x74, val]]


def mov_direct_a(direct):
    return ["b", [0xF5, direct]]


def mov_dptr(addr):
    return ["b", [0x90, (addr >> 8) & 0xff, addr & 0xff]]


def movx_write():
    return ["b", [0xF0]]


def movx_read():
    return ["b", [0xE0]]


def wait_eebusy(limit=0xff, _n=[0]):
    """Bounded version of the `EEBusy Cleared?` loop of figures 23-1/23-2 (printed pp.90-91).

    Bounded on purpose: a model that never clears the flag must not take the floor down with
    it.  255x255 trips at 5 machine cycles is ~325k cycles, well past the 17000 a 17 ms cycle
    costs at 12 MHz in standard mode and the 34000 in X2.  If it ever does exhaust, 0x4F gets
    0xEE and the legs that read 0x40.. report a wrong byte instead of hanging.
    """
    _n[0] += 1
    tag = "ee%d" % _n[0]                      # unique per call: assemble() keeps one address
    inner, poll, done = tag + "i", tag + "p", tag + "d"   # per label, so a second wait in the
    return [["b", [0x79, limit]],                         # same image cannot be aimed at the
            ["label", inner],                             # first block's tail and swallow the
            ["b", [0x78, limit]],                         # code in between (it did, silently:
            ["label", poll],                              # the JNB of wait #1 landed past the
            mov_a(EECON),                                 # write that wait #2 was meant to
            ["rel", [0x30, 0xE0], done, 3],               # follow) -- pinned here so it cannot
            ["rel", [0xD8], poll, 2],                     # come back.
            ["rel", [0xD9], inner, 2],
            mov_direct_imm(0x4F, 0xEE),   # MOV 0x4F,#0xEE (waited too long)
            ["label", done]]


def image(prog):
    """Assemble, terminate with SJMP $, return (path, address of the SJMP)."""
    code = assemble(list(prog) + [["b", [0x80, 0xFE]]])
    path = "/tmp/eepromtest_%d_%x.hex" % (len(code), sum(code) & 0xFFFF)
    write_hex(path, code)
    return path, len(code) - 2


# ----------------------------------------------------------------------------- console
def sim(cmds, chip, xtal=XTAL):
    argv = [SIM, "-t", chip, "-c", "-"]
    if xtal:
        argv += ["-X", xtal]
    cmds = list(cmds) + ["quit"]
    try:
        p = subprocess.run(argv, input="\n".join(cmds) + "\n",
                           capture_output=True, text=True, cwd="/tmp", timeout=TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) \
          else (exc.stdout or "")
        return (out or "") + "\n<<TIMEOUT after %gs>>" % TIMEOUT
    return p.stdout + p.stderr


def row(out, addr):
    """The `dump sfr` row of an SFR as (name or None, value or None)."""
    for ln in out.splitlines():
        if not re.match(r"0x%02x\s" % addr, ln):
            continue
        named = re.match(r"0x%02x\s+([A-Za-z_][A-Za-z0-9_./]*):" % addr, ln)
        hexes = re.findall(r"0x[0-9a-f]+", ln)
        return (named.group(1) if named else None,
                int(hexes[1], 16) if len(hexes) > 1 else None)
    return (None, None)


def iram(out, base, n):
    """Values of iram[base .. base+n-1] from one `dump iram` row (they come out on one line)."""
    for ln in out.splitlines():
        m = re.match(r"0x%02x\s+((?:[0-9a-f]{2}\s+){%d})" % (base, n), ln)
        if m:
            return [int(v, 16) for v in m.group(1).split()]
    return None


def run_to_end(prog, chip, cmds_before=(), cmds_after=(), xtal=XTAL):
    """Run the image to its final SJMP, then the extra commands; returns (console, end)."""
    path, end = image(prog)
    out = sim(['file "%s"' % path] + list(cmds_before) + ["break 0x%x" % end, "go",
                                                           "dump iram 0x40 0x4f"] +
              list(cmds_after), chip, xtal)
    return out, end


def clocks(chip, prog, xtal=XTAL):
    """Oscillator clocks from reset to the end of the image (`state`, the x2test idiom)."""
    out, _ = run_to_end(prog, chip, cmds_after=["state"], xtal=xtal)
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", out)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------- fail-fast guard
probe = sim(["info hw eeprom"], "89C51ED2")
if "eeprom[" not in probe:
    print("FAIL: %s has no `eeprom` element for -t 89C51ED2 (`info hw eeprom` printed only "
          "its usage line) -- the AT89C51ED2 data EEPROM of doc4235 section 23 is not built "
          "into this binary, so every leg below would be measuring its absence." % SIM)
    if not FORCE:
        print("      (set EEPROMTEST_FORCE=1 to run the legs anyway and audit what passes)")
        sys.exit(1)
    print("note: EEPROMTEST_FORCE=1, running the legs against a binary without the feature; "
          "the PASS lines below are the control audit, not a verdict on this binary.")

# --------------------------------------------------------------------------- presence
n = row(probe, 0)
case("ED2: `info hw eeprom` names the element", "eeprom[...]",
     "yes" if re.search(r"eeprom\[\d+\]", probe) else "no",
     bool(re.search(r"eeprom\[\d+\]", probe)))
case("ED2: it covers 0x0000..0x07ff (2048 B, printed p.89)", "window=0x0000..0x07ff",
     "found" if "window=0x0000..0x07ff" in probe else "missing",
     "window=0x0000..0x07ff" in probe)
rd2_probe = sim(["info hw eeprom"], "89C51RD2")
case("RD2: NO eeprom element (D14 negative control)", "usage line only",
     "usage only" if "eeprom[" not in rd2_probe else "ELEMENT PRESENT",
     "eeprom[" not in rd2_probe)

# -------------------------------------------------------------------------- registers
dump = sim(["dump sfr 0xd0 0xdf"], "89C51ED2")
name, val = row(dump, EECON)
case("ED2: EECON is named and resets to 0x00 (xxxx xx00b, table 3-11 p.7)", "EECON 0x00",
     "%s %s" % (name, None if val is None else hex(val)),
     name == "EECON" and val == 0x00)
warm = sim(["dump sfr[0xd2]", "reset", "dump sfr[0xd2]"], "89C51ED2")
rows = re.findall(r"0xd2\s+(?:EECON:\s+)?0b[01]+\s+(0x[0-9a-f]+)", warm)
case("ED2: EECON reads 0x00 again after `reset`", "two 0x00 rows",
     ",".join(rows) if rows else "no rows", len(rows) == 2 and set(rows) == {"0x00"})
rd2_dump = sim(["dump sfr 0xd0 0xdf"], "89C51RD2")


def sfr_rows(out):
    return re.findall(r"^0x([0-9a-f]{2})\s+(\S+):\s+(\S+)\s+(0x[0-9a-f]+)", out, re.M)


same = sfr_rows(dump) == sfr_rows(rd2_dump)
case("RD2 and ED2: the whole D0h SFR page dumps identically", "identical",
     "identical" if same else "DIFFERS", same)
case("RD2: its EECON row is the D14 one (named, 0x00, no new bits)",
     "EECON 0b00000000 0x00",
     "%s %s" % (row(rd2_dump, EECON)[0],
                hex(row(rd2_dump, EECON)[1]) if row(rd2_dump, EECON)[1] is not None else None),
     row(rd2_dump, EECON) == ("EECON", 0x00))

# EEBUSY is hardware-only.  Two directions, and both fail on a model that leaves EECON to the
# cell: writing 0x01 with nothing programmed must NOT be able to raise the flag, and writing
# 0x03 with EEE set must read back 0x02.
out, _ = run_to_end([mov_direct_imm(EECON, 0x01), mov_a(EECON), mov_direct_a(0x40)],
                    "89C51ED2")
case("ED2: `MOV EECON,#0x01` cannot set EEBUSY (p.91)", "0x00",
     hex(iram(out, 0x40, 1)[0]) if iram(out, 0x40, 1) else "??",
     iram(out, 0x40, 1) == [0x00])
out, _ = run_to_end([mov_direct_imm(EECON, 0x03), mov_a(EECON), mov_direct_a(0x40)],
                    "89C51ED2")
case("ED2: EEE takes, EEBUSY stays clear with no cycle in flight", "0x02",
     hex(iram(out, 0x40, 1)[0]) if iram(out, 0x40, 1) else "??",
     iram(out, 0x40, 1) == [0x02])

# ------------------------------------------------------------------ cold state / erased
path, end = image([mov_direct_imm(EECON, EEE)])
out = sim(['file "%s"' % path, "break 0x%x" % end, "go", "dump xram 0x0300 0x0301"],
          "89C51ED2")
cold = re.findall(r"^0x0300\s+([0-9a-f]{2})", out, re.M)
# With EEE set the console dump follows the remap, so this reads the array: 0xff is the
# declared erased state, and the plain map under these addresses answers 0x5a (the value the
# xram chip comes up with), so a model that fell through could not report 0xff.
case("ED2: an untouched array byte reads 0xff (declared erased state)", "0xff",
     "0x%s" % cold[0] if cold else "??", bool(cold) and cold[0] == "ff")
# 0x0100 and 0x0101 are seeded with two different values in the XRAM space (EEE clear), then
# with two OTHER values in the array (EEE set), then read back from both spaces.  A model that
# writes through, or that answers from one shared cell, gives 0xa5/0x5a or 0x3c/0x3c here.
DIST = [mov_direct_imm(EECON, 0x00),
        mov_dptr(0x0100), mov_a_imm(0xA5), movx_write(),
        mov_dptr(0x0101), mov_a_imm(0x5A), movx_write(),
        mov_direct_imm(EECON, EEE),
        mov_dptr(0x0100), mov_a_imm(0x3C), movx_write()] + \
     wait_eebusy() + \
     [mov_dptr(0x0101), mov_a_imm(0xC3), movx_write()] + \
     wait_eebusy() + \
     [mov_dptr(0x0100), movx_read(), mov_direct_a(0x40),
      mov_dptr(0x0101), movx_read(), mov_direct_a(0x41),
      mov_direct_imm(EECON, 0x00),
      mov_dptr(0x0100), movx_read(), mov_direct_a(0x42),
      mov_dptr(0x0101), movx_read(), mov_direct_a(0x43)]
out, _ = run_to_end(DIST, "89C51ED2")
got = iram(out, 0x40, 4)
# One case, both directions, because the two halves are what make each other mean anything: a
# binary that simply writes through to XRAM also reads back 3c/c3 (it does, on the control), so
# the array bytes are only identified by the XRAM bytes that have to survive underneath them at
# the very same addresses.
case("ED2: EEE=1 answers the array (3c c3) where EEE=0 answers the XRAM bytes (a5 5a)",
     "3c c3 a5 5a",
     " ".join("%02x" % v for v in got) if got and len(got) == 4 else "??",
     got is not None and got == [0x3c, 0xc3, 0xa5, 0x5a])

# ------------------------------------------------------- the collision with the XRAM window
# Section 9 (printed p.23) hands the low MOVX space to the on-chip expanded RAM, sized by
# AUXR.XRS2:XRS0 (reset 010 = 768 bytes, so 0x0100 is inside it either way) and withdrawn by
# AUXR.EXTRAM, while section 23 (printed p.89) states EEE with no reference to either.  The
# decision recorded in rd2_eepromcl.h is that EEE wins; asserted as a pair -- the byte written
# with EEE clear has to survive underneath while the array answers its own two bytes.
for auxr, auxname in ((0x08, "the 768 B on-chip window of reset"),
                      (EXTRAM | 0x08, "AUXR.EXTRAM set, the window on the bus")):
    COLL = [mov_direct_imm(AUXR, auxr),
            mov_direct_imm(EECON, 0x00),
            mov_dptr(0x0100), mov_a_imm(0x33), movx_write(),
            mov_direct_imm(EECON, EEE),
            mov_dptr(0x0100), movx_read(), mov_direct_a(0x40),
            mov_a_imm(0x99), movx_write()] + wait_eebusy() + \
           [mov_dptr(0x0100), movx_read(), mov_direct_a(0x41),
            mov_direct_imm(EECON, 0x00),
            mov_dptr(0x0100), movx_read(), mov_direct_a(0x42)]
    out, _ = run_to_end(COLL, "89C51ED2")
    got = iram(out, 0x40, 3)
    case("ED2: EEE wins over %s (array ff then 99, the byte under it still 33)" % auxname,
         "ff 99 33",
         " ".join("%02x" % v for v in got) if got and len(got) == 3 else "??",
         got is not None and got == [0xff, 0x99, 0x33])

# The byte the MOVX path answers must come from the array and not from the XRAM cell underneath
# it, and the console is the only thing that can write that cell out from under a program.  Note
# the ordering trap this leg is built around: `set memory xram` with EEE SET is NOT a backdoor --
# it goes through the same memory operator and starts a programming cycle (measured: pending
# becomes 0x0100:0x11 and the window reads 0xff), so the cell poke happens at a breakpoint where
# the image has already cleared EEE.  Both halves are one session because the array lives in the
# process, not in the hex file.
PRE = [mov_direct_imm(EECON, EEE), mov_dptr(0x0100), mov_a_imm(0x3C), movx_write()] + \
      wait_eebusy() + [mov_direct_imm(EECON, 0x00)]
POST = [mov_direct_imm(EECON, EEE), mov_dptr(0x0100), movx_read(), mov_direct_a(0x40)]
_, split = image(PRE)            # image() ends with SJMP $, so this is len(assemble(PRE))
whole, end = image(PRE + POST)   # and therefore where POST starts inside the whole image
out = sim(['file "%s"' % whole, "break 0x%x" % split, "break 0x%x" % end, "go",
           "set memory xram 0x0100 0x11", "dump xram 0x0100 0x0100", "go",
           "dump iram 0x40 0x40"], "89C51ED2")
cellv = re.findall(r"^0x0100\s+([0-9a-f]{2})", out, re.M)
case("ED2: the cell under the array holds the byte poked into it (0x11) while the MOVX read "
     "answers the array (0x3c)", "11 3c",
     "%s %s" % (("0x" + cellv[0]) if cellv else "??",
                hex(iram(out, 0x40, 1)[0]) if iram(out, 0x40, 1) else "??"),
     bool(cellv) and cellv[0] == "11" and iram(out, 0x40, 1) == [0x3c])

# ------------------------------------------------------------------- boundary at 07FFh
BOUNDS = [mov_direct_imm(EECON, EEE),
          mov_dptr(0x07FF), mov_a_imm(0x77), movx_write()] + wait_eebusy() + \
         [mov_dptr(0x07FF), movx_read(), mov_direct_a(0x40),
          mov_dptr(0x0800), mov_a_imm(0x27), movx_write(),
          mov_direct_imm(EECON, 0x00),
          mov_dptr(0x07FF), movx_read(), mov_direct_a(0x41),
          mov_dptr(0x0800), movx_read(), mov_direct_a(0x42)]
out, _ = run_to_end(BOUNDS, "89C51ED2")
got = iram(out, 0x40, 3)
# Same address, two readings: 0x07FF is inside the array only while EEE is set.  Asserted as a
# pair because either half alone is what a plain XRAM write-through also produces.
case("ED2: 0x07FF is the array's last byte with EEE set and the plain map's with it clear",
     "0x77 and then not 0x77",
     "%s / %s" % (hex(got[0]), hex(got[1])) if got and len(got) > 1 else "??",
     got is not None and len(got) > 1 and got[0] == 0x77 and got[1] != 0x77)
case("ED2: 0x0800 with EEE set stays on the bus (readable back, no aliasing)", "0x27",
     hex(got[2]) if got else "??", got is not None and got[2] == 0x27)

# --------------------------------------------------- the busy window, byte by byte
BUSY = [mov_direct_imm(EECON, 0x00), mov_dptr(0x0400), mov_a_imm(0xA5), movx_write(),
        mov_direct_imm(EECON, EEE),
        mov_dptr(0x0400), mov_a_imm(0xAA), movx_write(),      # starts the cycle
        mov_a(EECON), mov_direct_a(0x40),                     # EEBUSY must be 1 here
        movx_read(), mov_direct_a(0x41),                      # a read inside the window
        mov_dptr(0x0401), mov_a_imm(0xBB), movx_write(),      # refused, not queued
        mov_a(EECON), mov_direct_a(0x4F)] + \
       wait_eebusy() + \
       [mov_a(EECON), mov_direct_a(0x42),
        mov_dptr(0x0400), movx_read(), mov_direct_a(0x43),
        mov_dptr(0x0401), movx_read(), mov_direct_a(0x44)]
out, _ = run_to_end(BUSY, "89C51ED2",
                    cmds_after=["info hw eeprom"])
got = iram(out, 0x40, 5)
case("ED2: EEBUSY reads 1 from the MOVX that starts the cycle (EEE|EEBUSY=0x03)", "0x03",
     hex(got[0]) if got else "??", got is not None and got[0] == 0x03)
case("ED2: a read inside the window answers 0xff, not the byte being written", "0xff",
     hex(got[1]) if got else "??", got is not None and got[1] == 0xff)
# Both halves again: the flag has to be seen going 1 -> 0, and 0x02 on its own is what a binary
# that never sets the flag also reads afterwards.
case("ED2: EEBUSY clears by hardware when the cycle is over (0x03 during, 0x02 after)",
     "0x03 -> 0x02",
     "%s -> %s" % (hex(got[0]), hex(got[2])) if got and len(got) > 2 else "??",
     got is not None and len(got) > 2 and got[0] == 0x03 and got[2] == 0x02)
case("ED2: the byte appears only once the flag clears (0xff inside, 0xaa after)", "0xff -> 0xaa",
     "%s -> %s" % (hex(got[1]), hex(got[3])) if got and len(got) > 3 else "??",
     got is not None and len(got) > 3 and got[1] == 0xff and got[3] == 0xaa)
case("ED2: a second write inside the window is refused, not queued", "0xff",
     hex(got[4]) if got else "??", got is not None and got[4] == 0xff)
m = re.search(r"program_events=(\d+)", out)
b = re.search(r"blocked_writes=(\d+)", out)
case("ED2: counters key on bytes=/*_events= (1 cycle, 1 refused write)", "1 1",
     "%s %s" % (m.group(1) if m else "??", b.group(1) if b else "??"),
     bool(m) and bool(b) and m.group(1) == "1" and b.group(1) == "1")

# ------------------------------------------------------------------- tWRITE, differenced
# Same image twice, one byte apart: the EECON immediate that arms the remap.  With EEE clear
# the MOVX is a plain XRAM write, no cycle starts and the wait loop falls straight through, so
# the clock difference IS the programming time and every launch/setup cost cancels.
def twrite_setup(ckcon, imm):
    return [mov_direct_imm(CKCON0, ckcon),
            mov_direct_imm(EECON, 0x00),
            mov_dptr(0x0100), mov_a_imm(0x3C),
            mov_direct_imm(EECON, imm),
            movx_write()] + wait_eebusy() + [mov_a(EECON), mov_direct_a(0x40)]


for label, ckcon, want in (("standard mode", 0x00, TWRITE_CLKS),
                           ("X2 mode (CKCON0.X2)", 0x01, TWRITE_CLKS)):
    on = clocks("89C51ED2", twrite_setup(ckcon, EEE))
    off = clocks("89C51ED2", twrite_setup(ckcon, 0x00))
    span = None if on is None or off is None else on - off
    near = span is not None and abs(span - want) <= 0.02 * want
    case("ED2: tWRITE is %s of simulated time (%d clocks at -X %s, %s)"
         % ("17 ms" if label == "standard mode" else
            "the same 17 ms measured in clocks", int(want), XTAL, label),
         int(want), span, near)

# ------------------------------------------------------------------------ persistence
p1, e1 = image([mov_direct_imm(EECON, EEE), mov_dptr(0x0200), mov_a_imm(0x77),
                movx_write()] + wait_eebusy())
p2, e2 = image([mov_direct_imm(EECON, EEE), mov_dptr(0x0200), movx_read(),
                mov_direct_a(0x40)])
out = sim(['file "%s"' % p1, "break 0x%x" % e1, "go", "reset", "dump sfr 0xd0 0xdf",
           "set memory xram 0x0200 0x11", 'file "%s"' % p2, "break 0x%x" % e2, "go",
           "dump iram 0x40 0x40"], "89C51ED2")
ee = row(out, EECON)
# `reset` leaves EEE clear, so the console poke below lands in the plain XRAM cell, and the
# read-back image then asks for 0x0200 through the array.  A byte that is merely the XRAM write
# of image 1 having survived `reset` -- which is what the control reports -- answers 0x11 here;
# only an array that outlived the reset can answer 0x77.
case("ED2: the array survives `reset`, and is not the plain byte under it", "0x77",
     hex(iram(out, 0x40, 1)[0]) if iram(out, 0x40, 1) else "??",
     iram(out, 0x40, 1) == [0x77])
case("ED2: its volatile state does not survive (EECON reads 0x00 after reset)", "0x00",
     hex(ee[1]) if ee and ee[1] is not None else "??", ee[1] == 0x00)

# ------------------------------------------------- no security bit in EECON (documented)
# Bits 7:2 are Reserved ("Do not set this bit", printed p.90) and are not a lock: setting all
# of them alongside EEE must not stop the round-trip.  The array is seeded against a write-through
# first, so this cannot pass by falling through to XRAM.
SEC = [mov_direct_imm(EECON, 0x00), mov_dptr(0x0300), mov_a_imm(0x11), movx_write(),
       mov_direct_imm(EECON, 0xFE),
       mov_dptr(0x0300), mov_a_imm(0x66), movx_write()] + wait_eebusy() + \
      [mov_dptr(0x0300), movx_read(), mov_direct_a(0x40),
       mov_direct_imm(EECON, 0x00),
       mov_dptr(0x0300), movx_read(), mov_direct_a(0x41)]
out, _ = run_to_end(SEC, "89C51ED2")
got = iram(out, 0x40, 2)
case("ED2: reserved bits 7:2 are no lock (0x66 in the array) and the XRAM byte under it stays "
     "the one written with EEE clear (0x11)", "66 11",
     " ".join("%02x" % v for v in got) if got and len(got) == 2 else "??",
     got is not None and got == [0x66, 0x11])

# -------------------------------------------------- the RD2 side of every one of those
# The D14 leg, re-run here so this suite alone shows what the RD2 must NOT do: EEE set leaves
# the MOVX target alone and EEBUSY never answers 1.
RD2 = [mov_dptr(0x0100), mov_a_imm(0x22), movx_write(),
       mov_dptr(0x0100), movx_read(), mov_direct_a(0x40),
       mov_direct_imm(EECON, EEE),
       mov_a(EECON), mov_direct_a(0x41),
       mov_dptr(0x0100), movx_read(), mov_direct_a(0x42)]
out, _ = run_to_end(RD2, "89C51RD2")
got = iram(out, 0x40, 3)
# 0x40 and 0x42 are the two MOVX reads of 0x0100, 0x41 is EECON itself: setting EEE must
# neither move the byte nor answer a busy flag, which is the D14 negative control in one image.
case("RD2: EEE set leaves the MOVX target alone (0x0100 reads 0x22 both times)", "22 22",
     " ".join("%02x" % v for v in (got[0], got[2])) if got and len(got) > 2 else "??",
     got is not None and len(got) > 2 and got[0] == 0x22 and got[2] == 0x22)
case("RD2: EEBUSY stays 0 after `MOV EECON,#0x02`", "0x02",
     hex(got[1]) if got and len(got) > 1 else "??", got is not None and got[1] == 0x02)

if REF and os.path.exists(REF):
    def transcript(binary, chip, cmds):
        p = subprocess.run([binary, "-t", chip, "-c", "-"],
                           input="\n".join(list(cmds) + ["quit"]) + "\n",
                           capture_output=True, text=True, cwd="/tmp", timeout=TIMEOUT)
        txt = p.stdout + p.stderr
        return re.sub(r"^(Host usage:.*|Runtime:.*|\s+[0-9.]+ sec(?:, rate=.*)?)$", "", txt,
                      flags=re.M)
    legs = [["dump sfr 0x80 0xff"], ["dump sfr 0xd0 0xdf", "info hw timer"],
            ['file "%s"' % image(RD2)[0], "step 20", "dump iram 0x40 0x42"]]
    diffs = [i for i, leg in enumerate(legs)
             if transcript(SIM, "89C51RD2", leg) != transcript(REF, "89C51RD2", leg)]
    case("RD2: transcripts byte-identical against $S51_REF (%d legs)" % len(legs),
         "0 diffs", "%d diffs" % len(diffs), not diffs)
else:
    print("note: S51_REF not set/absent, RD2 transcript comparison skipped")

print("%-74s %18s %18s %6s" % ("case", "expected", "measured", "verdict"))
bad = 0
for name, expect, got, ok in results:
    bad += 0 if ok else 1
    print("%-74s %18s %18s %6s" % (name, expect, str(got)[:18], "PASS" if ok else "FAIL"))
print("\n%s: %d/%d (%s)" % ("PASS" if not bad else "FAIL", len(results) - bad, len(results), SIM))
sys.exit(1 if bad else 0)
