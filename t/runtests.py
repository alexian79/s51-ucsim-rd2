#!/usr/bin/env python3
"""Run the ucsim s51 testsuite against its stored baselines.

Replicates src/test-lib.mk's run-sim recipe (the fork's make plumbing wants a
srcdir layout that isn't present in this checkout), including the ELIDE rules
that mask version/timing lines.
"""
import os
import re
import subprocess
import sys

ROOT = os.environ.get("S51") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "vendor/ucsim-stc/ucsim/src")
SIM = os.environ.get("S51_SIM") or os.path.join(SRC, "sims/s51.src/ucsim_51")
TESTDIR = os.path.join(SRC, "sims/s51.src/test")

import shlex
testlib = os.path.join(SRC, "test-lib.mk")
if not os.path.exists(testlib):
    sys.exit("no test-lib.mk at %s (set $S51 to the repo root or $S51_SIM to the "
             "simulator under test)" % testlib)
elide = []
for ln in open(testlib):
    m = re.match(r"\s*ELIDE\s*\+=\s(.*)$", ln)
    if m:
        elide += shlex.split(m.group(1))
if not elide:
    sys.exit("no ELIDE rules parsed from test-lib.mk")
sed = ["sed", "-E"] + elide
cpu_override = sys.argv[1] if len(sys.argv) > 1 else None

fails = 0
total = 0
for d in sorted(os.listdir(TESTDIR)):
    tdir = os.path.join(TESTDIR, d, "baseline")
    if not os.path.isdir(tdir):
        continue
    for tgt in sorted(os.listdir(tdir)):
        cmdfile = os.path.join(TESTDIR, d, "test-%s.cmd" % tgt)
        if d == "config":
            # upstream builds this one inline: dump config, registers, memory map
            eargs = ["-e", "conf; info reg; info mem"]
            aux = []
        elif os.path.exists(cmdfile):
            eargs = ["-e", 'exec "%s"' % cmdfile]
            aux = [os.path.join(TESTDIR, d, f)
                   for f in os.listdir(os.path.join(TESTDIR, d)) if f.endswith(".ihx")]
        else:
            print("%-8s %-8s SKIP (no test-%s.cmd)" % (d, tgt, tgt))
            continue
        if cpu_override:
            tgt = cpu_override
        r = subprocess.run([SIM, "-q", "-R", "0", "-t", tgt] + eargs + aux,
                           capture_output=True, text=True, cwd="/tmp",
                           stdin=subprocess.DEVNULL, timeout=120)
        p = subprocess.run(sed, input=r.stdout + r.stderr, capture_output=True,
                           text=True)
        # the version banner differs between ucsim releases; it is not behaviour
        strip = lambda s: "".join(
            l + "\n" for l in s.splitlines()
            if not re.match(r"^(uCsim|This is free software|under certain conditions)", l))
        want = strip(open(os.path.join(tdir, "51" if cpu_override else tgt)).read())
        total += 1
        got_s = strip(p.stdout)
        if got_s == want:
            print("%-8s %-8s PASS" % (d, tgt))
        else:
            got, exp = got_s.splitlines(), want.splitlines()
            nd = sum(1 for a, b in zip(got, exp) if a != b) + abs(len(got) - len(exp))
            print("%-8s %-8s FAIL (%d differing lines)" % (d, tgt, nd))
            for a, b in list(zip(got, exp)):
                if a != b:
                    print("    got: %r\n    exp: %r" % (a[:90], b[:90]))
                    break
            fails += 1
print("\n%d/%d baselines match" % (total - fails, total))
sys.exit(1 if fails else 0)
