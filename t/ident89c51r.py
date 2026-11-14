#!/usr/bin/env python3
"""-t 89C51R must come out byte-identical: same transcripts, two binaries.

Only the "Host usage" / "Runtime" lines (wall clock) are normalised away.
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

NEW = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("S51_SIM")) or \
  os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
OLD = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("S51_REF")) or "/tmp/s51-ref-b"

HERE = os.path.dirname(os.path.abspath(__file__))

# The four images this suite used to inherit as side effects of other suites are
# generated here instead, byte for byte as those generators emit them, into a
# directory this suite creates: rd2registers.py:153,157 wrote the first pair,
# rd2identity.py:98 the third, and nothing in the repo ever produced bitsprobe.hex,
# so its code bytes are reproduced verbatim from the fixture it left behind.
GEN_DIR = os.path.join(tempfile.gettempdir(), "ident89c51r-images-%d" % os.getuid())
SJMP = bytes([0x80, 0xFE])
RD2REG_NAME_CODE = (bytes([0x74, 0x55, 0xF5, 0xC3, 0x74, 0xAA, 0xF5, 0xC7,
                           0x74, 0x0F, 0xF5, 0xB1, 0x74, 0x3C, 0xF5, 0x97]) + SJMP)
RD2REG_BITBAND_CODE = (bytes([0x75, 0xB0, 0x00, 0x75, 0xB1, 0xFF]) +
                       bytes([0x90, 0x00, 0x00, 0xE4, 0xF0]))
RD2ID_NAME_CODE = (bytes([0x74, 0x0C, 0xF5, 0x8E, 0x74, 0xA6, 0xF5, 0xA6,
                          0x74, 0x40, 0xF5, 0xD8, 0x74, 0x55, 0xF5, 0x80]) + SJMP)
BITSPROBE_CODE = bytes.fromhex("75C8FF75C0F075E80F75A8FF900000E4F0")

GENERATED = [("rd2reg_name.hex", RD2REG_NAME_CODE),
             ("rd2reg_bitband.hex", RD2REG_BITBAND_CODE),
             ("rd2id_name.hex", RD2ID_NAME_CODE),
             ("bitsprobe.hex", BITSPROBE_CODE)]

try:
    os.makedirs(GEN_DIR, exist_ok=True)
except OSError as exc:
    print("FAIL: cannot create image directory %s (%s)" % (GEN_DIR, exc))
    sys.exit(1)
IMAGES = []
for name, code in GENERATED:
    path = os.path.join(GEN_DIR, name)
    try:
        write_hex(path, code)
    except OSError as exc:
        print("FAIL: cannot write image %s (%s)" % (path, exc))
        sys.exit(1)
    IMAGES.append(path)
IMAGES += [os.path.join(HERE, n) for n in
           ("tC.hex", "tD.hex", "tE.hex", "up128.hex", "dptr2.hex")]

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


def require_readable(path):
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        print("FAIL: missing or unreadable image %s (%s)" % (path, exc))
        sys.exit(1)


for img in IMAGES:
    require_readable(img)

TOTAL = len(IMAGES) * len(LEGS)
bad = 0
n = 0
for img in IMAGES:
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
      % ("PASS" if not bad else "FAIL", TOTAL - bad, TOTAL, NEW, OLD))
sys.exit(1 if bad else 0)
