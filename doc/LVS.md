# LVS: does the layout implement the netlist?

DRC asks "can this be manufactured". LVS asks "is it the circuit we meant": it
extracts the devices and connections from the final GDS and compares them with
the netlist OpenROAD routed. A layout can pass every DRC check and still be wrong
(a macro without power, a swapped pad), so a submission needs both.

```sh
cd klayout
./run_lvs.sh                  # uses out/croc.filled.gds.gz and ../openroad/out/croc.odb
croc-view lvs-log             # verdict
croc-view lvs                 # KLayout + Tools > Netlist Browser, for mismatches
```

Results are in `klayout/lvs-run/`. Takes about 15 minutes on a 12-core machine;
the extraction is single-threaded.

## What is compared, and what the verdict means

IHP's own deck (`libs.tech/klayout/tech/lvs/sg13cmos5l.lvs` in the PDK) does the
extraction and the device-level comparison, unmodified. `run_lvs.sh` prepares its
inputs and adds one check on top, because KLayout's top-level matcher cannot
close this design on its own (see "Why there is a second check").

| Check | What | Result on this design |
| --- | --- | --- |
| KLayout, circuit types | every standard cell compared transistor by transistor; every macro compared by its named pins | 65 of 65 circuit types match |
| KLayout, named ports | the 52 top-level ports and the SRAM pin nets, by name | all match |
| Structural check (`scripts/lvs_structural.py`) | every standard-cell instance and net of the layout against the netlist, with cell type, pin names and anchor nets fixed | identical: 34,200 instances, 318 anchor nets, 12 refinement rounds |
| KLayout, top-level pairing | pairing of the 34k instances / 33k nets | about 99% paired; the rest unresolved, see below |

`run_lvs.sh` ends with `PASSED` if KLayout's own top-level comparison passes, or
if all circuit types match in KLayout **and** the structural check finds the two
netlists identical. It fails otherwise.

### Why there is a second check

KLayout pairs the instances of the top circuit by the topology around them. Two
things defeat that on this design:

- `sg13g2_buf_2`, `buf_4` and `buf_8` (and other drive-strength variants) are the
  same transistor graph, so KLayout treats them as interchangeable and can pair a
  layout `buf_4` with a netlist `buf_2`. The deck does not compare transistor
  widths.
- Groups of identical sub-structures (a driver and a load per SRAM data bit,
  repeated 32 times per bank) have no names in the layout, which carries only
  top-level port labels.

The result is a few hundred unpaired nets and instances (about 0.6% of the
instances) around the SRAM interface, in an otherwise matching netlist. The
structural check compares what KLayout leaves open and is stricter in the one
respect that matters here: it keeps the cell type, so a layout `buf_4` where the
netlist has `buf_2` is a difference.

It is a Weisfeiler-Leman colour refinement on the instance/net graph of both
sides. A swapped, opened or shorted connection changes the colouring at the first
round. This was checked: injecting a swap of one gate input, an open net, or a
short into the extracted netlist is reported in round 1 and names the gates
involved, while the unmodified pair is identical through 12 rounds. It is not a
proof of graph isomorphism, but for netlists with this many anchors a
non-isomorphic pair that survives 12 rounds is not a realistic failure mode.

## How the inputs are prepared

| Step | What | Why |
| --- | --- | --- |
| `scripts/lvs_stubs.py` | an empty subcircuit per hard macro (IO cells, SRAM, bond pad), pin names and order from the LEF | the schematic needs every macro by name and pins |
| OpenROAD `write_cdl` | the netlist of the routed database, with the PDK's standard-cell CDL | the schematic side |
| `scripts/lvs_layout.py` | the final GDS, with fill dropped, via cells flattened, macro pins labelled, wrapper dissolved | the layout side |
| wrapper deck | blanks the macros, then includes the PDK deck | macros are compared by pin name |
| post-extraction hook | removes substrate pins, opens floating outputs, folds tie cells | see below |

- **Macros are black boxes.** Their pins are compared by name; their insides are
  not. IHP's own IO cells and SRAM macro do not pass IHP's own LVS deck when
  checked alone (ESD primitives in the IO cells, a dummy delay cell in the SRAM),
  and are verified by IHP as vendor IP. They are blanked **before** the deck's
  first netlist step: blanking after the deck's own purge silently drops the
  macros from both sides and the comparison becomes vacuous for every pad and
  SRAM pin.
- **Fill is dropped from the layout view.** Standard-cell fill and the density
  fill added by the finishing step (about 170k instances) have no devices and are
  isolated from every wire by the DRC spacing rules. As empty circuits they make
  KLayout's netlist purge quadratic.
- **Via cells are flattened.** The DEF reader makes one shape-only cell per via
  type, one instance per routing via (about 270k). Same purge problem; the
  geometry is unchanged (checked on Via1: 2,662,294 shapes before and after).
- **Substrate pins are removed from the macros.** The deck derives a
  substrate/well region for every cell from its extent, which leaves the cell as
  unnamed pins; the vendor schematic of a macro has none.
- **Floating outputs are opened on both sides.** An output left open on purpose
  is an explicit single-pin net in OpenROAD's netlist and no net in the layout.
- **Tie cells are folded into the supply nets on both sides.** Hundreds of
  identical cells each driving one unnamed net form groups KLayout cannot pair.
  A tie cell only makes a constant. A tie net shorted to a signal net still
  shows: that signal moves onto the supply on one side only.
- **The SRAM pins carry net names.** The two banks are identical macros and the
  layout has no instance names, so each SRAM pin is labelled with the net the
  routed netlist connects to it (positions matched by instance origin).

The same hook writes the layout netlist as extracted, before the normalisations
above, for the structural check.

## What it found: the SRAM had no power

The first end-to-end run showed that **the two SRAM macros were not connected to
the power grid** in the golden layout. DRC could not see this.

The 5-layer power-grid script (`openroad/scripts/power_grid_sg13cmos5l.tcl`)
draws TopMetal1 stripes across the SRAMs and hopes to hit their Metal4 supply
pins, but defined no macro grid, so `pdngen` placed no via there. In the
OpenROAD database `check_power_grid` reported 136 unconnected nodes on VDD and 72
on VSS, all inside the two SRAM footprints, and the `VDD!`, `VDDARRAY!` and
`VSS!` pins of both banks floating.

Fix (in `power_grid_sg13cmos5l.tcl`): a macro grid for the SRAM, as the 7-layer
script already has:

```tcl
define_pdn_grid -macro -name sram_grid -cells {RM_IHPSG13_1P_512x32_c2_bm_bist} \
    -grid_over_pg_pins -starts_with POWER
add_pdn_connect -grid {sram_grid} -layers {Metal4 TopMetal1}
```

After the fix `check_power_grid` reports "All shapes on net VDD are connected"
and the same for VSS, on the full flow's final database. `05_finishing.tcl` now
runs that check on every build, so a regression shows up as `[ERROR PSM-0069]`
in the OpenROAD log, which CI's log scan already fails on. Full DRC on the fixed
layout is unchanged: the same 873 recommended `Pad.*` findings.

## Reading a failure

1. `croc-view lvs-log`: the verdict, circuit-type results and the structural
   check.
2. `lvs-structural.txt` names the first layout and netlist instances that have no
   counterpart. Those are where the connection differs.
3. `croc-view lvs`: **Tools > Netlist Browser**; circuits that did not match are
   marked, expand one for unmatched nets, devices and pins.
4. A pin-name mismatch on a macro usually means a supply or signal net that did
   not reach it. Check `check_power_grid` first.
5. `lvs.log` has the deck's own messages; extraction errors such as
   `Must-connect subnet ... does not have any pin` name the cell and the shape.

## Limits

- KLayout's own top-level matcher does not give a clean result on this design
  (above). If a future KLayout or deck version resolves it, the structural check
  stays as a second opinion.
- The structural check covers standard cells and their connections. Hard macros
  are covered by pin name only.
- The LVS netlist comes from OpenROAD's database, not from the synthesis
  netlist. Equivalence of those two is the job of the Yosys/OpenROAD flow's own
  checks, not of LVS.
