#!/bin/sh
# Control: does the timer-2 `cycles' patch move any output in the project's
# own s51 config test (src/sims/s51.src/test/config, the one test that needs
# no sdcc)?  Runs the same command test-lib.mk's run-sim uses, for every
# device the simulator advertises, with both binaries, and diffs
#   (a) fixed output vs unfixed output   -- must be empty
#   (b) fixed output vs the checked-in baseline (pre-existing drift, if any)
# Usage: ./config_baseline_diff.sh <unfixed-sim> <fixed-sim> [src-tree]
set -e
UNFIXED=${1:?usage: $0 <unfixed> <fixed> [src-tree]}
FIXED=${2:?usage: $0 <unfixed> <fixed> [src-tree]}
SRC=${3:-/tmp/ucsim-mainline}
TEST=$SRC/src/sims/s51.src/test/config
OUT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)/out-config
rm -rf "$OUT.unfixed" "$OUT.fixed"
mkdir -p "$OUT.unfixed" "$OUT.fixed"

# ELIDE, copied verbatim from src/test-lib.mk
elide() {
  sed -E \
    -e 's/^(uCsim )[^,]*(, Copyright \(C\) )[[:digit:]]+(-[[:digit:]]+)?/\1[ELIDED]\2[ELIDED]/' \
    -e 's/^(ucsim version ).*/\1[ELIDED]/' \
    -e 's/^(Loading from ).*/\1[ELIDED]/' \
    -e 's/( words read from ).*/\1[ELIDED]/' \
    -e 's/( File: ).*/\1[ELIDED]/' \
    -e 's/^(Simulated [[:digit:]]+ ticks ).*/\1[ELIDED]/' \
    -e 's/^(Host usage: ).*/\1[ELIDED]/'
}

for sim in "$UNFIXED" "$FIXED"; do
  case "$sim" in "$UNFIXED") d="$OUT.unfixed";; *) d="$OUT.fixed";; esac
  for dev in $("$sim" -H | sort -u -k2b | cut -d' ' -f1 | grep -v '^Parameter$'); do
    "$sim" -q -R 0 -t "$dev" -e 'conf; info reg; info mem' \
      2>&1 < /dev/null | elide > "$d/$dev" || true
  done
done

echo "devices compared: $(ls "$OUT.fixed" | wc -l)"
if diff -urN "$OUT.unfixed" "$OUT.fixed"; then
  echo "RESULT: fixed output is byte-identical to unfixed for every device"
else
  echo "RESULT: outputs DIFFER, see above"
fi
if [ -d "$TEST/baseline" ]; then
  echo "--- fixed vs checked-in baseline $TEST/baseline ---"
  diff -urN "$TEST/baseline" "$OUT.fixed" | head -40 || true
  echo "(rows above are pre-existing drift if any; the fix must add none)"
fi
