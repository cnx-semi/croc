#!/usr/bin/env bash
# Copyright (c) 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CROC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DRYRUN=0
GDS_ARG=""
ODB_ARG=""
RUN_DIR_ARG=""

show_help() {
    cat <<'EOF'
KLayout LVS coordinator

Usage: ./run_lvs.sh [OPTIONS]

Options:
    --gds FILE          Layout to check (default: out/<PROJ_NAME>.filled.gds.gz,
                        else out/<PROJ_NAME>.sealed.gds.gz)
    --odb FILE          OpenROAD database the netlist is written from
                        (default: ../openroad/out/<PROJ_NAME>.odb)
    --run-dir DIR       Where to write results (default: klayout/lvs-run)
    --dry-run, -n       Print the planned steps without running them
    --verbose, -v       Print commands while executing
    --help, -h          Show this help message

Compares the final chip layout against the netlist OpenROAD routed, using the
IHP PDK's own KLayout LVS deck unmodified. Standard cells are compared at
transistor level. Hard macros (IO cells, SRAM, bond pads) are black boxes: their
pins are compared by name, not their insides. Results are written to
klayout/lvs-run/ (lvs.lvsdb opens in KLayout: Tools > Netlist Browser).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) show_help; exit 0 ;;
        --dry-run|-n) DRYRUN=1; shift ;;
        --verbose|-v) set -x; shift ;;
        --gds) [[ $# -ge 2 ]] || { echo "[ERROR][LVS] --gds needs a value" >&2; exit 2; }; GDS_ARG="$2"; shift 2 ;;
        --run-dir) [[ $# -ge 2 ]] || { echo "[ERROR][LVS] --run-dir needs a value" >&2; exit 2; }; RUN_DIR_ARG="$2"; shift 2 ;;
        --odb) [[ $# -ge 2 ]] || { echo "[ERROR][LVS] --odb needs a value" >&2; exit 2; }; ODB_ARG="$2"; shift 2 ;;
        *) echo "[ERROR][LVS] Unknown option: $1 (use --help for usage)" >&2; exit 2 ;;
    esac
done

if [[ "$DRYRUN" -eq 1 ]]; then
    export CROC_SKIP_TECH_SETUP=1
fi
source "$CROC_ROOT/env.sh"

if [[ "$CROC_PDK" != "sg13cmos5l" ]]; then
    echo "[ERROR][LVS] Only CROC_PDK=sg13cmos5l is supported (the LVS deck is the 5-layer one)" >&2
    exit 1
fi

if [[ -n "$GDS_ARG" ]]; then
    GDS_PATH="$GDS_ARG"
elif [[ -f "$SCRIPT_DIR/out/${PROJ_NAME}.filled.gds.gz" ]]; then
    GDS_PATH="$SCRIPT_DIR/out/${PROJ_NAME}.filled.gds.gz"
else
    GDS_PATH="$SCRIPT_DIR/out/${PROJ_NAME}.sealed.gds.gz"
fi
ODB_PATH="${ODB_ARG:-$CROC_ROOT/openroad/out/${PROJ_NAME}.odb}"
RUN_DIR="${RUN_DIR_ARG:-$SCRIPT_DIR/lvs-run}"
LVS_DECK="$KLAYOUT_PATH/tech/lvs/sg13cmos5l.lvs"
STDCELL_CDL="$IHP_G2_DIR/libs.ref/sg13g2_stdcell/cdl/sg13g2_stdcell.cdl"
TECH_LEF="$PDK_DIR_LEF_TECH/$PDK_TECH_LEF_FILE"
IO_LEF="$PDK_DIR_LEF_IOS/$PDK_IO_LEF_FILE"
BOND_LEF="$PDK_DIR_LEF_BOND/$PDK_BONDPAD_LEF"
SRAM_LEFS=("$PDK_DIR_LEF_SRAMS"/RM_IHPSG13*.lef)

if [[ "$DRYRUN" -eq 0 ]]; then
    for f in "$GDS_PATH" "$ODB_PATH" "$LVS_DECK" "$STDCELL_CDL" "$TECH_LEF" "$IO_LEF" "$BOND_LEF" "${SRAM_LEFS[@]}"; do
        [[ -f "$f" ]] || { echo "[ERROR][LVS] Missing input: $f" >&2; exit 1; }
    done
    mkdir -p "$RUN_DIR"
fi

run_cmd() {
    if [[ "$DRYRUN" -eq 1 ]]; then printf '%s\n' "$*"; else "$@"; fi
}

echo "[INFO][LVS] Layout:    $GDS_PATH"
echo "[INFO][LVS] Netlist:   $ODB_PATH"
echo "[INFO][LVS] LVS deck:  $LVS_DECK"
echo "[INFO][LVS] Run dir:   $RUN_DIR"

# 1. Black-box schematics for the hard macros, from their LEF pin lists
echo "[INFO][LVS] 1/4 Macro stubs"
run_cmd python3 "$SCRIPT_DIR/scripts/lvs_stubs.py" "$RUN_DIR/macro_stubs.cdl" \
    "$IO_LEF" "${SRAM_LEFS[@]}" "$BOND_LEF"

# 2. Netlist of the routed design, as CDL (OpenROAD writes it from the database)
echo "[INFO][LVS] 2/4 Netlist from OpenROAD"
if [[ "$DRYRUN" -eq 1 ]]; then
    echo "openroad -no_init -exit $RUN_DIR/write_cdl.tcl   # read_db $ODB_PATH; write_cdl -include_fillers"
else
    cat > "$RUN_DIR/write_cdl.tcl" <<EOF
read_db $ODB_PATH
write_cdl -include_fillers -masters [list $STDCELL_CDL $RUN_DIR/macro_stubs.cdl] $RUN_DIR/${TOP_DESIGN}.top.cdl
EOF
    openroad -no_init -exit "$RUN_DIR/write_cdl.tcl" > "$RUN_DIR/write_cdl.log" 2>&1 \
        || { echo "[ERROR][LVS] write_cdl failed, see $RUN_DIR/write_cdl.log" >&2; exit 1; }
    # fill cells are dropped from the layout view too (see scripts/lvs_layout.py)
    grep -v -E '^X.* sg13(g2|cmos5l)_fill_[0-9]+$' "$RUN_DIR/${TOP_DESIGN}.top.cdl" > "$RUN_DIR/${TOP_DESIGN}.top.nofill.cdl"
    cat "$STDCELL_CDL" "$RUN_DIR/macro_stubs.cdl" "$RUN_DIR/${TOP_DESIGN}.top.nofill.cdl" > "$RUN_DIR/${TOP_DESIGN}.schematic.cdl"
fi

# 2b. Net names on the SRAM pins. The two SRAM banks are identical macros with
# nearly identical surroundings, and the layout has no instance names, so without
# names on their pin nets KLayout cannot tell which bank is which and reports
# everything attached to them (tie cells, drivers) as mismatched.
NETLABELS="$RUN_DIR/macro_nets.txt"
if [[ "$DRYRUN" -eq 1 ]]; then
    echo "openroad -no_init -exit $RUN_DIR/macro_nets.tcl   # SRAM pin -> net names into $NETLABELS"
else
    cat > "$RUN_DIR/macro_nets.tcl" <<EOF
read_db $ODB_PATH
set block [ord::get_db_block]
set fh [open $NETLABELS w]
foreach inst [\$block getInsts] {
    set master [[\$inst getMaster] getName]
    if {![string match "RM_IHPSG13*" \$master]} continue
    lassign [\$inst getOrigin] ox oy
    puts \$fh "INST \$master \$ox \$oy"
    foreach it [\$inst getITerms] {
        set net [\$it getNet]
        if {\$net eq "NULL"} continue
        set mt [\$it getMTerm]
        if {[\$mt getSigType] ne "SIGNAL"} continue
        puts \$fh "PIN [\$mt getName] [\$net getName]"
    }
}
close \$fh
EOF
    openroad -no_init -exit "$RUN_DIR/macro_nets.tcl" > "$RUN_DIR/macro_nets.log" 2>&1 \
        || { echo "[ERROR][LVS] macro net dump failed, see $RUN_DIR/macro_nets.log" >&2; exit 1; }
fi
export LVS_MACRO_NETS="$NETLABELS"

# 3. Layout view: macros as LEF abstracts, seal-ring wrapper dissolved
echo "[INFO][LVS] 3/4 LVS layout"
run_cmd python3 "$SCRIPT_DIR/scripts/lvs_layout.py" "$GDS_PATH" "$RUN_DIR/${TOP_DESIGN}.lvs.gds.gz" \
    "$PDK_LAYER_MAP_FILE" "$TOP_DESIGN" "$TECH_LEF" "$IO_LEF" "${SRAM_LEFS[@]}" "$BOND_LEF"

# 4. The PDK's LVS deck, unmodified, behind a thin wrapper that black-boxes the macros
echo "[INFO][LVS] 4/4 Extraction and comparison"
WRAPPER="$RUN_DIR/lvs_blackbox.lvs"
if [[ "$DRYRUN" -eq 0 ]]; then
    cat > "$WRAPPER" <<EOF
# Generated by run_lvs.sh. Black-boxes the hard macros, then runs the PDK deck.
# The SPICE reader upper-cases schematic names while the layout keeps the
# GDS spelling, and glob patterns are case sensitive, so both spellings are listed.
\$croc_blackbox = %w[sg13cmos5l_* SG13CMOS5L_* RM_IHPSG13_* bondpad* BONDPAD*]

# A macro pin has several port shapes; shapes carrying the same pin name inside
# one macro are one electrical pin.
\$croc_blackbox.each { |pattern| connect_implicit(pattern, '*') }

# Blank the macros on both sides right after extraction: a blanked circuit keeps
# its (named) pins, is compared by pin name only, and is exempt from the netlist
# purge. This has to happen before the deck's first netlist step, which purges.
\$croc_blackbox.each { |pattern| blank_circuit(pattern) }

# A macro circuit also gets unnamed pins for the substrate and well regions that
# the deck derives for every cell (the whole cell extent), which leave the cell
# because the neighbouring cells share them. The vendor schematic of a macro has
# no such pins. Disconnect them from every instance and remove them, so macros are
# compared by their named pins only. Runs after the blanking above, right after
# extraction and before the deck's first netlist step.
@netter.instance_variable_get(:@post_extract_config) << lambda do |l2n|
  nl = l2n.netlist
  removed = 0
  nl.each_circuit do |c|
    next unless c.name =~ /^(sg13cmos5l_|rm_ihpsg13_|bondpad)/i
    ids = c.each_pin.select { |p| p.name.to_s.empty? }.map { |p| p.id }
    next if ids.empty?
    c.each_ref { |sc| ids.each { |i| sc.connect_pin(i, nil) } }
    ids.each { |i| c.remove_pin(i) }
    removed += ids.size
  end
  puts "Macro circuits: removed #{removed} unnamed substrate/well pins"

  # The layout netlist as extracted, for the structural cross-check
  # (scripts/lvs_structural.py). Written before the normalisations below, which
  # change what KLayout sees but not the check.
  raw_writer = RBA::NetlistSpiceWriter.new
  raw_writer.use_net_names = true
  nl.write("$RUN_DIR/${TOP_DESIGN}.layout_raw.cir", raw_writer)

  # Outputs that are left open on purpose (clock-load cells, unused outputs) are an
  # explicit single-pin net in the netlist OpenROAD writes and no net at all in the
  # extracted layout, and KLayout reports a mismatch around each. Open them on both
  # sides: a net that makes exactly one subcircuit connection and nothing else is
  # disconnected from that pin.
  opened = Hash.new(0)
  [[:layout, nl], [:schematic, schematic]].each do |side, netlist|
    netlist.each_circuit do |c|
      c.each_net.to_a.each do |n|
        next unless n.each_pin.to_a.empty? && n.each_terminal.to_a.empty?
        refs = n.each_subcircuit_pin.to_a
        next unless refs.size == 1
        refs[0].subcircuit.connect_pin(refs[0].pin_id, nil)
        opened[side] += 1
      end
    end
  end
  puts "Open outputs: disconnected #{opened[:layout]} layout and #{opened[:schematic]} schematic single-pin nets"

  # Tie cells: hundreds of identical cells, each driving a net with no name. They
  # form large groups of interchangeable sub-structures, which KLayout's matcher
  # reports as mismatches even though both sides are equivalent. A tie cell only
  # produces a constant, so on both sides the loads of its output net are
  # connected to the supply net directly and the cell is dropped. A short of a
  # tie net to a signal net still shows up: that signal moves onto the supply on
  # one side only.
  tied = Hash.new(0)
  [[:layout, nl], [:schematic, schematic]].each do |side, netlist|
    netlist.each_circuit do |c|
      supplies = { 'tielo' => c.net_by_name('VSS'), 'tiehi' => c.net_by_name('VDD') }
      next if supplies.values.any?(&:nil?)
      c.each_subcircuit.to_a.each do |sc|
        kind = sc.circuit_ref.name.to_s.downcase[/(tielo|tiehi)\z/, 1]
        next unless kind
        out = sc.circuit_ref.each_pin.map(&:id).find do |pid|
          nm = sc.circuit_ref.pin_by_id(pid).name.to_s.downcase
          !(nm == 'vdd' || nm == 'vss')
        end
        net = out && sc.net_for_pin(out)
        if net
          net.each_subcircuit_pin.to_a.each do |ref|
            next if ref.subcircuit.id == sc.id
            ref.subcircuit.connect_pin(ref.pin_id, supplies[kind])
          end
        end
        c.remove_subcircuit(sc)
        tied[side] += 1
      end
    end
  end
  puts "Tie cells: replaced #{tied[:layout]} layout and #{tied[:schematic]} schematic instances by the supply nets"
end

# %include $LVS_DECK
EOF
fi
KLAYOUT_CMD=(klayout -b -r "$WRAPPER"
    -rd run_mode=deep -rd no_net_names=false -rd spice_comments=false -rd net_only=false
    -rd top_lvl_pins=false -rd no_simplify=false -rd no_series_res=false -rd no_parallel_res=false
    -rd combine_devices=false -rd disable_tap_extraction=false -rd purge=false -rd purge_nets=false
    -rd "topcell=$TOP_DESIGN" -rd "input=$RUN_DIR/${TOP_DESIGN}.lvs.gds.gz"
    -rd "schematic=$RUN_DIR/${TOP_DESIGN}.schematic.cdl"
    -rd ignore_top_ports_mismatch=false -rd implicit_nets=
    -rd "report=$RUN_DIR/lvs.lvsdb" -rd "log=$RUN_DIR/lvs.log" -rd "target_netlist=$RUN_DIR/extracted.cir")
if [[ "$DRYRUN" -eq 1 ]]; then
    printf '%q ' "${KLAYOUT_CMD[@]}"; printf '\n'
    exit 0
fi

set +e
"${KLAYOUT_CMD[@]}" 2>&1 | tee "$RUN_DIR/lvs-console.log"
status=${PIPESTATUS[0]}
set -e
[[ "$status" -eq 0 ]] || { echo "[ERROR][LVS] KLayout exited with status $status" >&2; exit "$status"; }

echo "[INFO][LVS] ---- verdict ----"
# (a) KLayout: devices of every circuit type and every named pin. The top circuit's
# own net/instance pairing is reported separately below.
python3 "$SCRIPT_DIR/scripts/lvs_report.py" "$RUN_DIR/lvs.lvsdb" 0 > "$RUN_DIR/lvs-report.txt" || true
cat "$RUN_DIR/lvs-report.txt"
CIRCUITS_OK=0
if python3 - "$RUN_DIR/lvs.lvsdb" "$TOP_DESIGN" <<'PYEOF'
import sys
import klayout.db as db
lvs = db.LayoutVsSchematic()
lvs.read(sys.argv[1])
xref = lvs.xref()
S = db.NetlistCrossReference
good = (S.Match, S.MatchWithWarning)
bad = [p.first().name if p.first() else p.second().name
       for p in xref.each_circuit_pair()
       if p.status() not in good and (p.first().name if p.first() else p.second().name).lower() != sys.argv[2].lower()]
print(f"[INFO][LVS] KLayout: {sum(1 for _ in xref.each_circuit_pair()) - 1} circuit types compared, "
      f"{len(bad)} not matching" + (": " + ", ".join(bad[:8]) if bad else ""))
sys.exit(1 if bad else 0)
PYEOF
then CIRCUITS_OK=1; fi

# (b) the top level: KLayout's own verdict, then the structural cross-check
KLAYOUT_TOP_OK=0
grep -Fq "Congratulations! Netlists match" "$RUN_DIR/lvs.log" && KLAYOUT_TOP_OK=1
STRUCT_OK=0
if python3 "$SCRIPT_DIR/scripts/lvs_structural.py" "$RUN_DIR/${TOP_DESIGN}.layout_raw.cir" \
        "$RUN_DIR/${TOP_DESIGN}.schematic.cdl" "$TOP_DESIGN" 12 | tee "$RUN_DIR/lvs-structural.txt"; then
    STRUCT_OK=1
fi

if [[ "$KLAYOUT_TOP_OK" -eq 1 ]]; then
    echo "[INFO][LVS] PASSED: layout and netlist match (KLayout)"
elif [[ "$CIRCUITS_OK" -eq 1 && "$STRUCT_OK" -eq 1 ]]; then
    echo "[INFO][LVS] PASSED: all circuit types match in KLayout and the top-level connectivity is"
    echo "[INFO][LVS]         identical in the structural check; KLayout's own top-level matcher leaves"
    echo "[INFO][LVS]         pairs of interchangeable cells unresolved (see doc/LVS.md)"
else
    echo "[ERROR][LVS] FAILED: see $RUN_DIR/lvs.log, $RUN_DIR/lvs-structural.txt and open" >&2
    echo "[ERROR][LVS]         $RUN_DIR/lvs.lvsdb in KLayout (Tools > Netlist Browser)" >&2
    exit 1
fi
