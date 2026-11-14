#!/usr/bin/env python3
"""Before/after evidence for the mainline ucsim timer-2 `cycles' patch.

Run it as:

    ./timer2_cycles_demo.py --before /tmp/ucsim_51-prefix \
                            --after  /tmp/ucsim_51-fixed

Nothing here needs a baseline: every case prints TL2/TH2 read straight out of
the simulator, and the same script runs against any two binaries.

Cases
  1  counter mode, no input ............... TL2 must not move at all
  2  counter mode, N pending edges ........ TL2 must equal the edge count
  3  capture mode, N pending edges ........ same contract, other function
  4  timer mode, instruction-mix control .. TL2 must be IDENTICAL in both
                                            binaries (this is the control)
"""
import argparse
import os
import re
import subprocess
import sys

TL2, TH2, T2CON, P1 = 0xcc, 0xcd, 0xc8, 0x90
# T2CON bit images
COUNTER_RELOAD = 0x06        # C_T2 | TR2            : count events on T2
COUNTER_CAPTURE = 0x07       # C_T2 | CP_RL2 | TR2   : capture, source = T2
TIMER_RELOAD = 0x04          # TR2 only              : count machine cycles


# ---------------------------------------------------------------- images
def ihx(data, start=0x0000):
    def rec(rectype, addr, payload):
        line = bytes([len(payload), (addr >> 8) & 0xff, addr & 0xff, rectype]) + payload
        return ":" + (line + bytes([(-sum(line)) & 0xff])).hex().upper()
    out = [rec(0x04, 0, b"\x00\x00")]
    for off in range(0, len(data), 16):
        out.append(rec(0x00, start + off, data[off:off + 16]))
    out.append(rec(0x01, 0, b""))
    return "\n".join(out) + "\n"


SETUP = bytes([0x75, TL2, 0x00, 0x75, TH2, 0x00,      # MOV TL2,#0 / MOV TH2,#0
               0x75, 0xca, 0x00, 0x75, 0xcb, 0x00,    # RCAP2L/H = 0
               0x75, T2CON, TIMER_RELOAD])            # start timer 2, timer mode
TAIL = bytes([0x80, 0xfe])                            # SJMP $
MIX = {"mov": (bytes([0x75, 0x30, 0x00]), 2),         # MOV 0x30,#0 : 2 hw ticks
       "mul": (bytes([0xa4]), 4),                     # MUL AB      : 4 hw ticks
       "nop": (bytes([0x00]), 1)}                     # NOP         : 1 hw tick


def mix_image(kind, body_cycles=200):
    """Body burns `body_cycles` hw ticks; instruction count follows the mix."""
    op, ticks = MIX[kind]
    n = body_cycles // ticks
    return SETUP + op * n + TAIL, 5 + n, 10 + body_cycles


# ---------------------------------------------------------------- runner
def run(sim, device, cmds, image=None):
    argv = [sim, "-q", "-t", device] + ([image] if image else [])
    res = subprocess.run(argv, input="\n".join(cmds) + "\n",
                         capture_output=True, text=True, timeout=120)
    return res.stdout + res.stderr


COUNTER = re.compile(r"0x%02x TL2:\s+\S+\s+(0x[0-9a-f]+)" % TL2, re.I)
TH2VAL = re.compile(r"0x%02x TH2:\s+\S+\s+(0x[0-9a-f]+)" % TH2, re.I)
CLKS = re.compile(r"Total time since last reset=\s+\S+ sec \((\d+) clks\)")
STEPPED = re.compile(r"stepped (\d+) ticks")


def counter_of(out):
    # the piped console echoes commands between dumps, so trust the last dump
    tl, th = COUNTER.findall(out), TH2VAL.findall(out)
    if not tl or not th:
        return None
    return int(th[-1], 16) * 256 + int(tl[-1], 16)


def set_up(mode):
    return ["set memory sfr 0x%02x 0x%02x" % (T2CON, mode),
            "set memory sfr 0x%02x 0x00" % TL2,
            "set memory sfr 0x%02x 0x00" % TH2]


def toggle_edges(n):
    """n falling edges on P1.0 (T2), all queued before the next tick()."""
    cmds = ["set memory sfr 0x%02x 0xff" % P1]
    for _ in range(n):
        cmds += ["set memory sfr 0x%02x 0xfe" % P1,
                 "set memory sfr 0x%02x 0xff" % P1]
    return cmds


def pending_edge_check(name, edge_rows):
    """Unfixed must count 1 whatever is pending; fixed must scale with it."""
    out = []
    unit = {n: b for n, _, b in edge_rows}.get(1)
    for n, unfixed, fixed in edge_rows:
        if unfixed is None or fixed is None:
            out.append("%s n=%d: no TL2 in transcript" % (name, n))
        elif unfixed > 1:
            out.append("%s n=%d: unfixed counted %d, expected 1" % (name, n, unfixed))
        elif unit and fixed != unit * n:
            out.append("%s n=%d: fixed count %d != %d x single-edge count %d"
                       % (name, n, fixed, n, unit))
    return out


# ---------------------------------------------------------------- matrix
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--device", default="C52")
    ap.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)) + "/transcripts")
    ap.add_argument("--edges", default="1,2,3,4,6")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    bins = {"unfixed": args.before, "fixed": args.after}
    rows, fails = [], []

    def emit(name, cmds, image=None):
        got = {}
        for label, sim in bins.items():
            out = run(sim, args.device, cmds, image)
            path = os.path.join(args.outdir, "%s.%s.txt" % (name, label))
            with open(path, "w") as fh:
                fh.write("$ %s -q -t %s%s\n" % (sim, args.device, " " + image if image else ""))
                fh.write("\n".join(cmds) + "\n\n" + out)
            got[label] = out
        return got

    # case 1: counter mode, zero input pulses
    print("== case 1: counter mode (T2CON=0x%02x), NO input pulses, tick 5" % COUNTER_RELOAD)
    got = emit("case1-counter-idle", set_up(COUNTER_RELOAD) + ["tick 5",
               "dump sfr 0x%02x 0x%02x" % (TL2, TH2), "quit"])
    a, b = counter_of(got["unfixed"]), counter_of(got["fixed"])
    print("   TL2 before=%s after=%s  (want 0)" % (a, b))
    rows.append(("1 idle, tick 5", "-", a, b))
    if b != 0:
        fails.append("case 1: fixed binary still counts with no input")

    # case 2: counter mode, N pending edges consumed by one tick
    print("== case 2: counter mode, N falling edges queued, single tick")
    edge_rows = []
    for n in [int(x) for x in args.edges.split(",")]:
        got = emit("case2-counter-edges-%d" % n,
                   set_up(COUNTER_RELOAD) + toggle_edges(n) +
                   ["tick 1", "dump sfr 0x%02x 0x%02x" % (TL2, TH2), "quit"])
        a, b = counter_of(got["unfixed"]), counter_of(got["fixed"])
        print("   edges=%-2d TL2 before=%-4s after=%-4s" % (n, a, b))
        rows.append(("2 edges", n, a, b))
        edge_rows.append((n, a, b))
    fails += pending_edge_check("case 2", edge_rows)

    # case 3: capture mode, N pending edges
    print("== case 3: capture mode (T2CON=0x%02x), N falling edges queued, single tick" % COUNTER_CAPTURE)
    cap_rows = []
    for n in [int(x) for x in args.edges.split(",")]:
        got = emit("case3-capture-edges-%d" % n,
                   set_up(COUNTER_CAPTURE) + toggle_edges(n) +
                   ["tick 1", "dump sfr 0x%02x 0x%02x" % (TL2, TH2), "quit"])
        a, b = counter_of(got["unfixed"]), counter_of(got["fixed"])
        print("   edges=%-2d TL2 before=%-4s after=%-4s" % (n, a, b))
        rows.append(("3 capture", n, a, b))
        cap_rows.append((n, a, b))
    fails += pending_edge_check("case 3", cap_rows)

    # case 4: control -- timer mode, same machine cycles, different mix
    print("== case 4 CONTROL: timer mode (T2CON=0x%02x), same machine cycles, "
          "different instruction mix" % TIMER_RELOAD)
    for kind in ("mov", "mul", "nop"):
        code, steps, cycles = mix_image(kind)
        img = os.path.join(args.outdir, "mix-%s.hex" % kind)
        with open(img, "w") as fh:
            fh.write(ihx(code))
        got = emit("case4-mix-%s" % kind,
                   ["step %d" % steps, "dump sfr 0x%02x 0x%02x" % (TL2, TH2),
                    "state", "quit"], image=img)
        a, b = counter_of(got["unfixed"]), counter_of(got["fixed"])
        clk = [CLKS.search(got[k]).group(1) for k in ("unfixed", "fixed")]
        stp = [STEPPED.search(got[k]).group(1) for k in ("unfixed", "fixed")]
        print("   %-4s instr=%-4d clks=%s/%s TL2 before=%s after=%s"
              % (kind, steps, clk[0], clk[1], a, b))
        rows.append(("4 control %s (instr %d)" % (kind, steps), clk[0], a, b))
        if a != b:
            fails.append("control: mix=%s moved (%s -> %s); the patch must be a "
                         "no-op in timer mode" % (kind, a, b))
        if len(set(clk)) != 1:
            fails.append("control: clock accounting differs for %s" % kind)

    print("\n%-28s %-8s %-10s %-10s" % ("case", "input", "unfixed TL2", "fixed TL2"))
    for r in rows:
        print("%-28s %-8s %-10s %-10s" % r)
    print("\ntranscripts: %s" % args.outdir)
    for f in fails:
        print("FAIL: %s" % f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
