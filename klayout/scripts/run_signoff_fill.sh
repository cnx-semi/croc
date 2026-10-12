#!/usr/bin/env bash
# Convenience wrapper: GDS export -> seal ring -> fabrication-ready fill, in
# one command. As of this fix, `run_finishing.sh --fill-metal --fill-activ`
# already produces the fully-fixed out/<PROJ_NAME>.filled.gds.gz directly (it
# runs the vendor fill, then the supplemental pass that closes the density
# gaps the vendor's single-pass filler leaves behind -- see
# doc/DRC_SIGNOFF.md and IHP-Open-PDK issue #438). This script just calls
# those official steps in sequence; it holds no fill logic of its own.
#
# Output: klayout/out/<PROJ_NAME>.filled.gds.gz  -- the signoff-ready GDS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLAYOUT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$KLAYOUT_DIR"
./run_finishing.sh --gds --seal --fill-metal --fill-activ

PROJ_NAME="${PROJ_NAME:-croc}"
echo "[INFO] Done -- out/${PROJ_NAME}.filled.gds.gz is the signoff-ready GDS"
