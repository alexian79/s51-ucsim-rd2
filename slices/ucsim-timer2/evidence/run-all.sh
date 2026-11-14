#!/bin/sh
# Regenerate every artefact in this directory.
#   ./run-all.sh <unfixed-ucsim_51> <patched-ucsim_51> [src-tree]
# Both binaries must be built from the same tree except for timer2-cycles.patch.
set -e
HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
UNFIXED=${1:?usage: $0 <unfixed> <patched> [src-tree]}
FIXED=${2:?usage: $0 <unfixed> <patched> [src-tree]}
SRC=${3:-/tmp/ucsim-mainline}
LOG="$HERE/SUMMARY.txt"

{
  for b in "$UNFIXED" "$FIXED"; do
    echo "binary: $b"
    sha256sum "$b"
    echo 'quit' | "$b" -q -t C52 2>&1 | head -1
  done
  echo "================ timer 2 cycle contract (transcripts in transcripts/) ="
} 2>&1 | tee "$LOG"

"$HERE/timer2_cycles_demo.py" --before "$UNFIXED" --after "$FIXED" 2>&1 | tee -a "$LOG"
echo | tee -a "$LOG"
echo "================ s51 config test, 14 devices (out-config.*) ============" | tee -a "$LOG"
"$HERE/config_baseline_diff.sh" "$UNFIXED" "$FIXED" "$SRC" 2>&1 |
  grep -E 'devices compared|^RESULT' | tee -a "$LOG"
echo | tee -a "$LOG"
echo "see PR.md for the reading of these numbers" | tee -a "$LOG"
