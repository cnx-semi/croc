#!/usr/bin/env python3
"""Build the LVS view of a finished chip GDS.

The geometry under test is the final GDS: routing, standard cells and the real
layouts of the macros (IO cells, SRAM, bond pad). Nothing is moved or removed from
the routing. What changes:

1. Fill is dropped: standard-cell fill and the density fill added by the
   finishing step. It has no devices and is isolated from every wire by
   construction (the DRC spacing rules), and tens of thousands of empty circuits
   make KLayout's netlist purge quadratic, so the comparison would not finish.
2. Via cells are flattened into the top cell. The DEF reader makes one
   shape-only cell per via type and one instance per routing via (about 270k
   here); the same purge cost applies. The geometry is unchanged.
3. The pins of the IO cells and of the bond pad are labelled with their LEF pin
   names. The real GDS of the bond pad carries no label, and the IO cells have
   rails split per metal layer that a label ties together. The SRAM is left
   alone: its GDS names buses A_DIN<13> and a second, LEF-style name on the
   same pin makes KLayout join them into a name that matches neither.
4. The pins of the two SRAM banks are labelled with the net names of the routed
   netlist (from run_lvs.sh via LVS_MACRO_NETS). The banks are identical and the
   layout has no instance names, so without names nothing tells them apart.
5. The seal-ring wrapper is dissolved, so the top cell is the design itself
   (<design>_sealed -> <design>), the name the netlist uses.

Usage:
  lvs_layout.py <in.gds> <out.gds> <layer.map> <design> <tech.lef> <macro.lef> [...]
Environment: LVS_MACRO_NETS = file written by run_lvs.sh (SRAM pin -> net names)
"""
import fnmatch
import os
import re
import sys

import klayout.db as db

METALS = ('Metal1', 'Metal2', 'Metal3', 'Metal4', 'TopMetal1')

# Fill: standard-cell spacers and the density fill the finishing step adds.
FILL_PATTERNS = ('sg13g2_fill_*', 'sg13cmos5l_fill_*', '*_FILL_CELL', '_extra_fill_*')
# Shape-only via cells from the DEF reader.
VIA_PATTERNS = ('VIA_*',)
# Macros that keep their own labels (see 3 above).
OWN_LABEL_MACROS = ('RM_IHPSG13',)


def lef_pin_rects(path):
    """{macro: [(pin, layer, x1, y1, x2, y2), ...]} for every PIN PORT RECT on a routing layer."""
    out = {}
    text = open(path).read()
    for m in re.finditer(r'^MACRO\s+(\S+)(.*?)^END\s+\1\s*$', text, re.S | re.M):
        rects = []
        for p in re.finditer(r'^\s*PIN\s+(\S+)(.*?)^\s*END\s+\1\s*$', m.group(2), re.S | re.M):
            layer = None
            for line in p.group(2).splitlines():
                tok = line.split()
                if not tok:
                    continue
                if tok[0] == 'LAYER':
                    layer = tok[1]
                elif tok[0] == 'RECT' and layer in METALS:
                    rects.append((p.group(1), layer) + tuple(float(v) for v in tok[1:5]))
        out[m.group(1)] = rects
    return out


def label_layers(map_file):
    """{metal name: (layer, datatype)} from the 'NAME <metal>/PIN l d' rows."""
    out = {}
    for line in open(map_file):
        tok = line.split()
        if len(tok) == 4 and tok[0] == 'NAME' and tok[1].endswith('/PIN'):
            out[tok[1][:-4]] = (int(tok[2]), int(tok[3]))
    return out


def read_macro_nets(path):
    """{master: {(ox, oy): {pin: net}}} from the file run_lvs.sh writes."""
    placements = {}
    cur = None
    for line in open(path):
        tok = line.rstrip('\n').split(None, 2)
        if not tok:
            continue
        if tok[0] == 'INST':
            master, ox, oy = line.split()[1:4]
            cur = placements.setdefault(master, {}).setdefault((int(ox), int(oy)), {})
        elif tok[0] == 'PIN' and cur is not None and len(tok) == 3:
            cur[tok[1]] = tok[2]
    return placements


def matches(name, patterns):
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def main():
    if len(sys.argv) < 7:
        print(__doc__)
        sys.exit(1)
    in_gds, out_gds, layer_map, design = sys.argv[1:5]
    lefs = sys.argv[5:]          # first = technology LEF (unused), rest = macro LEFs

    ly = db.Layout()
    ly.read(in_gds)
    dbu = ly.dbu
    sealed, core = ly.cell(design + "_sealed"), ly.cell(design)
    top = core if core is not None else sealed
    wrapper = sealed if (sealed is not None and core is not None) else None

    pin_rects = {}
    for lef in lefs[1:]:
        pin_rects.update(lef_pin_rects(lef))
    label_ld = label_layers(layer_map)

    # 3. pin names on the IO cells and the bond pad
    n_pin_labels = 0
    for name, rects in pin_rects.items():
        cell = ly.cell(name)
        if cell is None or name.upper().startswith(OWN_LABEL_MACROS):
            continue
        for pin, layer, x1, y1, x2, y2 in rects:
            if layer not in label_ld:
                continue
            cx = int(round((x1 + x2) / 2 / dbu))
            cy = int(round((y1 + y2) / 2 / dbu))
            cell.shapes(ly.layer(*label_ld[layer])).insert(db.Text(pin, db.Trans(db.Point(cx, cy))))
            n_pin_labels += 1
    print(f"[INFO][LVS] {n_pin_labels} pin labels added to the IO cells and the bond pad")

    # 4. net names on the SRAM pins
    nets_file = os.environ.get('LVS_MACRO_NETS')
    n_net_labels = 0
    if nets_file and os.path.exists(nets_file):
        placements = read_macro_nets(nets_file)
        for inst in top.each_inst():
            name = inst.cell.name
            if name not in pin_rects or name not in placements:
                continue
            tr = inst.cell_inst.trans
            nets = placements[name].get((tr.disp.x, tr.disp.y))
            if nets is None:
                print(f"[WARNING][LVS] no netlist instance of {name} at "
                      f"({tr.disp.x * dbu:.3f}, {tr.disp.y * dbu:.3f})")
                continue
            seen = set()
            for pin, layer, x1, y1, x2, y2 in pin_rects[name]:
                if pin not in nets or pin in seen or layer not in label_ld:
                    continue
                seen.add(pin)
                cx = int(round((x1 + x2) / 2 / dbu))
                cy = int(round((y1 + y2) / 2 / dbu))
                top.shapes(ly.layer(*label_ld[layer])).insert(
                    db.Text(nets[pin], db.Trans(tr * db.Point(cx, cy))))
                n_net_labels += 1
    print(f"[INFO][LVS] {n_net_labels} net-name labels placed on the SRAM pins")

    # 1. drop the fill (density fill sits in the wrapper, spacers in the design cell)
    n_fill = 0
    for cell in [c for c in (top, wrapper) if c is not None]:
        insts = [i.cell_inst for i in cell.each_inst()]
        keep = [ci for ci in insts if not matches(ly.cell(ci.cell_index).name, FILL_PATTERNS)]
        if len(keep) != len(insts):
            n_fill += len(insts) - len(keep)
            cell.clear_insts()
            for ci in keep:
                cell.insert(ci)
    print(f"[INFO][LVS] {n_fill} fill instance(s) dropped")

    # 2. flatten the via cells into the top cell, in bulk (per-instance flatten
    # costs ~0.2 s each at this size)
    insts = [i.cell_inst for i in top.each_inst()]
    vias = [ci for ci in insts if matches(ly.cell(ci.cell_index).name, VIA_PATTERNS)]
    if vias:
        keep = [ci for ci in insts if not matches(ly.cell(ci.cell_index).name, VIA_PATTERNS)]
        scratch = ly.create_cell("__lvs_via_scratch")
        for ci in vias:
            scratch.insert(ci)
        top.clear_insts()
        for ci in keep:
            top.insert(ci)
        scratch.flatten(1, False)
        top.copy_shapes(scratch)
        ly.delete_cell(scratch.cell_index())
    print(f"[INFO][LVS] {len(vias)} via instance(s) flattened into {top.name}")

    # 5. dissolve the seal-ring wrapper
    if wrapper is not None:
        for inst in list(wrapper.each_inst()):
            if inst.cell_index == core.cell_index():
                inst.flatten(1)        # one level: the design's own sub-cells stay cells
        ly.delete_cell(core.cell_index())
        wrapper.name = design
        top = wrapper
        print(f"[INFO][LVS] dissolved wrapper {design}_sealed, top cell is {design}")

    # cells that nothing uses any more (fill, vias)
    removed = 0
    while True:
        orphans = [c for c in ly.top_cells() if c.cell_index() != top.cell_index()]
        if not orphans:
            break
        for c in orphans:
            ly.delete_cell(c.cell_index())
            removed += 1
    print(f"[INFO][LVS] {removed} unused cell(s) removed")

    ly.write(out_gds)
    print(f"[INFO][LVS] wrote {out_gds} (top cell {top.name}, {ly.cells()} cells)")


if __name__ == "__main__":
    main()
