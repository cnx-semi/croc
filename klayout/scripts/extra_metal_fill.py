#!/usr/bin/env python3
"""
Supplemental metal fill pass for M1/M2/M3 density gaps.

Why this exists: the vendor filler macro
(ihp13/sg13cmos5l/libs.tech/klayout/tech/macros/sg13cmos5l_filler_Metal.lym)
does exactly ONE grid-aligned fill pass per layer (medium cell, then a second
grid pass of the 1x1um cell on the leftover). That is a known, still-open
upstream limitation: IHP-Open-PDK issue #438 "Relax metal filler rules"
describes the identical failure mode -- a single-origin grid fill cannot
tile irregular leftover shapes (e.g. narrow channels next to dense,
direction-preferred routing), and the reporter's own workaround was
"five passes of the smallest fill shape with different offsets".

This script reproduces that workaround, without touching the vendored PDK
macro, by:
  1. re-deriving the same "fillable" region the vendor script computes
     (EdgeSeal.holes minus metal/filler+0.42um margin minus nofill/slit)
  2. running additional 1x1um / 0.42um-pitch fill passes at different grid
     origins until the leftover stops shrinking or is empty
  3. writing the extra fill shapes onto the same (layer, datatype=22)
     "filler" layer the vendor script uses, so DRC treats them identically

Correctness note: each pass re-derives its exclusion region from scratch,
including a 0.42um margin around every shape placed by a PREVIOUS pass (not
just the vendor's original fill). This guarantees the MxFil.b filler-to-filler
minimum spacing rule holds across passes with different grid origins, which a
single fill_region() call's own internal "remaining" bookkeeping does not by
itself promise for our multi-origin use.

Only run on layers that actually failed signoff DRC's MxFil.h density rule
(M1, M2, M3 for this design -- M4 and TopMetal1 already passed).

Usage:
    python3 extra_metal_fill.py <in.gds.gz> <out.gds.gz> [layer,layer,...]
"""
import sys
import time
import klayout.db as db

_T0 = time.time()


def stamp(msg):
    """Print with elapsed wall-clock, flushed, so a slow stage is visibly alive."""
    print(f"[{(time.time() - _T0) / 60:6.1f} min] {msg}", flush=True)

MARGIN_UM = 0.42          # MxFil.b/.c: min metal:filler / filler:filler spacing
CELL_UM = 1.0              # smallest legal filler cell (MxFil.a1: min width 1.00)
PITCH_UM = CELL_UM + MARGIN_UM   # 1.42um center-to-center on a tight grid
MAX_PASSES = 8

# different offsets each pass, matching the "different offsets" technique
# from IHP-Open-PDK #438. Kept sub-pitch so each pass explores new phase.
OFFSETS_UM = [
    (0.00, 0.00), (0.71, 0.00), (0.00, 0.71), (0.71, 0.71),
    (0.355, 0.355), (1.065, 0.355), (0.355, 1.065), (1.065, 1.065),
]


def um(layout, v):
    return int(round(v / layout.dbu))


def base_layers(layout, top, metal_layer):
    """Everything that does NOT change between passes."""
    li_metal = layout.layer(metal_layer, 0)
    li_filler = layout.layer(metal_layer, 22)
    li_nofill = layout.layer(metal_layer, 23)
    li_slit = layout.layer(metal_layer, 24)
    li_seal = layout.layer(39, 0)
    li_nomet = layout.layer(160, 0)

    stamp(f"metal layer {metal_layer}: collecting + merging shapes")
    metal = db.Region(top.begin_shapes_rec(li_metal)).merged()
    stamp(f"  metal merged ({metal.count()} polygons)")
    filler = db.Region(top.begin_shapes_rec(li_filler)).merged()
    stamp(f"  vendor filler merged ({filler.count()} polygons)")
    nofill = db.Region(top.begin_shapes_rec(li_nofill)).merged()
    slit = db.Region(top.begin_shapes_rec(li_slit)).merged()
    seal = db.Region(top.begin_shapes_rec(li_seal)).merged()
    nomet = db.Region(top.begin_shapes_rec(li_nomet)).merged()

    margin_dbu = um(layout, MARGIN_UM)
    static_excl = metal.sized(margin_dbu) | filler.sized(margin_dbu) | nofill | slit | nomet
    stamp(f"  static exclusion built ({static_excl.count()} polygons)")
    universe = seal.holes()
    return universe, static_excl, li_filler


def supplemental_fill(layout, top, metal_layer, log):
    universe, static_excl, li_filler = base_layers(layout, top, metal_layer)
    dbu = layout.dbu
    margin_dbu = um(layout, MARGIN_UM)
    cell_dbu = um(layout, CELL_UM)
    pitch_dbu = um(layout, PITCH_UM)
    fc_box = db.Box(0, 0, cell_dbu, cell_dbu)

    fill_cell_name = f"_extra_fill_m{metal_layer}"
    fc = layout.create_cell(fill_cell_name)
    fc.shapes(li_filler).insert(fc_box)
    fc_idx = fc.cell_index()

    placed_so_far = db.Region()   # unsized union of everything WE place
    start_leftover = universe - static_excl
    log(f"metal layer {metal_layer}: leftover before extra fill = "
        f"{start_leftover.area()*dbu*dbu:.1f} um^2")

    for i, (ox_um, oy_um) in enumerate(OFFSETS_UM[:MAX_PASSES]):
        # Re-derive the exclusion EVERY pass: static exclusion plus a fresh
        # margin around everything placed so far (vendor fill + our own
        # earlier passes), so filler-to-filler spacing holds across passes
        # regardless of fill_region()'s own internal margin handling.
        excl = static_excl | placed_so_far.sized(margin_dbu)
        fillable_now = universe - excl
        if fillable_now.is_empty():
            log(f"  pass {i+1}: nothing left to fill, stopping early")
            break

        origin = db.Point(um(layout, ox_um), um(layout, oy_um))
        row_step = db.Vector(pitch_dbu, 0)
        col_step = db.Vector(0, pitch_dbu)
        remaining = db.Region()

        top.fill_region(
            fillable_now, fc_idx, fc_box, row_step, col_step, origin,
            remaining, db.Vector(0, 0)
        )
        placed_this_pass = fillable_now - remaining
        placed_so_far = (placed_so_far | placed_this_pass).merged()
        log(f"  pass {i+1} offset=({ox_um},{oy_um})um  "
            f"placed {placed_this_pass.area()*dbu*dbu:.1f} um^2 this pass, "
            f"leftover now {remaining.area()*dbu*dbu:.1f} um^2")

    final_excl = static_excl | placed_so_far.sized(margin_dbu)
    final_leftover = universe - final_excl
    log(f"metal layer {metal_layer}: leftover after extra fill  = "
        f"{final_leftover.area()*dbu*dbu:.1f} um^2  "
        f"(placed {placed_so_far.area()*dbu*dbu:.1f} um^2 of new filler total)")

    # No in-script spacing self-check on purpose. An earlier version ran
    # db.Region.space_check() on the merged fill region: it over-reports (it
    # flags merge-boundary notches between passes that the real MxFil.b rule
    # does not), and on a dense layer (M1: ~775k um^2 of fill) it never
    # returns -- it silently stalled the whole flow for hours. Filler spacing
    # is guaranteed by construction (every pass excludes a 0.42um margin
    # around everything already placed) and checked by the real rule in
    # `run_drc.sh --full` (M?Fil.a1 / M?Fil.b).


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    in_gds, out_gds = sys.argv[1], sys.argv[2]
    layers = [int(x) for x in sys.argv[3].split(',')] if len(sys.argv) > 3 else [8, 10, 30]

    layout = db.Layout()
    layout.read(in_gds)
    top = layout.top_cell()
    stamp(f"loaded {in_gds}: top cell = {top.name}, dbu = {layout.dbu}")

    for metal_layer in layers:
        supplemental_fill(layout, top, metal_layer, stamp)

    layout.write(out_gds)
    stamp(f"wrote {out_gds}")


if __name__ == "__main__":
    main()
