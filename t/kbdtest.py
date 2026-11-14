#!/usr/bin/env python3
"""AT89C51RD2/ED2 keyboard interface: KBLS (0x9c), KBE (0x9d), KBF (0x9e) and the
KBD interrupt source (vector 0x003b, IEN1 bit 0, levels on IPL1/IPH1 bit 0, polling
rank 8).

Ground truth is Atmel doc4235K, page text extracted under /tmp/ds/ (pNNN.txt; p000
is the printed cover, so p058.txt is printed page 59).  The rules asserted here:

  section 15, p.59 (p058.txt): "8 inputs with programmable interrupt capability on
      both high or low level"; the keyboard pins are "Alternate function of Port 1"
      (p.3 block diagram, p002.txt); figure 15-2 draws the request as
      (KBF.x AND KBE.x) ORed into KBDIT.
  table 15-1, p.60 (p059.txt): KBF resets to 0000 0000b, "read only access, all
      flags are automatically cleared by reading the register", each bit "Set by
      hardware when the Port line N detects a programmed level" and "Must be
      cleared by software" -> read-clear, and a write changes nothing.
  table 15-2, p.61 (p060.txt): KBE "Cleared to enable standard I/O pin.  Set to
      enable KBF.x bit in KBF register to generate an interrupt request."  KBE
      gates the REQUEST, not the flag: a key with KBE.x=0 still sets KBF.x, which
      is why legs C1/C5/C7 also assert KBF -- without that half they would be
      vacuous on a build that has no keyboard at all.
  table 15-3, p.62 (p061.txt): KBLS selects the level that counts as a key per
      line, 0 = low, 1 = high.  C1 versus C1b is the polarity pair on one pin (the
      same low level flags with KBLS.0=0 and does not with KBLS.0=1); C4/C5 are the
      two senses over all eight lines with every pin high.
  table 17-2, p.73 (p072.txt): KBDIT -> vector 003Bh, polling priority 8, i.e.
      behind timer 2 (7) and ahead of the 0043h slot, which has no source.
  table 17-6, p.77 (p076.txt): IEN1 bit 0 = KBD enable.  Tables 17-7/17-8,
      pp.78-79 (p077/p078.txt): IPL1 bit 0 KBDL and IPH1 bit 0 KBDH give the same
      four levels as the IPL0/IPH0 pair; bit 1 of both is Reserved (and so is bit
      7, which is what leg E2 pins).
  table 7-1, pp.18-19 (p017/p018.txt): CKCON0 has WDX2 PCAX2 SIX2 T2X2 T1X2 T0X2
      X2 and NO keyboard bit, so the detector is asynchronous and the model samples
      it every machine cycle instead of counting it.

What the datasheet does NOT say, and the reading the model takes (argued in
rd2_kbdcl.h): nothing states whether KBF.x is set only on the transition into the
programmed level or continuously while the level is present.  p.82 (p081.txt) lists
the keyboard with INT0#/INT1# as a power-down exit with "same behavior as external
interrupt", i.e. level behaviour, so the flag is set for as long as the level is
applied.  Two consequences are asserted rather than mentioned: leg D2 (key still
down: a read clears the flag and the next sample sets it again, so two reads both
return 0x01) and leg D1 (key released: the flag survives one read as a latch, the
read clears it, the next read returns 0).

HOW A KEY IS INJECTED.  The model reads the port through the port's own read hook,
which answers `latch AND port_pins' -- the same value `MOV A,P1' answers -- so a test
image presses a key with an ordinary `MOV P1,#..' and releases it with another one.
No console plumbing, no cfg variable, no new command is involved.  Two consequences
the legs work around, both of which produced a plausible false result while this file
was being written:
  - a high-detect line is a KEY while its pin is high, and P1 resets to 0xff, so the
    act of programming KBLS can press a key.  Every setup therefore reads KBF once
    (clearing whatever the setup itself caused) into IRAM 0x3f before it enables
    anything, and the "must not fire" legs assert KBF == 0 after that clear.
  - releasing means "drive the line to the level KBLS says is NOT a key", so a
    high-detect line is released LOW: kbd_isr(release_to=0xfe), not 0xff.

Four console traps defended against here:
  - `dump sfr 0x9c 0x9e' is NOT a passive read once 0x9e carries a read-clear hook:
    it consumes the flag.  Asking the instrument about the request afterwards then
    answers `request=no' with a key held down, which looks exactly like a masking
    defect (it produced the two apparent C8/C9 failures).  Instrument first, dumps
    second -- the trap will bite any other read-clear register in this fork.
  - asserting that an unwritten SFR reads 0 proves nothing: it passes on a binary with
    no peripheral at all.  The first draft of A1 did exactly that and passed against
    the vendor build; A1 now asserts the P1-cell relation only the model can answer,
    and the reset values are asserted through the element's instrument line (A2).
  - `break' + `go' with an unreached breakpoint wedges the console until the
    subprocess timeout, so every run sets a SECOND breakpoint on a firmware deadline
    (MOV R7,#0xff ; DJNZ R7,$ ; SJMP $) and whichever hits first ends the run.  A leg
    on a binary with no keyboard falls through to the deadline and fails by name.
  - `dump data' is not a command in this build (`dump iram' is) and `dump sfr' needs
    a range; a typo there prints a usage block and no value, which reads as "the byte
    is zero" to anything that only looks for a number.
  - IRAM is NOT zeroed by reset in this build (observed 0x41 0xe7 0x3e), so every
    counter asserted here is written by the setup first; asserting an uninitialised
    byte once reported "232 handler entries" for a handler that ran once.
"""

import os
import re
import subprocess
import sys

SIM = (os.environ.get("S51_SIM") or os.environ.get("S51") or
       os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51"))
WORKDIR = "/tmp/kbd-suite"
CHIP = "89C51RD2"
TIMEOUT = 30

KBLS, KBE, KBF = 0x9c, 0x9d, 0x9e
P1, IP, IPH, TCON, TMOD = 0x90, 0xb8, 0xb7, 0x88, 0x89
IEN1, IPL1, IPH1, P4, IE, PCON = 0xb1, 0xb2, 0xb3, 0xc0, 0xa8, 0x87
EA, ET1 = 0x80, 0x08
VECTOR = 0x3b
MARK = 0x3b                       # what the KBD handler writes into P4

CLEAR_TO = 0x3f                   # IRAM byte the setup's clearing read lands in
KBF_SEEN = 0x30                   # IRAM byte the handler's read of KBF lands in
COUNT = 0x31                      # IRAM byte the handler increments

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


def mov_a(a):
    """MOV A,direct."""
    return bytes([0xe5, a])


def mov_d(a):
    """MOV direct,A."""
    return bytes([0xf5, a])


def _hex(code):
    lines = []
    for off in range(0, len(code), 16):
        chunk = code[off:off + 16]
        b = bytes([len(chunk), (off >> 8) & 0xff, off & 0xff, 0]) + chunk
        lines.append(":%02X%04X%02X%s%02X" % (len(chunk), off & 0xffff, 0,
                                              chunk.hex().upper(), (-sum(b)) & 0xff))
    lines.append(":00000001FF")
    text = "\n".join(lines) + "\n"
    for ln in text.splitlines():
        assert sum(bytes.fromhex(ln[1:])) & 0xFF == 0, "bad checksum in %r" % ln
    return text.encode()


def run(code, cmds, name="k", chip=CHIP, timeout=TIMEOUT):
    """Load `code`, run `cmds`, return the transcript ('<<TIMEOUT>..' if it wedged)."""
    os.makedirs(WORKDIR, exist_ok=True)
    img = os.path.join(WORKDIR, "%s_%d_%x.hex" % (name, len(code), sum(code) & 0xffff))
    with open(img, "wb") as f:
        f.write(_hex(code))
    args = [SIM, "-t", chip, "-c", "-"]
    stdin = ('file "%s"' % img) + "\n" + "\n".join(cmds) + "\n"
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, text=True,
                           cwd=WORKDIR, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "<<TIMEOUT: console wedged (unreached breakpoint?)>>"
    return p.stdout


# ---------------------------------------------------------------- image building
#
# Layout of every image built here:
#   0x0000  setup MOVs: P4 (the marker port) first, the marker counters zeroed, the
#           registers configured, one read of KBF to clear anything the setup itself
#           pressed, and the global enable LAST, so no vector can be taken before the
#           marker is clear;
#   then     MOV R7,#0xff / DJNZ R7,$   the firmware deadline (~510 machine cycles);
#   then     SJMP $                     the deadline landing point -- break here;
#   0x003b   KBD handler.
# The deadline is what makes every run finite on a binary that has no keyboard source
# at all: that breakpoint is always reachable.

DEADLINE = bytes([0x7d, 0xff, 0xd7, 0xfe])       # MOV R7,#0xff ; DJNZ R7,$


def image(setup, isr=b"", isr_at=VECTOR, pad_to=0x40):
    """Place `setup` at 0, the deadline right after it, and `isr` at `isr_at`."""
    body = setup + DEADLINE
    total = max(pad_to, isr_at + len(isr), len(body) + 2)
    b = bytearray(total)
    b[0:len(body)] = body
    b[len(body)] = 0x80
    b[len(body) + 1] = 0xfe                       # SJMP $ : the deadline landing point
    if isr:
        b[isr_at:isr_at + len(isr)] = isr
    return bytes(b), len(body)


def kbd_isr(marker=MARK, release_to=0xff, count_at=COUNT, kbf_to=KBF_SEEN):
    """Handler: mark P4, drive the lines to the level that means `no key' for the
    KBLS pattern in use, read KBF into IRAM, count the entries, RETI."""
    out = bytearray(mov(P4, marker))
    if release_to is not None:
        out += mov(P1, release_to)
    out += mov_a(KBF) + mov_d(kbf_to) + bytes([0x05, count_at]) + bytes([0x32])
    return bytes(out)


def arm(kbls=0x00, kbe=0x00, p1=None, ien1=0x01, ie=EA, extra=b""):
    """Standard setup: marker cleared, counters zeroed, registers loaded, KBF read to
    clear what the setup pressed, then the enables.  Returns (code, deadline_addr)."""
    setup = mov(P4, 0) + mov(COUNT, 0) + mov(KBF_SEEN, 0) + mov(IPL1, 0) + mov(IPH1, 0)
    setup += mov(KBLS, kbls) + mov(KBE, kbe)
    if p1 is not None:
        setup += mov(P1, p1)
    setup += extra
    setup += mov_a(KBF) + mov_d(CLEAR_TO)         # clear whatever the setup pressed
    setup += mov(IEN1, ien1) + mov(IE, ie)        # enables last
    return image(setup, isr=kbd_isr())


# ------------------------------------------------------------------- parsing
def stop_at(out):
    m = re.search(r"^Stop at 0x([0-9a-f]{6}):", out, re.M)
    return int(m.group(1), 16) if m else None


def acc(out):
    m = re.search(r"ACC=\s+0x([0-9a-f]{2})", out)
    return int(m.group(1), 16) if m else None


def sfr(out, addr):
    """`0x9e KBF:   0b00000001 0x01 '.' 1' -> 0x01."""
    m = re.search(r"^0x%02x \w+:\s+0b[01]+\s+0x([0-9a-f]{2})" % addr, out, re.M)
    return int(m.group(1), 16) if m else None


def hx(v):
    return "0x%02x" % v if v is not None else "None"


def kbd_info(out):
    m = re.search(r"^kbd\[\d+\] (.*)$", out, re.M)
    return m.group(1) if m else None


def kbd_field(out, name):
    line = kbd_info(out)
    if not line:
        return None
    m = re.search(re.escape(name) + r"=(\S+)", line)
    return m.group(1) if m else None


def kbd_level(out):
    """(vector, poll, level) from the element's second instrument line."""
    m = re.search(r"^KBF&KBE=0x[0-9a-f]{2} vector=(0x[0-9a-f]+) poll=(\d+) level=(\d+)",
                  out, re.M)
    return m.groups() if m else None


def iram(out, addr):
    """`0x30   01 00 00 ...' -> the byte at `addr'.  Every dump line is examined: a
    second `dump iram 0x3f 0x3f' after a `dump iram 0x30 0x31' is its own row, and a
    parser that stops at the first row answers None for anything outside it."""
    for m in re.finditer(r"^0x([0-9a-f]{2,4})\s+((?:[0-9a-f]{2} ){1,16})", out, re.M):
        base = int(m.group(1), 16)
        vals = m.group(2).split()
        if base <= addr < base + len(vals):
            return int(vals[addr - base], 16)
    return None


def irq_row(out, vector=VECTOR):
    """`  0x00003b dis  0 no  act keyboard'.  `info hardware irq' prints 6 hex digits
    where `dump' prints 8, so match the value rather than a width."""
    want = "0x%06x" % vector
    for ln in out.splitlines():
        if ln.strip().startswith(want):
            return ln
    return None


def dump_sfr(*addrs):
    return "dump sfr 0x%02x 0x%02x" % (min(addrs), max(addrs))


def dump_iram(*addrs):
    return "dump iram 0x%02x 0x%02x" % (min(addrs), max(addrs))


# ------------------------------------------------------------------------ legs
def leg_reset():
    print("== A: reset state and registration ==")
    # A1 has to be something ONLY the model can answer, and "read 0x9c/0x9d/0x9e and
    # expect 0" is not: unwritten SFR space reads 0 in this simulator, so that version
    # PASSED against a binary with no keyboard at all (Main measured it on the vendor
    # build).  The reset values are still asserted -- in A2, through the element's own
    # instrument line, which does not exist without the model.  What is asserted here
    # is the relation the whole block rests on: the comparator watches the real P1
    # cell, so the latch firmware writes is the level it samples (rd2_kbd.cc reads P1
    # through cl_port::read() and never writes that cell).
    out = run(bytes([0x80, 0xfe]), ["step 1", "info hardware kbd", "exit"],
              name="p1_reset")
    at_reset = kbd_field(out, "P1")
    out = run(mov(P1, 0xfe) + bytes([0x80, 0xfe]),
              ["step 4", "info hardware kbd", dump_sfr(P1), "exit"], name="p1_latch")
    check("A1 the element watches the real P1 cell: 0xff at reset, follows MOV P1",
          at_reset == "0xff" and kbd_field(out, "P1") == "0xfe" and
          sfr(out, P1) == 0xfe and kbd_field(out, "KBF") == "0x01",
          "instrument P1=%s at reset, %s after MOV P1,#0xfe (dump sfr 0x90 says %s); "
          "that press also flagged KBF=%s, so the comparator reads that cell" %
          (at_reset, kbd_field(out, "P1"), hx(sfr(out, P1)), kbd_field(out, "KBF")))

    out = run(bytes([0x80, 0xfe]), ["info hardware kbd", "exit"], name="info")
    line = kbd_info(out)
    check("A2 kbd element exists and reports the three registers idle",
          line is not None and kbd_field(out, "KBLS") == "0x00" and
          kbd_field(out, "KBE") == "0x00" and kbd_field(out, "KBF") == "0x00" and
          kbd_field(out, "request") == "no" and kbd_field(out, "KBD") == "0" and
          kbd_field(out, "EA") == "0",
          "kbd line: %s" % (line if line else "NO kbd[] ELEMENT (element absent)"))
    g = kbd_level(out)
    check("A3 second line names vector, polling rank and level",
          g == ("0x003b", "8", "0"), "got %s (table 17-2 p.73, 17-7/17-8 pp.78-79)" % (g,))

    out = run(bytes([0x80, 0xfe]), ["info hardware irq", "exit"], name="irq")
    row = irq_row(out)
    check("A4 a source is registered at 0x003b named keyboard",
          row is not None and "keyboard" in row,
          "row: %s" % (row.strip() if row else "NO ROW AT 0x00003b"))
    rows = [ln.strip() for ln in out.splitlines()
            if re.match(r"\s*0x0{4}(2b|3b)\b", ln)]
    check("A5 KBD polls behind timer #2 (rank 8 of table 17-2)",
          bool(rows) and rows[-1].startswith("0x00003b"),
          "order: %s" % " | ".join(r.split()[0] for r in rows))

    out = run(bytes([0x80, 0xfe]), ["info hardware kbd", "info hardware irq", "exit"],
              name="51r", chip="89C51R")
    check("A6 absent on 89C51R: no kbd element, no 0x3b source",
          kbd_info(out) is None and irq_row(out) is None,
          "GUARD: the AT89C51RB2/RC2 must not gain the interface (SIMULATION-PLAN D13)")


def leg_fire():
    print("== B: an enabled key vectors to 0x003b ==")
    code, dl = arm(kbls=0x00, kbe=0x01, p1=0xfe, ien1=0x01, ie=EA)
    out = run(code, ["break 0x%02x" % VECTOR, "break 0x%02x" % dl, "go",
                     dump_sfr(KBF), dump_sfr(P4), "exit"], name="fire1")
    check("B1 entry PC is the keyboard vector 0x003b", stop_at(out) == VECTOR,
          "Stop at 0x%04x, deadline 0x%04x -- nothing vectors to 0x3b unless the "
          "source exists" % ((stop_at(out) or 0), dl))
    check("B2 KBF is set as the vector is taken, handler body not yet run",
          sfr(out, KBF) == 0x01 and sfr(out, P4) == 0x00,
          "KBF=%s P4=%s" % (hx(sfr(out, KBF)), hx(sfr(out, P4))))

    code2, _ = image(arm_setup(kbls=0x00, kbe=0x01, p1=0xfe, ien1=0x01, ie=EA),
                     isr=kbd_isr())
    out = run(code2, ["step 80", dump_sfr(P4), dump_iram(KBF_SEEN, COUNT),
                      dump_sfr(KBF), "info hardware kbd", "exit"], name="fire2")
    check("B3 the handler at 0x003b ran and read KBF as 0x01",
          sfr(out, P4) == MARK and iram(out, KBF_SEEN) == 0x01,
          "P4=%s want 0x%02x, KBF read=%s" % (hx(sfr(out, P4)), MARK,
                                              hx(iram(out, KBF_SEEN))))
    check("B4 exactly one entry after the handler released the key",
          iram(out, COUNT) == 0x01, "entry counter (IRAM 0x%02x) = %s" %
          (COUNT, hx(iram(out, COUNT))))
    check("B5 the read in the handler cleared KBF: level gone, request down",
          sfr(out, KBF) == 0x00 and kbd_field(out, "request") == "no",
          "KBF=%s request=%s" % (hx(sfr(out, KBF)), kbd_field(out, "request")))


def arm_setup(kbls=0x00, kbe=0x00, p1=None, ien1=0x01, ie=EA, extra=b""):
    """The setup bytes arm() builds, exposed for legs that need their own image()."""
    setup = mov(P4, 0) + mov(COUNT, 0) + mov(KBF_SEEN, 0) + mov(IPL1, 0) + mov(IPH1, 0)
    setup += mov(KBLS, kbls) + mov(KBE, kbe)
    if p1 is not None:
        setup += mov(P1, p1)
    setup += extra
    setup += mov_a(KBF) + mov_d(CLEAR_TO)
    setup += mov(IEN1, ien1) + mov(IE, ie)
    return setup


def leg_masked():
    print("== C: what must NOT fire, and what still sets KBF ==")

    def run_case(name, **kw):
        """`dump sfr 0x9c 0x9e' is not a passive read here: going through the 0x9e
        cell consumes the read-clear of table 15-1, so anything that asks about the
        request afterwards sees a cleared register.  A draft of C8/C9 put `dump sfr'
        first and dutifully reported `request=no' with a key held down -- the model
        was right and the transcript looked exactly like a masking defect.  The
        instrument lines therefore come before the dumps."""
        isr = kw.pop("isr_release_to", 0xff)
        code, dl = image(arm_setup(**kw), isr=kbd_isr(release_to=isr))
        return code, dl, run(code, ["break 0x%02x" % dl, "go",
                                    "info hardware kbd", "info hardware irq",
                                    dump_iram(CLEAR_TO, CLEAR_TO),
                                    dump_iram(KBF_SEEN, COUNT),
                                    dump_sfr(KBF), dump_sfr(P4), "exit"], name=name)

    _, dl, out = run_case("c_kbe", kbls=0x00, kbe=0x00, p1=0xfe, ien1=0x01, ie=EA)
    check("C1 KBE=0: no request, but the pin still set KBF (table 15-2)",
          stop_at(out) == dl and sfr(out, P4) == 0 and sfr(out, KBF) == 0x01 and
          kbd_field(out, "request") == "no",
          "PC=%s P4=%s KBF=%s request=%s" % (hx(stop_at(out)), hx(sfr(out, P4)),
                                             hx(sfr(out, KBF)), kbd_field(out, "request")))

    _, dl, out = run_case("c_sense", kbls=0x01, kbe=0x00, p1=0xfe, ien1=0x01, ie=EA)
    check("C1b the SAME low line does not flag with KBLS.0=1 (polarity pair)",
          stop_at(out) == dl and sfr(out, KBF) == 0x00 and sfr(out, P4) == 0 and
          kbd_field(out, "request") == "no",
          "KBF=%s P4=%s request=%s -- the low level is a key only for a low-detect "
          "line, and the setup's own clearing read saw %s" %
          (hx(sfr(out, KBF)), hx(sfr(out, P4)), kbd_field(out, "request"),
           hx(iram(out, CLEAR_TO))))

    _, dl, out = run_case("c_ien1", kbls=0x00, kbe=0x01, p1=0xfe, ien1=0x00, ie=EA)
    check("C2 IEN1 bit 0 clear: flag set, request up, no vector (table 17-6)",
          stop_at(out) == dl and sfr(out, P4) == 0 and sfr(out, KBF) == 0x01 and
          kbd_field(out, "KBD") == "0",
          "P4=%s KBF=%s KBD=%s" % (hx(sfr(out, P4)), hx(sfr(out, KBF)),
                                   kbd_field(out, "KBD")))

    _, dl, out = run_case("c_ea", kbls=0x00, kbe=0x01, p1=0xfe, ien1=0x01, ie=0x00)
    check("C3 EA clear masks it like every other source",
          stop_at(out) == dl and sfr(out, P4) == 0 and sfr(out, KBF) == 0x01,
          "P4=%s KBF=%s" % (hx(sfr(out, P4)), hx(sfr(out, KBF))))

    _, dl, out = run_case("c_low", kbls=0x00, kbe=0xff, p1=None, ien1=0x01, ie=EA)
    check("C4 KBLS=0 with every line high is not a key (table 15-3)",
          stop_at(out) == dl and sfr(out, KBF) == 0x00 and sfr(out, P4) == 0 and
          kbd_field(out, "request") == "no",
          "KBF=%s (setup's own clear read saw %s) P4=%s request=%s" %
          (hx(sfr(out, KBF)), hx(iram(out, CLEAR_TO)), hx(sfr(out, P4)),
           kbd_field(out, "request")))

    _, dl, out = run_case("c_high_none", kbls=0xff, kbe=0x00, p1=None, ien1=0x01,
                          ie=EA)
    check("C5 KBLS=0xff with every line high flags all eight bits (table 15-3)",
          stop_at(out) == dl and sfr(out, KBF) == 0xff and sfr(out, P4) == 0 and
          kbd_field(out, "request") == "no",
          "KBF=%s P4=%s request=%s (P1 resets to 0xff, so a high-detect line is a key "
          "out of reset; the flag is set with KBE clear, table 15-2)" %
          (hx(sfr(out, KBF)), hx(sfr(out, P4)), kbd_field(out, "request")))

    code, dl = image(mov(P4, 0) + mov(COUNT, 0) + mov(KBF_SEEN, 0) + mov(IPL1, 0) +
                     mov(IPH1, 0) + mov(KBLS, 0x01) + mov(KBE, 0x01) + mov(P1, 0xfe) +
                     mov(P1, 0xff) + mov_a(KBF) + mov_d(CLEAR_TO) +
                     mov(IEN1, 0x01) + mov(IE, EA),
                     isr=kbd_isr(release_to=0xfe, count_at=COUNT, kbf_to=KBF_SEEN))
    out = run(code, ["break 0x%02x" % dl, "go", dump_sfr(KBF), dump_sfr(P4),
                     dump_iram(KBF_SEEN, COUNT), "info hardware kbd", "exit"],
              name="c_high_one")
    check("C6 one high-detect line: only bit0 flags and only bit0 fires",
          sfr(out, P4) == MARK and iram(out, KBF_SEEN) == 0x01 and
          iram(out, COUNT) == 0x01 and sfr(out, KBF) == 0x00 and
          kbd_field(out, "request") == "no",
          "handler read %s (want 0x01), entries=%s, KBF after release=%s" %
          (hx(iram(out, KBF_SEEN)), hx(iram(out, COUNT)), hx(sfr(out, KBF))))

    _, dl, out = run_case("c_bit", kbls=0x00, kbe=0x02, p1=0xfe, ien1=0x01, ie=EA)
    check("C7 KBE masks per bit: KBF.0 set, KBE.0 clear -> no request",
          stop_at(out) == dl and sfr(out, P4) == 0 and sfr(out, KBF) == 0x01 and
          kbd_field(out, "request") == "no",
          "KBF=%s P4=%s request=%s" % (hx(sfr(out, KBF)), hx(sfr(out, P4)),
                                       kbd_field(out, "request")))

    _, dl, out = run_case("c_req", kbls=0x00, kbe=0x03, p1=0xfe, ien1=0x01, ie=0x00)
    row = irq_row(out)
    check("C8 request asserted with KBD enabled but EA clear still does not vector",
          stop_at(out) == dl and sfr(out, P4) == 0 and
          kbd_field(out, "request") == "YES" and kbd_field(out, "KBD") == "1" and
          kbd_field(out, "EA") == "0",
          "request=%s KBD=%s EA=%s P4=%s" % (kbd_field(out, "request"),
                                             kbd_field(out, "KBD"),
                                             kbd_field(out, "EA"), hx(sfr(out, P4))))
    check("C9 the 0x3b row says enabled and requesting while EA masks the vector",
          row is not None and "en" in row.split() and "YES" in row.split(),
          "row (Handler En Pr Req Act Name): %s" %
          (row.strip() if row else "NO ROW AT 0x00003b"))

    # C10: section 15 p.59 says the keyboard inputs "allow to exit from idle and
    # power-down modes" and p.82 gives them "same behavior as external interrupt".
    # cl_51core::do_interrupt() ends stIDLE when it finds an eligible source
    # (uc51.cc:2151), so a request that already exists when PCON.idl is set does end
    # idle.  A press that happens WHILE idle cannot be seen: cl_uc::tick() ticks the
    # hardware only in stGO, so the comparator never samples the new level.  That gap
    # is a limitation of the core's idle loop, not of this peripheral, and it is
    # written up in /tmp/kbd-report.md rather than papered over here.
    idle = bytes([0x75, PCON, 0x01])            # MOV PCON,#bit0 (IDL)
    code, dl = image(arm_setup(kbls=0x00, kbe=0x01, p1=0xfe, ien1=0x01, ie=EA) + idle,
                     isr=kbd_isr())
    out = run(code, ["step 60", dump_sfr(P4), dump_sfr(PCON), "exit"], name="idle")
    pcon = sfr(out, PCON)
    check("C10 a request that exists before PCON.idl ends idle (p.59, p.82)",
          sfr(out, P4) == MARK and pcon is not None and pcon & 0x01 == 0,
          "P4=%s (want 0x%02x) PCON=%s (bit0 must be cleared by the wake)" %
          (hx(sfr(out, P4)), MARK, hx(sfr(out, PCON))))


def leg_flag():
    print("== D: KBF is a read-clear latch, and read-only ==")
    setup = (mov(P4, 0) + mov(KBLS, 0x00) + mov(KBE, 0x01) + mov(IEN1, 0x00) +
             mov(P1, 0xfe) + mov(P1, 0xff) +
             mov_a(KBF) + mov_d(0x30) + mov_a(KBF) + mov_d(0x31) +
             mov_a(KBF) + mov_d(0x32))
    code, dl = image(setup)
    out = run(code, ["step 24", dump_iram(0x30, 0x32), dump_sfr(KBF),
                     "info hardware kbd", "exit"], name="d_latch")
    got = [iram(out, a) for a in (0x30, 0x31, 0x32)]
    check("D1 key released: flag survives one read, that read clears it",
          got == [0x01, 0x00, 0x00],
          "three reads gave %s want [0x01, 0x00, 0x00] (table 15-1)" %
          "[0x%02x, 0x%02x, 0x%02x]" % tuple(v if v is not None else 0xff for v in got))

    setup = (mov(P4, 0) + mov(KBLS, 0x00) + mov(KBE, 0x01) + mov(IEN1, 0x00) +
             mov(P1, 0xfe) + mov_a(KBF) + mov_d(0x30) + mov_a(KBF) + mov_d(0x31))
    code, dl = image(setup)
    out = run(code, ["step 24", dump_iram(0x30, 0x31), "exit"], name="d_held")
    got = [iram(out, 0x30), iram(out, 0x31)]
    check("D2 key still down: the read clears it and the level sets it again",
          got == [0x01, 0x01],
          "two reads gave %s (level reading, see rd2_kbdcl.h)" %
          "[0x%02x, 0x%02x]" % tuple(v if v is not None else 0xff for v in got))

    setup = (mov(P4, 0) + mov(KBE, 0x01) + mov(KBF, 0xff) + mov_a(KBF) + mov_d(0x30))
    code, dl = image(setup)
    out = run(code, ["step 20", dump_iram(0x30, 0x30), dump_sfr(KBF),
                     "info hardware kbd", "exit"], name="d_ro")
    check("D3 KBF is read-only: MOV KBF,#0xff with no key reads back 0x00",
          iram(out, 0x30) == 0x00 and sfr(out, KBF) == 0x00 and
          kbd_field(out, "request") == "no",
          "read back %s, KBF=%s (a plain cell answers 0xff)" %
          (hx(iram(out, 0x30)), hx(sfr(out, KBF))))


def leg_level():
    print("== E: the KBD level comes from IPL1/IPH1 bit 0 ==")
    for n, (lo, hi, want) in enumerate(
            ((0x00, 0x00, "0"), (0x01, 0x00, "1"), (0x00, 0x01, "2"), (0x01, 0x01, "3"))):
        setup = (mov(P4, 0) + mov(KBLS, 0x00) + mov(KBE, 0x01) +
                 mov(IPL1, lo) + mov(IPH1, hi))
        code, dl = image(setup)
        out = run(code, ["step 12", "info hardware kbd", "exit"],
                  name="lvl%d" % n)
        g = kbd_level(out)
        check("E1%d IPL1=0x%02x IPH1=0x%02x -> KBD level %s" % (n, lo, hi, want),
              g is not None and g[2] == want,
              "level column says %s (tables 17-7/17-8: L is +1, H is +2)" %
              (g[2] if g else "no kbd element"))
    setup = mov(P4, 0) + mov(IPL1, 0x80) + mov(IPH1, 0x80)
    code, dl = image(setup)
    out = run(code, ["step 12", "info hardware kbd", "exit"], name="lvl_reserved")
    g = kbd_level(out)
    check("E2 reserved IPL1/IPH1 bit 7 does not raise the level",
          g is not None and g[2] == "0",
          "level=%s -- the pair selector must never reach the register mask "
          "(reserved bits read back what was written)" % (g[2] if g else "n/a",))


def main():
    print("SIM = %s" % SIM)
    if not os.path.exists(SIM):
        print("SIM does not exist: %s" % SIM)
        return 2
    leg_reset()
    leg_fire()
    leg_masked()
    leg_flag()
    leg_level()
    print("\nChecks run: %d" % _checks)
    if _failures:
        print("Failures (%d):" % len(_failures))
        for f in _failures:
            print("  %s" % f)
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
