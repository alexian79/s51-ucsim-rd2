#!/usr/bin/env python3
"""
AT89C51RD2 enhanced UART RECEIVE: batch-mode RX (SIMULATION-PLAN step 13), SM2 automatic
address recognition and the SMOD0/FE framing-error mux (step 12, doc4235 14.1/14.2).

Ground truth is Atmel doc4235 (files /tmp/ds/pNNN.txt = pdftotext of printed page NNN+1,
and table cells spill onto the NEXT page file):

  p.48 (p047.txt) 14.1: with PCON.SMOD0=1 "the receiver checks each incoming data frame for
      a valid stop bit. ... If a valid stop bit is not found, the Framing Error bit (FE) in
      SCON register is set. ... Once set, only software or a reset can clear FE bit.
      Subsequently received frames with valid stop bits cannot clear FE bit."
  p.49 (p048.txt) 14.2: "Only when the serial port recognizes its own address, the receiver
      sets RI bit ... This ensures that the CPU is not interrupted by command frames
      addressed to other devices."  Mode 1: "the stop bit takes the place of the ninth data
      bit.  Bit RI is set only when the received command frame address matches the device's
      address and is terminated by a valid stop bit."  Note: "setting SM2 bit in SCON
      register in mode 0 has no effect".
  p.49 given address = SADDR under the SADEN don't-care mask (zeros = don't-care); the
      broadcast address is SADDR|SADEN (p.49-50, /tmp/ds/p048.txt+p049.txt).
  p.53 (p052.txt) table 14-4: SCON.7 is SM0 when SMOD0=0 and FE when SMOD0=1 ("SMOD0 must
      be cleared to enable access to the SM0 bit"); RB8 = ninth bit received (mode 1: stop).

Model contract implemented in sims/s51.src/serial.cc and asserted here:
  - A rejected SM2 frame does not touch SBUF, RB8 or RI (the datasheet promises no
    interrupt and never says the payload is kept).
  - Everything is gated on the RD2 (serial discovers itself by the presence of the BRG
    hardware element), so -t 89C51R and the classic parts keep the byte-per-frame path
    with no 9th/stop/filter/FE behaviour at all.

HOW batch RX works here, pinned by leg 1 rather than trusted (this is what step 13 was
about; rx9 below extends it):
  `-S in=<file>,out=<file>` attaches the file as the UART input.  During `go` the
  commander hands bytes to the receiver, tick() starts a frame on REN && !s_receiving and
  sets SBUF+RI at frame end.  The 51 model reads the raw `input` slot, so with rx9=0
  EVERY stream byte is one frame.
  The io layer eats 0x00, so `uart0_rx9` framing bytes below are nonzero: rx9=1 makes the
  async modes consume [data, control] pairs, control bit0 = 9th bit, bit1 = stop bit
  (0 = framing error).  ctrl values used: 0x03 good, 0x02 ninth=0, 0x01 stop=0.
  There is no `set var` in this build; the console switch is `set memory uart_0_cfg 0x8 1`
  (slot 8 = uart0_rx9; `info var` names it).  uart0_check_often is slot 0x1.

Traps this file defends against (each one bit earlier):
  - `file` MUST be quoted or the loader runs an all-zero image (NOPs) and still reports
    clocks; bad HEX checksums are likewise not rejected -- _hex_bytes asserts both.
  - `go` without a reachable breakpoint wedges the console to the subprocess timeout.
  - the io console and the UART share fin when the UART io IS the console; with
    -S in=<file> the two channels are separate and post-`go` console commands still parse
    (leg "console_channel_separate" pins exactly that).
  - a 0x00 control byte is dropped by cl_f::process() (telnet lineend logic) -- an entire
    frame pair vanishes and the leg looks like a dead filter, not a dead model.
  - `dump data[0x30]` is not a command here; `dump iram 0x30 0x3f` is.
  - mode 2 fixed-rate RX is 12x fast (D11, deliberately untouched); every leg below clocks
    from timer 1 in mode 2 (TMOD=0x20, TH1=0xFD) or the mode-0 fixed rate, never mode 2.

Run:  python3 t/uartmp.py                       (vendor tree binary)
      S51_SIM=/path/to/ucsim_51 python3 t/uartmp.py
Negative control: S51_REF=/tmp/s51-ref-b (pre-feature binary) must show SBUF=0x11 where
the filter exists and SCON.7=0 where FE exists -- checked by the ref legs when present.
"""

import os
import re
import subprocess
import sys

SIM = os.environ.get("S51_SIM", os.path.expanduser(
    "~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51"))
REF = os.environ.get("S51_REF", "/tmp/s51-ref-b")
WORKDIR = "/tmp/uartmp"

PCON, SCON, SBUF = 0x87, 0x98, 0x99
RI_BIT, TI_BIT = 0x98, 0x99
SADDR, SADEN = 0xa9, 0xb9
TMOD, TCON, TH1, TL1 = 0x89, 0x88, 0x8d, 0x8b
SJMP = bytes([0x80, 0xFE])
JNB_RI = bytes([0x30, RI_BIT, 0xFD])          # JNB RI, $
CLR_RI = bytes([0xC2, RI_BIT])                # CLR RI
MOV_A_SBUF = bytes([0xE5, SBUF])
MOV_A_SCON = bytes([0xE5, SCON])
ANL_SCON_7F = bytes([0x53, SCON, 0x7F])       # ANL SCON,#0x7F (clear SCON.7)
MODE1, MODE3 = 0x40, 0xC0
REN, SM2 = 0x10, 0x20

_checks = 0
_failures = []


def check(name, ok, detail=""):
    global _checks
    _checks += 1
    print("%-58s %s%s" % (name, "PASS" if ok else "FAIL",
                          "  %s" % detail if detail else ""))
    if not ok:
        _failures.append(name)
    return ok


def mov(a, v):
    return bytes([0x75, a, v])


def or_imm(addr, v):
    return bytes([0x43, addr, v])


def sta(d):
    """MOV direct,A."""
    return bytes([0xF5, d])


# Timer 1 mode 2, TH1=TL1=0xFD: the classic 9600-ish overflow source, standard clock
# (CKCON0 left 0 by reset).  euarttest's warning applies: without TMOD the timer sits in
# 13-bit mode 0 and everything crawls ~1000x slower -- still "working", never ending.
T1 = mov(TMOD, 0x20) + mov(TH1, 0xFD) + mov(TL1, 0xFD) + or_imm(TCON, 0x40)


def _hex_bytes(code, base=0):
    lines = []
    for off in range(0, len(code), 16):
        chunk = code[off:off + 16]
        b = bytes([len(chunk), ((base + off) >> 8) & 0xff, (base + off) & 0xff, 0]) + chunk
        lines.append(":%02X%04X%02X%s%02X" %
                     (len(chunk), (base + off) & 0xffff, 0, chunk.hex().upper(),
                      (-sum(b)) & 0xff))
    lines.append(":00000001FF")
    text = "\n".join(lines) + "\n"
    for ln in text.splitlines():
        assert sum(bytes.fromhex(ln[1:])) & 0xFF == 0, "bad checksum in %r" % ln
    return text.encode()


def rx_run(tag, code, inbytes, rx9=False, extra=(), sim=None, timeout=60):
    """Run `code` (which must end in SJMP) with `inbytes` on the UART input, break at the
    final SJMP, then dump 0x30..0x3f.  Returns (dict of data values, transcript)."""
    os.makedirs(WORKDIR, exist_ok=True)
    img = os.path.join(WORKDIR, "%s_%d_%x.hex" % (tag, len(code), sum(code) & 0xFFFF))
    with open(img, "wb") as f:
        f.write(_hex_bytes(code))
    inp = os.path.join(WORKDIR, "%s_in.bin" % tag)
    with open(inp, "wb") as f:
        f.write(bytes(inbytes))
    out = os.path.join(WORKDIR, "%s_out.bin" % tag)
    cmds = (['file "%s"' % img] + list(extra) +
            (["set memory uart_0_cfg 0x8 1"] if rx9 else []) +
            ["break 0x%x" % (len(code) - 2), "go",
             "dump iram 0x30 0x3f", "exit"])
    args = [sim or SIM, "-t", "89C51RD2", "-c", "-", "-S", "in=%s,out=%s" % (inp, out)]
    try:
        p = subprocess.run(args, input="\n".join(cmds) + "\n", capture_output=True,
                           text=True, cwd=WORKDIR, timeout=timeout)
        transcript = p.stdout
    except subprocess.TimeoutExpired as e:
        transcript = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return {}, transcript
    vals = {}
    for ln in transcript.splitlines():
        if ln.startswith("0x30"):
            i = 0
            for t in ln.split()[1:]:
                if re.fullmatch(r"[0-9a-f][0-9a-f]", t) and i < 16:
                    vals[0x30 + i] = int(t, 16)
                    i += 1
                else:
                    break
            break
    return vals, transcript


# --------------------------------------------------------------------------- programs
def prog_save1(scon_pre=(), scon=MODE1 | REN, with_scon=True):
    """sentinel 0x30=0xAA, wait RI, save SBUF->0x30 and SCON->0x31, park."""
    return (b"".join(scon_pre) + T1 + mov(0x30, 0xAA) + mov(SCON, scon) +
            JNB_RI + MOV_A_SBUF + sta(0x30) +
            (MOV_A_SCON + sta(0x31) if with_scon else b"") + SJMP)


def prog_two_frames(scon=MODE1 | REN, smod0=False, clear_scon7=False):
    """two RI frames saved to (0x30,0x31) and (0x32,0x33); optional ANL SCON,#0x7F
    between them (software FE clear)."""
    code = bytearray(T1)
    if smod0:
        code += or_imm(PCON, 0x40)
    code += mov(SCON, scon)
    code += mov(0x30, 0xAA) + JNB_RI + MOV_A_SBUF + sta(0x30) + MOV_A_SCON + sta(0x31)
    if clear_scon7:
        code += ANL_SCON_7F + MOV_A_SCON + sta(0x34)
    code += CLR_RI
    code += mov(0x32, 0xAA) + JNB_RI + MOV_A_SBUF + sta(0x32) + MOV_A_SCON + sta(0x33)
    return bytes(code) + SJMP


def main():
    # 1 -- step 13: receive works from batch mode, byte-per-frame, no rx9 (L1).
    vals, tr = rx_run("legacy_rx", prog_save1(), b"K")
    check("batch_rx_byte_per_frame: -S in= byte reaches SBUF+data",
          vals.get(0x30) == 0x4B and vals.get(0x31) == 0x55,
          "0x30=%s expect 0x4b; SCON 0x31=%s expect 0x55 (RI set, RB8=stop=1)"
          % (hex(vals.get(0x30, -1)), hex(vals.get(0x31, -1))))
    check("console_channel_separate: post-go console commands still parse with -S in=",
          "Stop at" in tr and "0x30" in tr,
          "go stopped at the break and the post-go `dump iram` line still printed: file "
          "and console are different channels once -S in= owns fin")

    # 2 -- the documented console switches on the cfg chip (no `set var` exists).
    vals, tr = rx_run("check_often", prog_save1(), b"K",
                      extra=["dump uart_0_cfg"])
    co = [ln for ln in tr.splitlines() if "uart0_check_often" in ln]
    check("cfg_switch: `set memory uart_0_cfg 0x1 1`/dump shows the slot by name",
          bool(co) and "uart0_rx9" in tr,
          "dump uart_0_cfg names slot 1 and slot 8 (%s)" % (co[0].strip() if co else "line missing"))
    vals2, _ = rx_run("check_often_rx", prog_save1(), b"K",
                      extra=["set memory uart_0_cfg 0x1 1"])
    check("check_often=1 keeps RX byte-per-frame (no double consumption)",
          vals2.get(0x30) == 0x4B, "0x30=%s" % hex(vals2.get(0x30, -1)))

    # 3 -- SM2 filter, legacy byte stream: only the frame matching SADDR/SADEN sets RI.
    p = prog_save1(scon_pre=(mov(SADDR, 0x55), mov(SADEN, 0xFF)), scon=MODE3 | SM2 | REN)
    vals, _ = rx_run("sm2_legacy", p, [0x11, 0x55])
    check("sm2_legacy_stream: foreign address 0x11 skipped, own 0x55 accepted",
          vals.get(0x30) == 0x55 and vals.get(0x31) == 0xF5,
          "0x30=%s SCON=%s expect 0x55/0xf5 (RB8=ninth=1)"
          % (hex(vals.get(0x30, -1)), hex(vals.get(0x31, -1))))

    # 4/5 -- rx9 framed stream: [0x11,ctrl 9th=0] is a DATA frame, not an address frame,
    # so SM2 must drop it; [0x55,0x03] is the command frame that matches.
    vals, _ = rx_run("sm2_rx9", p, [0x11, 0x02, 0x55, 0x03], rx9=True)
    check("sm2_rx9: 9th=0 frame is data not address; matching command frame wins",
          vals.get(0x30) == 0x55 and vals.get(0x31) == 0xF5,
          "0x30=%s SCON=%s" % (hex(vals.get(0x30, -1)), hex(vals.get(0x31, -1))))

    # 6 -- a 9th=1 frame with a foreign address is still dropped (p.49: examined).
    p2 = prog_save1(scon_pre=(mov(SADDR, 0x55), mov(SADEN, 0xFF)),
                    scon=MODE3 | SM2 | REN, with_scon=False)
    vals, _ = rx_run("sm2_ninth_mismatch", p2, [0x2A, 0x03, 0x55, 0x03], rx9=True)
    check("sm2_ninth_mismatch: command frame to another address does not set RI",
          vals.get(0x30) == 0x55, "0x30=%s" % hex(vals.get(0x30, -1)))

    # 7 -- broadcast address SADDR|SADEN: 0x5F fits only the broadcast rule
    # (given address 0x50 under mask 0x0F needs low nibble 0).
    p3 = prog_save1(scon_pre=(mov(SADDR, 0x50), mov(SADEN, 0x0F)),
                    scon=MODE3 | SM2 | REN, with_scon=False)
    vals, _ = rx_run("sm2_broadcast", p3, [0x2A, 0x03, 0x1F, 0x03, 0x5F, 0x03], rx9=True)
    check("sm2_broadcast: only SADDR|SADEN frame accepted (2A and 1F fit neither rule)",
          vals.get(0x30) == 0x5F, "0x30=%s" % hex(vals.get(0x30, -1)))

    # 8 -- reset state SADDR=SADEN=0: mask rules are vacuous, but the 9th-bit command
    # frame rule still gates RI (multiprocessor rule, p.48-49).
    p4 = (T1 + mov(0x30, 0xAA) + mov(0x31, 0xAA) + mov(SCON, MODE3 | SM2 | REN) +
          JNB_RI + MOV_A_SBUF + sta(0x30) + CLR_RI + JNB_RI + MOV_A_SBUF + sta(0x31) + SJMP)
    vals, _ = rx_run("sm2_dontcare", p4, [0x11, 0x03, 0x33, 0x02, 0x22, 0x03], rx9=True)
    check("sm2_dontcare_reset: SADDR=SADEN=0 passes every command frame, 9th=0 stays data",
          vals.get(0x30) == 0x11 and vals.get(0x31) == 0x22,
          "0x30=%s 0x31=%s (0x33 with ctrl 0x02 = data frame, must not interrupt)"
          % (hex(vals.get(0x30, -1)), hex(vals.get(0x31, -1))))

    # 9 -- mode 0: SM2 has no effect (p.49 note) and rx9 must not consume a control byte
    # (framing is "provided for the three asynchronous modes", p.48).
    p5 = (mov(0x30, 0xAA) + mov(SCON, SM2 | REN) + JNB_RI +
          MOV_A_SBUF + sta(0x30) + MOV_A_SCON + sta(0x31) + SJMP)
    vals, _ = rx_run("mode0_sm2_inert", p5, [0x7E], rx9=True)
    check("mode0_sm2_inert: single byte accepted with SM2=1, RB8 untouched",
          vals.get(0x30) == 0x7E and vals.get(0x31) == 0x31,
          "0x30=%s SCON=%s expect 0x7e/0x31" % (hex(vals.get(0x30, -1)), hex(vals.get(0x31, -1))))
    p5c = p5.replace(bytes([0x75, SCON, 0x30]), bytes([0x75, SCON, 0x10]))
    vals, _ = rx_run("mode0_no_sm2", p5c, [0x7E], rx9=True)
    check("mode0_no_sm2: identical result without SM2 (filter is inert in mode 0)",
          vals.get(0x30) == 0x7E and vals.get(0x31) == 0x11,
          "SCON=%s" % hex(vals.get(0x31, -1)))

    # 10 -- FE: bad stop bit sets SCON.7 with SMOD0=1, and a later good frame must not
    # clear it (p.48).  RB8 in mode 1 is the stop bit (table 14-4, p.53).
    vals, _ = rx_run("fe_latch", prog_two_frames(smod0=True), [0x41, 0x01, 0x42, 0x03],
                     rx9=True)
    check("fe_sets_on_bad_stop: SCON.7=1 RI=1 RB8=0 on frame with stop=0",
          vals.get(0x30) == 0x41 and vals.get(0x31) == 0xD1,
          "0x30=%s SCON1=%s expect 0x41/0xd1" % (hex(vals.get(0x30, -1)), hex(vals.get(0x31, -1))))
    check("fe_sticks_after_good_frame: valid stop cannot clear FE (p.48), RB8 follows stop",
          vals.get(0x32) == 0x42 and vals.get(0x33) == 0xD5,
          "0x32=%s SCON2=%s expect 0x42/0xd5" % (hex(vals.get(0x32, -1)), hex(vals.get(0x33, -1))))

    # 11 -- software clears FE with ANL SCON,#0x7F; the mux keeps SM0 out of the way.
    vals, _ = rx_run("fe_sw_clear",
                     prog_two_frames(smod0=True, clear_scon7=True),
                     [0x41, 0x01, 0x42, 0x03], rx9=True)
    check("fe_software_clear: ANL SCON,#0x7F clears SCON.7 and mode1 survives",
          vals.get(0x34) == 0x51 and vals.get(0x33) == 0x55,
          "post-clear SCON=%s, 2nd frame SCON=%s expect 0x51/0x55 (mode1 RI RB8, FE stays 0)"
          % (hex(vals.get(0x34, -1)), hex(vals.get(0x33, -1))))

    # 12 -- SMOD0=0: nothing may show on SCON.7 (it is SM0, mode 1 => 0) while RB8 still
    # mirrors the bad stop bit.  Feature is invisible without the mux.
    vals, _ = rx_run("fe_gated", prog_save1(), [0x41, 0x01], rx9=True)
    check("fe_gated_by_smod0: SMOD0=0 keeps SCON.7=0 (SM0), RB8=0 from bad stop",
          vals.get(0x30) == 0x41 and vals.get(0x31) == 0x51,
          "0x30=%s SCON=%s expect 0x41/0x51" % (hex(vals.get(0x30, -1)), hex(vals.get(0x31, -1))))

    # 13 -- device gating: rx9 is bound on the RD2 only.
    def info_var(t):
        p = subprocess.run([SIM, "-t", t, "-c", "-"], input="info var\nexit\n",
                           capture_output=True, text=True, timeout=30)
        return p.stdout
    iv = info_var("89C51RD2")
    check("info_var: uart0_rx9 is a named RD2 cfg var",
          "uart0_rx9" in iv, [ln for ln in iv.splitlines() if "uart0_rx9" in ln][:1])
    check("info_var: classic 89C51R sees no uart0_rx9 (byte-identical gate)",
          "uart0_rx9" not in info_var("89C51R"))

    # 14 -- negative controls on the pre-feature binary: the filter and the FE latch do
    # not exist there, so RI fires on the FIRST stream byte and SCON.7 never sets.
    if os.path.exists(REF):
        vals, _ = rx_run("ref_sm2", p, [0x11, 0x55], sim=REF)
        check("ref-b_no_filter: pre-feature binary RI-fires on foreign 0x11",
              vals.get(0x30) == 0x11,
              "0x30=%s (proves leg 3 measures the NEW filter)" % hex(vals.get(0x30, -1)))
        vals, _ = rx_run("ref_fe", prog_two_frames(smod0=True), [0x41, 0x02, 0x42, 0x03],
                         sim=REF)
        check("ref-b_no_fe: pre-feature binary leaves SCON.7 clear (it is SM0 there)",
              vals.get(0x30) == 0x41 and not (vals.get(0x31, 0xFF) & 0x80),
              "0x30=%s SCON1=%s (no stop-bit check exists in that build)"
              % (hex(vals.get(0x30, -1)), hex(vals.get(0x31, -1))))
    else:
        print("SKIP  ref controls (%s absent)" % REF)

    print("\nChecks run: %d  failures: %d" % (_checks, len(_failures)))
    if _failures:
        for f in _failures:
            print("FAILED:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
