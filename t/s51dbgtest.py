#!/usr/bin/env python3
"""Gate for tools/s51dbg.py: does the driver report what the bed REALLY does?

Every leg asserts on the driver's PARSED output, never on exit status, and
every leg that could pass by accident carries a control.  Binary under test:
`$S51_SIM` (default: this tree's own build) -- ucsim 0.8.15, `-t 89C51R`.  The
numbers below were measured on THAT binary; a rebuilt/patched ucsim may
behave differently and a red leg then means "the bed moved", not "the driver
rotted".

Measured facts the legs encode:

  1  fetch breakpoint       `Stop at 0x000037: (104) Breakpoint`
  2  watchpoint             `Stop at 0x000003: (112) Event break` -- reported
     at the instruction AFTER the write
  3  CONTROL for leg 2      a watch on a byte that is never written must NOT
     stop.  With `settle > timeout` the driver never pokes ENTER, so it
     reports timeout, not a stop.
  4  hit count              `break 0x37 2` is POSITIONAL and the table lists
     `Cnt 2`.  Cnt counts PASSES, not `cont()` calls: t/isa.ihx walks 0x37
     once, so a Cnt-2 breakpoint there must NOT stop while a plain one stops
     immediately; a loop that walks 0x02 twice stops at 0x02 in ONE `run`.
  5  display                `display /X iram[0x41]` prints NOTHING when it is
     registered (the answer is the bare prompt); the value only appears at a
     stop or in the display LIST, so the driver reads the list.
  6  history                `history list` is a disassembled execution ring.
  7  vcd                    `set hw vcd file/add/start` + steps + stop; the
     program must WRITE the sampled SFR -- isa.ihx never touches P1, so a
     capture of it has one dumpvars value and zero transitions.
  8  the driver must discriminate, in both directions:
     a `expression dseg:0x30` -> `Parser error: syntax error` and THEN `0`;
       returning that 0 launders a broken expression into an answer.
     b a literal condition (`if 0`) is not a real condition: on this
       bed it is registered and the live Cond column evaluates it, so the
       driver flags it instead of reporting a plain success.
     c unquoted `if iram[0x40]==0x5a` -> Parser error, table stays EMPTY.
     d quoted `if "iram[0x40]==0x5a"` registers fine AND DOES FIRE here
       (3/3, and again from a cold start) -- so the driver must not claim
       brackets never fire.  It must, however, refuse to let "it did not
       stop" be read as "the breakpoint is broken": `iram[0x30]==0x00`
       registers, never fires, and the reason is that iram[0x30] is 0x41 at
       that moment, i.e. the condition was simply false.  `cond_holds_now`
       reports the value the bed itself gives for the condition, and that is
       the difference between a defect and a wrong assumption.
     e `var flag iram[0x40]` (cell form) works and is usable in a condition;
       the documented `var name memory <addr>` prints its usage block and
       registers nothing.
  9  batch `--once` splits `;` itself (the console cannot) and owns the
     session when the spec contains run/cont.
 10  state()/timer() snapshot fields.

NOT asserted anywhere: any claim that single-stepping cannot hit a fetch
breakpoint.  On stock ucsim the first fetch after a stop is exempt, which
makes step-onto-breakpoint parity-dependent on the unpatched build; that is a
property of the binary, not an invariant, so the gate never depends on it.
Every stop assertion here is on the stop CODE (104/112/109), never on the
address printed in `Stop at <addr>` -- a step that merely lands on an address
prints the same address as a breakpoint that fires there.

Run (from the repo root; `$S51_SIM` selects the binary under test):
  S51_SIM=$PWD/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51 \
    python3 t/s51dbgtest.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import s51dbg                                                      # noqa: E402

SIM = os.environ.get("S51_SIM") or s51dbg.DEFAULT_SIM
ISA = os.path.join(HERE, "isa.ihx")
VCD = "/tmp/s51dbg_%d.vcd" % os.getpid()

# t/isa.ihx walks 0x00,0x30,0x33,0x35,0x37,0x3a,0x3c,0x3d,0x3e,0x40,0x41,
# 0x42,0x44 -- all real instruction boundaries.  At 0x3a: ACC=0x5a,
# iram[0x40]=0x5a, iram[0x41]=0xa5, iram[0x30]=0x41 (a code byte, NOT zero).
BP_ADDR = 0x37
COND_ADDR = 0x3a
# Every leg whose point is that a breakpoint does NOT fire gets a SENTINEL
# breakpoint at an address the program reaches LATER.  Without it the target
# runs until the driver gives up, and a killed simulator loses its buffered
# stdout -- which looks exactly like "the breakpoint was never created".  With
# it, the stop banner proves the run really walked past the address under
# test.  `info break` rows are matched by address, so a sentinel row can never
# be mistaken for the row under test.
SENTINEL = 0x44

# MOV R2,#4 / MOV 0x41,#0x22 / SJMP back -- 0x02 is passed 4 times, so a Cnt-2
# breakpoint stops inside a single run.
LOOP_BYTES = (0x7a, 0x04,
              0x75, 0x41, 0x22,
              0x80, 0xfb)
# MOV P1,#0x11 / #0x22 / #0x33 / SJMP $ -- three sampled-SFR writes.
P1_BYTES = (0x75, 0x90, 0x11,
            0x75, 0x90, 0x22,
            0x75, 0x90, 0x33,
            0x80, 0xfe)
# MOV 0x30,#0x55 / RET -- the write is in instruction 0, so a watch on
# iram[0x30] stops at 0x0003, the instruction AFTER the access.
STORE_RET = (0x75, 0x30, 0x55, 0x22)

results = []
sessions = []
pids = []


def check(leg, sub, ok, expect, got, note=""):
    results.append((leg, sub, expect, got, bool(ok), note))
    return bool(ok)


def session():
    """Track every simulator so a leg that raises cannot leak the port."""
    d = s51dbg.S51Dbg(sim=SIM, timeout=12)
    pids.append(d._proc.pid)
    sessions.append(d)
    return d


def fresh():
    d = session()
    d.start(file=ISA)
    return d


def stopped_real(r):
    return bool(r.get("stopped")) and r.get("code") in (104, 112)


if not os.path.exists(SIM):
    print("no simulator at %s (set $S51_SIM)" % SIM)
    sys.exit(2)

# ---------------------------------------------------------------- 1. breakpoint
try:
    d = fresh()
    b = d.add_break(BP_ADDR)
    r = d.cont(timeout=10)
    check("1 break@executed", "break created",
          b.get("created") and b.get("in_table"), "created + in_table",
          "created=%s in_table=%s" % (b.get("created"), b.get("in_table")))
    check("1 break@executed", "stopped", stopped_real(r),
          "stopped true, code 104",
          "stopped=%s code=%s" % (r.get("stopped"), r.get("code")))
    check("1 break@executed", "code", r.get("code") == 104, "104",
          repr(r.get("code")))
    check("1 break@executed", "pc", r.get("pc") == "0x%06x" % BP_ADDR,
          "0x%06x" % BP_ADDR, repr(r.get("pc")))
    check("1 break@executed", "reason", r.get("reason") == "Breakpoint",
          "Breakpoint", repr(r.get("reason")))
    check("1 break@executed", "not a user stop", r.get("code") != 105,
          "not 105 (that is ENTER)", repr(r.get("code")),
          "105 would mean the ENTER poke, not the breakpoint")
    d.close()
except Exception as e:                                      # noqa: BLE001
    check("1 break@executed", "exception", False, "no exception", repr(e))

# --------------------------------------------------------------- 2. watchpoint
try:
    d = session()
    d.set_memory("rom", 0, *STORE_RET)
    w = d.watch("iram", "w", 0x30)
    check("2 watch iram w", "watchpoint in table",
          w.get("ok") and (w.get("watch") or {}).get("mode") == "write",
          "event row, mode write", "%s" % (w.get("watch"),),
          "no acknowledgement is printed; the row is the only proof")
    r = d.cont(timeout=10)
    check("2 watch iram w", "code", r.get("code") == 112, "112",
          repr(r.get("code")), "112 = Event break, not 105 from the poke")
    check("2 watch iram w", "pc", r.get("pc") == "0x000003", "0x000003",
          repr(r.get("pc")), "reported at the insn after the write")
    d.close()
except Exception as e:                                      # noqa: BLE001
    check("2 watch iram w", "exception", False, "no exception", repr(e))

# CONTROL for leg 2: same image, watch one byte over.  0x31 is never written,
# so nothing may stop -- without this, leg 2 passes for any driver that
# reports a stop whenever ENTER interrupts a run.  A sentinel at 0x0003 (the
# RET this image always reaches) makes "nothing stopped" a POSITIVE result:
# the run happened and the watch simply never triggered.
try:
    d = session()
    d.set_memory("rom", 0, *STORE_RET)
    w = d.watch("iram", "w", 0x31)
    check("3 control iram w", "watchpoint in table", w.get("ok"),
          "event row at 0x000031", "%s" % (w.get("watch"),))
    sent = d.add_break(0x03)
    check("3 control iram w", "sentinel row separate",
          sent.get("created") and
          (sent.get("breakpoint") or {}).get("type") == "fetch" and w.get("ok"),
          "fetch + event rows coexist",
          "rows=%s" % ([(x["id"], x["type"], x["addr"])
                        for x in (sent.get("table") or [])],))
    r = d.cont(timeout=8)
    check("3 control iram w", "no watch hit",
          stopped_real(r) and r.get("code") == 104 and
          r.get("pc") == "0x000003", "104 at the sentinel 0x000003",
          "code=%s pc=%s" % (r.get("code"), r.get("pc")),
          "the run happened; the watch never triggered")
    d.del_break()
    d.command("pc 0")
    d.watch("iram", "w", 0x31)
    r2 = d.cont(timeout=4, settle=99)          # settle>timeout: never poke
    check("3 control iram w", "driver honours settle>timeout",
          not r2.get("stopped") and r2.get("timeout"),
          "stopped false + timeout true",
          "stopped=%s timeout=%s" % (r2.get("stopped"), r2.get("timeout")),
          "no ENTER poke, so no banner is flushed either")
    d.close()
except Exception as e:                                      # noqa: BLE001
    check("3 control iram w", "exception", False, "no exception", repr(e))

# ---------------------------------------------------------- 4. positional hit
try:
    d = fresh()
    b2 = d.add_break(BP_ADDR, hit=2)
    check("4 hit count", "Cnt listed as 2", b2.get("hits_listed") == 2,
          "hits_listed 2", repr(b2.get("hits_listed")),
          "positional: `break 0x37 2`")
    sent = d.add_break(SENTINEL)
    r1 = d.cont(timeout=8)
    check("4 hit count", "Cnt=2 passes 0x37 unfired",
          sent.get("created") and stopped_real(r1) and
          r1.get("code") == 104 and r1.get("pc") == "0x%06x" % SENTINEL,
          "104 at sentinel 0x%06x" % SENTINEL,
          "code=%s pc=%s" % (r1.get("code"), r1.get("pc")),
          "isa.ihx walks 0x37 once; Cnt 2 wants a second pass")
    d.del_break()
    d.command("pc 0")
    d.add_break(BP_ADDR)
    r2 = d.cont(timeout=10)
    check("4 hit count", "Cnt=1 stops at the same address",
          stopped_real(r2) and r2.get("pc") == "0x%06x" % BP_ADDR,
          "stops at 0x%06x" % BP_ADDR,
          "code=%s pc=%s" % (r2.get("code"), r2.get("pc")),
          "only the count differs")
    d.close()
    d = session()
    d.set_memory("rom", 0, *LOOP_BYTES)
    bl = d.add_break(0x02, hit=2)
    rl = d.cont(timeout=10)
    check("4 hit count", "Cnt=2 fires in ONE run after 2 passes",
          stopped_real(rl) and rl.get("pc") == "0x000002" and
          bl.get("hits_listed") == 2, "stops at 0x000002",
          "code=%s pc=%s hits=%s" % (rl.get("code"), rl.get("pc"),
                                     bl.get("hits_listed")),
          "Cnt counts passes, not cont() calls")
    d.close()
except Exception as e:                                      # noqa: BLE001
    check("4 hit count", "exception", False, "no exception", repr(e))

# ------------------------------------------------------------------ 5. display
try:
    d = fresh()
    d.step(6)                                  # a stop: the list needs one
    d.set_memory("iram", 0x41, 0xa5)
    dv = d.display("iram[0x41]", "X")
    check("5 display", "value parsed as 0xa5", dv.get("value") == 0xA5,
          "value 0xa5 (int)", "%r (%s)" % (dv.get("value"),
                                           type(dv.get("value")).__name__),
          "reg echo=%r list=%r" % (dv.get("raw", "")[:12],
                                   (dv.get("listing_raw") or "")[:30]))
    check("5 display", "ok not laundered", dv.get("ok") is True, "ok true",
          repr(dv.get("ok")), "error=%r" % (dv.get("error"),))
    d.undisplay(1)
    d.close()
except Exception as e:                                      # noqa: BLE001
    check("5 display", "exception", False, "no exception", repr(e))

# ------------------------------------------------------------------- 6. history
try:
    d = fresh()
    d.step(8)
    h = d.history()
    rows = h.get("rows") or []
    check("6 history", "non-empty", h.get("count", 0) > 0, "count > 0",
          repr(h.get("count")))
    check("6 history", "disassembled rows",
          all(("pc" in x and x.get("mnemonic")) for x in rows) and
          any("LJMP" in (x.get("mnemonic") or "") for x in rows),
          "pc + mnemonic, LJMP seen",
          "%s" % ([x.get("mnemonic") for x in rows[:3]],),
          "ring_len=%s" % (h.get("ring_len"),))
    d.close()
except Exception as e:                                      # noqa: BLE001
    check("6 history", "exception", False, "no exception", repr(e))

# ---------------------------------------------------------------------- 7. vcd
try:
    d = session()
    if os.path.exists(VCD):
        os.unlink(VCD)
    d.set_memory("rom", 0, *P1_BYTES)
    v = d.vcd(VCD, ["sfr 0x90"], "auto")
    p = v.get("vcd") or {}
    check("7 vcd", "file written", bool(p.get("bytes")), "bytes > 0",
          repr(p.get("bytes")), "exists=%s" % v.get("exists"))
    check("7 vcd", "signal resolved by name",
          list((p.get("signals") or {}).values()) == ["ucsim_vcd_0/P1"],
          "ucsim_vcd_0/P1", "%s" % (p.get("signals"),),
          "$var is `type size id name`; the file is CRLF")
    check("7 vcd", "3+ value changes", p.get("n_changes", 0) >= 3,
          "n_changes >= 3", repr(p.get("n_changes")),
          "n_transactions=%s" % (p.get("n_transactions"),))
    vals = [val for _, val in (p.get("changes") or {}).get("ucsim_vcd_0/P1",
                                                           [])]
    check("7 vcd", "the three writes recorded",
          ["00010001", "00100010", "00110011"] == vals[-3:],
          "00010001,00100010,00110011", "%s" % (vals,))
    d.close()
except Exception as e:                                      # noqa: BLE001
    check("7 vcd", "exception", False, "no exception", repr(e))

# ------------------------------------------- 8. discriminate, both directions
try:
    d = fresh()
    e = d.expr("dseg:0x30")
    check("8 discriminate", "8a dseg:0x30 refused",
          e.get("ok") is False and e.get("value") is None and
          e.get("parse_error") is True,
          "ok false, value None, parse_error",
          "ok=%s value=%r parse_error=%s" % (e.get("ok"), e.get("value"),
                                             e.get("parse_error")),
          "raw=%r" % (e.get("raw", "")[:44],))
    good = d.expr("iram[0x41]")
    check("8 discriminate", "8a control iram[0x41] parses",
          good.get("ok") and good.get("value") is not None, "int value",
          "value=%r hex=%r" % (good.get("value"), good.get("hex")),
          "the bed answers with a BARE DECIMAL")

    b0 = d.add_break(COND_ADDR, cond="0")
    refused = b0.get("created") is False
    check("8 discriminate", "8b literal cond is not trusted",
          refused or (b0.get("trusted") is False and
                      "constant" in (b0.get("warning") or "")),
          "refused, or flagged as a constant",
          "created=%s trusted=%s warning=%r" % (b0.get("created"),
                                                b0.get("trusted"),
                                                (b0.get("warning") or "")[:52]),
          "a patched bed refuses `if 0`; this one registers it -- either way "
          "the driver must not call it a plain success")
    if not refused:
        check("8 discriminate", "8b literal cond reported live",
              b0.get("cond_now") in (True, False), "cond_now is a bool",
              repr(b0.get("cond_now")),
              "info break's Cond column is the LIVE value, not the stored text")
    d.del_break()

    bu = d.add_break(COND_ADDR, cond="iram[0x40]==0x5a", quote=False)
    check("8 discriminate", "8c unquoted bracket -> no breakpoint",
          bu.get("created") is False and bu.get("ok") is False and
          bu.get("table") == [], "created false, table EMPTY",
          "created=%s rows=%s" % (bu.get("created"), bu.get("table")),
          "console: %s" % (bu.get("raw", "").split("\n")[0][:56],))

    bq = d.add_break(COND_ADDR, cond="iram[0x30]==0x00")
    check("8 discriminate", "8d false bracket cond still registers",
          bq.get("created") is True and
          bq.get("cond_registered") == "iram[0x30]==0x00",
          "created, cond echoed back",
          "created=%s registered=%r" % (bq.get("created"),
                                        bq.get("cond_registered")),
          "the bed registers bracket conditions when they are quoted")
    check("8 discriminate", "8d driver says WHY it cannot fire",
          bq.get("trusted") is False and bq.get("cond_holds_now") is False,
          "untrusted + cond_holds_now False",
          "trusted=%s cond_holds=%s warning=%r" % (bq.get("trusted"),
                                                   bq.get("cond_holds_now"),
                                                   (bq.get("warning") or "")[:60]),
          "iram[0x30] is 0x41 there: the condition is FALSE, not broken")
    d.add_break(SENTINEL)
    rq = d.cont(timeout=8)
    check("8 discriminate", "8d it really never fires",
          rq.get("code") == 104 and rq.get("pc") == "0x%06x" % SENTINEL,
          "104 at sentinel 0x%06x" % SENTINEL,
          "code=%s pc=%s" % (rq.get("code"), rq.get("pc")),
          "walked over 0x%06x with the condition false" % COND_ADDR)
    d.del_break()

    d.command("pc 0")
    bt = d.add_break(COND_ADDR, cond="iram[0x40]==0x5a")
    check("8 discriminate", "8d control: TRUE bracket cond is trusted",
          bt.get("created") is True and bt.get("cond_holds_now") is True,
          "cond_holds_now True at PC=0",
          "created=%s cond_holds=%s" % (bt.get("created"),
                                        bt.get("cond_holds_now")),
          "iram[0x40]==0x5a holds after the program has run once; see 8e")
    rt = d.cont(timeout=8)
    check("8 discriminate", "8d TRUE bracket cond FIRES",
          stopped_real(rt) and rt.get("pc") == "0x%06x" % COND_ADDR,
          "stops at 0x%06x" % COND_ADDR,
          "code=%s pc=%s" % (rt.get("code"), rt.get("pc")),
          "so the driver must NOT tell the caller brackets never fire")
    d.del_break()

    d.command("pc 0")
    var = d.var("flag", "iram", 0x40)
    check("8 discriminate", "8e var cell form registers",
          var.get("ok") and var.get("in_variables"), "listed in info variables",
          "in_variables=%s" % (var.get("in_variables"),))
    bad = d.command("var mem2 memory 0x40")
    listed = d.command("info variables")["raw"]
    check("8 discriminate", "8e documented var form refused",
          bad.get("ok") is False and bad.get("usage") is True and
          "mem2" not in listed, "not ok, usage printed, name absent",
          "ok=%s usage=%s raw=%r" % (bad.get("ok"), bad.get("usage"),
                                     bad.get("raw", "").split("\n")[0][:34]),
          "the bed prints its synopsis and registers NOTHING")
    bv = d.add_break(COND_ADDR, cond="flag==0x5a")
    rv = d.cont(timeout=10)
    check("8 discriminate", "8e var usable in a condition",
          bv.get("ok") and stopped_real(rv) and
          rv.get("pc") == "0x%06x" % COND_ADDR,
          "stops at 0x%06x" % COND_ADDR,
          "code=%s pc=%s cond_registered=%r" % (rv.get("code"), rv.get("pc"),
                                                bv.get("cond_registered")))
    d.close()
except Exception as e:                                      # noqa: BLE001
    check("8 discriminate", "exception", False, "no exception", repr(e))

# ------------------------------------------------------------- batch/interface
try:
    out = s51dbg.once('file "%s"; break %d; cont' % (ISA, BP_ADDR), SIM,
                      "89C51R", None, 25)
    stop = out.get("stop") or {}
    check("9 batch --once", "owned run through a `;` spec",
          out.get("mode") == "owned" and stop.get("code") == 104 and
          stop.get("pc") == "0x%06x" % BP_ADDR, "stop 104 @0x%06x" % BP_ADDR,
          "mode=%s code=%s pc=%s" % (out.get("mode"), stop.get("code"),
                                     stop.get("pc")),
          "the console cannot split `;` itself")
    out2 = s51dbg.once("expression dseg:0x30", SIM, "89C51R", None, 25)
    check("9 batch --once", "stdio batch flags the parser error",
          out2.get("mode") == "stdio" and out2.get("ok") is False,
          "mode stdio, ok false",
          "mode=%s ok=%s" % (out2.get("mode"), out2.get("ok")))
except Exception as e:                                      # noqa: BLE001
    check("9 batch --once", "exception", False, "no exception", repr(e))

try:
    d = fresh()
    st = d.state()
    check("10 state", "snapshot fields",
          st.get("pc") == "0x000000" and "acc" in st.get("registers", {}) and
          st.get("memory_spaces"), "pc + registers + spaces",
          "pc=%s regs=%d spaces=%d" % (st.get("pc"),
                                       len(st.get("registers", {})),
                                       len(st.get("memory_spaces") or [])))
    t = d.timer("measure")
    check("10 state", "timer counts cycles", (t.get("clks") or 0) > 0,
          "clks > 0", repr(t.get("clks")), "sec=%s" % (t.get("sec"),))
    d.quit()
except Exception as e:                                      # noqa: BLE001
    check("10 state", "exception", False, "no exception", repr(e))

# ------------------------------------------------- 11: the load is PROVEN
# Measured: a REFUSED load prints on the network console (`File name is
# missing.` for an unquoted path, `no loadable file found (...)` for a missing
# one) and leaves ROM at its 0xff power-on fill; a SUCCESSFUL load reports
# `Loading from <p>` + `N words read` only to the STDIO console and only without
# `-q` (leg 14) -- never to this driver's network console, pipe or not.  So ROM
# readback is the proof.
try:
    d = session()
    good = d.start(file=ISA, expect={0x0000: [0x02, 0x00, 0x30],
                                     0x0001: [0x00, 0x30, 0x32]})
    check("11 load proof", "11a expect matches ROM",
          good["ok"] and all(c["matches"] for c in good["expect_checks"]),
          "ok + every expect row matches",
          "ok=%s matches=%s" % (good["ok"],
                                [c["matches"] for c in good["expect_checks"]]),
          "isa.ihx opens with LJMP 0x0030: 02 00 30 32")
    same = d.start(file=ISA)
    check("11 load proof", "11b reload not a false failure",
          same["ok"] and same["rom_reloaded"] and not same["rom_changed"],
          "ok + rom_reloaded, changed False",
          "ok=%s reloaded=%s changed=%s" % (same["ok"], same["rom_reloaded"],
                                            same["rom_changed"]),
          "the same image cannot change ROM twice")
    wrong = d.expect_rom(0x0000, [0x00, 0x00, 0x00])
    check("11 load proof", "11c expect_rom says no to wrong bytes",
          not wrong["matches"], "matches False",
          "got=%s" % wrong["got"][:3], "the check is not hard-wired to pass")
    d.close()
    sessions.remove(d)
    d2 = session()
    miss = d2.start(file="/tmp/no-such-image-xyz.ihx")
    check("11 load proof", "11d missing file refused, text surfaced",
          miss["ok"] is False and
          "no loadable file found" in (miss.get("refusal") or ""),
          "ok False + refusal verbatim",
          "ok=%s refusal=%r" % (miss["ok"], (miss.get("refusal") or "")[:44]),
          "refusals DO print on the network console; only the success report "
          "needs a terminal (leg 14)")
    check("11 load proof", "11e refused load leaves the 0xff fill",
          (miss.get("rom_after") or [])[:3] == ["0xff", "0xff", "0xff"],
          "ROM still 0xff ff ff", "rom_after[:3]=%s"
          % ((miss.get("rom_after") or [])[:3],),
          "0xff is MOV R7,A, so a refused load still steps and looks alive")
    d2.close()
    sessions.remove(d2)
except Exception as e:                                      # noqa: BLE001
    check("11 load proof", "exception", False, "no exception", repr(e))

# ------------------------------------------------- 12: delete by id, proven
try:
    d3 = fresh()
    d3.add_break(0x3a)
    d3.add_break(0x44)
    ids = [r["id"] for r in d3.info_break()["rows"]]
    one = d3.del_break(0x3a)
    check("12 delete", "delete by address resolves the id",
          one["ok"] and one["deleted"] and
          all(r["addr"] != "0x00003a" for r in one["rows"]),
          "the 0x3a row gone",
          "ids=%s deleted=%s left=%s" % (ids, one["deleted"],
                                         [r["addr"] for r in one["rows"]]),
          "`delete` wants the id from `info break`, not an address")
    allgone = d3.del_break()
    check("12 delete", "bare delete empties the table",
          allgone["ok"] and not allgone["remaining"], "no rows left",
          "remaining=%s" % allgone["remaining"], "")
    dead = d3.command("del break")
    check("12 delete", "the documented `del break` prints Error",
          "Error" in dead["raw"] and not dead["ok"],
          "raw has Error, ok False", "raw=%r ok=%s" % (dead["raw"][:20],
                                                       dead["ok"]),
          "dead-end verb; del_break() never sends it")
    d3.close()
    sessions.remove(d3)
except Exception as e:                                      # noqa: BLE001
    check("12 delete", "exception", False, "no exception", repr(e))

# ------------------------------------------- 13: the file's own bytes diffed
# t/isa.ihx is bog-standard Intel HEX: 15 type-00 records + 1 EOF, every length
# and checksum valid, 233 bytes in one range 0..232.  So the load can be proven
# byte-for-byte without anyone knowing the image.  (A word count says a file
# parsed, not that bytes landed; see leg 14.)
try:
    d = fresh()
    img = s51dbg.S51Dbg.parse_intel_hex(ISA)
    ef = d.expect_file(ISA)
    check("13 file diff", "13a ROM equals the file",
          ef["ok"] and ef["checked"] == img["count"] == 233 and
          not ef["mismatch"], "233/233 bytes match",
          "checked=%s file=%s ranges=%s mismatch=%s"
          % (ef["checked"], img["count"], len(ef["ranges"]),
             len(ef["mismatch"])),
          "one contiguous range, 0..232")
    addr = 0x40
    poke = d.set_memory("rom", addr, (img["bytes"][addr] ^ 0xFF) & 0xFF)
    tam = d.expect_file(ISA)
    check("13 file diff", "13b tampered ROM is caught",
          tam["ok"] is False and bool(tam["mismatch"]), "ok False + mismatch",
          "ok=%s mismatch=%s" % (tam["ok"], tam["mismatch"][:1]),
          "the diff is not hard-wired to pass (%s)" % poke.get("cmd"))
    bogus = d.expect_file(os.path.join(HERE, "s51dbgtest.py"))
    check("13 file diff", "13c a non-HEX file is refused",
          bogus["ok"] is False and "cannot parse" in (bogus["error"] or ""),
          "ok False, cannot parse",
          "ok=%s err=%r" % (bogus["ok"], (bogus["error"] or "")[:44]),
          "an empty comparison is never a match")
    d.close()
    sessions.remove(d)
except Exception as e:                                      # noqa: BLE001
    check("13 file diff", "exception", False, "no exception", repr(e))

# --------------------- 14: the load report: WHICH console, and when it lands
# Decided by console selection and `-q', never by terminal-ness.  stdout is a
# PIPE in 14c/14d/14f/14g, a pty in 14a/14b.  Evidence here is the two report
# LINES and the word count: both lines interpolate the path, so the size of the
# capture says nothing and is never asserted.
LOAD_LINES = ["Loading from %s" % ISA, "233 words read from %s" % ISA]


def has_load_lines(res):
    return all(any(w in ln for ln in res["lines"]) for w in LOAD_LINES)


try:
    plain = s51dbg.S51Dbg.tty_load_report(sim=SIM, path=ISA)
    quiet = s51dbg.S51Dbg.tty_load_report(sim=SIM, path=ISA, extra=["-q"])
    check("14 load report", "14a pty stdin console reports the load",
          plain["report"] and plain["words"] == 233 and has_load_lines(plain),
          "both report lines, `233 words read`, on a pty",
          "words=%s lines=%s" % (plain["words"], plain["lines"]),
          "line-buffered, so it arrives live")
    check("14 load report", "14b -q silences it on a pty too",
          not quiet["report"] and quiet["words"] is None, "no report under -q",
          "report=%s words=%s" % (quiet["report"], quiet["words"]),
          "-q is the silencer; the pipe is not")
    pipe = s51dbg.S51Dbg.stdio_load_report(sim=SIM, path=ISA)
    check("14 load report", "14c PIPE stdin console reports it too",
          pipe["report"] and pipe["words"] == 233 and has_load_lines(pipe),
          "both report lines through a pipe",
          "words=%s lines=%s bytes_before_exit=%d"
          % (pipe["words"], pipe["lines"], pipe["bytes_before_exit"]),
          "block-buffered: %d bytes while the child lives, everything at exit "
          "-- read to EOF, never peek" % pipe["bytes_before_exit"])
    nofl = s51dbg.S51Dbg.stdio_load_report(sim=SIM, path=ISA, extra=["-q"])
    check("14 load report", "14d -q silences it through a pipe",
          not nofl["report"] and nofl["words"] is None and not nofl["lines"],
          "no report line under -q",
          "report=%s words=%s lines=%s" % (nofl["report"], nofl["words"],
                                           nofl["lines"]),
          "same file, same flags, one extra `-q'")
    noc = s51dbg.S51Dbg.stdio_load_report(sim=SIM, path=ISA, flags=())
    check("14 load report", "14f report survives dropping `-c -'",
          noc["report"] and noc["words"] == 233 and has_load_lines(noc),
          "both report lines without `-c -'",
          "words=%s lines=%s" % (noc["words"], noc["lines"]),
          "identical on both builds: this console read stdin either way, so "
          "`-c -' selects nothing -- do NOT read a short capture as a refusal, "
          "the lines carry the path and so set the byte count")
    rel = s51dbg.S51Dbg.stdio_load_report(sim=SIM,
                                         path=os.path.relpath(ISA))
    check("14 load report", "14g relative path spelling still loads",
          rel["report"] and rel["words"] == 233 and
          all(ISA in ln for ln in rel["lines"]),
          "`233 words read`, lines name the absolute path",
          "spelling=%s words=%s lines=%s" % (os.path.relpath(ISA), rel["words"],
                                            rel["lines"]),
          "the driver abspaths, so both spellings give the same WORDS and the "
          "same lines; only the capture size differs (a relative path measured "
          "54 bytes shorter on one binary, both loads successful)")
    d = session()
    st = d.start(file=ISA)                       # `file' on the NETWORK console
    aux_at_exit = d._aux_from(0)
    d.close()
    sessions.remove(d)
    check("14 load report", "14e network console never reports it",
          st["words_read"] is None and "Loading from" not in aux_at_exit,
          "no report line anywhere, even after exit",
          "words_read=%s aux_has_Loading=%s"
          % (st["words_read"], "Loading from" in aux_at_exit),
          "this is why ROM readback, not the report, is the driver's proof")
    rc = session().start(file=ISA, report_check=True)
    check("14 load report", "14h report_check corroborates without a pty",
          rc["ok"] and rc["report_check"]["words"] == 233
          and rc["words_read"] is None,
          "session stays silent, cross-check counts 233",
          "ok=%s report_check=%s words_read=%s"
          % (rc["ok"], rc["report_check"], rc["words_read"]),
          "the driver's own console cannot see the report; the throwaway stdio "
          "console confirms the same image is loadable")
except Exception as e:                                      # noqa: BLE001
    check("14 load report", "exception", False, "no exception", repr(e))

# ------------------------------------- 15: what `info break` columns are NOT
try:
    d = fresh()
    d.del_break()
    b = d.add_break(0x3a)
    row = [r for r in d.info_break()["rows"] if r["addr"] == "0x00003a"][0]
    check("15 table artifacts", "15a a fresh row is not a counter",
          row["hit"] == 1 and row["cnt"] == 1 and b["hits_listed"] == 1,
          "Hit 1 Cnt 1 before anything ran",
          "hit=%s cnt=%s hits_listed=%s" % (row["hit"], row["cnt"],
                                            b["hits_listed"]),
          "asserting Hit==0 at creation asserts against a display value")
    check("15 table artifacts", "15b Cond is live, not stored",
          row["cond"] is None and row["cond_live"] is True,
          "stored None, live True",
          "cond=%r cond_live=%r" % (row["cond"], row["cond_live"]),
          "an unconditional row prints `true`: it is evaluated per `info break`")
    d.del_break()
    c = d.add_break(0x3a, cond="iram[0x40]==0x5a")
    before = [r for r in d.info_break()["rows"] if r["addr"] == "0x00003a"][0]
    d.cont(timeout=10.0)
    after = [r for r in d.info_break()["rows"] if r["addr"] == "0x00003a"][0]
    check("15 table artifacts", "15c Cond tracks the live value",
          before["cond_live"] is False and after["cond_live"] is True and
          before["cond"] == after["cond"] == "iram[0x40]==0x5a",
          "False before the run, True after",
          "before=%r after=%r stored=%r" % (before["cond_live"],
                                            after["cond_live"], before["cond"]),
          "iram[0x40] is 0xff until the program writes 0x5a (trusted=%s)"
          % c["trusted"])
    d.close()
    sessions.remove(d)
except Exception as e:                                      # noqa: BLE001
    check("15 table artifacts", "exception", False, "no exception", repr(e))

# --------------------- 16: fetch-bp stepping, per the binary's OWN documented
# rule.  Family probe is `break 0x5 if 0` (same discriminator as t/wptrtest.py):
# a pre-fix cmd_bp build silently creates an unconditional fetch bp ("prefix")
# and carries the BLANKET first-fetch exemption; the fixed build refuses it
# ("postfix") and carries the ADDRESS-SPECIFIC exemption.  Both directions are
# asserted -- the leg never just skips the one it did not measure.
_FAM = {}


def family():
    if "f" not in _FAM:
        d = fresh()
        d.command("break 0x5 if 0")
        rows = [r for r in d.info_break()["rows"] if r["addr"] == "0x000005"]
        _FAM["f"] = "prefix" if rows else "postfix"
        d.close()
        sessions.remove(d)
    return _FAM["f"]


try:
    fam = family()
    d = fresh()
    d.add_break(0x3a)
    d.command("pc 0x37")
    codes, bp_stop = [], None
    for _ in range(12):
        s = d.step(1)
        if s.get("stopped"):
            codes.append(s.get("code"))
            if s.get("code") == 104:
                bp_stop = s.get("pc")
                break
    if fam == "postfix":
        ok = 104 in codes and bp_stop == "0x00003a"
        expect = "postfix: a step onto 0x3a fires (104)"
    else:
        ok = 104 not in codes
        expect = "prefix: blanket exemption, no (104)"
    check("16 stepping family", "16a 12 single steps across 0x3a", ok, expect,
          "%s codes=%s bp_stop=%s" % (fam, codes, bp_stop),
          "rule chosen by the `if 0' probe; asserting on the code, not `Stop at'")
    d.close()
    sessions.remove(d)
except Exception as e:                                      # noqa: BLE001
    check("16 stepping family", "exception", False, "no exception", repr(e))

# ----------------------------- 17: keyword/arity matrix, pinned per family
# The same five raw commands on both binaries:
#   bed     -> rows at 05 06 07 08 09, Cnt 1 everywhere.  `if 0', `if 1' and
#              even `bogus 3' all become unconditional breakpoints (their row
#              prints Cond `true' because the condition was thrown away) and
#              the `hit N' keyword is dropped on the floor.
#   patched -> rows at 07 09 only: `hit 2' honoured (Cnt 2) and the quoted
#              condition kept; `if 0', `if 1' and the junk keyword refused.
try:
    fam = family()
    d = fresh()
    for cmd in ("break 0x5 if 0", "break 0x6 if 1", "break 0x7 hit 2",
                "break 0x8 bogus 3", 'break 0x9 if "0"'):
        d.command(cmd)
    rows = {r["addr"]: r for r in d.info_break()["rows"]}
    want = ({"0x000005": True, "0x000006": True, "0x000007": True,
             "0x000008": True, "0x000009": True} if fam == "prefix" else
            {"0x000005": False, "0x000006": False, "0x000007": True,
             "0x000008": False, "0x000009": True})
    got = {a: (a in rows) for a in want}
    cnt = (rows.get("0x000007") or {}).get("cnt")
    qc = (rows.get("0x000009") or {}).get("cond")
    want_cnt = 2 if fam == "postfix" else 1
    check("17 keyword matrix", "17a accepted forms match the family",
          got == want and qc == "0",
          "%s" % ("all five accepted" if fam == "prefix"
                  else "only `hit 2' and the quoted condition"),
          "%s present=%s quoted_cond=%r"
          % (fam, sorted(a for a in got if got[a]), qc),
          "the bed discards `if 0' and prints Cond `true'; the fix refuses it")
    check("17 keyword matrix", "17b `hit N' honoured per family",
          cnt == want_cnt, "Cnt %d on the `%s' build" % (want_cnt, fam),
          "Cnt=%s for `break 0x7 hit 2'" % cnt,
          "the pre-fix build drops the keyword and leaves Cnt 1; the POSITIONAL "
          "form works on both, which is what leg 4 and the driver use")
    check("17 keyword matrix", "17c family probe is decisive",
          fam in ("prefix", "postfix"), "family in {prefix,postfix}",
          "family=%s (probe: `break 0x5 if 0')" % fam,
          "same discriminator as t/wptrtest.py WP-K0")
    d.close()
    sessions.remove(d)
except Exception as e:                                      # noqa: BLE001
    check("17 keyword matrix", "exception", False, "no exception", repr(e))

# -------------------------------------------------------------------- summary
# a leg that raised never reached its close(); shut everything down before
# deciding whether the gate leaked a simulator
for s in sessions:
    try:
        s.close()
    except Exception:                                       # noqa: BLE001
        pass
print("%-20s %-36s %-30s %-5s %s" % ("leg", "check", "expected", "v", "measured"))
bad = 0
for leg, sub, expect, got, ok, note in results:
    bad += 0 if ok else 1
    print("%-20s %-36s %-30s %-5s %s" % (leg, sub, expect,
                                         "ok" if ok else "BAD",
                                         ("%s | %s" % (got, note)).strip()))
print("binary: %s" % SIM)
alive = [p for p in pids if os.path.exists("/proc/%d" % p)]
print("child pids: %d started, %d still alive%s" %
      (len(pids), len(alive), (" " + ",".join(map(str, alive))) if alive
       else ""))
if alive:
    bad += 1
    print("orphan simulators left behind: %s" % alive)
try:
    os.unlink(VCD)
except OSError:
    pass
print("%s: %d/%d checks" % ("FAIL" if bad else "PASS",
                            len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
