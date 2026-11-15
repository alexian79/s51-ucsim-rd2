#!/bin/sh
# Simulation floor: every non-Ghidra suite against one binary.
#   t/floor.sh [/path/to/ucsim_51]
# The two Ghidra suites (langtest, rmwtest) and the Rust-core cross-check (xcompare) are NOT
# here: the first two drive analyzeHeadless with fixed project names and must run one at a
# time, and xcompare writes a fixed /tmp/xval.ihx (SIMULATION-PLAN.md section 5).
#
# Verdict rules, learned the hard way on 2026-11-14:
#   * runtests is REPORTED but NEVER counted, so "floor: 1 suite(s) not green" has never meant
#     runtests. It exits non-zero by design at its 11/20 baseline; any report that read the
#     count as "just the baseline" was misreading it.
#   * a suite is not green when it exits non-zero OR prints a FAIL line while exiting 0.
#   * only suites whose cutover has landed are listed below; a model that is not in the vendor
#     tree yet carries its own suite in its slice until the merge brings it in.
ROOT="${S51_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
BIN=${1:-$ROOT/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51}
[ -x "$BIN" ] || { echo "no binary: $BIN"; exit 2; }
export S51_SIM="$BIN"                       # the binary under test, honoured by every suite
export S51="${S51:-$ROOT}"                  # repo root, for the suites that want a directory
cd "$ROOT" || exit 2

GREP='Checks run|cases agree|baselines match|byte-identical|PASS|FAIL'

SUITES="ident89c51r rd2identity rd2registers banktest ccapmrst dpstest t2oetest wdttest x2test xramtest hwtest irctest pcapin euarttest uartmp motortest runtests"
for f in kbdtest spitest ckrltest bootromtest eepromtest t2cnttest bpcmdtest wptrtest symtest maptest s51dbgtest; do
  [ -f "t/$f.py" ] && SUITES="$SUITES $f"
done

fail=0; names=""
for s in $SUITES; do
  [ -f "t/$s.py" ] || { printf '%-14s MISSING\n' "$s"; fail=$((fail+1)); continue; }
  out=$(timeout 900 python3 "t/$s.py" 2>&1)
  rc=$?
  # the LAST summary match, not the first: an early PASS row used to hide a trailing FAIL
  sum=$(printf '%s\n' "$out" | grep -E "$GREP" | tail -1 | cut -c1-110)
  [ -n "$sum" ] || sum=$(printf '%s\n' "$out" | tail -1 | cut -c1-110)
  case "$s:$rc" in
    runtests:*) printf '%-14s %s  (baseline 11/20)\n' "$s" "$sum" ;;
    *) printf '%-14s %s\n' "$s" "$sum" ;;
  esac
  case "$s" in
    runtests) ;;          # vendor meta-runner: reported, NEVER counted (its baseline is 11/20)
    *)
      neg=$(printf '%s\n' "$out" | grep -cE '(^| )FAIL|FAIL:|Failed: [1-9]|failed: [1-9]')
      if [ $rc -ne 0 ]; then
        fail=$((fail+1)); names="$names $s(rc=$rc)"
      elif [ "$neg" -ne 0 ]; then
        # an exit-0 suite that printed FAIL is exactly the bug that hid irctest for two cutovers
        fail=$((fail+1)); names="$names $s(exit0-but-$neg-FAIL-lines)"
      fi
      ;;
  esac
done
# Process hygiene: nothing may survive the floor.  A console pipeline whose timeout took only the
# driving `sh' leaves its simulator reparented to init, and a core sitting in `run' never polls
# stdin, so the closed pipe cannot stop it either -- it burns a core until someone notices (the fix
# is t/simproc.py:run_pipe).  Match the DEVICE argument as well as the binary name: the control
# builds this bed compares against are not called ucsim_*, and a name-only pattern walked straight
# past eleven orphaned consoles once.  Only PPID 1 rows count, which excludes every suite's own
# children -- and this check's own ps/awk -- by construction.
strays=$(ps -eo ppid=,args= | awk '$1 == 1 && (/-t 89C51/ || /ucsim/)' | wc -l)
if [ "$strays" -ne 0 ]; then
  printf 'hygiene: %s reparented simulator console(s) survived this run\n' "$strays"
  ps -eo pid=,ppid=,etimes=,args= | awk '$2 == 1 && (/-t 89C51/ || /ucsim/)' | cut -c1-110
  fail=$((fail+1)); names="$names process-leak($strays)"
fi
echo "binary: $BIN"
echo "floor: $fail suite(s) not green${names:+ --$names}"
exit $fail
