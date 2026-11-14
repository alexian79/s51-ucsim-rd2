#!/usr/bin/env python3
"""-t 89C51R must come out byte-identical: same transcripts, two binaries.

Only the "Host usage" / "Runtime" lines (wall clock) are normalised away.
"""
import os
import re
import subprocess
import sys

NEW = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("S51_SIM")) or \
  os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
OLD = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("S51_REF")) or "/tmp/s51-ref-b"

IMAGES = ["/tmp/rd2reg_name.hex", "/tmp/rd2reg_bitband.hex", "/tmp/rd2id_name.hex",
          "/tmp/bitsprobe.hex", os.path.expanduser("~/s51/t/tC.hex"),
          os.path.expanduser("~/s51/t/tD.hex"), os.path.expanduser("~/s51/t/tE.hex"),
          os.path.expanduser("~/s51/t/up128.hex"), os.path.expanduser("~/s51/t/dptr2.hex")]

LEGS = [["file \"{i}\"", "step 8", "dump sfr 0x80 0xff", "state"],
        ["file \"{i}\"", "step 1", "step 3", "step 5", "dump sfr 0x90 0x9f",
         "dump bits 0x80 0x8f", "dump bits 0xc0 0xcf"],
        # `info hw <category>` prints only the usage line for a category the part does
        # not have, so these two are what catches a `kbd` or `spi` element leaking onto
        # the AT89C51RB2/RC2 from cl_uc89c51r::mk_hw_elements(): a leak makes this binary
        # print a block where the reference prints the usage line.  There is no
        # list-all form of the command, so a plain transcript diff cannot catch it.
        ["file \"{i}\"", "step 20", "info hw wdt", "info hw pca",
         "info hw kbd", "info hw spi", "dump sfr 0xa0 0xbf"],
        # `run` on these images is an endless SJMP $ loop, so the fourth leg steps.
        ["file \"{i}\"", "step 40", "dump sfr 0xc0 0xdf", "dump mem 0 0x1f", "state"]
]

# Wall-clock lines only: "Host usage:", "Runtime:" and the wrapped value line that
# ucsim prints under them (" 0.000015 sec, rate=2.8").  Whole lines, not prefixes.
NOISE = re.compile(r"^(Host usage:.*|Runtime:.*|\s+[0-9.]+ sec(?:, rate=.*)?)$", re.M)


def norm(text):
    return NOISE.sub("", text)


bad = 0
n = 0
for img in IMAGES:
    if not os.path.exists(img):
        print("skip missing image", img)
        continue
    for j, leg in enumerate(LEGS):
        cmds = [c.format(i=img) for c in leg] + ["quit"]
        outs = []
        for sim in (NEW, OLD):
            p = subprocess.run([sim, "-t", "89C51R", "-c", "-"],
                               input="\n".join(cmds) + "\n",
                               capture_output=True, text=True, timeout=120)
            outs.append(norm(p.stdout + p.stderr))
        n += 1
        if outs[0] != outs[1]:
            bad += 1
            print("DIFF %s leg %d" % (img, j))
            a, b = outs[0].splitlines(), outs[1].splitlines()
            for k in range(max(len(a), len(b))):
                la = a[k] if k < len(a) else "<eof>"
                lb = b[k] if k < len(b) else "<eof>"
                if la != lb:
                    print("   new: %r\n   old: %r" % (la, lb))
                    break
print("%s: %d/%d transcripts byte-identical (%s vs %s)"
      % ("PASS" if not bad else "FAIL", n - bad, n, NEW, OLD))
sys.exit(1 if bad else 0)
