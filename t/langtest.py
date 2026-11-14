#!/usr/bin/env python3
"""Acceptance test for the AT89C51RD2 Ghidra language.

Checks on gh/blkmove.hex, gh/auxr.hex and t/isa.ihx; the summary denominator is counted as the
checks run, never written down:

1. dual DPTR resolved. The doc4235 sec 8 block move sets DPTR1=0x1000 and DPTR0=0x2000 and
   increments both every iteration. The pcode must therefore write DPTR1 and DPTR0 separately and
   increment each one. Under the stock language all four collapse onto a single DPTR.
2. no decode regression. The patched sinc must disassemble the whole ISA test image exactly as the
   stock language does, mnemonic for mnemonic.
3. the stock language still reports the gap, so the test cannot pass vacuously if the new language
   silently fails to load.

Run after any edit to ~/s51/lang/AT89C51RD2; langbuild is invoked here so a stale .sla cannot
masquerade as a pass.
"""
import os
import re
import subprocess
import sys

S51 = os.environ.get("S51") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GHIDRA = os.path.expanduser(os.environ.get("GHIDRA", "~/ghidra_12.1.4_PUBLIC"))
HEADLESS = os.path.join(GHIDRA, "support", "analyzeHeadless")
RD2 = "AT89C51RD2:BE:16:default"
STOCK = "8051:BE:16:default"
TMP = "/tmp/langtest"


def headless(proj, img, lang, script):
    """Import img under lang with no analysis and run script, returning its println output."""
    root = os.path.join(TMP, proj)
    subprocess.run(["rm", "-rf", root], check=True)
    os.makedirs(root)
    p = subprocess.run(
        [HEADLESS, root, "p", "-import", img, "-processor", lang, "-noanalysis",
         "-scriptPath", os.path.join(S51, "gh"), "-postScript", script],
        capture_output=True, text=True, timeout=900)
    out = p.stdout + p.stderr
    mark = "%s> " % script          # script already ends in .java; do not append it again
    return re.sub(r" \(GhidraScript\)\s*$", "",
                  "\n".join(l.split(mark, 1)[1] for l in out.splitlines() if mark in l))


def main():
    subprocess.run([os.path.join(S51, "langbuild")], capture_output=True, text=True,
                   timeout=600, check=True)
    os.makedirs(TMP, exist_ok=True)
    blk = os.path.join(S51, "gh", "blkmove.hex")
    isa = os.path.join(S51, "t", "isa.ihx")
    bad = 0
    done = 0

    pc = headless("pc", blk, RD2, "Pcode51.java")
    want = [
        (r"COPY\(1000\)->DPTR1", "DPTR1 = 0x1000 (source, DPS=1)"),
        (r"COPY\(2000\)->DPTR0", "DPTR0 = 0x2000 (dest, DPS=0)"),
        (r"INT_ADD\(DPTR1[^\n]*->DPTR1", "INC DPTR increments DPTR1"),
        (r"INT_ADD\(DPTR0[^\n]*->DPTR0", "INC DPTR increments DPTR0"),
    ]
    print("dual DPTR pcode, %s:" % RD2)
    for pat, label in want:
        ok = re.search(pat, pc) is not None
        done += 1
        bad += 0 if ok else 1
        print("  %s %-45s %s" % ("PASS" if ok else "FAIL", label, pat))

    stock = headless("st", blk, STOCK, "Pcode51.java")
    collapsed = len(re.findall(r"->DPTR:", stock)) >= 2 and "DPTR1" not in stock
    done += 1
    bad += 0 if collapsed else 1
    print("  %s %-45s %d DPTR writes, DPTR1 present=%s"
          % ("PASS" if collapsed else "FAIL", "stock language still collapses both pointers",
             len(re.findall(r"->DPTR:", stock)), "DPTR1" in stock))

    a = headless("ia", isa, STOCK, "Dump51.java")
    b = headless("ib", isa, RD2, "Dump51.java")

    def mnems(t):
        return [l.split("|")[0].strip() for l in t.splitlines()
                if re.match(r"^(CODE|RAM):", l)]

    ma, mb = mnems(a), mnems(b)
    same = ma == mb and len(ma) > 0
    done += 1
    bad += 0 if same else 1
    print("ISA decode: %s %d instructions, identical=%s" %
          ("PASS" if same else "FAIL", len(mb), ma == mb))
    if ma != mb:
        for x, y in zip(ma, mb):
            if x != y:
                print("    first difference:\n      stock: %s\n      rd2:   %s" % (x, y))
                break

    # external data memory must be mapped, or every MOVX lands in nothing at all
    bx = headless("xb", os.path.join(S51, "gh", "auxr.hex"), RD2, "Blocks51.java")
    blocks = dict(re.findall(r"PROBE EXTMEM:([0-9a-f]+) block=(\S+)", bx))
    expect_blocks = {"0100": "XRAM", "06ff": "XRAM", "0700": "XDATA", "1234": "XDATA"}
    ok = blocks == expect_blocks
    done += 1
    bad += 0 if ok else 1
    print("XRAM map:   %s boundary at 0x700, got %s" %
          ("PASS" if ok else "FAIL", blocks or "nothing mapped"))

    # the window decode in Dptr51 must agree with doc4235 Table 9-1 / the simulator's xram_size[]
    ax = headless("xd", os.path.join(S51, "gh", "auxr.hex"), RD2, "Dptr51.java")
    sizes = dict((m[0], int(m[1])) for m in
                 re.findall(r"MOV 0x8e,#0x([0-9a-f]+)\s+\[[^]]*?(\d+) bytes\)", ax))
    want_sizes = {"0": 256, "8": 768, "c": 1024, "10": 1792}
    ok = all(sizes.get(k) == v for k, v in want_sizes.items())
    done += 1
    bad += 0 if ok else 1
    print("XRAM window:%s XRS -> %s" % ("PASS" if ok else "FAIL", sizes))
    extram = "EXTRAM=1" in ax and "dynamic window" in ax
    done += 1
    bad += 0 if extram else 1
    print("  %s %-45s" % ("PASS" if extram else "FAIL",
                          "EXTRAM=1 and non-constant AUXR both reported"))

    # RMW forms on AUXR1: five resolvable, only MOV AUXR1,A genuinely dynamic
    m = re.search(r"HAZARD AUXR1.*sites=(\d+) modelled=(\d+) unmodelled=(\d+)", ax)
    ok = m is not None and (m.group(1), m.group(2), m.group(3)) == ("6", "5", "1")
    done += 1
    bad += 0 if ok else 1
    print("RMW bank:   %s %s" % ("PASS" if ok else "FAIL",
                                 m.group(0).split("@")[0].strip() if m else "no AUXR1 report"))

    # doc4235 SFR map, resolved through Ghidra's own symbol table (Symbols51 dumps every symbol)
    sx = headless("sy", blk, RD2, "Symbols51.java")
    syms = {}
    primaries = {}
    for addr, name, primary in re.findall(r"^SYM (\S+) (\S+) primary=(\w+)", sx, re.M):
        syms.setdefault(addr, []).append(name)
        if primary == "true":
            primaries.setdefault(addr, []).append(name)

    def at(a):
        return sorted(syms.get(a, []))

    def prim(a):
        return sorted(primaries.get(a, []))

    # D5: Table 3-8 puts the SPI registers at C3/C4/C5, so C1/C2 must be bare
    want_spi = {"SFR:c3": "SPCON", "SFR:c4": "SPSTA", "SFR:c5": "SPDAT"}
    ok = all(want in at(a) for a, want in want_spi.items())
    done += 1
    bad += 0 if ok else 1
    print("SPI map:    %s Table 3-8 -> %s"
          % ("PASS" if ok else "FAIL", {a: at(a) for a in want_spi}))

    bare = {a: at(a) for a in ("SFR:c1", "SFR:c2", "SFR:df")}
    ok = all(v == [] for v in bare.values()) and "PCON1" not in set(sum(syms.values(), []))
    done += 1
    bad += 0 if ok else 1
    print("  %s %-45s %s" % ("PASS" if ok else "FAIL",
                             "C1/C2 unmapped, no symbol at DF (PCON1 gone)", bare))

    # row D0 of Table 3-11: PSW@D0, FCON@D1, EECON@D2 (Table 3-10), DFh unused
    row = {a: at("SFR:" + a) for a in ("d0", "d1", "d2")}
    ok = row == {"d0": ["PSW"], "d1": ["FCON"], "d2": ["EECON"]}
    done += 1
    bad += 0 if ok else 1
    print("D0 row:     %s %s" % ("PASS" if ok else "FAIL", row))

    # Table 3-3 says IEN0/IEN1, p4 says IE0/IE1, toolchain headers say IE/IE1; all resolve at one
    ie = {"SFR:a8": ["IE", "IEN0", "IE0"], "SFR:b1": ["IEN1", "IE1"]}
    ok = all(all(n in at(a) for n in ns) for a, ns in ie.items())
    done += 1
    bad += 0 if ok else 1
    print("IE aliases: %s %s" % ("PASS" if ok else "FAIL", {a: at(a) for a in ie}))

    # the alias must not steal the TCON flag names (Table 3-5: IE1/IT1/IE0/IT0 at bits 8B/8A/89/88)
    ok = "IE0" in at("BITS:89") and "IE1" in at("BITS:8b")
    done += 1
    bad += 0 if ok else 1
    print("  %s %-45s %s %s" % ("PASS" if ok else "FAIL",
                                "TCON bit names IE0/IE1 still resolve", at("BITS:89"), at("BITS:8b")))

    # Table 3-4/3-11: C0h is P4 and E8h is P5 (bit addressable); no FIFLG/HIFLG on this chip
    ports = {"SFR:c0": "P4", "BITS:c0": "P4.0", "BITS:c7": "P4.7",
             "BITS:e8": "P5.0", "BITS:ef": "P5.7"}
    phantom = sorted(n for n in set(sum(syms.values(), [])) if "IFLG" in n)
    ok = all(want in at(a) for a, want in ports.items()) and not phantom
    done += 1
    bad += 0 if ok else 1
    print("P4/P5 bits: %s %s phantom=%s"
          % ("PASS" if ok else "FAIL", {a: at(a) for a in ports}, phantom or "none"))

    # Table 3-4 declares P5 twice, at E8h (bit addressable) and C7h (byte addressable), so
    # MOV direct,E8 must name the register instead of printing a bare E8. Each address keeps its
    # own primary symbol even though the two share a name.
    p5 = {"SFR:e8": ["P5"], "SFR:c7": ["P5"]}
    ok = all(at(a) == n for a, n in p5.items()) and all(prim(a) == n for a, n in p5.items())
    done += 1
    bad += 0 if ok else 1
    print("P5 byte:    %s E8/C7 both named: %s primary=%s"
          % ("PASS" if ok else "FAIL", {a: at(a) for a in p5}, {a: prim(a) for a in p5}))

    # B8 carries the header name IP (primary, as every 8052 toolchain spells it) plus the
    # datasheet's IPL0 (Table 3-3, p4) as a non-primary alias
    ok = at("SFR:b8") == ["IP", "IPL0"] and prim("SFR:b8") == ["IP"]
    done += 1
    bad += 0 if ok else 1
    print("IPL0 alias: %s SFR:b8 all=%s primary=%s" % ("PASS" if ok else "FAIL",
                                                       at("SFR:b8"), prim("SFR:b8")))

    total = done              # counted at run time; a hardcoded denominator overstated coverage
    print("\n%s: %d/%d checks" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
