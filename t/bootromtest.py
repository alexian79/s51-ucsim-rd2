#!/usr/bin/env python3
"""
AT89C51RD2/ED2 boot ROM, AUXR1.ENBOOT and the on-chip Flash API (USER_CALL at 0FFF0h).

Ground truth is Atmel doc4235K, page by page; every assertion cites the printed page it
comes from.  The pages this suite lives on:

  table 8-1, printed p. 21   ENBOOT (AUXR1.5): "Cleared to disable boot ROM.  Set to map
                             the boot ROM between F800h - 0FFFFh."  Both ends -- and with
                             Features on printed p. 1, "2048 Bytes Boot ROM" -- the size
                             of the window come from here.
  section 24.1/24.2, p. 93   64K of program memory in 512 pages of 128 bytes, "Byte-wise
                             read without wait state", and "The programming of a byte or
                             a page includes a self erase before programming".
  table 24-3, pp. 95-96      the identification bytes 58h / D7h / ECh / EFh.
  figure 24-5, printed p.101 reset ENBOOT follows the BLJB fuse, which the model does not
                             read; table 3-11 (printed p. 8) leaves AUXR1.5 as X and this
                             model brings it up CLEARED, so every leg below starts inert.
  table 24-9, printed p. 111 erase geometry: block0 0000h-1FFFh, block1 2000h-3FFFh,
                             block2 4000h-7FFFh, block3 8000h-BFFFh, block4 C000h-FFFFh --
                             deliberately unequal -- and Full Chip Erase, which belongs to
                             the ISP command set of that same table and is NOT in the API.
  section 24.10.1/2, p. 112  "All calls are made through a common interface USER_CALL at
                             the address FFF0h", it "must be done by LCALL instruction to
                             be able to comeback in the application", "Before jump at the
                             USER_CALL, the bit ENBOOT in AUXR1 register must be set", and
                             the bootloader does NOT disable interrupts.
  table 24-10, pp. 112-113   the command rows, quoted per leg below.
  table 25-7, printed p. 115 tWRITE = 7..17 ms with no typical value -- the reason the
                             model returns instantly, which leg_timing_decision pins.

Shape of the evidence.  A leg that would also pass on a binary without the feature proves
nothing, so:

  * every flash cell an API command is aimed at is SEEDED: the loader programs a
    distinctive byte there first, so a read-back that names that byte cannot be reading an
    untouched cell (an untouched cell reads 0xff, and no ID command answers 0xff);
  * ACC is seeded with 0xa5 before every USER_CALL, so "ACC unchanged" (a refusal) and
    "ACC = the value the row says" (an accepted call) are different observations, and
    neither can be a zero nobody wrote;
  * the array carries a stub at 0FFF0h (`MOV P0,#0x2e ; RET`) in every image: if the boot
    ROM is mapped the stub is unreachable and P0 stays 0ffh; if it is not mapped the stub
    runs and P0 reads 02eh.  That is the inverted pair -- one image, differing only in the
    AUXR1 byte, one observation per side;
  * erase geometry is pinned from OUTSIDE the erased block as well as inside it (0x1fff
    and 0x4000 keep their seeds when 0x2000 is erased), which is what a model that divided
    64K by 5 would get wrong;
  * every program parks in a self-jump and every leg steps generously, so a binary that
    takes a different path still stops in bounded time and the control audit fails with a
    named reason instead of spinning.

Step accounting.  `step N' counts executed instructions, and the API really costs two: the
LCALL, then the boot ROM's own RET as a second slot (on the part that RET is a fetched
instruction too), while a refused call runs the array's two-instruction stub instead.  The
legs do not chase those numbers -- they park and overshoot deliberately.  Only
leg_timing_decision needs an exact count, and both of its runs take the same path.

Binary selection: S51_SIM is the binary under test (t/floor.sh exports it).  With no
S51_SIM but S51_REF set, the whole suite runs on S51_REF -- that is the control audit, and
it must fail with named reasons.  S51_TIMEOUT bounds every console run.

Traps this file defends against, each of which produced a plausible wrong answer while the
block was written:
  - `file' must be quoted; an unquoted path loads NOTHING and the all-zero image runs as
    NOPs, advancing PC and ticking as if the program had been there.
  - `dump sfr' needs an explicit range, and `get sfr' answers from another space.  There
    is no `set sfr' command, but `set memory sfr <addr> <val>' writes an SFR and the ENBOOT
    banker follows it (leg_enboot_via_console checks that the MAPPING moved, which is the
    only way to tell a written register from a followed one).
  - the code space is `dump rom ...'; `dump code' prints a usage line, and a usage line in
    front of a missing value reads like "the byte is not there".
  - dump rows print the whole aligned group, so a probe that wants one byte must dump one
    byte and must not assume the value is followed by its ASCII form.
  - P0..P4 reset to 0ffh and dptr_chip/iram come up uninitialised, so every input a leg
    reads is stamped by the program itself, never assumed to be 0.
  - the console echoes its own input into the same stream and the echo can land in front of
    an output line, so no probe regex here is anchored at line start.
"""

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import rec

SIM = os.environ.get("S51_SIM") or os.environ.get("S51_REF") or \
    "/tmp/ucsim-boot/ucsim/src/sims/s51.src/ucsim_51"
REF = os.environ.get("S51_REF", "/tmp/s51-ref-b")
TIMEOUT = int(os.environ.get("S51_TIMEOUT", "30"))
WORK = tempfile.mkdtemp(prefix="bootromtest-")

AUXR1, ENBOOT, DPS = 0xa2, 0x20, 0x01            # table 8-1, printed p. 21
P0, P1, P2, P3, P4 = 0x80, 0x90, 0xa0, 0xb0, 0xc0
ACC = 0xe0
USER_CALL = 0xfff0                               # section 24.10.1, printed p. 112
BOOT_BASE, BOOT_SIZE = 0xf800, 0x800             # table 8-1 p. 21 / Features p. 1
MANUF, ID1, ID2, ID3 = 0x58, 0xd7, 0xec, 0xef    # table 24-3, printed pp. 95-96
SEED_ACC = 0xa5                                  # "nothing wrote ACC" marker
SEED_STUB = 0x2e                                 # the array stub writes this to P0
STUB_BYTE = 0x0f                                 # the stub's first byte, seen by a read
SLACK = 8                                        # legs park, so overshoot is harmless

_checks = 0
_failures = []


def check(name, ok, detail=""):
    global _checks
    _checks += 1
    detail = "" if detail in (None, "") else str(detail)
    if ok:
        print("ok    %s%s" % (name, ("  [%s]" % detail) if detail else ""))
    else:
        _failures.append(name)
        print("FAIL  %s%s" % (name, ("  [%s]" % detail) if detail else ""))


# ---------------------------------------------------------------- opcodes
def mov_di(a, v): return [0x75, a, v & 0xff]      # MOV direct,#imm
def mov_ai(v):    return [0x74, v & 0xff]         # MOV A,#imm
def mov_r1(v):    return [0x79, v & 0xff]         # MOV R1,#imm
def clr_a():      return [0xe4]                   # CLR A
def mov_dptr(v):  return [0x90, (v >> 8) & 0xff, v & 0xff]
def movc():       return [0x93]                   # MOVC A,@A+DPTR
def mov_da(a):    return [0xf5, a]                # MOV direct,A
def mov_ad(a):    return [0xe5, a]                # MOV A,direct
def lcall(a):     return [0x12, (a >> 8) & 0xff, a & 0xff]
def sjmp():       return [0x80, 0xfe]             # SJMP $
def ret_():       return [0x22]                   # RET
def movx_ai():    return [0xf0]                   # MOVX @DPTR,A


def api(cmd, acc=None, dptr0=None, auxr1=ENBOOT):
    """Set ENBOOT, stage R1/ACC/DPTR0 and LCALL the API.

    ENBOOT is written by the program itself (auxr1=0 leaves it clear, auxr1=ENBOOT sets it,
    auxr1=ENBOOT|DPS sets both) the way firmware writes it, never from the console, so the
    register path is the path under test.  Returns (code, n_instructions)."""
    code = []
    n = 0

    def ins(seq):
        code.extend(seq)
    ins(mov_di(AUXR1, auxr1)); n += 1
    if acc is not None:
        ins(mov_ai(acc)); n += 1
    if dptr0 is not None:
        ins(mov_dptr(dptr0)); n += 1
    ins(mov_r1(cmd)); n += 1
    ins(lcall(USER_CALL)); n += 1
    return code, n


def set_dptrs(auxr1, dptr1=None, dptr0=None):
    """Program both pointer banks: DPTR1 while DPS is set, DPTR0 after clearing it.
    `MOV DPTR,#imm' only ever touches the SELECTED register, so a leg that needs both has
    to switch DPS -- which is also the evidence that the ENBOOT banker leaves DPS alone."""
    code, n = [], 0
    if dptr1 is not None:
        code += mov_di(AUXR1, auxr1 | DPS) + mov_dptr(dptr1); n += 2
    code += mov_di(AUXR1, auxr1); n += 1
    if dptr0 is not None:
        code += mov_dptr(dptr0); n += 1
    return code, n


def movc_to(port, addr, code, n):
    """Append `MOV DPTR,#addr / CLR A / MOVC A,@A+DPTR / MOV port,A' (4 instructions)."""
    code.extend(mov_dptr(addr) + clr_a() + movc() + mov_da(port))
    return code                       # the caller adds 4 to its instruction count


def park(code, n):
    """End with `SJMP $' so a generous step count lands here on any binary."""
    code.extend(sjmp())
    return code, n + 1


# ---------------------------------------------------------------- images
def sparse_hex(name, chunks):
    """Intel HEX from [(base, bytes), ...]; gaps stay unprogrammed (the array is 0xff).

    Records never cross a 16-byte boundary, and every record is checksum-verified the way
    t/mkhex.py does it, because a bad checksum is not rejected: the loader zeroes the bytes
    instead, and 0x00 is NOP, which looks exactly like a program that ran."""
    path = os.path.join(WORK, name + ".hex")
    lines = []
    for base, data in chunks:
        data = bytes(data)
        off = 0
        while off < len(data):
            room = 16 - ((base + off) & 0xf)
            part = data[off:off + min(room, len(data) - off)]
            lines.append(rec(0, (base + off) & 0xffff, part))
            off += len(part)
    lines.append(":00000001FF")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    for ln in open(path):
        b = bytes.fromhex(ln.strip()[1:])
        assert sum(b) & 0xFF == 0, "bad checksum in %r" % ln
    return path


def stub_chunk():
    """What the flash array would hold at the boot vector: mark P0, return.

    It starts with INC A (0fh) so that a `dump rom' of the vector reads a byte that can be
    neither an erased cell (0ffh) nor an empty image (00h) nor an opcode the program body
    happens to share; three instructions run before it returns, which is why the legs park
    instead of counting steps."""
    return (USER_CALL, bytes([0x0f] + mov_di(P0, SEED_STUB) + ret_()))


def program_image(name, code, n, seeds=(), stub=True, entry_addr=0):
    """(path, n_instructions).  `seeds' are (addr, bytes) the loader writes into the array;
    they are the cells the API is aimed at, so a read-back that names them says which cell
    answered."""
    chunks = [(entry_addr, bytes(code))] + list(seeds)
    if stub:
        chunks.append(stub_chunk())
    return sparse_hex(name, chunks), n


# ---------------------------------------------------------------- console
def run(path, cmds, leg, device="89C51RD2", sim=None, timeout=None):
    args = [sim or SIM, "-t", device, "-c", "-"]
    script = "\n".join((['file "%s"' % path] if path else []) + cmds + ["exit"]) + "\n"
    try:
        p = subprocess.run(args, input=script, capture_output=True, text=True,
                           timeout=timeout or TIMEOUT, cwd=WORK,
                           env=dict(os.environ, S51=os.environ.get("S51", "/home/oleksiy/s51")))
    except subprocess.TimeoutExpired:
        return "<<TIMEOUT leg %s: console did not finish in %ds>>" % (leg, timeout or TIMEOUT)
    return p.stdout + p.stderr


def go(name, code, n, cmds, seeds=(), stub=True, device="89C51RD2", extra=SLACK,
       sim=None):
    """Build the image, park the program, step past the park, run `cmds', return output."""
    code, n = park(code, n)
    img, n = program_image(name, code, n, seeds=seeds, stub=stub)
    return run(img, ["step %d" % (n + extra)] + list(cmds), name, device=device, sim=sim)


def byte_at(text, addr):
    """One byte out of a `dump sfr'/`dump rom' row whose FIRST byte is at addr.

    Two row shapes exist: a named cell prints `0x90 P1:  0b11111111 0xff '.' 255', a group
    prints `0x91   00 00 ..'.  The console echoes its own input into the same stream and
    the echo can be spliced in front of a row, so nothing here is anchored at line start."""
    m = re.search(r"0x%x\s+[A-Za-z0-9_]+:\s+0b[01]+\s+0x([0-9a-f]{2})" % addr, text)
    if m:
        return int(m.group(1), 16)
    m = re.search(r"0x%x\s+([0-9a-f]{2})\s" % addr, text)
    return int(m.group(1), 16) if m else None


def rom_byte(text, addr):
    """One byte of the code space, from `dump rom <addr> <addr>'.

    A missing row reads as None, never as 0xff: 0xff is what an erased flash cell and an
    unmapped ROM both answer, so conflating them would turn a broken probe into a pass."""
    return byte_at(text, addr)


def ports(text):
    return dict((n, byte_at(text, a)) for n, a in
                (("P0", P0), ("P1", P1), ("P2", P2), ("P3", P3), ("P4", P4)))


def readback(name, code, n, seeds=(), stub=True, device="89C51RD2", sim=None, info=False):
    """Run a program whose instructions stored the results into P0..P4 and read them back.

    info=True also asks the instrument, in the SAME run, so a counter and the array state
    it describes cannot come from two different executions."""
    cmds = ["dump sfr 0x80 0x80", "dump sfr 0x90 0x90", "dump sfr 0xa0 0xa0",
            "dump sfr 0xb0 0xb0", "dump sfr 0xc0 0xc0"]
    if info:
        cmds.append("info hw boot")
    t = go(name, code, n, cmds, seeds=seeds, stub=stub, device=device, sim=sim)
    return t, ports(t)


def boot_info(text):
    """`info hardware boot' into a dict, or None when the element is absent."""
    m = re.search(r"boot\[(\d+)\] (\w+) AUXR1=0x([0-9a-f]{2}) ENBOOT=(\d) DPS=(\d) "
                  r"window=([0-9a-f]+)-([0-9a-f]+) \((\d+) B\) -> (\S+)", text)
    if not m:
        return None
    st = {"id": int(m.group(1)), "on": m.group(2), "auxr1": int(m.group(3), 16),
          "enboot": int(m.group(4)), "dps": int(m.group(5)),
          "lo": int(m.group(6), 16), "hi": int(m.group(7), 16),
          "size": int(m.group(8)), "maps": m.group(9)}
    m = re.search(r"USER_CALL=([0-9a-f]+) calls=(\d+) refused=(\d+) bytes_written=(\d+)"
                  r" bytes_erased=(\d+) last=([0-9a-f]{2}) \"([^\"]*)\"", text)
    if m:
        st.update({"entry": int(m.group(1), 16), "calls": int(m.group(2)),
                   "refused": int(m.group(3)), "written": int(m.group(4)),
                   "erased": int(m.group(5)), "last_cmd": int(m.group(6), 16),
                   "last": m.group(7)})
    return st


def clks(text):
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", text)
    return int(m.group(1)) if m else None


def tail(text, n=160):
    return text.strip()[-n:].replace("\n", " | ")


# ---------------------------------------------------------------- legs
def leg_element_and_window():
    """The window is a stated number, not a fitted one: F800h-FFFFh, 2048 B."""
    t = run(None, ["info hw boot"], "element")
    st = boot_info(t)
    check("the RD2 has a `boot' element that reports its window", st is not None,
          tail(t) if st is None else "")
    if not st:
        return
    check("window = F800h-FFFFh (table 8-1 printed p.21: \"map the boot ROM between "
          "F800h - 0FFFFh\")", (st["lo"], st["hi"]) == (BOOT_BASE, 0xffff),
          "%04x-%04x" % (st["lo"], st["hi"]))
    check("window size = 2048 B (Features printed p.1: \"2048 Bytes Boot ROM\")",
          st["size"] == BOOT_SIZE, st["size"])
    check("entry point = 0FFF0h (section 24.10.1 printed p.112: \"a common interface "
          "USER_CALL at the address FFF0h\")", st.get("entry") == USER_CALL,
          hex(st.get("entry") or -1))


def leg_inert_at_reset():
    """ENBOOT at reset is BLJB-fuse driven (figure 24-5 printed p.101) and table 3-11
    printed p.8 leaves AUXR1.5 as X; this model brings it up cleared, so nothing is mapped
    yet and no call has been taken."""
    t = go("inert", clr_a(), 1, ["dump sfr 0x%02x 0x%02x" % (AUXR1, AUXR1),
                                 "dump rom 0x%04x 0x%04x" % (USER_CALL, USER_CALL),
                                 "info hw boot"])
    st = boot_info(t)
    check("AUXR1 reads 0x00 out of reset (table 3-11 printed p.8 lists bit5 as X; the "
          "model resolves that X to disabled)", byte_at(t, AUXR1) == 0x00,
          byte_at(t, AUXR1))
    check("the instrument agrees ENBOOT is clear", st is not None and st["enboot"] == 0)
    check("with ENBOOT clear the window answers from the flash array",
          st is not None and st["maps"].startswith("rom_chip"), st and st["maps"])
    check("and a fetch of FFF0h really sees the array byte %02xh the loader wrote, not an "
          "erased cell and not a ROM" % STUB_BYTE,
          rom_byte(t, USER_CALL) == STUB_BYTE, rom_byte(t, USER_CALL))
    check("nothing has been called or refused yet",
          st is not None and (st.get("calls"), st.get("refused")) == (0, 0),
          "%s/%s" % (st and st.get("calls"), st and st.get("refused")))


def leg_enboot_via_console():
    """One address, three readings: array, boot ROM, array again.  This is the mapping
    proof that does not involve the API at all, and it is also the proof that writing the
    register moves the MAPPING and not merely the register."""
    t = go("mapab", clr_a(), 1,
           ["dump rom 0x%04x 0x%04x" % (USER_CALL, USER_CALL),
            "set memory sfr 0x%02x 0x%02x" % (AUXR1, ENBOOT),
            "info hw boot",
            "dump rom 0x%04x 0x%04x" % (USER_CALL, USER_CALL),
            "set memory sfr 0x%02x 0x%02x" % (AUXR1, 0x00),
            "dump rom 0x%04x 0x%04x" % (USER_CALL, USER_CALL)])
    rows = re.findall(r"0x%04x\s+([0-9a-f]{2})\s" % USER_CALL, t)
    st = boot_info(t)
    check("ENBOOT clear: FFF0h answers %02xh, the byte the loader put in the array"
          % STUB_BYTE, rows[:1] == ["%02x" % STUB_BYTE], rows)
    check("writing AUXR1.5 moves the mapping: the instrument reports the boot ROM",
          st is not None and st["enboot"] == 1 and st["maps"] == "boot_rom_chip",
          st and "%s %s" % (st["enboot"], st["maps"]))
    check("ENBOOT set: the same address answers 0ffh -- the ROM, not the array",
          rows[1:2] == ["ff"], rows)
    check("clearing ENBOOT restores the array byte, so bank 0 really is the flash array at "
          "F800h and the flat mapping outside the window is untouched",
          rows[2:3] == ["%02x" % STUB_BYTE], rows)


def leg_window_is_code_space():
    """The ROM replaces the array in the space the core FETCHES from, and MOVC reads that
    same space.  One program, two readings, differing only in the AUXR1 byte between them."""
    code, n = [], 0
    code += mov_di(AUXR1, ENBOOT); n += 1
    code = movc_to(P1, USER_CALL, code, n); n += 4      # read the vector while mapped
    code += mov_di(AUXR1, 0x00); n += 1
    code = movc_to(P2, USER_CALL, code, n); n += 4      # and after unmapping
    t, p = readback("movc", code, n)
    check("MOVC of FFF0h with ENBOOT set reads 0ffh from the boot ROM, so the ROM sits in "
          "code space (section 24 printed p.93 calls the array \"program memory\" and "
          "promises \"Byte-wise read without wait state\")", p["P1"] == 0xff, p["P1"])
    check("MOVC of FFF0h after clearing ENBOOT reads the array byte %02xh, so fetch and "
          "MOVC answer out of one space -- and nothing ran on either reading (P0 still "
          "0ffh), which is the other half of the point: reading the vector must not run "
          "the API or the stub" % STUB_BYTE,
          p["P2"] == STUB_BYTE and p["P0"] == 0xff, "%s/%s" % (p["P2"], p["P0"]))


def leg_fetch_reaches_rom():
    """With ENBOOT set the call cannot reach the array: the ROM is where the core fetches,
    its RET comes back, and the array's stub is never fetched."""
    code, n = api(0x00, acc=SEED_ACC, dptr0=0x0000)
    code += mov_da(P1); n += 1
    t, p = readback("fetchset", code, n, info=True)
    st = boot_info(t)
    check("USER_CALL with ENBOOT set answers the manufacturer ID 58h in ACC (table 24-10 "
          "printed p.112 \"DPTR0 = 0000h -> READ Manufacturer Id, ACC = Manufacturer Id\" "
          "and table 24-3 printed pp.95-96 \"58h Atmel\")", p["P1"] == MANUF, p["P1"])
    check("the array's stub at FFF0h was never fetched (P0 still 0ffh): the window really "
          "is where execution goes, table 8-1 printed p.21", p["P0"] == 0xff, p["P0"])
    check("the instrument counts one accepted call and no refusal",
          st is not None and (st.get("calls"), st.get("refused")) == (1, 0),
          "%s/%s" % (st and st.get("calls"), st and st.get("refused")))
    check("the instrument names the command it ran",
          st is not None and "READ MANUF ID" in st.get("last", ""), st and st.get("last"))


def leg_refused_without_enboot():
    """The inverted half of the same image: ENBOOT clear means there is no ROM at FFF0h, so
    the call must be refused -- loudly, with ACC left alone -- and the array byte executed."""
    code, n = api(0x00, acc=SEED_ACC, dptr0=0x0000, auxr1=0x00)
    code += mov_da(P1); n += 1
    t, p = readback("fetchclr", code, n, info=True)
    st = boot_info(t)
    check("with ENBOOT clear ACC keeps its seed %02xh: the API did not answer" % SEED_ACC,
          p["P1"] == SEED_ACC, p["P1"])
    check("with ENBOOT clear the array's stub at FFF0h DOES run (P0 = %02xh): refusing the "
          "call does not swallow the fetch (section 24.10.1 printed p.112 requires ENBOOT "
          "before the jump)" % SEED_STUB, p["P0"] == SEED_STUB, p["P0"])
    check("the refusal is diagnosable: the message names USER_CALL and ENBOOT",
          "refused" in t and "ENBOOT" in t.split("refused", 1)[-1][:200], tail(t) if
          "refused" not in t else "")
    check("the instrument counts one refusal and no accepted call",
          st is not None and (st.get("calls"), st.get("refused")) == (0, 1),
          "%s/%s" % (st and st.get("calls"), st and st.get("refused")))


def leg_device_ids():
    """R1=00h with DPTR0 = 0000h..0003h, the four identification bytes (table 24-10 printed
    p.112 taking their values from table 24-3 printed pp.95-96).  ACC is seeded every time,
    so a wrong answer is a wrong byte rather than a zero."""
    for sel, (want, label) in enumerate([(MANUF, "manufacturer"), (ID1, "device ID1"),
                                         (ID2, "device ID2"), (ID3, "device ID3")]):
        code, n = api(0x00, acc=SEED_ACC, dptr0=sel)
        code += mov_da(P1); n += 1
        t, p = readback("id%d" % sel, code, n)
        check("READ %s: DPTR0=%04xh -> ACC=%02xh and the seed is gone" % (label, sel, want),
              p["P1"] == want, "%s (stub fetched: P0=%s)" % (p["P1"], p["P0"]))


def leg_undocumented_id_selector():
    """DPTR0=0004h is not in table 24-10, so the only honest answer is the erased byte,
    with a message that says the selector was not listed rather than inventing an ID."""
    code, n = api(0x00, acc=SEED_ACC, dptr0=0x0004)
    code += mov_da(P1); n += 1
    t, p = readback("idbad", code, n)
    check("an unlisted DPTR0 says so and answers 0ffh -- the erased byte -- instead of a "
          "number that would be mistaken for an ID",
          p["P1"] == 0xff and "does not" in t, "%s msg=%s" % (p["P1"], "does not" in t))


def leg_dptr0_answers_under_dps():
    """ENBOOT and DPS are bits of ONE register (table 8-1 printed p.21: bit5 ENBOOT, bit0
    DPS) and the API names DPTR0/DPTR1, not the selected pointer.  With DPS set the live
    pointer is DPTR1, so the core's DPL/DPH read DPTR1 while the call still uses DPTR0."""
    code, n = [], 0
    code += mov_di(AUXR1, ENBOOT); n += 1
    code += mov_dptr(0x0001); n += 1                 # DPTR0 while DPS=0
    code += mov_di(AUXR1, ENBOOT | DPS); n += 1      # now DPTR1 is the live pointer
    code += mov_dptr(0x0000); n += 1                 # DPTR1 = 0000h
    code += mov_r1(0x00); n += 1
    code += lcall(USER_CALL); n += 1
    code += mov_da(P1); n += 1                       # the ID the API returned
    code += mov_ad(0x82) + mov_da(P2); n += 2        # DPL as the core now sees it
    t, p = readback("dps", code, n, info=True)
    st = boot_info(t)
    check("with DPS set the API still answers from DPTR0: DPTR0 holds 0001h, so 0d7h, and "
          "not the 058h that DPTR1's 0000h would have asked for", p["P1"] == ID1, p["P1"])
    check("and DPS itself still switches the live pointer (DPL reads DPTR1's low byte 00h, "
          "not DPTR0's 01h) -- one register, two independent masters", p["P2"] == 0x00,
          p["P2"])
    check("the instrument reports both bits at once",
          st is not None and (st["enboot"], st["dps"]) == (1, 1),
          st and "%s/%s" % (st["enboot"], st["dps"]))


def leg_program_and_read():
    """PROGRAM DATA BYTE, then MOVC of the same address: the erase-program-read round-trip,
    read back through the array the core executes from."""
    seed, want = 0x3b, 0x5c
    code, n = api(0x02, acc=want, dptr0=0x0100)      # value in A, address in DPTR0
    code += mov_da(P2); n += 1                       # what the row says ACC becomes
    code = movc_to(P1, 0x0100, code, n); n += 4     # read it back with MOVC
    t, p = readback("prog", code, n, seeds=[(0x0100, bytes([seed]))])
    check("PROGRAM DATA BYTE (R1=02h) reports DONE: ACC = 0 (table 24-10 printed p.112 "
          "\"ACC = 0: DONE\")", p["P2"] == 0x00, p["P2"])
    check("MOVC of the programmed address reads %02xh, the value written and not the seed "
          "%02xh the loader left there: the write landed in the array the core fetches "
          "from" % (want, seed), p["P1"] == want, p["P1"])


def leg_program_self_erases():
    """Section 24.2, printed p.93: "It is not necessary to erase a byte or a page before
    programming.  The programming of a byte or a page includes a self erase before
    programming."  Seed and value are chosen so a bit-AND model answers a third byte."""
    seed, want = 0x3b, 0xc7
    code, n = api(0x02, acc=want, dptr0=0x0100)
    code = movc_to(P1, 0x0100, code, n); n += 4
    t, p = readback("prog2", code, n, seeds=[(0x0100, bytes([seed]))])
    check("programming over different data lands the whole byte %02xh, so the self erase "
          "of section 24.2 printed p.93 is modelled and not a bit-AND into the old "
          "contents (which would read %02xh)" % (want, seed & want), p["P1"] == want,
          p["P1"])


def leg_erase_block_geometry():
    """ERASE BLOCK with DPH=20h must erase exactly block1 ("Erase block1 (2000h-3FFFh)",
    table 24-9 printed p.111) and nothing on either side.  The neighbours are the point: a
    model that divided 64K by 5 would take 4000h with it."""
    seeds = [(0x1fff, bytes([0x77])), (0x2000, bytes([0x55])), (0x3fff, bytes([0x66])),
             (0x4000, bytes([0x44]))]
    code, n = api(0x01, acc=SEED_ACC, dptr0=0x2000)  # DPH=20h selects block1
    code += mov_da(P4); n += 1                       # ACC = DPH, per the row
    code = movc_to(P1, 0x1fff, code, n); n += 4      # last byte of block0
    code = movc_to(P2, 0x2000, code, n); n += 4      # first byte of block1
    code = movc_to(P3, 0x4000, code, n); n += 4      # first byte of block2
    t, p = readback("erase1", code, n, seeds=seeds, info=True)
    st = boot_info(t)
    check("ERASE BLOCK echoes DPH into ACC (table 24-10 printed p.112 \"DPTR = Address of "
          "Flash block.  ACC = DPH\")", p["P4"] == 0x20, p["P4"])
    check("0x2000 reads 0ffh: erased", p["P2"] == 0xff, p["P2"])
    check("0x1FFF, the last byte of block0, keeps its seed 77h: block1 does not reach down "
          "into block0", p["P1"] == 0x77, p["P1"])
    check("0x4000, the first byte of block2, keeps its seed 44h: the geometry is the "
          "unequal table 24-9 printed p.111, not 64K divided by 5", p["P3"] == 0x44,
          p["P3"])
    check("the instrument counts the 8192 bytes of block1 as erased",
          st is not None and st.get("erased") == 0x2000, st and st.get("erased"))


def leg_erase_block4_is_16k():
    """The last block is C000h-FFFFh -- 16K (table 24-9 printed p.111) -- so it reaches the
    top of the address space while block3 (8000h-BFFFh) stays untouched."""
    seeds = [(0xbfff, bytes([0x77])), (0xc000, bytes([0x55])), (0xc123, bytes([0x66])),
             (0xfff8, bytes([0x44]))]
    code, n = api(0x01, acc=SEED_ACC, dptr0=0xc000)
    code = movc_to(P1, 0xbfff, code, n); n += 4
    code = movc_to(P2, 0xc000, code, n); n += 4
    code = movc_to(P3, 0xc123, code, n); n += 4
    code = movc_to(P4, 0xfff8, code, n); n += 4
    t, p = readback("erase4", code, n, seeds=seeds)
    check("BFFFh, the last byte of block3, keeps its seed 77h", p["P1"] == 0x77, p["P1"])
    check("C000h is erased", p["P2"] == 0xff, p["P2"])
    check("C123h, well inside the block, is erased too: the whole 16K, not an 8K slice",
          p["P3"] == 0xff, p["P3"])
    check("FFF8h, near the top of the array, is erased with it", p["P4"] == 0xff, p["P4"])


def leg_bad_erase_selector_and_no_chip_erase():
    """Two refusals with different reasons.  A DPH that is not one of 00/20/40/80/C0h names
    no block in table 24-9 printed p.111 and must erase nothing; R1=07h -- Full Chip Erase
    in the ISP set of that same table -- is not part of the API at all."""
    seeds = [(0x4000, bytes([0x44])), (0x8000, bytes([0x88]))]
    for cmd, dptr0, want_acc, why in [
            (0x01, 0x6000, 0x60, "not one of 00/20/40/80/C0"),
            (0x07, 0x0000, SEED_ACC, "not implemented")]:
        code, n = api(cmd, acc=SEED_ACC, dptr0=dptr0)
        code += mov_da(P1); n += 1
        code = movc_to(P2, 0x4000, code, n); n += 4
        code = movc_to(P3, 0x8000, code, n); n += 4
        t, p = readback("erase%d" % cmd, code, n, seeds=seeds)
        check("R1=%02xh says %s" % (cmd, why), why in t, tail(t))
        check("R1=%02xh leaves both blocks alone (44h at 4000h, 88h at 8000h)" % cmd,
              p["P2"] == 0x44 and p["P3"] == 0x88, "%s/%s" % (p["P2"], p["P3"]))
        check("R1=%02xh leaves ACC as the row says (%02xh)" % (cmd, want_acc),
              p["P1"] == want_acc, p["P1"])


def leg_program_data_page():
    """PROGRAM DATA PAGE (R1=09h): "A = number of bytes to program from 1 up to 128",
    "DPTR0 = Flash address", "DPTR1 = XRAM address", "ACC = 0: DONE" (table 24-10 printed
    p.113).  The source is real XRAM -- written with MOVX, read back through the array --
    and the byte just past the request must keep its seed."""
    seed = 0x99
    code, n = [], 0
    # Fill XRAM 0030h..0037h with 10h..17h through DPTR1, the bank the row names, so the
    # fill runs with DPS set.
    code += mov_di(AUXR1, ENBOOT | DPS); n += 1
    for i in range(8):
        code += mov_dptr(0x0030 + i) + mov_ai(0x10 + i) + movx_ai(); n += 3
    src, nsrc = set_dptrs(ENBOOT, dptr1=0x0030, dptr0=0x0200)   # source, then destination
    code += src; n += nsrc
    code += mov_ai(8); n += 1
    code += mov_r1(0x09); n += 1
    code += lcall(USER_CALL); n += 1
    code += mov_da(P2); n += 1                          # DONE
    code = movc_to(P1, 0x0200, code, n); n += 4         # first byte of the page
    code = movc_to(P3, 0x0207, code, n); n += 4         # last byte of the request
    code = movc_to(P4, 0x0208, code, n); n += 4         # just past it
    t, p = readback("page", code, n, seeds=[(0x0200, bytes([seed] * 10))], info=True)
    st = boot_info(t)
    check("PROGRAM DATA PAGE (R1=09h) reports DONE: ACC = 0", p["P2"] == 0x00, p["P2"])
    check("the first byte of the page is 10h, taken from XRAM 0030h through DPTR1, not the "
          "seed 99h the loader left in the array", p["P1"] == 0x10, p["P1"])
    check("the eighth byte is 17h (XRAM 0037h): the request was written through to its end, "
          "not only its first byte", p["P3"] == 0x17, p["P3"])
    check("the ninth byte, past the count, keeps its seed 99h: the count in A is honoured",
          p["P4"] == seed, p["P4"])
    check("the instrument counts 8 bytes written through the API",
          st is not None and st.get("written") == 8, st and st.get("written"))


def leg_program_page_must_stay_in_one_page():
    """The remark on the same row: "number of bytes to program is limited such as the Flash
    write remains in a single 128 bytes page.  Hence, when ACC is 128, valid values of DPL
    are 00h, or, 80h" (printed p.113).  16 bytes from offset 78h crosses 80h, so the call
    must refuse: no DONE, nothing programmed."""
    seed = 0x88
    code, n = [], 0
    code += mov_di(AUXR1, ENBOOT | DPS); n += 1
    code += mov_dptr(0x0030) + mov_ai(0x10) + movx_ai(); n += 3   # XRAM source byte
    src, nsrc = set_dptrs(ENBOOT, dptr1=0x0030, dptr0=0x0278)     # 16B from offset 78h
    code += src; n += nsrc
    code += mov_ai(16); n += 1
    code += mov_r1(0x09); n += 1
    code += lcall(USER_CALL); n += 1
    code += mov_da(P1); n += 1                          # ACC: no DONE expected
    code = movc_to(P2, 0x0278, code, n); n += 4
    t, p = readback("pagecross", code, n, seeds=[(0x0278, bytes([seed] * 20))])
    check("a page request that leaves its 128-byte page is refused and says why",
          "128-byte page" in t, tail(t))
    check("no DONE is reported: ACC still holds the 10h byte count the program loaded, so "
          "the row's \"ACC = 0: DONE\" is only for requests the API accepts",
          p["P1"] == 16, p["P1"])
    check("and the array is untouched (seed 88h still at 0278h), so the refusal happened "
          "before any byte moved", p["P2"] == seed, p["P2"])


def leg_timing_decision():
    """The decision the plan demands be taken out loud (SIMULATION-PLAN.md Phase 5 item 16):
    the API does NOT burn tWRITE = 7..17 ms (table 25-7 printed p.115 gives min and max and
    leaves the typical column blank).  Both runs here take the same path -- accepted call,
    ROM's RET, four read-back instructions, park -- and are stepped by the same instruction
    count, so equal clocks is a real measurement and not a coincidence of counting.  If the
    stall is ever modelled this leg fails, which is the point: it makes the choice visible
    instead of implicit."""
    outs = []
    for cmd in (0x00, 0x02):
        code, n = api(cmd, acc=0x5c, dptr0=0x0100)
        code = movc_to(P1, 0x0100, code, n); n += 4
        code, n = park(code, n)
        img, n = program_image("t%d" % cmd, code, n, seeds=[(0x0100, bytes([0x3b]))])
        # exact count here: n instructions plus the boot ROM's own RET slot
        t = run(img, ["step %d" % (n + 1), "state", "dump sfr 0x90 0x90", "info hw boot"],
                "timing%d" % cmd)
        outs.append((clks(t), ports(t)["P1"], t))
    check("the byte-programming call costs exactly the same clocks as the ID read (%s vs "
          "%s): PROGRAM and ERASE return instantly by decision, because table 25-7 printed "
          "p.115 gives tWRITE as 7-17 ms with no typical value, and a fabricated "
          "14000-34000 machine-cycle stall at 24 MHz would corrupt every timing leg in the "
          "tree" % (outs[1][0], outs[0][0]),
          outs[0][0] is not None and outs[0][0] == outs[1][0],
          "%s/%s" % (outs[0][0], outs[1][0]))
    check("and it returns instantly because it is finished, not because it did nothing: the "
          "programmed byte reads back 5ch", outs[1][1] == 0x5c, outs[1][1])
    check("the decision is stated at the instrument too, so a later reader of `info hw "
          "boot' learns what is deliberately not modelled",
          "tWRITE" in outs[0][2], tail(outs[0][2], 200))


def leg_flash_survives_reset():
    """The array is non-volatile: a programmed byte survives `reset'.  ENBOOT is a register
    and does not survive it.  Persistence across simulator runs (a host-side backing file)
    is out of scope and is not asserted here."""
    code, n = api(0x02, acc=0x5c, dptr0=0x0100)
    img, n = program_image("persist", code + sjmp(), n + 1, seeds=[(0x0100, bytes([0x3b]))])
    t = run(img, ["step %d" % (n + SLACK), "dump rom 0x0100 0x0100", "reset",
                  "dump rom 0x0100 0x0100", "dump sfr 0x%02x 0x%02x" % (AUXR1, AUXR1)],
            "persist")
    rows = re.findall(r"0x0100\s+([0-9a-f]{2})\s", t)
    check("the programmed byte is still in the array after `reset' (flash is not part of "
          "the reset state): %s" % rows, len(rows) == 2 and rows[0] == rows[1] == "5c",
          rows)
    check("ENBOOT is clear again after the reset (table 3-11 printed p.8 leaves the bit X, "
          "figure 24-5 printed p.101 makes the value fuse driven, and the model resolves "
          "both to disabled)", byte_at(t, AUXR1) == 0x00, byte_at(t, AUXR1))


def leg_nothing_leaks_to_89c51r():
    """The AT89C51RB2/RC2 share cl_uc89c51r and have no boot ROM, so they must not gain the
    element, the window or the call: writing AUXR1.5 there is a write to a reserved bit and
    FFF0h must keep answering from their own array."""
    code, n = api(0x00, acc=SEED_ACC, dptr0=0x0000, auxr1=ENBOOT)
    code += mov_da(P1); n += 1
    a = run(None, ["info hw boot"], "rpart-elem", device="89C51R")
    check("-t 89C51R has no `boot' element (it is registered in cl_uc89c51rd2 only)",
          "boot[" not in a, tail(a))
    t, p = readback("rpart", code, n, device="89C51R")
    check("-t 89C51R keeps its flat mapping and executes its array at FFF0h: the stub marks "
          "P0 with %02xh even though the program wrote AUXR1.5" % SEED_STUB,
          p["P0"] == SEED_STUB, p["P0"])
    check("-t 89C51R answers nothing from a boot ROM it does not have: ACC keeps its seed "
          "%02xh instead of the ID" % SEED_ACC, p["P1"] == SEED_ACC, p["P1"])


def leg_reference_proof(ref):
    """The control half, stated rather than assumed: what a pre-boot-ROM binary does with
    the same images.  The legs that pass there are the absence legs, and they are the
    reason the rest can be believed."""
    if not os.path.exists(ref):
        check("control binary present (%s) -- point S51_REF at it to run the audit" % ref,
              False, "missing")
        return
    a = run(None, ["info hw boot"], "ref-elem", sim=ref)
    check("the control binary reports no `boot' element, so every window and ID leg above "
          "measures this change and not something that was already there",
          "boot[" not in a, tail(a))
    t = go("refmap", clr_a(), 1, ["dump rom 0x%04x 0x%04x" % (USER_CALL, USER_CALL)],
           sim=ref)
    rows = re.findall(r"0x%04x\s+([0-9a-f]{2})\s" % USER_CALL, t)
    check("the control binary leaves the window flat: FFF0h answers with the array byte "
          "%02xh" % STUB_BYTE, rows[:1] == ["%02x" % STUB_BYTE], rows)
    code, n = api(0x00, acc=SEED_ACC, dptr0=0x0000)
    code += mov_da(P1); n += 1
    t, p = readback("refcall", code, n, sim=ref)
    check("the control binary has no API to answer the call: ACC keeps its seed %02xh and "
          "the array's own stub ran (P0 = %02xh) -- which is why the ID legs above cannot "
          "have been passing before this change" % (SEED_ACC, SEED_STUB),
          p["P1"] == SEED_ACC and p["P0"] == SEED_STUB, "%s/%s" % (p["P1"], p["P0"]))


def main():
    print("SIM =", SIM)
    print("REF =", REF)
    leg_element_and_window()
    leg_inert_at_reset()
    leg_enboot_via_console()
    leg_window_is_code_space()
    leg_fetch_reaches_rom()
    leg_refused_without_enboot()
    leg_device_ids()
    leg_undocumented_id_selector()
    leg_dptr0_answers_under_dps()
    leg_program_and_read()
    leg_program_self_erases()
    leg_erase_block_geometry()
    leg_erase_block4_is_16k()
    leg_bad_erase_selector_and_no_chip_erase()
    leg_program_data_page()
    leg_program_page_must_stay_in_one_page()
    leg_timing_decision()
    leg_flash_survives_reset()
    leg_nothing_leaks_to_89c51r()
    leg_reference_proof(REF)
    print("\nChecks run: %d  Failed: %d" % (_checks, len(_failures)))
    if _failures:
        print("FAILURES: " + "; ".join(_failures))
        sys.exit(1)
    print("ALL BOOTROM CHECKS PASSED")


if __name__ == "__main__":
    main()
