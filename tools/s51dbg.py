#!/usr/bin/env python3
"""Persistent driver for one ucsim_51 (-t 89C51R) session.

Why this exists: the console REPL freezes its own stdin while the target runs
(`run` stops only on ENTER), so probing the simulator means either respawning
it for every question or owning the console properly.  This module owns the
latter: it spawns the binary with `-Z <free port>`, keeps ONE session open and
answers requests over it, in-process or as a JSONL server.

Everything here is measured against the bed `$S51_SIM` (default: this tree's
own build, ucsim 0.8.15, `-t 89C51R`), NOT taken from the ucsim
documentation.  The facts that shaped the code:

* Spawn is `SIM -t 89C51R -c - -Z PORT`.  `-c -` is NOT optional: without it
  the CPU free-runs as soon as the telnet console opens (PC was already
  0xdd before the first command) and a later `step` kills the process and the
  socket.  `-c -` adds a stdio console, so the prompt becomes `3> `.  `-G`
  means GO here -- never pass it.
* The console is a *telnet* server: it opens with IAC negotiation, every byte
  of output is ANSI-painted, escape sequences SPLIT ACROSS socket reads, and
  the command you typed is echoed back.  `command()` strips IAC + ANSI
  (carrying a partial sequence to the next read) and drops the echo line.
* End of response = a line ending in `> ` at the tail of the stream.  The
  echo never ends that way, so it cannot be mistaken for a prompt.
* `file` needs a QUOTED path: unquoted it answers `File name is missing.` and
  ROM keeps its 0xff power-on fill, which is `MOV R7,A` -- the CPU still steps,
  so a refused load still LOOKS alive.  A load that succeeds prints NOTHING on
  the network console, so `start()` proves it by reading ROM back; an `N words
  read` report on a build that does print it comes back as `words_read` (always
  None on this build).
* Expressions use BRACKETS, not colons: `iram[0x41]`, `sfr[0x8e]`, `rom[0]`,
  or a name from `info variables` (`AUXR`, `ACC`, `flag`).  `dseg:0x30` prints
  `Parser error: syntax error` and THEN `0`; `expr()` reports the error with
  value None and never launders it into zero.
* Fetch breakpoints take a bare address.  `break rom ADDR`, a named watchpoint
  (`break AUXR w`) and `break ADDR if <expr>`-style keyword junk are rejected;
  watchpoints are `break <space> r|w <addr>`.  Hit counts are POSITIONAL
  (`break 0x37 2`); `break 0x37 hit 2` silently keeps Hit 1.
* Hit count means PASSES of the address, not `cont()` calls: a loop walked
  inside one `run` stops at Cnt 2, while `break 0x37 2` on a program that
  walks 0x37 once never stops at all.
* Conditions DO work, but the console splits its input on spaces, so `cond` is
  quoted before sending and the `cond="..."` text from `info break` is what
  gets reported back.  Two traps are surfaced, never hidden -- see
  `cond_trust()`.
* `var name memory <addr>` (the documented form) prints its usage block and
  registers nothing; the cell form `var flag iram[0x40]` works and the name is
  then usable in conditions.
* There is NO usable disassembler: `disasm A N` is a Parser error in every
  spelling and `disassemble A N` prints nothing.  An address is therefore only
  PROVEN to be an instruction boundary by being seen executing in `history
  list`; `at_instruction_boundary` is tri-state and None means "unknown",
  never "fine".
* Stop codes are the only trustworthy signal, because a `step` that merely
  lands on an address prints the same `Stop at <addr>` as a breakpoint that
  fires there: `(104) Breakpoint`, `(112) Event break` (reported at the
  instruction AFTER the access), `(109) stepped N ticks`, `(105) User stopped`
  -- that last one is what the ENTER poke produces, so a watchpoint that never
  triggers looks exactly like a successful interrupt.
* Whether single-stepping onto a fetch breakpoint stops is PARITY-DEPENDENT on
  the stock build (its first fetch after a stop is exempt, so `step 30` can
  hit what twelve `step` calls miss).  Nothing here relies on either
  direction; that is a property of one binary, not an invariant.
* Loads: a REFUSAL is printed (`File name is missing.` for an unquoted path,
  `no loadable file found (...)` for a missing one) and leaves ROM at its 0xff
  power-on fill -- `MOV R7,A`, so the CPU still steps and looks alive.  The
  SUCCESS report (`Loading from <p>`, `233 words read from <p>`) depends on
  WHICH CONSOLE got the command and on `-q`, never on terminal-ness.  Measured
  with stdout a PIPE in every row: `file` on the `-c -` stdin console -> report
  present; no `-c -` -> present too; `file` on the `-Z` network console -> no
  report anywhere; `-q` -> none.  The trap: a PIPE is BLOCK-buffered, so nothing
  appears until the child exits (`stdio_load_report` closes stdin and reads to
  EOF; `tty_load_report` reads it live off a pty, `start(report_check=True)`
  asks for it without disturbing the session).  Match the two LINES and the
  word count -- never the size of the capture: both lines interpolate the path,
  so the same successful load measured 359 bytes via an absolute path and 305
  via a relative one.  ROM readback is still the proof -- `expect_file()` diffs
  the file's own bytes against `dump rom`, `expect={addr: [bytes]}` checks a
  handful.
* A brand-new breakpoint row reads `Hit 1  Cnt 1` and an unconditional row's
  `Cond` column reads `true`.  Neither is execution evidence: `Hit`/`Cnt` are
  display values at creation, and `Cond` is re-evaluated LIVE every time
  `info break` runs -- it is not the stored condition (measured: it goes
  `false` -> `true` across a run while the stored text never changes).
* Breakpoint KEYWORDS are build-dependent, so the driver sends only the
  POSITIONAL form.  On the pre-fix build `break 0x7 hit 2` keeps `Cnt 1` (the
  keyword is dropped) and `break 0x5 if 0`, `if 1`, even `break 0x8 bogus 3`
  each create an UNCONDITIONAL breakpoint whose row then prints Cond `true`;
  the fixed build honours `hit N` and refuses those conditions and keywords.
  `t/s51dbgtest.py` classifies the binary once (`break 0x5 if 0`: row or
  refusal) and pins whichever matrix that build belongs to.
* `delete` wants the ID from `info break`.  `delete 0x3a` / `delete 58` print
  NOTHING and change nothing; the documented `del break` prints `Error`.
  `del_break()` resolves the address to its id and re-reads the table, because
  the reply is never the proof here.
* `run` freezes the console.  `cont()` sends `run`, waits for the stream to go
  quiet, and only then pokes exactly one ENTER (`stop()` is that poke alone).
  With `settle > timeout` it never pokes, so a target that never stops reports
  `{"stopped": false, "timeout": true}` instead of hanging.
* Reading a read-clear SFR through the console consumes the clear: `dump()`
  and `regs()` are perturbing reads, not observations.
* The bed exits when the console closes.  `close()` says `quit` and reaps;
  driver-side SIGINT restores the previous handler first so Ctrl-C cannot
  orphan a simulator holding the port.

Usage
  tools/s51dbg.py --jsonl                    # one JSON request per line
  tools/s51dbg.py --once 'file "/x.ihx"; break 0x37; cont'
  tools/s51dbg.py --cmd '{"cmd":"load","path":"t/isa.ihx",
    "expect":{"0":[2,0,48]}}' --cmd '{"cmd":"expect_file",
    "path":"t/isa.ihx"}' --cmd '{"cmd":"pc"}'
  from s51dbg import S51Dbg

Prove a load explicitly -- `expect={addr: [bytes]}` on the load request, or
`expect_file` to diff the file's own bytes into `dump rom`.  The `N words read`
report is only a signal that the command reached the STDIO console without
`-q`; this driver's own sessions (network console) never get it.

Standard library only (`tty_load_report` imports `pty` for its live read).
"""

import argparse
import atexit
import errno
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time

# The bed: this tree's own build, unless `$S51_SIM` names another binary.
DEFAULT_SIM = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "vendor", "ucsim-stc", "ucsim", "src", "sims", "s51.src", "ucsim_51")
DEFAULT_DEVICE = "89C51R"
# The console polls its input on a 1 ms tick but only re-prompts once the
# target has been interrupted; poking faster than it drains just queues input
# that whatever runs next then consumes.  Stay well above that.
SHELL_TICK_S = 1.0

SPACES = ("iram", "xram", "sfr", "rom", "bits", "regs", "dptr", "variables")
WATCH_MODES = ("r", "w", "rw")
STOP_BREAKPOINT = 104
STOP_USER = 105
STOP_STEPPED = 109
STOP_EVENT = 112
STOP_INTERRUPT = 114

# ------------------------------------------------------------------ escaping
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"               # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC ... BEL or ST
    r"|\x1b[@-Z\\-_]")                       # 2-byte escapes
# A run that could still grow into a whole sequence when the next read lands;
# hold it back instead of half-stripping it.
_ANSI_PARTIAL_RE = re.compile(r"\x1b(?:\[([0-?]*[ -/]*)?|\][^\x07\x1b]*)?\Z",
                              re.S)
_TELNET_CMD = (0xfa, 0xfb, 0xfc, 0xfd, 0xfe)   # SE WILL WONT DO DONT
_TELNET_WILL = (0xfb, 0xfd, 0xfc, 0xfe)
_TELNET_SUB_START = 0xfa
_TELNET_SE = 0xf0
PROMPT_RE = re.compile(r"[^\r\n]*> $")

STOP_RE = re.compile(r"Stop at 0x([0-9a-fA-F]+):\s*\((\d+)\)\s*(.*)")
BP_ADDED_RE = re.compile(r"Breakpoint\s+(\d+)\s+at\s+0x([0-9a-fA-F]+)"
                         r"(?:\s+if\s+(.*))?")
WORDS_RE = re.compile(r"(\d+)\s+words?\s+read")
LOAD_REFUSE_RE = re.compile(r"(?m)^(File name is missing\.?|no loadable file"
                            r" found \(.*\).*)$")
BREAK_HEADERS = ("Num", "Type", "Disp", "Hit", "Cnt", "Address")
USAGE_MARKERS = ("Missing argument", "Wrong number of arguments", "Unknown",
                 "Parser error", "Invalid", "No such", "ambiguous",
                 "Illegal", "Out of range", "not supported", "Syntax error")
ERROR_MARKERS = USAGE_MARKERS + ("not allocated", "cannot", "failed", "Error",
                                 "is missing", "no loadable file found")


def strip_telnet(data):
    """Drop telnet negotiation and answer WILL/WONT/DO/DONT with DONT/DON'T.

    A bare IAC plus option is only complete if the following byte is not data,
    so a command split across reads is held rather than half-parsed.
    """
    out, i, n = bytearray(), 0, len(data)
    replies = bytearray()
    while i < n:
        b = data[i]
        if b != 0xff:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break                                   # IAC at the edge
        cmd = data[i + 1]
        if cmd == 0xff:                             # escaped 0xFF
            out.append(0xff)
            i += 2
        elif cmd == 0xf0:                           # end of subnegotiation
            j = data.find(b"\xff\xf0", i)
            i = n if j < 0 else j + 2
        elif cmd in _TELNET_CMD:                    # WILL/WONT/DO/DONT/SUB
            if cmd == _TELNET_SUB_START:
                j = data.find(b"\xff\xf0", i)
                i = n if j < 0 else j + 2
                continue
            if i + 2 >= n:
                break                               # option byte not here yet
            opt = data[i + 2]
            if cmd in _TELNET_WILL:                 # peer offers -> refuse
                replies += bytes((0xff, 0xfc, opt))
            i += 3
        else:
            i += 2
    return bytes(out), bytes(replies)


def strip_control(data, pending=""):
    """(clean_text, pending) for a stream chunk.

    `pending` carries an escape sequence that was cut in half by the previous
    read; ANSI colour is everywhere on this console and the sequences really
    do split across reads, so stripping must be stateful or a fragment leaks
    into the parsed output.  IAC negotiation is stripped at BYTE level, first:
    a 0xff byte can otherwise glue itself to an ANSI tail and hide it.
    """
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if pending:
        data = pending.encode("utf-8", "replace") + data
    data, replies = strip_telnet(data)
    text = data.decode("utf-8", "replace")
    text = _ANSI_RE.sub("", text)
    keep = ""
    m = _ANSI_PARTIAL_RE.search(text)
    if m and m.start() > 0 or (m and m.start() == 0 and m.group(0).endswith("\x1b")):
        keep = text[m.start():]
        text = text[:m.start()]
    text = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return text, keep, replies


def parse_stop(text):
    """`Stop at 0x000037: (104) Breakpoint` -> dict, or None.  LAST wins.

    The console echoes and interleaves, so the first occurrence in a buffer is
    often stale text from an earlier command.
    """
    m = None
    for m in STOP_RE.finditer(text or ""):
        pass
    if not m:
        return None
    code = int(m.group(2))
    return {"stopped": True,
            "pc": "0x%06x" % int(m.group(1), 16),
            "pc_int": int(m.group(1), 16),
            "code": code,
            "reason": m.group(3).strip(),
            "kind": {STOP_BREAKPOINT: "breakpoint",
                     STOP_EVENT: "event",
                     STOP_USER: "user",
                     STOP_STEPPED: "stepped",
                     STOP_INTERRUPT: "interrupt"}.get(code, "other")}


def looks_like_error(text):
    return any(m in (text or "") for m in ERROR_MARKERS)


def printed_usage(cmd, raw):
    """True when the console answered by RE-PRINTING ITS SYNOPSIS.

    An unparseable command line (`var mem2 memory 0x40`) is answered with the
    usage block -- `var name [memory addr [bit_nr]]` -- and NOTHING else: no
    error marker, no explanation, clean exit status.  A driver that only greps
    for error words reports success for a command that did nothing, so
    recognise the shape: a row that starts with the verb, is not the echoed
    command, and carries a `[...]` argument group.
    """
    verb = cmd.split()[0] if cmd.split() else ""
    if not verb or verb in ("help", "helpkw", "?"):
        return False
    sent = cmd.strip()
    for line in (raw or "").replace("\r", "\n").split("\n"):
        ln = line.strip()
        if (ln.startswith(verb + " ") and "[" in ln and
                ln.lower() != sent.lower()):
            return True
    return False


def parse_display(text):
    """`1:/X iram[0x41] = 0xa5` -> (int, shown).  Last row wins."""
    m = None
    for m in re.finditer(r"=\s*(0x[0-9a-fA-F]+|-?\d+)\s*$", text or "", re.M):
        pass
    if not m:
        return None, None
    tok = m.group(1)
    return (int(tok, 16) if tok.lower().startswith("0x") else int(tok)), tok


def parse_expr_value(text):
    """The answer to `expression <e>` -> (int, shown) or (None, None).

    Measured: this bed answers with a BARE DECIMAL row -- `expression
    iram[0x41]` gives `165`, `expression AUXR` gives `8`, and a comparison
    gives `1`/`0` -- while the documented `name = 0x..` shape appears on other
    builds.  Both are accepted; anything that is not a single number stays
    None so a command that printed no answer can never look like a zero.
    """
    for line in (text or "").replace("\r", "\n").split("\n"):
        ln = line.strip()
        if not ln or ln.endswith(">"):
            continue
        m = re.fullmatch(r"(?:=\s*)?(0x[0-9a-fA-F]+|\d+)", ln)
        if m:
            tok = m.group(1)
            return ((int(tok, 16) if tok.lower().startswith("0x")
                     else int(tok)), tok)
    return None, None


def parse_regs(text):
    """`info registers` -> {name: value}.  Best effort, never raises.

    Measured shape::

           R0 R1 R2 R3 R4 R5 R6 R7
           a5 20 88 00 03 bc 95 00
      @R0 01 .  ACC= 0x00   0 .  B= 0x00
      @R1 a7 .  PSW= 0x00 CY=0 AC=0 OV=0 P=0
      SP 0x60 -> f6 8d e3 0e da a3 0d 94
        *DPTR0= 0x00d9 @DPTR0= 0xd8 216 .
         DPTR1= 0x40d2 @DPTR1= 0x36  54 6
    """
    regs = {}
    for name, val in re.findall(r"\b(ACC|B|DPTR0|DPTR1|PSW|DPL|DPH)\s*=\s*"
                                r"(0x[0-9a-fA-F]+|\d+)", text or ""):
        regs[name.lower().rstrip("01") if name.startswith("DPTR")
             else name.lower()] = val
    m = re.search(r"^\s*SP\s+(0x[0-9a-fA-F]+)", text or "", re.M)
    if m:
        regs["sp"] = m.group(1)
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(R0(?:\s+R\d){1,7})\s*$", line)
        if not m or i + 1 >= len(lines):
            continue
        vals = re.match(r"^\s*((?:[0-9a-fA-F]{2}\s+){2,8})\s*$", lines[i + 1])
        if vals:
            for nm, v in zip(m.group(1).split(), vals.group(1).split()):
                regs[nm.lower()] = "0x" + v
            break
    m = re.search(r"\bCY\s*=\s*(\d+)\b[^\r\n]*?\bAC\s*=\s*(\d+)\b[^\r\n]*?"
                  r"\bOV\s*=\s*(\d+)\b[^\r\n]*?\bP\s*=\s*(\d+)\b", text or "")
    if m:
        regs["psw_bits"] = {"cy": int(m.group(1)), "ac": int(m.group(2)),
                            "ov": int(m.group(3)), "p": int(m.group(4))}
    return regs


def parse_dump(text):
    """`dump <space> A B` -> {"start","bytes","count","rows"}.

    The bed prints two different row shapes (both measured)::

        0x0000           02 00 30 32 ff ff ff ff ..02....     rom/xram/iram
        0x80 P0:         0b11111111 0xff '.' 255 ( -1)        sfr

    Bytes stop at the ASCII gutter: `\\b[0-9a-f]{2}\\b` alone would happily
    read `02` out of `..02....` and invent bytes that are not there.
    """
    words, rows = [], []
    for line in (text or "").splitlines():
        m = re.match(r"\s*0x([0-9a-fA-F]+)(.*)$", line)
        if not m:
            continue
        addr, rest = int(m.group(1), 16), m.group(2)
        sfr = re.match(r"\s*(\w+):\s+0b[01]+\s+(0x[0-9a-fA-F]+)", rest)
        if sfr:
            words.append(sfr.group(2).lower())
            rows.append({"addr": "0x%02x" % addr, "name": sfr.group(1),
                         "value": sfr.group(2).lower()})
            continue
        got = []
        for tok in rest.replace("|", " ").split():
            if not re.fullmatch(r"[0-9a-fA-F]{2}", tok):
                break
            got.append("0x" + tok.lower())
        if got:
            words.extend(got)
            rows.append({"addr": "0x%04x" % addr, "bytes": len(got)})
    return {"start": rows[0]["addr"] if rows else None, "bytes": words,
            "count": len(words), "rows": rows}


def _insn_rows(text):
    """Rows of `history list` (the only place instructions are disassembled).

    `history list` numbers its rows (`0:`, `1..`) and can print `?` where it
    cannot decode a byte::

        0x000037 75 41 a5  MOV    0x41,#0xa5
        0:  0x000037 75 41 a5  MOV    0x41,#0xa5
    """
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"(?:[\s,.]*\d+[.:]{1,2}[\s,]*)?[\s,]*0x([0-9a-fA-F]+)"
                     r"[\s,]+((?:[0-9a-fA-F?]{1,2}[\s,]+){1,5})\s*(\S.*)$",
                     line)
        if m:
            rows.append({"pc": "0x%06x" % int(m.group(1), 16),
                         "bytes": m.group(2).split(),
                         "mnemonic": m.group(3).strip()})
    return rows


parse_history = _insn_rows
parse_disasm = _insn_rows          # only `history list` ever feeds this


def parse_breaks(text):
    """`info break` -> rows.

    Measured table (and note what the columns MEAN)::

        Num Type Disp Enb   What       Enb Address Hit Cnt  Cond
        1   fetch keep     0x000037    true  0x000037 1 1   true
        2   event keep     0x000031    write
        3   fetch keep     0x00003a    false cond="ACC==0x5a"

    `Cond` is the condition's LIVE value at the instant `info break` ran -- a
    breakpoint whose cond says `false` evaluated to false NOW, not one that is
    disabled; the text lives in `cond="..."`.
    """
    rows = []
    for line in (text or "").splitlines():
        ln = line.strip()
        p = ln.split()
        if not p or not re.fullmatch(r"\d+", p[0]):
            continue
        if len(p) < 4 or p[1] not in ("fetch", "event", "sw", "hw"):
            continue
        rest = p[3:]
        row = {"id": int(p[0]), "type": p[1], "disp": p[2], "raw": ln}
        addr = next((t.lower() for t in rest if t.lower().startswith("0x")),
                    None)
        if addr:
            row["addr"] = addr
        nums = [t for t in rest if re.fullmatch(r"\d+", t)]
        if len(nums) >= 2:
            row["hit"], row["cnt"] = int(nums[0]), int(nums[1])
        elif nums:
            row["hit"] = row["cnt"] = int(nums[0])
        else:
            row["hit"] = row["cnt"] = None
        row["mode"] = next((t for t in ("read-write", "write", "read")
                            if t in rest), None)
        m = re.search(r"cond\s*=\s*\"([^\"]*)\"", ln)
        if not m:
            m = re.search(r"cond\s+(\S+)", ln)
        row["cond"] = m.group(1) if m else None
        # A bare true/false column is the condition's LIVE value at the
        # instant `info break` ran (measured:
        # `3 fetch keep 1 1 0x00003a false cond="ACC==0x5a"`) -- NOT an enable
        # flag.  A row saying `false` evaluated false NOW and may well fire
        # later; reading it as "disabled" is how a working breakpoint gets
        # deleted by mistake.
        bools = [t for t in rest if t.lower() in ("true", "false")]
        row["cond_live"] = (bools[0].lower() == "true") if bools else None
        row["raw_rest"] = row["mode"] or row["cond"] or row.get("addr")
        rows.append(row)
    return {"rows": rows, "count": len(rows),
            "raw": text or "",
            "headers_seen": all(h in (text or "") for h in BREAK_HEADERS)}


def cond_trust(cond, quoted=True):
    """How much can a `break ... if <cond>` be believed, on THIS bed?

    Measured at 0x3a of t/isa.ihx (where ACC=0x5a, iram[0x40]=0x5a and
    iram[0x30]=0x41 -- a code byte, NOT zero), three trials each on the frozen
    binary:

      if ACC==0x5a            stops
      if ACC==0x5b / !=0x5a   does not
      if AUXR==0x08           stops          (its reset value)
      if "ACC == 0x5a"        stops          (quoted multi-token works)
      if "iram[0x40]==0x5a"   STOPS          (quoted brackets DO work)
      if "sfr[0xe0]==0x5a"    STOPS
      if "iram[0x40]==90"     STOPS          (decimal right-hand side too)
      if "iram[0x30]==0x00"   does not, and CORRECTLY so: iram[0x30] is 0x41
                              there, so the condition is simply false
      if iram[0x30]==0x00     parse error, and NO breakpoint is created
      if 0                    STOPS          -- a bare literal is truthy

    So there are exactly two traps: a bare literal (silently always true) and
    an UNQUOTED condition containing spaces or brackets (parser error, no
    breakpoint, no explanation).  A quoted bracket condition WORKS, so this
    function does not claim otherwise -- and it refuses to let "it never
    stopped" be read as "the breakpoint is broken" while the value stays
    unmeasured, which is what `cond_holds_now` from `add_break()` answers.
    """
    if cond is None:
        return {"trusted": True, "warnings": []}
    c = cond.strip()
    warns = []
    if re.fullmatch(r"\(?\s*(0[xX][0-9a-fA-F]+|\d+)\s*\)?", c):
        warns.append("constant condition %r: the bed treats a bare literal as "
                     "true, so this fires whether or not the state you meant "
                     "holds (verified: `if 0` stops)" % c)
    if ("[" in c or "]" in c) and not quoted:
        warns.append("bracket condition sent UNQUOTED: the console splits on "
                     "spaces and rejects it (`Parser error`), which creates NO "
                     "breakpoint at all -- check `created`/`table` instead of "
                     "concluding the breakpoint is inert")
    elif " " in c and not quoted:
        warns.append("multi-token condition sent unquoted: only its first "
                     "token reaches the parser")
    if re.search(r"[<>]", c) and "==" not in c:
        warns.append("relational operator in a console-split condition: verify "
                     "it registered before trusting it")
    return {"trusted": not warns, "warnings": warns}


def parse_vcd(path):
    """Parse a VCD so tests can assert on content, not on exit status.

    Measured header from this bed (CRLF line endings -- stripping only the LF
    leaves the CR glued to the last token and invents a scope named `$end`),
    and `$var` fields are `type size id name`:

        $scope module ucsim_vcd_0 $end
        $var wire 8 ! P1 $end
        $upscope $end
        $enddefinitions $end
        #0
        $dumpvars
        b11111111 !
        $end
        #1085069
        b00010001 !

    `changes` maps each signal name to [(ps, value)] in file order.
    """
    signals, scope, changes = {}, [], {}
    cur, n_trans = None, 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            t = line.strip()
            p = t.split()
            if not p:
                continue
            if p[0] == "$scope" and len(p) >= 4:
                scope.append(p[2])
            elif p[0] == "$upscope":
                if scope:
                    scope.pop()
            elif p[0] == "$var" and len(p) >= 5:
                signals[p[3]] = "/".join(scope + [p[4]])
            elif t.startswith("#"):
                n_trans += 1
                try:
                    cur = int(t[1:].strip())
                except ValueError:
                    cur = None
            elif t[:1] in ("b", "B") and len(p) >= 2 and p[1] in signals:
                changes.setdefault(p[1], []).append((cur, p[0][1:]))
            elif t[:1] in ("0", "1", "x", "z", "p", "n") and len(p) == 1:
                if t[0] in signals:
                    changes.setdefault(t[0], []).append((cur, t[1:]))
    return {"signals": {k: signals[k] for k in sorted(signals)},
            "changes": {signals.get(k, k): v for k, v in changes.items()},
            "n_changes": sum(len(v) for v in changes.values()),
            "n_signals": len(signals),
            "n_transactions": n_trans,
            "bytes": os.path.getsize(path)}


def parse_load_report(text):
    """Pull `Loading from <p>` / `N words read from <p>` out of console text.

    The report exists when the command reached the STDIO console and `-q` was
    not used; whether that console sits on a pty or a pipe makes no difference
    (`stdio_load_report`).  A missing report is NOT a failed load -- on the
    network console it simply never exists.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()
             if "Loading from" in ln or "words read" in ln]
    w = WORDS_RE.search(text or "")
    return {"report": bool(lines), "words": int(w.group(1)) if w else None,
            "lines": lines}

# ------------------------------------------------------------------- helpers
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _as_int(v):
    return v if isinstance(v, int) else int(str(v), 0)


def _first_line(text):
    for ln in (text or "").splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def _hex(v):
    return None if v is None else "0x%x" % int(v)


class ConsoleError(RuntimeError):
    pass


class S51Dbg:
    """Owns one ucsim_51 process and its console.

    Usage::

        with S51Dbg() as d:
            d.start("t/isa.ihx")
            d.break_at(0x37)
            print(d.cont()["pc"])
    """

    def __init__(self, sim=None, device=None, xtal=None, timeout=15.0,
                 port=None, log=None):
        self.sim = os.path.abspath(sim or os.environ.get("S51_SIM") or
                                   DEFAULT_SIM)
        if not os.path.exists(self.sim):
            raise ConsoleError("no simulator at %s (set $S51_SIM)" % self.sim)
        self.device = device or DEFAULT_DEVICE
        self.xtal = xtal
        self.timeout = float(timeout)
        self.port = int(port) if port else free_port()
        self.log_path = log or os.environ.get("S51DBG_LOG") or None
        self._buf = ""
        self._pending = ""              # half an escape sequence
        self._aux = ""                  # the simulator's own stdout
        self._aux_lock = threading.Lock()
        self._running = False
        self._proc = None
        self._sock = None
        self._handlers = {}
        self._files = set()             # programs this session has loaded
        self._words = None
        atexit.register(self.close)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP,
                    signal.SIGQUIT):
            try:
                self._handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass
        self._spawn()

    def _on_signal(self, signum, frame):
        """Restore the previous handler first, then shut the simulator down.

        The bed exits when its console closes, so a KeyboardInterrupt that
        propagated out of a read would otherwise leave the child holding the
        console port and the ELF image.
        """
        self.close()
        old = self._handlers.get(signum, signal.SIG_DFL)
        signal.signal(signum, signal.SIG_DFL)
        if callable(old) and old not in (self._on_signal, signal.SIG_DFL):
            old(signum, frame)
        elif old is signal.SIG_DFL:
            os.kill(os.getpid(), signum)

    # ------------------------------------------------------------- plumbing
    def _log(self, mark, text):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a") as f:
                f.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), mark, text))
        except OSError:
            pass

    def _spawn(self):
        # `-c -` is load-bearing: without it the CPU is free-running the
        # moment the telnet console opens.  stdout is a PIPE and IS drained:
        # the engine's own messages land there and must never reach the
        # console stream, where they would be mistaken for a prompt.
        self._proc = subprocess.Popen(
            [self.sim, "-t", self.device] +
            (["-X", str(self.xtal)] if self.xtal else []) +
            ["-c", "-", "-Z", str(self.port)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
        t = threading.Thread(target=self._drain_stdout, daemon=True)
        t.start()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                self._sock = socket.create_connection(("127.0.0.1", self.port),
                                                      timeout=1.0)
                break
            except OSError:
                if self._proc.poll() is not None:
                    raise ConsoleError("simulator exited during startup (rc=%s)"
                                       % self._proc.returncode)
                time.sleep(0.15)
        else:
            raise ConsoleError("no console on port %d" % self.port)
        self._sock.setblocking(False)
        self._running = False
        self._wait_prompt(6.0, 0, "startup")
        self._log("spawn", "port=%d device=%s pid=%d"
                  % (self.port, self.device, self._proc.pid))

    def _drain_stdout(self):
        f = self._proc.stdout
        while True:
            try:
                chunk = f.read(4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self._aux_lock:
                self._aux += chunk.decode("utf-8", "replace")

    def _fill(self, timeout):
        """One select+read; returns True when bytes arrived."""
        if self._sock is None:
            return False
        try:
            r, _, _ = select.select([self._sock], [], [], timeout)
        except (OSError, ValueError):
            return False
        if not r:
            return False
        try:
            data = self._sock.recv(65536)
        except BlockingIOError:
            return False
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise ConsoleError("console read failed: %s" % e)
        if not data:
            raise ConsoleError("console socket closed by the simulator")
        clean, self._pending, replies = strip_control(data, self._pending)
        if replies and self._sock is not None:
            try:
                self._sock.sendall(replies)
            except OSError:
                pass
        self._buf += clean
        self._log("sock", repr(clean[:180]))
        return True

    def _send(self, line):
        if self._sock is None:
            raise ConsoleError("console is closed")
        try:
            self._sock.sendall((line + "\n").encode())
        except OSError as e:
            raise ConsoleError("console write failed: %s" % e)
        self._log("cmd", line)

    def _peek(self, start=0):
        return self._buf[start:]

    def _wait_prompt(self, timeout, start=0, what=""):
        """Wait until the tail of the stream is a prompt, and return it."""
        deadline = time.time() + max(0.05, timeout)
        while True:
            tail = self._peek(start)
            m = PROMPT_RE.search(tail)
            if m and m.end() == len(tail):
                return tail
            if self._proc.poll() is not None:
                raise ConsoleError("simulator exited (rc=%s) awaiting %s"
                                   % (self._proc.returncode, what))
            if time.time() >= deadline:
                raise ConsoleError("no prompt after %s within %.1fs; tail=%r"
                                   % (what, timeout, self._peek(start)[-240:]))
            self._fill(min(0.25, max(0.01, deadline - time.time())))

    def _aux_from(self, pos):
        with self._aux_lock:
            return self._aux[pos:]

    # ------------------------------------------------------- one console call
    def command(self, line, timeout=None):
        """One console command -> {ok, cmd, raw, simout, usage, error}.

        `raw` is the response with the echoed command line removed (the bed
        echoes what you typed, so an unfiltered read returns the question
        instead of the answer).  `simout` is whatever the simulator printed on
        its own stdout while the command ran.
        """
        timeout = timeout or self.timeout
        if self._running:
            self.stop()
        pos = len(self._buf)
        apos = len(self._aux)
        self._send(line)
        deadline = time.time() + timeout
        while "\n" not in self._peek(pos):
            if time.time() >= deadline:
                break
            self._fill(0.2)
        rel = self._peek(pos).find("\n")
        start = pos + rel + 1 if rel >= 0 else pos
        try:
            raw = self._wait_prompt(max(0.1, deadline - time.time()), start,
                                    repr(line))
        except ConsoleError as e:
            return {"ok": False, "cmd": line, "raw": self._peek(start),
                    "simout": self._aux_from(apos), "usage": False,
                    "error": str(e)}
        bad = next((m for m in ERROR_MARKERS if m in raw), None)
        usage = bad is None and printed_usage(line, raw)
        err = (("console reported %r" % bad) if bad else
               ("console printed usage for %r instead of doing the work -- the "
                "command line was not accepted: %s"
                % (line, _first_line(raw))) if usage else None)
        return {"ok": bad is None and not usage, "cmd": line, "raw": raw,
                "simout": self._aux_from(apos), "usage": usage, "error": err}

    def raw_batch(self, cmds, timeout=None):
        cap, errs = [], []
        for c in cmds:
            r = self.command(c, timeout)
            cap.append(r["raw"])
            if not r["ok"]:
                errs.append("%s -> %s" % (c, r["error"]))
        return "\n".join(cap), errs

    # --------------------------------------------------------------- session
    def rom_probe(self, count=0x30):
        """ROM bytes 0..count-1 as a list of `0x..` strings, or None."""
        r = self.command("dump rom 0 0x%x" % (count - 1))
        d = parse_dump(r["raw"])
        return d["bytes"] if d["count"] else None

    def expect_rom(self, addr, want):
        """ROM at `addr` against `want` (ints or `0x..` strings).

        Byte-exact evidence that the bytes landed.  A word count says a file
        parsed and a driver session cannot even see it (see
        `tty_load_report`), while ROM before any load is the 0xff power-on fill,
        so "ROM is non-zero" proves nothing either.  `expect_file()` diffs the
        whole file; this checks a caller-supplied handful.
        """
        want = ["0x%02x" % (_as_int(b) & 0xff) for b in want]
        got = parse_dump(self.command(
            "dump rom 0x%x 0x%x" % (_as_int(addr),
                                    _as_int(addr) + len(want) - 1))
            ["raw"])["bytes"]
        mismatch = [(i, w, g) for i, (w, g) in enumerate(zip(want, got))
                    if w != g]
        return {"addr": "0x%04x" % _as_int(addr), "want": want, "got": got,
                "mismatch": mismatch,
                "matches": len(got) == len(want) and not mismatch}

    @staticmethod
    def parse_intel_hex(path):
        """Standard Intel HEX -> {addr: byte}, plus the contiguous ranges.

        `t/isa.ihx` is bog-standard: 15 type-00 data records and one type-01
        EOF, every length and checksum valid.  Record type sits at chars 7:9 of
        the line (LL at 1:3, AAAA at 3:7); reading it at 8:10 invents phantom
        record types.  Types 02/04 move the base address, 05 carries the start
        linear address, 01 ends the file; 03/06 are ignored.
        """
        data, base, eof = {}, 0, False
        for lineno, line in enumerate(open(path, "r", errors="replace"), 1):
            line = line.strip()
            if not line.startswith(":"):
                continue
            try:
                rec = bytes.fromhex(line[1:])
            except ValueError:
                raise ValueError("%s:%d: non-hex record" % (path, lineno))
            if len(rec) < 5 or (sum(rec) & 0xFF) or len(rec) - 5 != rec[0]:
                raise ValueError("%s:%d: bad length or checksum" % (path, lineno))
            n, addr, typ = rec[0], rec[1] << 8 | rec[2], rec[3]
            payload = rec[4:4 + n]
            if eof:
                raise ValueError("%s:%d: record after EOF" % (path, lineno))
            if typ == 0x00:
                for i, b in enumerate(payload):
                    data[base + addr + i] = b
            elif typ == 0x01:
                eof = True
            elif typ == 0x02 and n == 2:
                base = (payload[0] << 8 | payload[1]) << 4
            elif typ == 0x04 and n == 2:
                base = (payload[0] << 8 | payload[1]) << 16
        if not data and not eof:
            raise ValueError("%s: not an Intel HEX file (no `:'-led data "
                             "record, no EOF record) -- an empty comparison "
                             "must never look like a match" % path)
        ranges, lo = [], None
        for a in sorted(data):
            if lo is None:
                lo = hi = a
                continue
            if a == hi + 1:
                hi = a
                continue
            ranges.append((lo, hi))
            lo = hi = a
        if lo is not None:
            ranges.append((lo, hi))
        return {"bytes": data, "ranges": ranges, "count": len(data),
                "path": os.path.abspath(path)}

    def expect_file(self, path, limit=512):
        """Byte-exact load proof: the file's own bytes against `dump rom`.

        Stronger than `expect=` because nobody has to know the image: parse the
        HEX, dump each contiguous range back, diff.  `limit` caps how many bytes
        are compared (console round-trips), and `checked_partial` says when it
        bit.  Zero-length result is a failure, never a vacuous pass.
        """
        try:
            img = self.parse_intel_hex(path)
        except (OSError, ValueError) as e:
            return {"ok": False, "error": "cannot parse %s: %s" % (path, e),
                    "matches": False, "checked": 0, "ranges": [], "mismatch": []}
        ranges, checked, mismatch = [], 0, []
        for lo, hi in img["ranges"]:
            if checked >= limit:
                break
            if hi - lo + 1 > limit - checked:
                hi = lo + (limit - checked) - 1
            got = parse_dump(self.command(
                "dump rom 0x%x 0x%x" % (lo, hi))["raw"])["bytes"]
            want = ["0x%02x" % img["bytes"][a] for a in range(lo, hi + 1)]
            bad = [(lo + i, w, g) for i, (w, g) in enumerate(zip(want, got))
                   if w != g]
            mismatch.extend(bad)
            checked += len(got)
            ranges.append({"start": "0x%04x" % lo, "stop": "0x%04x" % hi,
                           "bytes": len(got), "mismatch": len(bad)})
        return {"ok": bool(checked) and not mismatch,
                "matches": bool(checked) and not mismatch,
                "path": img["path"], "file_bytes": img["count"],
                "checked": checked, "ranges": ranges, "mismatch": mismatch[:8],
                "checked_partial": checked < img["count"],
                "error": (None if checked and not mismatch else
                          ("nothing was compared -- the file has no data "
                           "records" if not checked else
                           "%d byte(s) differ from the file, starting at %s "
                           "(rom %s, file %s)"
                           % (len(mismatch), _hex(mismatch[0][0]),
                              mismatch[0][2], mismatch[0][1]))),
                "note": ("ROM holds the file's own bytes over %d address(es)"
                         % len(ranges) if checked and not mismatch else None)}

    @staticmethod
    def tty_load_report(sim=None, device=None, path=None, extra=()):
        """Throwaway TERMINAL session that reads the load report LIVE.

        On a tty the stdio console is line-buffered, so this returns as soon as
        the two lines land.  VERBATIM capture from the author's tree -- that
        path is where his copy of `t/isa.ihx` happened to live, not something
        this tool looks for (the text is matched as lines, whatever the path)::

            Loading from /home/oleksiy/s51/t/isa.ihx
            233 words read from /home/oleksiy/s51/t/isa.ihx

        A pipe carries the SAME two lines -- but only once the child exits, see
        `stdio_load_report` -- and `-q` silences them on either console.  Use
        this when you want the report without waiting for an exit; use
        `stdio_load_report` in scripted runs.  `extra` carries flags such as
        ["-q"].  (The pty is opened with `pty.openpty` and driven with `\\r`;
        `script -qec` hangs and is not a substitute.)
        """
        import pty                                       # tty-only plumbing
        cmd = [sim or DEFAULT_SIM, "-t", device or "89C51R", "-c", "-"]
        cmd += list(extra)
        m, s = pty.openpty()
        proc = subprocess.Popen(cmd, stdin=s, stdout=s,
                                stderr=subprocess.STDOUT)
        os.close(s)
        buf = []
        try:
            os.set_blocking(m, False)
            time.sleep(0.5)
            os.write(m, ('file "%s"\r' % os.path.abspath(path)).encode())
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    chunk = os.read(m, 65536)
                except (BlockingIOError, OSError):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                if not chunk:
                    break
                buf.append(chunk)
        finally:
            proc.kill()
            proc.wait()
            os.close(m)
        txt = strip_control(b"".join(buf))[0]
        out = parse_load_report(txt)
        out.update({"flags": list(extra), "raw": txt, "console": "pty"})
        return out

    @staticmethod
    def stdio_load_report(sim=None, device=None, path=None, extra=(),
                          flags=("-c", "-"), settle=0.6):
        """Read the load report the way a SCRIPT can: pipes, no terminal.

        Measured on one binary, stdout a PIPE in every row:

        * `-c -`, `file` typed on stdin  -> `Loading from <p>` + `N words read`
        * no `-c -`, `file` on stdin     -> same (the console still read stdin)
        * `-c -` + `-Z`, `file` on the network console -> nothing, anywhere
        * `-c -` + `-q`                  -> nothing

        The report is decided by WHICH console got the command and by `-q`,
        never by terminal-ness.  The trap that makes people think a pipe kills
        it: on a pipe the stdio console is BLOCK-buffered, so ZERO bytes appear
        while the child lives (measured `live=0`) and everything arrives when it
        exits.  So: close stdin, read to EOF, never peek.

        Assert `report`, `words` and `lines` -- NOT `bytes_total`, which is a
        function of the path length (both lines echo it) and of whatever else
        the console happens to print: the same successful load captured 359
        bytes when the path was absolute and 305 when it was relative (the 54
        byte gap is the two path interpolations).  The
        path is abspath'd before it is sent, so a relative spelling loads the
        same image and its report lines name the absolute one.
        """
        cmd = [sim or DEFAULT_SIM, "-t", device or "89C51R"]
        cmd += list(flags) + list(extra)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        buf = []
        th = threading.Thread(
            target=lambda: [buf.append(b) for b in
                            iter(lambda: proc.stdout.read(65536), b"")],
            daemon=True)
        th.start()
        time.sleep(0.4)
        proc.stdin.write(('file "%s"\n' % os.path.abspath(path)).encode())
        proc.stdin.flush()
        time.sleep(settle)
        live = sum(len(b) for b in buf)
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        th.join(3)
        raw = b"".join(buf)
        txt = strip_control(raw)[0]
        out = parse_load_report(txt)
        out.update({"flags": list(flags) + list(extra), "raw": txt,
                    "bytes_before_exit": live, "bytes_total": len(raw),
                    "console": "stdio"})
        return out

    def start(self, file=None, device=None, xtal=None, expect=None,
              report_check=False):
        """Load a program; the CPU comes back stopped at PC=0.

        The path is QUOTED.  Measured on this bed: an unquoted path is answered
        with `File name is missing.`, a missing file with `no loadable file
        found (...)`, and BOTH leave ROM at its 0xff power-on fill -- which is
        `MOV R7,A`, not NOP, so the CPU still steps and still LOOKS like a
        running program.  A load that SUCCEEDS does report, but only to the
        console that received the command and only without `-q` (see
        `stdio_load_report`); a command sent to the network console -- what this
        driver uses -- never produces that report anywhere.
        Proof comes from ROM: `expect_file()` diffs the file's own bytes into
        `dump rom`, `expect={addr: [bytes]}` checks a caller-supplied handful.
        In order:

        * a printed refusal is surfaced verbatim in `refusal` and decides `ok`;
        * `expect` ({addr: [bytes], or [[addr, bytes], ...]}) is checked
          byte-for-byte and decides `ok` when given;
        * otherwise ROM is read back before and after: a change is a load, no
          change is a failure, and no change on a path already loaded in this
          session is a reload rather than a failure.

        `words_read` is parsed only in case a build does print the report into
        this session's own stream; it is None here because the command goes to
        the network console.  `report_check=True` loads as usual and THEN asks
        a throwaway stdio console for the same file (`stdio_load_report`), so a
        scripted run can confirm the image is loadable and count its words
        without a pty and without disturbing this session.  It never changes
        `ok`: ROM readback remains the proof, the report is corroboration.
        """
        cmds = []
        if device:
            cmds.append('cpu "%s"' % device)
        if xtal:
            cmds.append("hw fpi hf %s" % xtal)
        path = os.path.abspath(file) if file else None
        reload_ = bool(path) and path in self._files
        before = self.rom_probe() if file else None
        if file:
            cmds.append('file "%s"' % path)
        raw, errs = self.raw_batch(cmds)
        self._running = False
        if file and not errs:
            self._files.add(path)
        r = self.pc()
        after = self.rom_probe() if file else None
        words = WORDS_RE.search(raw + "\n" + self._aux_from(0))
        self._words = int(words.group(1)) if words else None
        out = {"ok": not errs, "raw": raw, "error": "; ".join(errs) or None,
               "pc": r.get("pc"), "device": device or self.device,
               "words_read": self._words}
        if not file:
            return out
        m = LOAD_REFUSE_RE.search(raw)
        if m:
            out.update({"ok": False, "refusal": m.group(0).strip(),
                        "rom_before": before, "rom_after": after,
                        "rom_changed": bool(before != after),
                        "rom_verified": False})
            out["error"] = ("the bed refused the load: %s.  An unquoted path "
                            "is answered with `File name is missing.`; ROM "
                            "keeps its 0xff power-on fill, which is MOV R7,A, "
                            "so stepping still looks alive" % out["refusal"])
            return out
        changed = bool(before is not None and after is not None and
                       before != after)
        nonzero = bool(after) and any(b != "0x00" for b in after)
        checks = []
        if expect:
            items = expect.items() if isinstance(expect, dict) else expect
            checks = [self.expect_rom(a, b) for a, b in items]
        exp_ok = all(c["matches"] for c in checks) if checks else None
        verified = (exp_ok if exp_ok is not None
                    else (changed or (reload_ and nonzero)))
        out.update({"rom_before": before, "rom_after": after,
                    "rom_changed": changed, "rom_nonzero": nonzero,
                    "rom_reloaded": reload_, "expect_checks": checks,
                    "rom_verified": bool(verified)})
        if not verified:
            out["ok"] = False
            bad = [c for c in checks if not c["matches"]]
            if bad:
                out["error"] = ("ROM at %s is %s, not the %s the caller asked "
                                "for -- `file %s` did not put the image where "
                                "expected"
                                % (bad[0]["addr"], bad[0]["got"][:4],
                                   bad[0]["want"][:4], file))
            else:
                out["warning"] = (
                    "ROM at 0..0x2f is unchanged by `file %s` and was %s "
                    "before, so nothing proves the image was read in.  A "
                    "REFUSAL is printed (see `refusal`); a load that SUCCEEDS "
                    "prints nothing, and the untouched 0xff fill still steps "
                    "(MOV R7,A, not NOP) -- pass expect= or re-check the path"
                    % (file, "all zero" if not nonzero else "non-zero"))
        elif exp_ok is None and not reload_:
            out["note"] = ("%d ROM bytes differ from the pre-load image, so "
                           "the file was read in; the network console prints "
                           "no load report -- report_check=True cross-checks "
                           "it on a throwaway stdio console" % len(after))
        if report_check:
            rep = self.stdio_load_report(sim=self.sim, device=self.device,
                                         path=path)
            out["report_check"] = {"report": rep["report"],
                                   "words": rep["words"],
                                   "lines": rep["lines"]}
            if not rep["report"]:
                out["report_warning"] = (
                    "a throwaway STDIO console printed no load report for this "
                    "path either, so the image is not loadable (or `-q' is in "
                    "play) -- ROM readback stays the proof")
        return out

    def reset(self):
        self._running = False
        return self.command("reset")

    def pc(self):
        """Current PC.  `pc` prints a disassembly row, not `PC[cpu0]=`."""
        r = self.command("pc")
        pc = None
        m = re.search(r"PC\[\w+\]\s*=\s*0x([0-9a-fA-F]+)", r["raw"])
        if m:
            pc = int(m.group(1), 16)
        else:
            rows = parse_disasm(r["raw"])
            if rows:
                pc = int(rows[0]["pc"], 16)
        r["pc"] = "0x%06x" % pc if pc is not None else None
        r["pc_int"] = pc
        rows = parse_disasm(r["raw"])
        if rows:
            r["at"] = rows[0]
        r["stopped"] = not self._running
        return r

    def regs(self):
        r = self.command("info registers")
        r["registers"] = parse_regs(r["raw"])
        return r

    def state(self):
        """One snapshot: pc, registers, breakpoints, memory spaces."""
        was = self.stop_if_running()
        pc, regs, brk = (self.pc(), self.regs(),
                         self.command("info break"))
        info, mem = self.command("info cpu"), self.command("info memory")
        spaces = sorted(set(re.findall(r"(?m)^\s*(%s)\b" % "|".join(SPACES),
                                       info["raw"] + "\n" + mem["raw"])))
        return {"ok": all(x["ok"] for x in (pc, regs, brk)),
                "raw": "\n".join(x["raw"] for x in (pc, regs, brk, info)),
                "pc": pc.get("pc"), "registers": regs.get("registers", {}),
                "memory_spaces": spaces or list(SPACES),
                "running": False, "was_running": was.get("was_running"),
                "stopped": True, "device": self.device, "port": self.port,
                "pid": self._proc.pid,
                "breakpoints": parse_breaks(brk["raw"]),
                "variables_raw": self.command("info variables")["raw"]}

    def stop_if_running(self):
        if not self._running:
            return {"ok": True, "was_running": False}
        return self.stop()

    # -------------------------------------------------------------- breaking
    def info_break(self):
        r = self.command("info break")
        r.update(parse_breaks(r["raw"]))
        return r

    def _fresh_rows(self, before, table):
        """Rows in `table` that were not in `before`.

        Identity is (id, meaning), not id alone: `del break` and a fresh
        `break` reuse number 1, so a table read between the two would
        otherwise make the new row look pre-existing.
        """
        old = {(r["id"], r.get("raw_rest")) for r in before["rows"]}
        return [r for r in table["rows"]
                if (r["id"], r.get("raw_rest")) not in old]

    def _instruction_pcs(self, addr, span=8):
        """PCs PROVEN to be instruction boundaries, from `history list`.

        There is no usable disassembler on this bed (`disasm A N` is a Parser
        error in every spelling, `disassemble A N` prints nothing), so the
        only evidence that an address is an instruction start is having
        actually executed.  Nothing here guesses at instruction lengths.
        """
        r = self.command("history list")
        return ([i["pc"] for i in parse_history(r["raw"])], r)

    def add_break(self, addr, hit=1, cond=None, quote=True):
        """Fetch breakpoint: `break ADDR [N] [if COND]`.

        N (hit count) is POSITIONAL -- `break 0x37 hit 2` silently keeps Hit 1
        -- and it counts PASSES of the address, not calls to `cont()`.  The
        console has no disassembler, so `at_instruction_boundary` is tri-state:
        True only when the address was seen executing in `history list`.
        Whether an unexecuted address is a gap between instructions or simply
        cold cannot be told from the console alone.
        """
        a = _as_int(addr)
        parts = ["break", str(a)]
        sent = None
        hit = int(hit or 1)
        if hit != 1:
            parts.append(str(hit))
        if cond is not None:
            sent = '"%s"' % cond if quote else str(cond)
            parts += ["if", sent]
        line = " ".join(parts)
        before = self.info_break()
        res = self.command(line)
        table = self.info_break()
        fresh = self._fresh_rows(before, table)
        announced = None
        for m in BP_ADDED_RE.finditer(res["raw"]):
            announced = {"id": int(m.group(1)),
                         "addr": "0x%06x" % int(m.group(2), 16),
                         "cond_echo": m.group(3)}
        # The table can legitimately hold other rows -- a sentinel breakpoint
        # at an executed address past the target, so that a run which never
        # hits the bp under test stops instead of running until the driver
        # gives up (and a KILLED simulator loses its buffered stdout, which
        # looks exactly like "the breakpoint was never created").  So pick the
        # row whose ADDRESS is the one just requested; the announced id is
        # only a fallback.
        want = "0x%06x" % a
        row = next((r for r in fresh
                    if _as_int(r.get("addr") or "0") == a), None)
        if row is None and announced:
            row = next((r for r in table["rows"]
                        if r["id"] == announced["id"]), None)
        if row is None:
            row = fresh[0] if len(fresh) == 1 else None
        trust = cond_trust(cond, quote)
        warns = list(trust["warnings"])
        registered = row.get("cond") if row else None
        constant = bool(re.fullmatch(r"\(?\s*(0[xX][0-9a-fA-F]+|\d+)\s*\)?",
                                     str(cond or "").strip()))
        if cond is not None and row is not None and not constant:
            want_txt = str(cond).strip()
            if (registered or "").strip() != want_txt:
                warns.append("console registered cond=%r for the requested "
                             "%r: it splits its input on spaces, so quote the "
                             "condition" % (registered, want_txt))
        # Ask the bed what the condition evaluates to RIGHT NOW.  This is the
        # difference between a broken breakpoint and a condition that is simply
        # false, and the console will not tell you which without it: measured,
        # `if "iram[0x30]==0x00"` registers, never fires, and the reason is
        # that iram[0x30] is 0x41 (a code byte) -- the condition was false.
        # `expression` refuses quotes, so spaces are removed for the probe.
        eval_cmd = holds = eval_val = eval_pc = None
        if cond is not None and row is not None:
            eval_cmd = "expression %s" % re.sub(r"\s+", "", str(cond))
            ev = self.command(eval_cmd)
            eval_val, _ = parse_expr_value(ev["raw"])
            holds = None if eval_val is None else bool(eval_val)
            eval_pc = self.pc().get("pc")
            if holds is False:
                warns.append("condition %s evaluates to 0 at PC=%s (%s), so "
                             "this breakpoint cannot stop until that becomes "
                             "true -- measure the value before blaming the "
                             "breakpoint" % (cond, eval_pc, eval_cmd))
            elif holds is None:
                warns.append("condition %s could not be re-evaluated as an "
                             "expression (%s said %r); trust nothing about "
                             "when it fires"
                             % (cond, eval_cmd, _first_line(ev["raw"])))
        executed = self._instruction_pcs(a)[0]
        boundary = True if want in executed else None
        res.update({"ok": row is not None,
                    "created": row is not None,
                    "breakpoint": row,
                    "announced": announced,
                    "in_table": row is not None,
                    "addr": want,
                    "hits_wanted": hit,
                    "hits_listed": row["hit"] if row else None,
                    "cond": cond,
                    "cond_sent": ("if %s" % sent) if sent is not None else None,
                    "cond_registered": registered,
                    "cond_now": row.get("cond_live") if row else None,
                    "cond_eval_cmd": eval_cmd,
                    "cond_eval_value": eval_val,
                    "cond_holds_now": holds,
                    "cond_eval_at": eval_pc,
                    "table": table["rows"],
                    "at_instruction_boundary": boundary,
                    "boundary_note": None if boundary is True else
                        "no disassembler on this bed (`disasm` is a Parser "
                        "error, `disassemble A N` prints nothing): True means "
                        "the address was observed executing in `history list`, "
                        "None means unknown, and an unknown address can be a "
                        "mid-instruction byte that never fires",
                    "trusted": row is not None and not warns,
                    "warnings": warns})
        if row is None:
            res["error"] = "no breakpoint in `info break` after `%s`: %s" % (
                line, _first_line(res["raw"]) or "console printed nothing")
        elif warns:
            res["warning"] = "; ".join(warns)
        elif row["hit"] != hit:
            res["error"] = ("hit count not honoured: asked %s, table says "
                            "Hit %s" % (hit, row["hit"]))
        return res

    def break_if(self, addr, cond, quote=True):
        """Explicit conditional breakpoint; always reports trust."""
        r = self.add_break(addr, cond=cond, quote=quote)
        r["cond"] = cond
        return r

    def disasm(self, addr, n=8):
        """Report the truth about disassembling: this bed cannot.

        `disasm A N` -> `Parser error: syntax error` in every spelling tried
        (two args, `rom` prefixed, one arg) and `disassemble A N` is accepted
        but prints NOTHING.  The only disassembly available is of what has
        already executed (`history list`), so `insns` here is what history
        happens to hold near that address -- possibly empty, and never
        invented.
        """
        r = self.command("disasm %s %d" % (_as_int(addr), int(n)))
        hist = self.command("history list")
        rows = parse_history(hist["raw"])
        near = [x for x in rows if abs(int(x["pc"], 16) - _as_int(addr)) <= 0x40]
        r.update({"ok": False, "insns": near, "disassembler": "unavailable",
                  "error": "`disasm A N` is a Parser error on this bed and "
                           "`disassemble A N` prints nothing; `insns` is "
                           "history-list evidence near the address (%d rows), "
                           "not a disassembly of it" % len(near)})
        return r

    def watch(self, space, mode, addr):
        """`break <space> <r|w> <addr>`; refuses forms this build rejects.

        A watchpoint prints NO acknowledgement at all -- only the new `event`
        row in `info break` proves it exists, so that is the proof used here.
        `break sfr w AUXR` and `break rom ADDR` are rejected by this build.
        """
        if space not in SPACES:
            return {"ok": False, "raw": "",
                    "error": "memory space %r is not one of %s"
                             % (space, ", ".join(SPACES))}
        if mode not in WATCH_MODES:
            return {"ok": False, "raw": "",
                    "error": "mode %r must be one of %s"
                             % (mode, "|".join(WATCH_MODES))}
        a = _as_int(addr)
        before = self.info_break()
        res = self.command("break %s %s %d" % (space, mode, a))
        table = self.info_break()
        want = {"r": "read", "w": "write", "rw": "read-write"}[mode]
        fresh = [r for r in self._fresh_rows(before, table)
                 if r["type"] == "event" and _as_int(r.get("addr") or "0") == a]
        row = next((r for r in fresh if r["mode"] == want),
                   fresh[0] if len(fresh) == 1 else None)
        res.update({"ok": row is not None, "watch": row,
                    "space": space, "mode": mode,
                    "addr": "0x%02x" % a if space == "sfr"
                             else "0x%04x" % a,
                    "table": table["rows"]})
        if row is None:
            res["error"] = ("no event row for %s %s 0x%x in `info break`; "
                            "console said: %s"
                            % (space, mode, a,
                               _first_line(res["raw"]) or "nothing"))
        elif row["mode"] and row["mode"] != want:
            res["warning"] = ("asked for %s, the table says %r"
                              % (want, row["mode"]))
        return res

    def var(self, name, space, addr, mode=None):
        """`var NAME space[addr]` -- the cell form; the documented one fails.

        `var name memory <addr>` prints its usage block and registers nothing
        (measured), so this sends the form the binary actually accepts.  The
        name is then usable inside a breakpoint condition.
        """
        if space not in SPACES:
            return {"ok": False, "raw": "",
                    "error": "memory space %r is not one of %s"
                             % (space, ", ".join(SPACES))}
        a = _as_int(addr)
        bits = "[7:0]" if mode == "byte" else ""
        res = self.command("var %s %s[0x%x]%s" % (name, space, a, bits))
        lst = self.command("info variables")
        listed = re.search(r"(?m)^%s\s+\S+\s*=" % re.escape(name), lst["raw"])
        res.update({"ok": bool(listed) and res["ok"], "name": name,
                    "space": space, "addr": "0x%04x" % a,
                    "in_variables": bool(listed),
                    "line": listed.group(0).strip() if listed else None,
                    "variables_raw": lst["raw"]})
        if not listed:
            res["error"] = ("%s not in `info variables`; console said: %s"
                            % (name, _first_line(res["raw"]) or "nothing"))
        return res

    def del_break(self, addr=None):
        """`delete [nr...]` -- by breakpoint ID, then PROVEN against the table.

        Measured traps: `del break` and `delete break` print a bare `Error` and
        change nothing; `delete 0x3a` / `delete 58` (an address where an id
        belongs) print NOTHING at all and change nothing either.  So the address
        is resolved to its id through `info break` first, and the table is
        re-read afterwards -- the reply is never the proof here.
        """
        before = self.info_break()
        if addr is None:
            cmd = "delete"
            want = [r["id"] for r in before["rows"]]
        else:
            a = _as_int(addr)
            want = [r["id"] for r in before["rows"]
                    if _as_int(r.get("addr") or "0") == a]
            if not want:
                return {"ok": True, "cmd": "delete", "raw": "", "wanted": [],
                        "deleted": [],
                        "remaining": [r["id"] for r in before["rows"]],
                        "note": "no breakpoint at 0x%x to delete" % a}
            cmd = "delete %s" % " ".join(str(i) for i in want)
        res = self.command(cmd)
        after = self.info_break()
        left = [r["id"] for r in after["rows"]]
        gone = [i for i in want if i not in left]
        res.update({"ok": len(gone) == len(want), "wanted": want,
                    "deleted": gone, "remaining": left, "rows": after["rows"]})
        if not res["ok"]:
            res["error"] = ("`%s` left %s in place (table now %s): `delete` "
                            "wants the id from `info break`, not an address, "
                            "and it fails silently"
                            % (cmd, [i for i in want if i not in gone], left))
        return res

    # -------------------------------------------------------------- execution
    def _move(self, res):
        stop = parse_stop(res.get("raw", ""))
        res.update(stop or {"stopped": False})
        if stop and "pc_int" in stop:
            res["pc"] = stop["pc"]
        else:
            rows = parse_disasm(res.get("raw", ""))
            if rows:
                res["pc"] = rows[0]["pc"]
                res["pc_int"] = int(rows[0]["pc"], 16)
        m = re.search(r"stepped\s+(\d+)\s+ticks", res.get("raw", ""))
        if m:
            res["ticks"] = int(m.group(1))
        return res

    def step(self, n=1):
        """`step N` = N INSTRUCTIONS; units are s|ms|us|ns|vclk|fclk|rclk|wclk.

        `cycle` is not a unit on this build (`Unknown unit.`).
        """
        return self._move(self.command("step %d" % int(n)))

    def step_units(self, units):
        return self._move(self.command("step %s" % units))

    def next(self):
        return self._move(self.command("next"))

    def cont(self, timeout=15.0, settle=SHELL_TICK_S):
        """`run`, then wait for the stop banner.

        The console is frozen while the target runs: nothing typed is read and
        the banner is only flushed by the interrupt handler.  So send `run`,
        wait for the stream to go quiet, then poke exactly ONE ENTER (the
        documented stop key) and read the banner.  A banner that arrives on its
        own means we never poke.  `settle > timeout` disables the poke
        altogether, which is how you ask "did anything stop it?" and get an
        honest `timeout: true` when nothing did.
        """
        if self._running:
            return {"ok": False, "stopped": False, "raw": "",
                    "error": "already running; call stop() first"}
        start = len(self._buf)
        t0 = time.time()
        self._send("run")
        self._running = True
        self._log("run", "PC=run")
        deadline = t0 + timeout
        poked = False
        while time.time() < deadline:
            stop = parse_stop(self._peek(start))
            if stop:
                break
            idle = 0.0
            while idle < settle and time.time() < deadline:
                if self._fill(0.1):
                    idle = 0.0
                else:
                    idle += 0.1
            if time.time() >= deadline:
                break
            stop = parse_stop(self._peek(start))
            if stop or settle > timeout:
                break
            try:
                self._send("")
                poked = True
            except ConsoleError:
                break
            self._log("poke", "ENTER")
        try:
            raw = self._wait_prompt(3.0, start, "run stop")
        except ConsoleError:
            raw = self._peek(start)
        self._running = False
        stop = parse_stop(raw)
        out = {"ok": bool(stop), "cmd": "run", "raw": raw,
               "poked": poked, "elapsed": round(time.time() - t0, 3)}
        out.update(stop or {"stopped": False})
        out["timeout"] = not bool(stop)
        if not stop:
            out["error"] = ("target still running after %.1fs without a stop "
                            "banner" % timeout)
        return out

    def stop(self, timeout=6.0):
        """Interrupt a running target with one ENTER.

        ENTER is the documented stop key; SIGINT to the child closes its
        console socket instead of stopping the CPU, so it is never used here.
        """
        if not self._running:
            return {"ok": True, "was_running": False, "raw": ""}
        start = len(self._buf)
        try:
            self._send("")
        except ConsoleError:
            pass
        self._log("poke", "ENTER(stop)")
        try:
            raw = self._wait_prompt(timeout, start, "stop")
        except ConsoleError as e:
            raw = self._peek(start)
            self._running = False
            return {"ok": False, "raw": raw, "was_running": True,
                    "error": str(e)}
        self._running = False
        out = {"ok": True, "raw": raw, "was_running": True}
        out.update(parse_stop(raw) or {"stopped": False})
        return out

    def run_until(self, timeout=20.0, accept=(STOP_BREAKPOINT, STOP_EVENT)):
        last, tries = None, 0
        t_end = time.time() + timeout
        while time.time() < t_end:
            r = self.cont(timeout=max(1.0, t_end - time.time()))
            last, tries = r, tries + 1
            if not r.get("stopped"):
                return {"ok": False, "stopped": False, "tries": tries,
                        "raw": r.get("raw", ""), "error": r.get("error")}
            if r.get("code") in accept:
                return {"ok": True, "stopped": True, "tries": tries,
                        "raw": r["raw"], **r}
        return {"ok": False, "stopped": False, "tries": tries, "raw": "",
                "error": "no accepted stop within %.1fs (last=%r)"
                         % (timeout, last and last.get("reason"))}

    # ----------------------------------------------------------- inspection
    def expr(self, expression):
        """Evaluate a console expression.  Parser errors stay errors.

        `expression dseg:0x30` answers `Parser error: syntax error` and THEN a
        `0` row; returning that 0 launders a broken expression into a
        plausible answer, so a parser error forces value=None.
        """
        res = self.command("expression %s" % expression)
        raw = res["raw"]
        bad = looks_like_error(raw)
        val, shown = (None, None) if bad else parse_expr_value(raw)
        res.update({"value": val, "shown": shown, "hex": _hex(val),
                    "parse_error": bool(bad), "expr": expression})
        if bad:
            res["ok"] = False
            res["error"] = "console refused %r: %s" % (expression,
                                                      _first_line(raw))
        return res

    def display(self, expression, fmt=None):
        """`display [/X] expr` -> the VALUE, not just text.

        Measured: registering a display prints NOTHING at all (the response is
        the bare prompt).  The bed shows `1:/X iram[0x41] = 0xa5` only at a
        stop or when asked for the display LIST, so the value is read back
        through the list.  No value -> ok False: never a quiet success.
        """
        line = "display %s%s" % (("/%s " % fmt) if fmt else "", expression)
        res = self.command(line)
        listing = self.command("display")
        val, shown = parse_display(listing["raw"])
        if val is None:
            val, shown = parse_display(res["raw"])
        bad = looks_like_error(res["raw"]) or looks_like_error(listing["raw"])
        res.update({"value": None if bad else val,
                    "shown": None if bad else shown,
                    "hex": _hex(None if bad else val),
                    "registered": val is not None and not bad,
                    "listing_raw": listing["raw"], "expr": expression})
        if bad:
            res["ok"] = False
            res["error"] = "console refused %r: %s" % (expression,
                                                      _first_line(res["raw"]))
        elif val is None:
            res["ok"] = False
            res["error"] = ("%s registered but the display list shows no "
                            "value (list=%r)" % (expression,
                                                 _first_line(listing["raw"])))
        return res

    def undisplay(self, ident=1):
        return self.command("undisplay %d" % ident)

    def set_memory(self, space, addr, *values):
        if space not in SPACES:
            return {"ok": False, "raw": "",
                    "error": "memory space %r is not one of %s"
                             % (space, ", ".join(SPACES))}
        vals = " ".join("0x%02x" % _as_int(v) for v in values)
        return self.command("set memory %s %d %s" % (space, _as_int(addr),
                                                     vals))

    def dump(self, space, start, stop):
        if space not in SPACES:
            return {"ok": False, "raw": "",
                    "error": "memory space %r is not one of %s"
                             % (space, ", ".join(SPACES))}
        res = self.command("dump %s %d %d" % (space, _as_int(start),
                                              _as_int(stop)))
        res.update(parse_dump(res["raw"]))
        res["space"] = space
        return res

    def history(self, n=None):
        """The execution ring: `history list` is a DISASSEMBLY of what ran.

        `history info` prints `len: 10000` (the ring size), `history list
        [N]` prints the rows.  This is the bed's only disassembler and the
        reason `at_instruction_boundary` can ever be True.
        """
        res = self.command("history list" if n is None else
                           "history list %d" % int(n))
        info = self.command("history info")
        rows = parse_history(res["raw"])
        m = re.search(r"len[:=]\s*(\d+)", info["raw"])
        res["rows"] = rows
        res["count"] = len(rows)
        res["ring_len"] = int(m.group(1)) if m else None
        res["info_raw"] = info["raw"]
        return res

    def info_variables(self):
        res = self.command("info variables")
        res["variables"] = re.findall(r"(?m)^(\S+)\s+(\w+\[0x[0-9a-fA-F]+\])",
                                      res["raw"])
        return res

    def timer(self, action="get", ident=1):
        """Cycle stopwatch: `timer add|start|get`.  20 insns = 240 clks at
        -X 11.0592M on this bed."""
        if action == "measure":
            self.command("timer add %d" % ident)
            self.command("timer start %d" % ident)
            self.command("step 20")
            res = self.command("timer get %d" % ident)
        else:
            res = self.command("timer %s %d" % (action, ident))
        m = re.search(r"(\d+)\s*clks", res["raw"])
        res["clks"] = int(m.group(1)) if m else None
        m = re.search(r"([0-9.]+)\s*sec", res["raw"])
        res["sec"] = float(m.group(1)) if m else None
        return res

    # ------------------------------------------------------------------- vcd
    def vcd(self, path, signals, action="auto", timeout=None):
        """Drive `set hw vcd ...`; action add|start|stop|sample|auto.

        `help set hardware vcd` says `No such command` and bare `set hw vcd`
        prints a `conf` dump, but the real verbs work: the `add` keyword is
        REQUIRED (`set hw vcd sfr 0x90` is not a signal spec) and the signal
        is auto-named from the SFR (`P1`).
        """
        path = os.path.abspath(path)
        signals = list(signals or [])
        if action in ("auto", "add"):
            steps = ['set hw vcd file "%s"' % path] + \
                    ["set hw vcd add %s" % s for s in signals]
            if action == "add":
                raw, errs = self.raw_batch(steps, timeout)
                return {"ok": not errs, "raw": raw,
                        "error": "; ".join(errs) or None, "file": path,
                        "signals": signals}
        elif action == "start":
            steps = ["set hw vcd start"]
        elif action == "stop":
            steps = ["set hw vcd stop"]
        elif action == "sample":
            steps = ["set hw vcd start", "step 30", "set hw vcd stop"]
        else:
            return {"ok": False, "raw": "",
                    "error": "unknown vcd action %r" % action}
        if action == "auto":
            steps += ["set hw vcd start", "step 30", "set hw vcd stop"]
        raw, errs = self.raw_batch(steps, timeout)
        out = {"ok": not errs, "raw": raw, "error": "; ".join(errs) or None,
               "file": path, "signals": signals,
               "exists": os.path.exists(path)}
        if out["exists"] and action in ("auto", "sample", "stop"):
            out["vcd"] = parse_vcd(path)
        return out

    # -------------------------------------------------------------- process
    def quit(self):
        for line in ("shutdown", "quit", "exit"):
            try:
                self._send(line)
            except ConsoleError:
                break
            for _ in range(20):
                if self._proc.poll() is not None:
                    break
                time.sleep(0.05)
            if self._proc.poll() is not None:
                break
        rc = self._proc.poll()
        if rc is None:
            self._proc.terminate()
            try:
                rc = self._proc.wait(3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                rc = self._proc.wait()
        return {"ok": True, "exitcode": rc}

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None and self._proc.poll() is None:
            self.quit()
        self._running = False
        for sig, old in list(self._handlers.items()):
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass
        self._handlers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ----------------------------------------------------------------- batch mode
def split_batch(spec):
    """Split a `--once` spec into console lines.

    The console reads ONE command per line and does NOT understand `;`, so the
    driver splits it -- on `;` outside quotes, because `file "a;b.ihx"` is one
    path.
    """
    out, cur, q = [], [], None
    for ch in spec:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [ln.strip() for ln in out if ln.strip()]


MOVE_WORDS = ("run", "cont", "go")


def once(spec, sim, device, xtal, timeout):
    """Batch mode.  A spec containing run/cont switches to an owned session.

    A plain batch runs on the stdio console with stdin HELD OPEN by
    `communicate` (closing it makes ucsim exit before the console starts), and
    `;` is split here because the console cannot do it.
    """
    lines = split_batch(spec)
    if not any(ln.split()[0] in MOVE_WORDS for ln in lines):
        p = subprocess.Popen([sim, "-t", device] +
                             (["-X", str(xtal)] if xtal else []) +
                             ["-c", "-"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        try:
            out, _ = p.communicate("\n".join(lines + ["quit"]) + "\n",
                                   timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
        out = out or ""
        stop = parse_stop(out)
        bad = next((m for m in ERROR_MARKERS if m in out), None)
        return {"ok": p.returncode in (0, None) and bad is None,
                "mode": "stdio", "raw": out, "commands": lines,
                "error": ("console reported %r" % bad) if bad else None,
                "stop": stop}
    with S51Dbg(sim=sim, device=device, xtal=xtal, timeout=timeout) as dbg:
        cap, errs = [], []
        for ln in lines:
            if ln.split()[0] in MOVE_WORDS:
                r = dbg.cont(timeout=timeout)
            else:
                r = dbg.command(ln)
            cap.append(r.get("raw", ""))
            if not r.get("ok"):
                errs.append("%s -> %s" % (ln, r.get("error")))
        joined = "\n".join(cap)
        return {"ok": not errs, "mode": "owned", "raw": joined,
                "commands": lines, "error": "; ".join(errs) or None,
                "stop": parse_stop(joined)}


# ----------------------------------------------------------------- JSON verbs
def _handle(dbg, req):
    cmd = req.get("cmd")
    if cmd == "load":
        return dbg.start(file=req.get("path"), device=req.get("device"),
                         xtal=req.get("xtal"), expect=req.get("expect"),
                         report_check=bool(req.get("report_check")))
    if cmd == "reset":
        return dbg.reset()
    if cmd == "pc":
        return dbg.pc()
    if cmd == "regs":
        return dbg.regs()
    if cmd == "state":
        return dbg.state()
    if cmd == "cmd":
        return dbg.command(req["line"], req.get("timeout"))
    if cmd == "break":
        return dbg.add_break(req["addr"], req.get("hit", 1), req.get("cond"),
                             req.get("quote", True))
    if cmd == "watch":
        return dbg.watch(req["space"], req["mode"], req["addr"])
    if cmd == "var":
        return dbg.var(req["name"], req["space"], req["addr"],
                       req.get("mode"))
    if cmd == "del_break":
        return dbg.del_break(req.get("addr"))
    if cmd == "info_break":
        return dbg.info_break()
    if cmd == "disasm":
        return dbg.disasm(req["addr"], req.get("n", 8))
    if cmd == "step":
        return (dbg.step(req["n"]) if "n" in req else
                dbg.step_units(req["units"]) if "units" in req else
                dbg.step(1))
    if cmd == "next":
        return dbg.next()
    if cmd in ("cont", "run"):
        return dbg.cont(req.get("timeout", 15.0),
                       req.get("settle", SHELL_TICK_S))
    if cmd == "stop":
        return dbg.stop()
    if cmd == "expr":
        return dbg.expr(req["expression"])
    if cmd == "display":
        return dbg.display(req["expression"], req.get("fmt"))
    if cmd == "undisplay":
        return dbg.undisplay(req.get("id", 1))
    if cmd == "set_memory":
        return dbg.set_memory(req["space"], req["addr"], *req["values"])
    if cmd == "expect_rom":
        return dbg.expect_rom(req["addr"], req["bytes"])
    if cmd == "expect_file":
        return dbg.expect_file(req["path"], req.get("limit", 512))
    if cmd == "dump":
        return dbg.dump(req["space"], req["start"], req["stop"])
    if cmd == "history":
        return dbg.history(req.get("n"))
    if cmd == "info_variables":
        return dbg.info_variables()
    if cmd == "timer":
        return dbg.timer(req.get("action", "get"), req.get("id", 1))
    if cmd == "vcd":
        return dbg.vcd(req["path"], req.get("signals") or [],
                      req.get("action", "auto"))
    raise ConsoleError("unknown cmd %r" % cmd)


VERBS = ("load reset pc regs state cmd break watch var del_break info_break "
         "disasm step next cont run stop expr display undisplay set_memory "
         "expect_rom expect_file dump history info_variables timer vcd").split()


def jsonl_loop(dbg, reads, writes):
    buf = ""
    for chunk in reads:
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                out = _handle(dbg, req)
            except ConsoleError as e:
                out = {"ok": False, "error": str(e)}
            except Exception as e:                                   # noqa: BLE001
                out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            writes(json.dumps(out, default=str) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="persistent ucsim_51 console "
                                             "driver")
    ap.add_argument("--sim", default=os.environ.get("S51_SIM", DEFAULT_SIM))
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--xtal")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--jsonl", action="store_true",
                    help="read JSON requests from stdin (one per line)")
    ap.add_argument("--once", metavar="SPEC",
                    help="batch: one-line commands, `;` separated")
    ap.add_argument("--cmd", action="append", default=[],
                    help='JSON request, repeatable')
    ap.add_argument("--session", metavar="PORT", type=int,
                    help="port of an already-running driver")
    ap.add_argument("--log")
    args = ap.parse_args(argv)

    if args.once:
        out = once(args.once, args.sim, args.device, args.xtal, args.timeout)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out["ok"] else 1
    if args.cmd:
        rc = 0
        with S51Dbg(sim=args.sim, device=args.device, xtal=args.xtal,
                    timeout=args.timeout, log=args.log) as dbg:
            for c in args.cmd:
                try:
                    out = _handle(dbg, json.loads(c))
                except Exception as e:                              # noqa: BLE001
                    out = {"ok": False, "error": str(e)}
                print(json.dumps(out, indent=2, default=str))
                rc = rc or (0 if out.get("ok", True) else 1)
        return rc
    if args.jsonl:
        with S51Dbg(sim=args.sim, device=args.device, xtal=args.xtal,
                    timeout=args.timeout, log=args.log) as dbg:
            jsonl_loop(dbg, sys.stdin, sys.stdout.write)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
