#!/usr/bin/env python3
"""Phase 1: -t 89C51RD2 / -t 89C51ED2 must be real devices, not aliases of nothing.

The RD2 got its own cpu flag (CPU_89C51RD2 = 0x8000) so that RD2-only features can be
gated on it instead of leaking onto the Atmel 89C51RB2/RC2 (CPU_89C51R).  A fresh flag
in that block fails silently in three ways, and every leg below exists to catch one of
them:

  * if the flag is not ORed into CPU_ALL_52 (stypes.h), the ~72 registrations gated on
    that composite never run for the part, which boots with NO SFR names at all -- no
    error, just bare addresses in `dump sfr` and in the step trace.  The <P0> leg is the
    pure form of this: P0 is named only by a CPU_ALL_51|CPU_ALL_52|CPU_251 row.
  * if the name rows of uc51.cc are not widened to the new flag, AUXR, WDTRST, CCON and
    CKCON0 stay unnamed, so this also checks the names, not only the values.
  * if rd2_mem() (the only flag test in uc89c51r.cc) is not widened, the part loses its
    expanded-RAM window, X2, peripheral_cycles and RD2 reset defaults -- AUXR would read
    0 instead of 0x08 and MOVX would not land on-chip.

The timing legs are the t/x2test.py differencing pairs and the t/xramtest.py boundary
probes re-run under the new names: they vary only the chip name, so a pass says the new
device behaves like the 89C51R it replaced rather than merely starting up.

Nothing here edits the existing suites; they keep running under -t 89C51R.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex
# S51_SIM=<path> (the older S51=<path> still works) runs the same legs against another
# build, e.g. the pre-change /tmp/s51-ref, where every leg must fail because the device
# name does not exist yet.
SIM = (os.environ.get("S51_SIM") or os.environ.get("S51") or
       os.path.expanduser("~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51"))

CHIPS = ["89C51RD2", "89C51ED2"]

AUXR, CKCON0, P0, WDTRST, CCON, CMOD, CCON_CR = 0x8E, 0x8F, 0x80, 0xA6, 0xD8, 0xD9, 0x40
SJMP = bytes([0x80, 0xFE])
SIZES = {0: 256, 1: 512, 2: 768, 3: 1024, 4: 1792}   # AUXR.XRS2:XRS0 -> bytes

results = []


def case(name, expect, got, ok):
    results.append((name, expect, got, ok))


def sim(cmds, chip):
    p = subprocess.run([SIM, "-t", chip, "-c", "-"],
                       input="\n".join(cmds + ["exit"]) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=300)
    return p.stdout + p.stderr


def cell(out, addr):
    """(name or None, value) of a `dump sfr` row; the value is the SECOND 0x token."""
    for ln in out.splitlines():
        if not re.match(r"^0x%02x\s" % addr, ln):
            continue
        named = re.match(r"^0x%02x\s+([A-Za-z_][A-Za-z0-9_./]*):" % addr, ln)
        hexes = re.findall(r"0x[0-9a-f]+", ln)
        return (named.group(1) if named else None,
                int(hexes[1], 16) if len(hexes) > 1 else None)
    return (None, None)


def pre(pairs):
    b = bytearray()
    for a, v in pairs:
        b += bytes([0x75, a, v])
    return b


def run_clocks(code, chip):
    """Clocks from reset to the end of the image, the t/x2test.py measurement."""
    code = bytes(code)
    img = "/tmp/rd2id_clk_%d_%d.hex" % (len(code), sum(code) & 0xFFFF)
    write_hex(img, code)
    cmds = ['file "%s"' % img, "break 0x%x" % (len(code) - 2), "go", "state", "exit"]
    p = subprocess.run([SIM, "-t", chip, "-c", "-"], input="\n".join(cmds) + "\n",
                       capture_output=True, text=True, cwd="/tmp", timeout=300)
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)",
                  p.stdout + p.stderr)
    return int(m.group(1)) if m else None


def wait(bit):
    return bytes([0x30, bit, 0xFD])


# ---------------------------------------------------------------- image for the legs
# MOV A,#v ; MOV <sfr>,A  four times, so the step trace names AUXR, WDTRST, CCON and P0.
NAME_CODE = (bytes([0x74, 0x0C, 0xF5, AUXR, 0x74, 0xA6, 0xF5, WDTRST,
                    0x74, 0x40, 0xF5, CCON, 0x74, 0x55, 0xF5, P0]) + SJMP)
write_hex("/tmp/rd2id_name.hex", NAME_CODE)

for chip in CHIPS:
    # --- the name resolves and builds a controller -------------------------------
    out = sim(['file "/tmp/rd2id_name.hex"', "state"], chip)
    case("%s: device name resolves" % chip, "no error",
         "rejected" if "Unknown processor type" in out else "accepted",
         "Unknown processor type" not in out)
    neg = sim(["state"], "89C51RD3")
    case("%s: control, a near-miss name is rejected" % chip, "Unknown processor type",
         "yes" if "Unknown processor type" in neg else "no",
         "Unknown processor type" in neg)

    # --- the RD2 identity: its reset defaults come from rd2_mem() ----------------
    out = sim(['dump sfr[0x8e] 0x8f'], chip)
    n_auxr, v_auxr = cell(out, AUXR)
    n_ckcon, v_ckcon = cell(out, CKCON0)
    case("%s: AUXR resets to 0x08 (XRS=010)" % chip, "0x08", hex(v_auxr) if v_auxr is not None else "??", v_auxr == 0x08)
    case("%s: AUXR is named (CPU_ALL_52 + name row)" % chip, "AUXR", n_auxr, n_auxr == "AUXR")
    case("%s: CKCON0 resets to 0x00" % chip, "0x00", hex(v_ckcon) if v_ckcon is not None else "??", v_ckcon == 0)
    case("%s: CKCON0 is named" % chip, "CKCON0", n_ckcon, n_ckcon == "CKCON0")

    # --- names in the step trace, the thing that silently disappears -------------
    traced = ""
    for n in (1, 3, 5, 7):
        traced += sim(['file "/tmp/rd2id_name.hex"', "step %d" % n], chip)
    for want, label in (("<AUXR>", "AUXR"), ("<WDTRST>", "WDTRST"),
                        ("<CCON>", "CCON"), ("<P0>", "P0")):
        case("%s: trace names %s" % (chip, label), want,
             "found" if want in traced else "missing", want in traced)

    # --- every standard 51 SFR still carries a name ------------------------------
    dump = sim(["dump sfr 0x80 0xff"], chip)
    named = set(re.findall(r"^0x[0-9a-f]{2}\s+(\S+):", dump, re.M))
    need = ["P0", "SP", "PCON", "TCON", "TMOD", "TL0", "TL1", "TH0", "TH1", "P1",
            "SCON", "SBUF", "P2", "IE", "P3", "IP", "PSW", "ACC", "B", "AUXR",
            "CKCON0", "AUXR1", "WDTRST", "SADDR", "SADEN", "IPH", "CKCON1",
            "T2CON", "RCAP2L", "RCAP2H", "TL2", "TH2", "T2MOD", "CCON", "CMOD",
            "CCAPM0", "CCAPM4", "CL", "CH", "CCAP0L", "CCAP4L", "CCAP0H", "CCAP4H"]
    missing = [n for n in need if n not in named]
    case("%s: all %d expected SFR names present" % (chip, len(need)), "none missing",
         ",".join(missing) if missing else "none missing", not missing)

    # --- XRS window: the t/xramtest.py boundary probes ---------------------------
    def seq(auxr, addr, op, val=None):
        b = bytes([0x75, AUXR, auxr, 0x90, addr >> 8, addr & 0xff])
        if op == "w":
            return b + bytes([0x74, val, 0xF0]), 4
        return b + bytes([0xE0]), 3

    def build(xrs, addr):
        on, off = xrs << 2, (xrs << 2) | 0x02
        code, steps = bytearray(), 0
        for auxr, val in ((on, 0x22), (off, 0x11)):
            s, n = seq(auxr, addr, "w", val)
            code += s
            steps += n
        for auxr, dst in ((on, 0x40), (off, 0x41)):
            s, n = seq(auxr, addr, "r")
            code += s + bytes([0xF5, dst])
            steps += n + 1
        return bytes(code + bytes([0x80, 0xFE])), steps + 1

    def xram(xrs, addr):
        img = "/tmp/rd2id_xrs_%d_%x.hex" % (xrs, addr)
        code, steps = build(xrs, addr)
        write_hex(img, code)
        cmds = ['file "%s"' % img] + ["step"] * steps + ["dump iram 0x40 0x41"]
        rows = [l.split()[1:3] for l in sim(cmds, chip).splitlines()
                if re.match(r"^0x40\s", l)]
        return tuple(rows[0]) if rows else None

    bad_sizes = []
    for xrs, size in sorted(SIZES.items()):
        for addr, where in ((size - 1, "on-chip"), (size, "off-chip")):
            got = xram(xrs, addr)
            want = ("22", "11") if where == "on-chip" else ("11", "11")
            if got != want:
                bad_sizes.append("XRS%d/0x%x=%s" % (xrs, addr, got))
    case("%s: XRS window boundaries (10 probes)" % chip, "all 10 correct",
         ";".join(bad_sizes) if bad_sizes else "all 10 correct", not bad_sizes)
    # the leg the brief calls out by name: XRS=3 -> 1024 bytes, 0..0x3ff on-chip
    case("%s: XRS=3 keeps 0x3ff on-chip, 0x400 off-chip" % chip, "('22', '11')",
         "%s" % (xram(3, 0x3ff),), xram(3, 0x3ff) == ("22", "11"))

    # --- X2 / peripheral_cycles: the t/x2test.py differencing pairs --------------
    def t2_period(ckcon, rcap_lo):
        return run_clocks(pre([(CKCON0, ckcon), (0xCB, 0xFF), (0xCA, rcap_lo),
                               (0xCD, 0xFF), (0xCC, rcap_lo), (0xC8, 0x04)]) +
                          wait(0xCF) + SJMP, chip)

    def span(a, b):
        return None if a is None or b is None else a - b

    def t2_span(ckcon):
        return span(t2_period(ckcon, 0x00), t2_period(ckcon, 0x80))

    def t0_ticks(ckcon):
        return run_clocks(pre([(CKCON0, ckcon), (0x89, 0x01), (0x8C, 0x00),
                               (0x8A, 0x00)]) + bytes([0xD2, 0x8C]) +
                          wait(0x8D) + SJMP, chip)

    def pca_count(ckcon, cps, steps):
        code = pre([(CKCON0, ckcon), (CMOD, cps), (CCON, CCON_CR)]) + SJMP
        img = "/tmp/rd2id_pca_%02x_%02x_%d.hex" % (ckcon, cps, steps)
        write_hex(img, code)
        out = sim(['file "%s"' % img, "step %d" % steps,
                   "dump sfr[0xe9]", "dump sfr[0xf9]"], chip)
        v = {}
        for ln in out.splitlines():
            m = re.match(r"^0x(e9|f9)\s+\S+:\s+\S+\s+(0x[0-9a-f]+)", ln)
            if m:
                v[m.group(1)] = int(m.group(2), 16)
        if not v:
            return None                       # no CL/CH row at all: not a counter value
        return 256 * v.get("f9", 0) + v.get("e9", 0)

    def pca_span(ckcon, cps, n=500):
        return span(pca_count(ckcon, cps, 2 * n), pca_count(ckcon, cps, n))

    COUNTS, MC = 128, 1000

    def near(expect, got):
        return got is not None and abs(expect - got) <= max(1.0, abs(expect) * 0.02)

    for name, expect, got in (
            ("T2 standard mode", COUNTS * 12, t2_span(0x00)),
            ("T2 X2 mode", COUNTS * 6, t2_span(0x01)),
            ("T0 standard mode", 65536 * 12, t0_ticks(0x00)),
            ("T0 X2 mode", 65536 * 6, t0_ticks(0x01)),
            ("PCA standard mode", MC, pca_span(0x00, 0x00)),
            ("PCA X2, PCAX2 set", MC // 2, pca_span(0x21, 0x00))):
        case("%s: %s" % (chip, name), expect, got, near(expect, got))

print("%-46s %18s %18s %6s" % ("case", "expected", "measured", "verdict"))
bad = 0
for name, expect, got, ok in results:
    bad += 0 if ok else 1
    print("%-46s %18s %18s %6s" % (name, expect, got, "PASS" if ok else "FAIL"))
print("\n%s: %d/%d" % ("PASS" if not bad else "FAIL", len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
