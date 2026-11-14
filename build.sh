#!/bin/sh
# Build the vendored ucsim 8051 core. Idempotent.
#
# Root defaults to the directory holding this script; override with S51_ROOT. The vendor tree is
# NOT distributed with the sources -- NOTICE gives the clone + patch recipe that reproduces the
# exact tree every suite in t/ was run against.
set -e
ROOT="${S51_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
cd "$ROOT/vendor/ucsim-stc/ucsim" || {
  echo "build.sh: no vendor tree at $ROOT/vendor/ucsim-stc; see NOTICE" >&2; exit 3; }
[ -f src/sims/s51.src/ucsim_51 ] || {
  ./configure --prefix="$ROOT/install" > /tmp/s51-configure.log 2>&1
}
make -C . > /tmp/s51-make.log 2>&1 || { tail -30 /tmp/s51-make.log; exit 1; }
echo "built: $(pwd)/src/sims/s51.src/ucsim_51"
