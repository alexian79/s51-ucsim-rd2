#!/usr/bin/env python3
"""Watchpoints as assertions: who writes a byte, who may not, and what a
condition breakpoint on this binary can and cannot be asked to mean.

Two instruments, two job descriptions.

1. EVENT WATCHPOINTS (break {iram|sfr} r|w ADDR) answer "who touches this cell".
   They fire under `step` AND under `run`, so the writer set can be collected
   from a deterministic step transcript.  The stop banner is

        Stop at 0x000003: (112) Event break
        Event `write' at iram[0x41]

   and the reported PC is the instruction AFTER the access - every expected PC
   below is writer address + instruction length, consistently.  The headline
   leg (WP-A1) asks the only question that matters for a device model: an
   unconditional write watchpoint on AUXR (sfr 0x8e) armed over an image with
   exactly two authorized MOV AUXR instructions must yield EXACTLY those two
   stop PCs - any third PC is a hidden writer inside the model and is reported
   raw, not weakened.

2. CONDITIONAL FETCH BREAKPOINTS (break ADDR if <expr>) answer "stop here only
   when the machine state says so".  The measured truth on this binary, all of
   it re-pinned by legs WP-K1..K12 below (do not trust ucsim's help text):
     * conditions ARE evaluated at hit time, single-token (`if ACC==0x11`) or
       quoted (`if "ACC == 0x11"`); an unquoted multi-token condition is split
       by the console parser and creates NO breakpoint (WP-K6), and an
       unquoted bracket expression is a silent parse error, also NO breakpoint
       (WP-K7).  Quote them or take the hit count.
     * DEFECT A (pinned as a leg, so a future fix flips it visibly): a bare
       numeric literal is truthy - `break 0x5 if 0` STOPS and the table
       records Cond `true` (WP-K5).  A literal 0 is NOT a negative control.
     * quoted bracket conditions (`if "iram[0x50]==0x00"`) evaluate CORRECTLY
       on this binary: fire on match, skip on mismatch (WP-K8).  The
       `var name <space>[addr]' recipe works for both polarities (WP-K9); the
       documented `var name memory ADDR' form creates no variable (WP-K10).
       Beware when hand-checking such a condition: UNWRITTEN iram resets to
       0x76, not 0x00, and an unwritten-looking cell may have been written
       earlier by the program itself - isa.ihx has iram[0x30]==0x41 at 0x003a
       (WP-K11 pins exactly that, because a wrong hand-value is what makes a
       working condition look like a silent false).
     * fetch breakpoints obey a first-fetch-after-stop exemption
       (cl_uc::do_brk, core/sim.src/uc.cc), but WHICH first fetch is exempt is
       build-dependent, so the suite classifies it (`stepping' below) and names
       the answer in the measured column:
         - `exempt-first' (upstream 0.8.15): the test runs only under
           SIM_GO && rom && steps_done > 0, and steps_done resets at every stop,
           so the first fetch after EVERY stop is exempt.  Bare `step' can never
           reach an armed address, and once a `step' has landed on one, `run'
           sails past that breakpoint as well.
         - `address-specific' (this fork: cl_sim::brk_stop_addr): only the fetch
           of the address the breakpoint itself stopped us on is exempt -- the
           one case that must be -- so a `step' onto an armed address fires it.
       WP-K13 pins both halves.  `step 30' fires the bp mid-flight on either
       build (WP-K14: exactly one (104), PC parks), and `step 2' depends on
       parity relative to the stop (WP-K15 pins pc 0x37 + step = miss, pc 0x37 +
       step 2 = FIRE, pc 0x35 + step 2 = miss; all three hold on both builds).
       Event watchpoints do NOT share the gate - they are checked in the memory
       cell write path, not in do_brk - so their (112) reports under bare `step'
       (WP-W2, WP-R1) are by design.
       Counting `Stop at <addr>' is meaningless: every step landing prints
       one.  The STATUS CODE is the discriminator - (104) Breakpoint = fetch
       bp fired, (109) stepped N ticks = landing, (112) Event break =
       watchpoint - and every negative here is asserted on the code.  Hence
       every condition leg below runs, bounded by a SENTINEL: a second
       unconditional breakpoint one instruction past the target.  Positive leg
       -> stops=[target]; negative leg -> stops=[sentinel]; both exit cleanly
       and fast, so "did not stop" is proven by the sentinel being reached,
       not by a timeout.
     * THIS SUITE IS BINARY-FAMILY AWARE, on two independent axes.  The
       cmd_bp.cc arity/keyword fix turns three silent behaviours into refusals:
       `if <bare number>', an unknown keyword after the address (`iff ...'), and
       the `hit' keyword.  WP-K0 classifies that axis (probe: `break 0x5 if 0' ->
       unconditional row on a PRE-FIX build, refused on a POST-FIX one) and the
       affected legs (WP-K5, WP-K5b, WP-K5d) assert the family's pinned matrix.
       The do_brk exemption rule is a second axis, classified by `stepping' from
       15 bare steps across an armed bp and asserted by WP-K13.  Everything else
       here measured identical on the two builds available (the frozen pre-fix
       base and this fork).
     * `break <varname>' on a var arms TWO event breakpoints, one read and one
       write (WP-K12) - count accordingly.
     * `go TIME' is an alias of `run' and never returns bounded; a frozen
       run on an endless sled needs ENTER before the console reads again.
       That is why the WP-W1/WP-C1 legs drive bash with sleeps and why the
       control leg's pass condition is "no banner AND still spinning when killed".

Honours S51_SIM (then S51) for the binary.  Uses t/isa.ihx (shipped next to
this file) for the WP-K11 cross-check only.  NOT wired into t/floor.sh.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.environ.get("S51") or os.path.expanduser(
    "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
# absolute, because a relative S51_SIM resolves against whatever cwd a session
# happens to run in, and the symptom is a suite that prints nothing at all
SIM = os.path.abspath(os.path.expanduser(SIM))
if not os.path.exists(SIM):
    sys.exit("wptrtest: no simulator at %s" % SIM)
HERE = os.path.dirname(os.path.abspath(__file__))
ISA = os.path.join(HERE, "isa.ihx")
TMP = "/tmp/wptr_%d.hex" % os.getpid()
TMPZ = "/tmp/wptr_z_%d.hex" % os.getpid()
DEV = "89C51RD2"

STOP112 = re.compile(r"Stop at 0x([0-9a-f]{6}): \(112\) Event break")
STOP104 = re.compile(r"Stop at 0x([0-9a-f]{6}): \(104\) Breakpoint")
TABLE_RE = re.compile(r"^(\d+)\s+(event|fetch|any)\s+(\S+)\s+(\d+)\s+(\d+)\s+"
                      r"(0x[0-9a-f]+)\s*(.*?)\s*$", re.M)

# images ---------------------------------------------------------------
# writer 0x000000 (MOV 0x41,#0a5, 3 bytes), NOP, endless sled at 0x06.
IMG_W = bytes([0x75, 0x41, 0xA5, 0x00, 0x80, 0xFE])
# same write, then MOV A,0x41 (E5 41) at 0x0003 - the only READER of 0x41.
# target insn 0x0005 (NOP), sentinel 0x0006 (SJMP $).
IMG_R = bytes([0x75, 0x41, 0x11, 0xE5, 0x41, 0x00, 0x80, 0xFE])
# exactly two AUXR writers: 0x75 8E 00 at 0x0000 and 0x75 8E 04 at 0x0004.
# authorized stop PCs (stop == next insn): 0x0003 and 0x0007.  NOP + sled after.
IMG_A = bytes([0x75, 0x8E, 0x00, 0x00, 0x75, 0x8E, 0x04, 0x00, 0x80, 0xFE])
# zero-cell image: 0x0000 MOV 0x50,#0 (a WRITTEN cell that holds 0x00 - an
# unwritten cell would hold 0x76); 0x0004 MOV 0x41,#0x11; target 0x0007 (NOP),
# sentinel 0x0008 (SJMP $).
IMG_Z = bytes([0x75, 0x50, 0x00, 0x00, 0x75, 0x41, 0x11, 0x00, 0x80, 0xFE])

results = []


def session(path, cmds, device=DEV, timeout=25, strict=True):
    """One scripted session on stdin.  Refuses to score a run whose image did
    not load (a zero image steps happily) and - when strict - rejects any
    rejected-command echo, so a typo in the harness fails loudly instead of
    silently.  Refusal legs pass strict=False: their input is SUPPOSED to
    upset the parser, and they assert on the info break table instead."""
    p = subprocess.run([SIM, "-t", device, "-c", "-"],
                       input="\n".join(['file "%s"' % path] + cmds + ["exit"]) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=timeout)
    out = p.stdout + p.stderr
    if "words read" not in out:
        raise RuntimeError("image did not load: %s" % out.splitlines()[:6])
    if strict:
        for ln in out.splitlines():
            if ln.startswith("Error:") or "Unknown" in ln:
                raise RuntimeError("console rejected a command: %s" % ln)
    return out


def sess(img, cmds, **kw):
    write_hex(TMP, img)
    return session(TMP, cmds, **kw)


def run_session(pre_run, hold_s):
    """bash-driven `run' session (run freezes the console; ENTER after the stop
    restores it).  A session whose watchpoint fires resumes, prints the
    trailing commands and exits 0; one whose watchpoint never fires keeps
    running the sled and must be killed - which is itself the control's
    evidence."""
    write_hex(TMP, IMG_W)
    head = '\n'.join(['file "%s"' % TMP] + pre_run + ["run"])
    shell = ("( printf '%s\\n'; sleep %g; printf '\\npc\\nexit\\n'; sleep 1 ) | "
             "%s -t %s -c -" % (head, hold_s, SIM, DEV))
    try:
        p = subprocess.run(["bash", "-c", shell], capture_output=True,
                           text=True, timeout=hold_s + 4)
        return p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() + (e.stderr or b"").decode()
        return out, "killed"


def sentinel_run(path, pre, target, sent, **kw):
    """`run' bounded by an unconditional sentinel bp past the target.  Returns
    the list of (104) stop PCs: [target] if the pre bp fired, [sent] if not."""
    out = session(path, pre + ["break 0x%x" % sent, "run", "pc"], **kw)
    return [int(m, 16) for m in STOP104.findall(out)], out


def stops112(out):
    return [int(m, 16) for m in STOP112.findall(out)]


def event_lines(out, kind, space, addr):
    key = "Event `%s' at %s[0x%x]" % (kind, space, addr)
    return sum(1 for ln in out.splitlines() if ln.startswith(key))


def table_rows(out, addr=None):
    return [r for r in TABLE_RE.findall(out)
            if addr is None or r[5] == "0x%06x" % addr]


_FAMILY = {}


def family():
    """Classify the binary under test once.  The cmd_bp.cc arity/keyword fix
    flips three observable behaviours; the discriminator here is
    `break 0x5 if 0': a PRE-FIX build silently creates an unconditional fetch
    breakpoint, a POST-FIX build refuses it and creates nothing.  Legs that
    pin a behaviour the fix changes assert the matching family's matrix and
    name the family in their measured column, so one probe re-labels them all
    and an unknown family still fails loudly via WP-K0."""
    if "f" not in _FAMILY:
        out = sess(IMG_R, ["break 0x5 if 0", "info break"], strict=False)
        _FAMILY["f"] = "prefix" if table_rows(out, 0x5) else "postfix"
    return _FAMILY["f"]


def leg(name, fn, expect, note=""):
    try:
        shown, ok = fn()
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        shown, ok = ("error:" + str(e)[:36]), False
    results.append((name, expect, shown, ok, note))


# ----------------------------------------------------------- write watchpoint
# A run of an endless sled must be stopped by the watchpoint, exactly once,
# with the (112) banner at the instruction after the write.
def wp_w1():
    out, rc = run_session(["break iram w 0x41", "info break"], 3)
    got = stops112(out)
    ok = got == [0x0003] and event_lines(out, "write", "iram", 0x41) == 1 and rc == 0
    return ("stops=%s rc=%s" % ([hex(s) for s in got], rc)), ok


leg("WP-W1 run stopped by write-wp, one (112) at next insn", wp_w1,
    "stops=['0x3'] rc=0", "endless sled: only the watchpoint can stop it")


# The same watchpoint walked with `step' past the write and 4 sled laps must
# produce exactly ONE event stop in the whole transcript.
def wp_w2():
    out = sess(IMG_W, ["break iram w 0x41"] + ["step"] * 6 + ["pc"])
    got = stops112(out)
    ok = got == [0x0003] and event_lines(out, "write", "iram", 0x41) == 1
    return ("stops=%s ev=%d" % ([hex(s) for s in got],
                                event_lines(out, "write", "iram", 0x41))), ok


leg("WP-W2 stepped over sled: exactly one event stop", wp_w2,
    "stops=['0x3'] ev=1", "6 steps: write + NOP + 4x SJMP $")


# CONTROL for the run leg: identical session, watchpoint on 0x50 which the
# program never writes -> no (112) may appear and the process is still spinning
# when killed.  This is what makes WP-W1's stop attributable to the write.
def wp_c1():
    out, rc = run_session(["break iram w 0x50", "info break"], 5)
    n = len(stops112(out))
    ok = n == 0 and rc != 0
    return ("stops=%d rc=%s" % (n, rc)), ok


leg("WP-C1 CONTROL write-wp on never-written 0x50: never stops", wp_c1,
    "stops=0 rc=killed", "no banner + still running after 5s = did not stop")


# CONTROL at the step level: watchpoint armed on a dead address, program fully
# stepped; the table must show it armed and the transcript must show zero (112).
def wp_c2():
    out = sess(IMG_W, ["break iram w 0x50", "info break"] + ["step"] * 6 + ["pc"])
    armed = table_rows(out, 0x50)
    n = len(stops112(out))
    stepped = out.count("(109)")
    ok = n == 0 and len(armed) == 1 and armed[0][6] == "write" and stepped == 6
    return ("stops=%d armed=%d stepped=%d" % (n, len(armed), stepped)), ok


leg("WP-C2 CONTROL armed-but-unwritten wp silent over full step", wp_c2,
    "stops=0 armed=1 stepped=6", "armed row + real execution = not a vacuous pass")


# ------------------------------------------------------------- read watchpoint
def wp_r1():
    out = sess(IMG_R, ["break iram r 0x41"] + ["step"] * 5 + ["pc"])
    got = stops112(out)
    ok = got == [0x0005] and event_lines(out, "read", "iram", 0x41) == 1
    return ("stops=%s ev=%d" % ([hex(s) for s in got],
                                event_lines(out, "read", "iram", 0x41))), ok


leg("WP-R1 read-wp fires at insn after MOV A,0x41", wp_r1,
    "stops=['0x5'] ev=1", "E5 41 at 0x0003, 2 bytes -> stop at 0x0005")


def wp_c3():
    out = sess(IMG_R, ["break iram r 0x52", "info break"] + ["step"] * 5 + ["pc"])
    armed = table_rows(out, 0x52)
    n = len(stops112(out))
    ok = n == 0 and len(armed) == 1 and armed[0][6] == "read" \
        and out.count("(109)") == 5
    return ("stops=%d armed=%d stepped=%d" % (n, len(armed),
                                              out.count("(109)"))), ok


leg("WP-C3 CONTROL read-wp on never-read 0x52: silent", wp_c3,
    "stops=0 armed=1 stepped=5", "control for WP-R1")


# ----------------------------------------------- the real assertion: writer set
# Two authorized AUXR writers, per-hit (keep) watchpoint, 6 steps (both writers
# + NOP + 3 sled laps).  The stop-PC set must be EXACTLY {0x0003, 0x0007}; any
# additional stop PC is a hidden writer in the device model - report it raw.
AUTHORIZED = {0x0003, 0x0007}


def wp_a1():
    out = sess(IMG_A, ["break sfr w 0x8e"] + ["step"] * 6 + ["pc"])
    got = stops112(out)
    extra = [hex(g) for g in got if g not in AUTHORIZED]
    missing = [hex(a) for a in sorted(AUTHORIZED - set(got))]
    ok = not extra and not missing and len(got) == 2 and \
        event_lines(out, "write", "sfr", 0x8e) == 2
    if ok:
        shown = "stops=%s extra=[]" % [hex(g) for g in got]
    else:
        keep = [ln for ln in out.splitlines()
                if ln.startswith("Stop at") or ln.startswith("Event")]
        shown = "stops=%s extra=%s missing=%s || %s" % (
            [hex(g) for g in got], extra, missing, keep[-4:])
    return shown, ok


leg("WP-A1 AUXR writer PC-set == {0x3,0x7}, no hidden writer", wp_a1,
    "stops=['0x3','0x7'] extra=[]",
    "stop PC = writer addr + insn length, always")

# positional hit count on a watchpoint: `break sfr w 0x8e 2' arms ONE
# breakpoint whose table row reads Hit 2 Cnt 2 - it stops on the SECOND hit.
# The two-writer image must therefore show exactly one event stop, at the
# second writer's stop PC (0x0007), and nothing more in the remaining steps.
# (Positional form, so unaffected by the `hit' keyword work in cmd_bp.cc.)
def wp_w3():
    out = sess(IMG_A, ["break sfr w 0x8e 2", "info break"] +
               ["step"] * 6 + ["pc"])
    got = stops112(out)
    armed = [r for r in table_rows(out, 0x8e) if r[1] == "event"]
    ok = got == [0x0007] and len(armed) == 1 and armed[0][3] == "2"
    return ("stops=%s hit=%s" % ([hex(g) for g in got],
                                 armed[0][3] if armed else None)), ok


leg("WP-W3 positional hit-2 write-wp stops on SECOND write only", wp_w3,
    "stops=['0x7'] hit=2", "table row Hit 2 Cnt 2; `hit' keyword is separate")


# ------------------------------------------ conditional breakpoints, true matrix
def k_single_pos():
    got, _ = sentinel_run_sess(IMG_R, ["break 0x5 if ACC==0x11"], 0x5, 0x6)
    return ("stops=%s" % [hex(g) for g in got]), got == [0x05]


def k_single_neg():
    got, _ = sentinel_run_sess(IMG_R, ["break 0x5 if ACC==0x22"], 0x5, 0x6)
    return ("stops=%s" % [hex(g) for g in got]), got == [0x06]


def k_quoted_pos():
    got, _ = sentinel_run_sess(IMG_R, ['break 0x5 if "ACC == 0x11"'], 0x5, 0x6)
    return ("stops=%s" % [hex(g) for g in got]), got == [0x05]


def k_quoted_neg():
    got, _ = sentinel_run_sess(IMG_R, ['break 0x5 if "ACC == 0x22"'], 0x5, 0x6)
    return ("stops=%s" % [hex(g) for g in got]), got == [0x06]


def sentinel_run_sess(img, pre, target, sent):
    write_hex(TMP, img)
    return sentinel_run(TMP, pre, target, sent)


def k_defect_a():
    # behaviour changed by the cmd_bp.cc arity/keyword fix
    out = sess(IMG_R, ["break 0x5 if 0", "info break", "break 0x6", "run", "pc"],
               strict=False)
    got = [int(m, 16) for m in STOP104.findall(out)]
    rows = table_rows(out, 0x5)
    if family() == "prefix":
        # DEFECT (A) pre-fix: a bare numeric literal is truthy and carries no
        # string value, so `if 0' silently becomes an UNCONDITIONAL breakpoint
        # (Cond column `true', no cond="...").  A literal 0 is not usable as a
        # negative control on such a build.
        ok = got == [0x05] and len(rows) == 1 and rows[0][6] == "true"
    else:
        # POST-FIX: refused outright with a message; nothing created, so the
        # run passes the target and stops ONLY at the sentinel (0x06).
        msg = any("bare number" in ln for ln in out.splitlines())
        ok = got == [0x06] and not rows and msg
    return ("%s stops=%s rows=%d" % (family(), [hex(g) for g in got],
                                     len(rows))), ok


leg("WP-K1 single-token condition, match, stops at insn", k_single_pos,
    "stops=['0x5']", "conditions ARE evaluated at hit time")
leg("WP-K2 single-token condition, mismatch, runs to sentinel", k_single_neg,
    "stops=['0x6']", "sentinel bp bounds the negative leg")
leg("WP-K3 quoted condition, match, stops at insn", k_quoted_pos,
    "stops=['0x5']", "quoted form carries spaces")
leg("WP-K4 quoted condition, mismatch, runs to sentinel", k_quoted_neg,
    "stops=['0x6']", "")
leg("WP-K5 `if 0': PREFIX unconditional+stops, POSTFIX refused", k_defect_a,
    "prefix: stops=[0x5]+row | postfix: sentinel only, no row",
    "behaviour changed by the cmd_bp.cc arity/keyword fix")


def k_family():
    return ("family=%s" % family(), family() in ("prefix", "postfix"))


# behaviour changed by the cmd_bp.cc arity/keyword fix: pre-fix a keyword
# that is not `if' is IGNORED, so `iff <cond>' silently creates an
# UNCONDITIONAL breakpoint; post-fix it is refused and creates nothing.
def k_iff_keyword():
    out = sess(IMG_R, ["break 0x5 iff ACC==0x11", "info break", "break 0x6",
                       "run", "pc"], strict=False)
    got = [int(m, 16) for m in STOP104.findall(out)]
    rows = table_rows(out, 0x5)
    if family() == "prefix":
        ok = got == [0x05] and len(rows) == 1 and "cond=" not in rows[0][6]
    else:
        ok = got == [0x06] and not rows
    return ("%s stops=%s rows=%d" % (family(), [hex(g) for g in got],
                                     len(rows))), ok


# Forms BOTH families refuse (nothing created either way): a bare trailing
# `if', and a condition followed by junk.
def k_refused_both():
    o1 = sess(IMG_R, ["break 0x5 if", "info break"], strict=False)
    o2 = sess(IMG_R, ["break 0x5 if ACC==0x11 junk", "info break"], strict=False)
    r1, r2 = table_rows(o1), table_rows(o2)
    ok = not r1 and not r2
    return ("bare-if rows=%d junk rows=%d" % (len(r1), len(r2))), ok


# behaviour changed by the cmd_bp.cc arity/keyword fix: the `hit' KEYWORD.
# Pre-fix it is silently swallowed - the row reads Hit 1 whatever you asked
# for, for fetch AND event breakpoints; post-fix the row reads Hit 2 Cnt 2,
# identical to the positional `break 0x5 2', which both families honour.
def k_hit_keyword():
    o1 = sess(IMG_R, ["break 0x5 hit 2", "info break"], strict=False)
    o2 = sess(IMG_R, ["break 0x5 2", "info break"], strict=False)
    o3 = sess(IMG_W, ["break iram w 0x41 hit 2", "info break"], strict=False)
    f = table_rows(o1, 0x5)
    p = table_rows(o2, 0x5)
    w = [r for r in table_rows(o3, 0x41) if r[1] == "event"]
    want = "2" if family() == "postfix" else "1"
    ok = (len(f) == 1 and f[0][3] == want and len(w) == 1 and w[0][3] == want
          and len(p) == 1 and p[0][3] == "2")
    return ("%s keyword=%s/%s positional=%s" %
            (family(), f[0][3] if f else None, w[0][3] if w else None,
             p[0][3] if p else None)), ok


leg("WP-K0 binary family via `if 0' probe", k_family,
    "family in {prefix,postfix}", "prefix=pre cmd_bp fix; postfix refuses")
leg("WP-K5b `iff C': PREFIX unconditional bp, POSTFIX refused", k_iff_keyword,
    "prefix: stops=[0x5] no cond= | postfix: stops=[0x6] no row",
    "behaviour changed by the cmd_bp.cc arity/keyword fix")
leg("WP-K5c bare `if' and `if C junk': refused on BOTH builds", k_refused_both,
    "bare-if rows=0 junk rows=0", "family-independent rejection")
leg("WP-K5d `hit N' keyword: PREFIX silently 1, POSTFIX honoured", k_hit_keyword,
    "prefix: keyword=1 | postfix: keyword=2 (positional always 2)",
    "fetch AND event forms; positional `ADDR 2' unchanged")


def k_multitoken():
    out = sess(IMG_R, ["break 0x5 if ACC == 0x11", "info break"], strict=False)
    rows = table_rows(out)
    return ("rows=%d%s" % (len(rows), (" " + str(rows)) if rows else "")), \
        len(rows) == 0


def k_unquoted_bracket():
    out = sess(IMG_R, ["break 0x5 if iram[0x41]==0x11", "info break"], strict=False)
    rows = table_rows(out)
    return ("rows=%d%s" % (len(rows), (" " + str(rows)) if rows else "")), \
        len(rows) == 0


leg("WP-K6 unquoted MULTI-token cond: NO breakpoint", k_multitoken, "rows=0",
    "console splits on spaces: usage line, empty table")
leg("WP-K7 unquoted bracket cond: NO breakpoint", k_unquoted_bracket, "rows=0",
    "single token, still a silent parse error - quote or take hit counts")


def k_quoted_bracket():
    hit, _ = sentinel_run_sess(IMG_Z, ['break 0x7 if "iram[0x50]==0x00"'], 0x7, 0x8)
    miss, _ = sentinel_run_sess(IMG_Z, ['break 0x7 if "iram[0x50]==0x99"'], 0x7, 0x8)
    ok = hit == [0x07] and miss == [0x08]
    return ("hit=%s miss=%s" % ([hex(g) for g in hit], [hex(g) for g in miss])), ok


def k_var_recipe():
    hit, _ = sentinel_run_sess(IMG_R, ["var flag iram[0x41]",
                                       "break 0x5 if flag==0x11"], 0x5, 0x6)
    miss, _ = sentinel_run_sess(IMG_R, ["var flag iram[0x41]",
                                        "break 0x5 if flag==0x00"], 0x5, 0x6)
    ok = hit == [0x05] and miss == [0x06]
    return ("hit=%s miss=%s" % ([hex(g) for g in hit], [hex(g) for g in miss])), ok


def k_var_syntax():
    out = sess(IMG_R, ["var flag iram[0x41]", "var counter iram 0x30",
                       "var scratch", "info variables"])
    cell = re.search(r"(?m)^flag\s+iram\[0x41\]", out) is not None
    space = re.search(r"(?m)^counter\s+iram\[0x30\]", out) is not None
    bare = re.search(r"(?m)^scratch\s+variables\[0x00\]", out) is not None
    out2 = sess(IMG_R, ["var bad memory 0x41", "info variables"])
    memory = re.search(r"(?m)^bad\s", out2) is not None
    # cell form, space-separated form and bare `var NAME' (-> variables[]) all
    # create real variables on this binary.  `var NAME memory ADDR' is REFUSED
    # here too - the usage line's `memory' is a placeholder people type
    # literally, so that is a documentation bug, not a silent-garbage defect.
    ok = cell and space and bare and not memory
    return ("cell=%s space=%s bare=%s memory=%s" %
            (cell, space, bare, memory)), ok


def k_isa_matrix():
    # Cross-check on t/isa.ihx: ACC==0x5a at 0x003a (the documented positive),
    # ACC==0x5b (negative), and the cell that LOOKS like it should be 0x00:
    # at the 0x003a stop `dump iram 0x2e 0x32' shows iram[0x30]==0x41, written
    # there earlier by the program itself.  Pinned so that nobody again calls
    # a correctly-skipping condition a "silent false" off a wrong hand-value.
    p1, _ = sentinel_run(ISA, ["break 0x3a if ACC==0x5a"], 0x3a, 0x3c)
    n1, _ = sentinel_run(ISA, ["break 0x3a if ACC==0x5b"], 0x3a, 0x3c)
    p2, _ = sentinel_run(ISA, ['break 0x3a if "iram[0x30]==0x41"'], 0x3a, 0x3c)
    n2, _ = sentinel_run(ISA, ['break 0x3a if "iram[0x30]==0x00"'], 0x3a, 0x3c)
    ok = p1 == [0x3a] and n1 == [0x3c] and p2 == [0x3a] and n2 == [0x3c]
    return ("acc5a=%s acc5b=%s cell41=%s cell00=%s" %
            (p1 == [0x3a], n1 == [0x3c], p2 == [0x3a], n2 == [0x3c])), ok


def k_var_double_bp():
    out = sess(IMG_R, ["var flag iram[0x41]", "break flag", "info break"])
    rows = table_rows(out, 0x41)
    kinds = sorted(r[6] for r in rows)
    ok = kinds == ["read", "write"]
    return ("rows=%d kinds=%s" % (len(rows), kinds)), ok


leg("WP-K8 quoted bracket cond: fires on match, skips miss", k_quoted_bracket,
    "hit=['0x7'] miss=['0x8']", "written-zero cell - unwritten would be 0x76")
leg("WP-K9 var recipe works both polarities", k_var_recipe,
    "hit=['0x5'] miss=['0x6']", "runbook recipe, verified")
leg("WP-K10 var cell/space/bare forms work; memory form refused", k_var_syntax,
    "cell=True space=True bare=True memory=False",
    "usage line's `memory' is a placeholder - docs bug")
leg("WP-K11 isa.ihx: ACC matrix + iram[0x30]==0x41 at 0x3a", k_isa_matrix,
    "acc5a=True acc5b=True cell41=True cell00=True",
    "kills the wrong-hand-value 'silent false' story")
leg("WP-K12 break <var> arms TWO event bps: read + write", k_var_double_bp,
    "rows=2 kinds=['read','write']", "count hits accordingly")

# Two builds, two fetch-breakpoint stepping rules, and `stepping' below picks one
# from behaviour, never from a version string.  Upstream 0.8.15 tests a fetch
# breakpoint only under SIM_GO && rom && steps_done > 0, and steps_done resets to
# 0 at every stop, so the first fetch after EVERY stop is exempt -- which is what
# makes a bare `step' unable to reach an armed address.  This fork records which
# address the breakpoint actually stopped us on (cl_sim::brk_stop_addr, set in
# cl_sim::stop) and exempts only that fetch, the one that genuinely would
# re-trigger at once.  WP-K13/K14/K15 pin the rule; all three count the (104)
# CODE - the address in `Stop at 0x...' is printed by every step landing and
# means nothing.
_STEP = {}


def step_across_bp():
    """One memoized session: 15 bare `step' commands across a bp at 0x3a."""
    if "out" not in _STEP:
        _STEP["out"] = session(ISA, ["break 0x3a", "info break"] +
                               ["step"] * 15 + ["pc"])
    return _STEP["out"]


def stepping():
    """`exempt-first' (upstream) or `address-specific' (this fork)."""
    if "s" not in _STEP:
        _STEP["s"] = ("address-specific" if "(104)" in step_across_bp()
                      else "exempt-first")
    return _STEP["s"]


def k_step_pass_through():
    out = step_across_bp()
    n104 = out.count("(104)")
    landed = re.findall(r"(?m)^F (0x[0-9a-f]+)$", out)
    armed = table_rows(out, 0x3a)
    outr = session(ISA, ["break 0x3a", "run", "pc"])   # positive control
    fired = outr.count("(104)")
    if stepping() == "address-specific":
        # a step onto the armed address fires it; the steps left over walk on
        ok = (len(armed) == 1 and n104 == 1 and fired >= 1 and
              "0x00003a" in landed)
    else:
        ok = (len(armed) == 1 and n104 == 0 and fired >= 1 and
              "0x00003a" in landed and landed[-1] == "0x00004a")
    return ("%s step104=%d run104=%d last_F=%s" %
            (stepping(), n104, fired, landed[-1] if landed else None)), ok


leg("WP-K13 step reaches a fetch bp only under address-specific exemption",
    k_step_pass_through,
    "address-specific: step104=1 | exempt-first: step104=0 last_F=0x00004a",
    "count CODES; the rule in force is named in the measured column")


def k_step30_fires():
    out = session(ISA, ["break 0x3a", "info break", "step 30", "pc"])
    n104 = out.count("(104)")
    parked = [l for l in out.splitlines() if l.startswith("F ")]
    ok = out.count("(109)") == 0 and n104 == 1 and parked[-1] == "F 0x00003a"
    return ("104x%d 109x%d %s" % (n104, out.count("(109)"),
                                  parked[-1] if parked else None)), ok


leg("WP-K14 `step 30' crosses bp mid-flight: one (104), PC parks", k_step30_fires,
    "104x1 109x0 F 0x00003a",
    "mid-step fetches DO test the bp: the other half of the rule")


def k_parity_pair():
    def probe(pre):
        out = session(ISA, ["break 0x3a"] + pre + ["pc"])
        last = [l[2:] for l in out.splitlines() if l.startswith("F ")]
        return out.count("(104)"), (last[-1] if last else None)
    a1 = probe(["pc 0x37", "step"])     # bp is the exempt first fetch: lands
    a2 = probe(["pc 0x37", "step 2"])   # bp is the SECOND fetch: fires, parks
    a3 = probe(["pc 0x35", "step 2"])   # lands ON the bp: exempt, no fire
    ok = a1 == (0, "0x00003a") and a2 == (1, "0x00003a") and \
        a3 == (0, "0x00003a")
    return ("37+1:%d/%s 37+2:%d/%s 35+2:%d/%s" %
            (a1[0], a1[1], a2[0], a2[1], a3[0], a3[1])), ok


leg("WP-K15 fetch bp parity: first fetch after stop is exempt", k_parity_pair,
    "37+1:0 37+2:1 35+2:0 all @0x00003a",
    "do_brk steps_done gate: pins the RULE, not folklore")


# ------------------------------------------------------------------------- report
print("%-56s %-27s %-34s %-4s %s" %
      ("leg", "expected", "measured", "verdict", "note"))
bad = 0
for name, expect, got, ok, note in results:
    bad += 0 if ok else 1
    print("%-56s %-27s %-34s %-4s %s" % (name, expect, got,
                                         "ok" if ok else "BAD", note))
print("binary: %s" % SIM)
print("wptrtest: %s %d/%d legs" % ("FAIL" if bad else "PASS",
                                   len(results) - bad, len(results)))
if bad:
    print("wptrtest: %d leg(s) failed" % bad)
sys.exit(1 if bad else 0)
