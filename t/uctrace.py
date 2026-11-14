#!/usr/bin/env python3
"""Run an image on ucsim's 89C51R model, stepping one instruction at a time.

Prints (pc, machine_cycles, acc, psw) sampled before each retired instruction so
it lines up with xval's output on the i8051 core.
"""
import os
import re
import subprocess
import sys

ROOT = os.environ.get("S51") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.environ.get("S51_SIM") or os.path.join(
    ROOT, "vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")


def trace(image, n, cpu="89C51R"):
    cmds = ['file "%s"' % os.path.abspath(image)]
    for _ in range(n):
        cmds += ["state", "info registers", "step"]
    p = subprocess.run([SIM, "-t", cpu, "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=300)
    out = p.stdout + p.stderr
    pcs = [int(x, 16) for x in re.findall(r"CPU state= \S+ PC= 0x([0-9a-f]+)", out)]
    clks = [int(x) for x in re.findall(r"Total time since last reset= \S+ sec \((\d+) clks\)",
                                       out)]
    fl = re.findall(r"ACC= (0x[0-9a-f]{2}).*?PSW= (0x[0-9a-f]{2})", out, re.S)
    ac = [(int(a, 16), int(b, 16)) for a, b in fl]
    if len(ac) == 2 * n:
        ac = ac[0::2]
    if not (len(pcs) == len(clks) == len(ac) == n):
        sys.exit("parse mismatch pc=%d clk=%d acc=%d want %d" % (len(pcs), len(clks),
                                                                 len(ac), n))
    return [(p, c // 12, a, w) for p, c, (a, w) in zip(pcs, clks, ac)]


if __name__ == "__main__":
    img = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    for pc, mc, a, psw in trace(img, n):
        print("T %04x %8d %02x %02x" % (pc, mc, a, psw))
