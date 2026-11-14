#!/usr/bin/env python3
"""AT89C51RD2 peripheral support matrix: timer and UART behaviour vs datasheet.

f_xtal is ucsim's default 11.0592 MHz, so 1 machine cycle (MC) = 12 clocks.
Absolute elapsed times are reported with the polling overhead measured out: each
timing case is run twice, differing only in how many machine cycles elapse, and
the difference gives the exact rate.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkhex import write_hex

SIM = os.environ.get("S51_SIM") or os.path.expanduser(
  "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
CLK = 12                      # clocks per MC in STD (non-X2) mode
SJMP = bytes([0x80, 0xFE])    # every image ends here


def run(code, outfile=None):
    code = bytes(code)
    img = "/tmp/hw_%d_%d.hex" % (len(code), sum(code) & 0xFFFF)
    write_hex(img, code)
    stop = len(code) - 2
    cmds = ['file "%s"' % img, "break 0x%x" % stop, "go", "state", "exit"]
    args = [SIM, "-t", "89C51R", "-c", "-"]
    if outfile:
        args += ["-S", "out=%s" % outfile]
    p = subprocess.run(args, input="\n".join(cmds) + "\n", capture_output=True,
                       text=True, cwd="/tmp", timeout=180)
    m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", p.stdout + p.stderr)
    return int(m.group(1)) if m else None


def pre(pairs):
    b = bytearray()
    for a, v in pairs:
        b += bytes([0x75, a, v])
    return b


def wait(bit):
    return bytes([0x30, bit, 0xFD])


def mc(clks):
    return clks / CLK


results = []


def case(name, expect, got, unit, note=""):
    if isinstance(expect, (int, float)) and isinstance(got, (int, float)):
        ok = abs(expect - got) <= max(1.0, abs(expect) * 0.02)
    else:
        ok = expect == got
    results.append((name, expect, got, ok, note))


# --- rate: how many machine cycles does one timer-2 count take? --------------
# same program, two reload values -> difference isolates the timer cost exactly
def t2_period(rcap_lo):
    c = pre([(0xCB, 0xFF), (0xCA, rcap_lo), (0xCD, 0xFF), (0xCC, rcap_lo),
             (0xC8, 0x04)]) + wait(0xCF) + SJMP
    return run(c)


d = t2_period(0x00) - t2_period(0x80)          # 256 vs 128 counts -> 128 counts apart
case("T2 clocks per machine cycle", 12 * 128, d, "clks", "128-count span, 1 count/MC")

# --- timer 0 / timer 1 overflow ---------------------------------------------
c = pre([(0x89, 0x01), (0x8C, 0x00), (0x8A, 0x00)]) + bytes([0xD2, 0x8C]) + wait(0x8D) + SJMP
clk = run(c)
case("T0 mode1 overflow", 65536, round(mc(clk)), "MC", "16-bit from 0x0000")

def t1_span(th1):
    return run(pre([(0x89, 0x20), (0x8D, th1), (0x8B, th1)]) +
               bytes([0xD2, 0x8E]) + wait(0x8F) + SJMP)
d = t1_span(0x00) - t1_span(0x80)          # 256 vs 128 counts -> 128 counts
case("T1 mode2 counts per MC", 12 * 128, d, "clks", "128-count span")

# --- T0 interrupt ------------------------------------------------------------
c = bytearray(0x40)
c[0:3] = bytes([0x02, 0x00, 0x20])
c[0x0B:0x0E] = bytes([0xD2, 0x20, 0x32])            # ISR: SETB 0x20 ; RETI
c[0x20:] = pre([(0x89, 0x01), (0x8C, 0xFF), (0x8A, 0xFF)]) + \
    bytes([0xD2, 0xA9, 0xD2, 0xAF, 0xD2, 0x8C]) + SJMP
c = bytes(c[:0x20]) + c[0x20:]
clk, _ = (run(c), None) if False else (run(c), 0)
img = "/tmp/hw_irq.hex"
write_hex(img, c)
p = subprocess.run([SIM, "-t", "89C51R", "-c", "-"],
                   input='file "%s"\nbreak 0x0b\ngo\nstate\nexit\n' % img,
                   capture_output=True, text=True, cwd="/tmp", timeout=120)
m = re.search(r"Total time since last reset= \S+ sec \((\d+) clks\)", p.stdout + p.stderr)
hit = "0x00000b" in (p.stdout + p.stderr)
case("T0 interrupt vectors to 0x0B", True, hit, "", "ISR entered")

# --- UART, timer 1 as baud source -------------------------------------------
def uart_t1(smod=0, nbytes=1):
    c = pre([(0x89, 0x20), (0x8D, 0xFD), (0x8B, 0xFD)])
    if smod:
        c += bytes([0x43, 0x87, 0x80])
    c += bytes([0xD2, 0x8E, 0x75, 0x98, 0x50])
    for i in range(nbytes):
        c += bytes([0x74, 0x30 + i, 0xF5, 0x99]) + wait(0x99)
        if i < nbytes - 1:
            c += bytes([0xC2, 0x99])
    return c + SJMP


one, two = run(uart_t1(nbytes=1)), run(uart_t1(nbytes=2))
bit_mc = mc(two - one) / 10
case("UART T1-baud bit time", 96, round(bit_mc), "MC", "9600 baud @ TH1=0FDh")

one, two = run(uart_t1(smod=1, nbytes=1)), run(uart_t1(smod=1, nbytes=2))
bit_mc = mc(two - one) / 10
case("UART T1-baud SMOD=1 bit time", 48, round(bit_mc), "MC", "19200 baud")

try:
    os.remove("/tmp/hw_tx.bin")
except OSError:
    pass
run(uart_t1(nbytes=1), "/tmp/hw_tx.bin")
got = open("/tmp/hw_tx.bin", "rb").read().hex() if os.path.exists("/tmp/hw_tx.bin") else ""
case("UART T1 TX bytes", "30", got, "", "single byte 0x30")


def uart_t2(nbytes=1):
    c = pre([(0xCB, 0xFF), (0xCA, 0xFD), (0xCD, 0xFF), (0xCC, 0xFD), (0xC8, 0x34)])
    c += bytes([0x75, 0x98, 0x50])
    for i in range(nbytes):
        c += bytes([0x74, 0x30 + i, 0xF5, 0x99]) + wait(0x99)
        if i < nbytes - 1:
            c += bytes([0xC2, 0x99])
    return c + SJMP


one, two = run(uart_t2(nbytes=1)), run(uart_t2(nbytes=2))
bit_mc = mc(two - one) / 10
case("UART T2-baud bit time", 96, round(bit_mc), "MC",
     "RCAP2=0FFFD -> 3 MC overflow, datasheet wants 32/bit")

print("%-32s %10s %10s %6s  %s" % ("case", "expected", "measured", "unit", "verdict"))
for name, e, g, ok, note in results:
    unit = note
    print("%-32s %10s %10s        %s%s" % (name, e, g, "OK" if ok else "MISMATCH",
                                           ("   " + unit) if unit else ""))
bad = sum(1 for r in results if not r[3])
print("\n%d/%d cases match the datasheet" % (len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
