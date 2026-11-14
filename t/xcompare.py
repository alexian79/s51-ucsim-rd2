#!/usr/bin/env python3
"""Cross-validate ucsim's 89C51R core against the i8051 core in mcs51-rs.

Both run the identical linked image. For every retired instruction the two are
compared on (pc, cycles-before, acc-before). ucsim counts clock cycles; i8051
counts machine cycles, so ucsim's value is divided by 12 before comparison.
"""
import os
import re
import subprocess
import sys

SIM = os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
XVAL = os.path.expanduser("~/s51/xval/target/release/xval")
T = os.path.expanduser("~/s51/t")

asm = sys.argv[1] if len(sys.argv) > 1 else "isa.asm"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 104
ihx = "/tmp/xval.ihx"

r = subprocess.run([XVAL, os.path.join(T, asm), "5000", ihx], capture_output=True,
                   text=True, cwd=T)
if r.returncode:
    sys.exit("xval failed: " + r.stderr[:400])
rs = [(int(m[0], 16), int(m[1]), int(m[2], 16), int(m[3], 16))
      for m in re.findall(r"^T ([0-9a-f]{4})\s+(\d+) ([0-9a-f]{2}) ([0-9a-f]{2})$",
                          r.stdout, re.M)]
snap = re.search(r"^S pc=\S+ a=\S+ b=\S+ dptr=\S+ sp=\S+ psw=\S+ cycles=(\d+) inst=(\d+)",
                 r.stdout, re.M)
print("i8051: %d traced, %s cycles, %s instructions" % (len(rs), snap.group(1), snap.group(2)))

cmds = ['file "%s"' % ihx]
for _ in range(n):
    cmds += ["state", "info registers", "step"]
p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"], input="\n".join(cmds) + "\n",
                   capture_output=True, text=True, cwd=T, stdin=None, timeout=600)
out = p.stdout + p.stderr
pcs = [int(x, 16) for x in re.findall(r"CPU state= \S+ PC= 0x([0-9a-f]+)", out)]
clks = [int(x) for x in
        re.findall(r"Total time since last reset= \S+ sec \((\d+) clks\)", out)]
accs = [(int(x, 16), int(y, 16)) for x, y in
        re.findall(r"ACC= (0x[0-9a-f]{2}).*?PSW= (0x[0-9a-f]{2})", out, re.S)]
if len(accs) == 2 * n:
    # even indices come from the explicit `info registers` (pre-step); odd ones
    # are the register block step() echoes after executing
    accs = accs[0::2]
if not (len(pcs) == len(clks) == len(accs) == n):
    sys.exit("ucsim parse mismatch: pc=%d clk=%d acc=%d want %d" % (len(pcs), len(clks),
                                                                    len(accs), n))
uc = list(zip(pcs, clks, accs))

bad = 0
for i in range(min(n, len(rs))):
    upc, uclk, (uacc, upsw) = uc[i]
    rpc, rmc, racc, rpsw = rs[i]
    if (upc, uclk // 12, uacc, upsw) != (rpc, rmc, racc, rpsw):
        bad += 1
        if bad <= 14:
            print("  step %3d @%04x  ucsim mc=%d a=%02x psw=%02x | i8051 mc=%d a=%02x psw=%02x"
                  % (i, upc, uclk // 12, uacc, upsw, rmc, racc, rpsw))
print("\n%s: %d/%d instructions agree on (pc, machine_cycles, acc, psw)"
      % ("MATCH" if bad == 0 else "DIVERGENCE", min(n, len(rs)) - bad, min(n, len(rs))))
sys.exit(1 if bad else 0)
