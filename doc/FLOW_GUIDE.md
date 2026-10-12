# Croc flow: run each step, then look at what it made

A short, practical walkthrough. Every step has **what it does**, **how to run it**,
and **how to see the result**. Work top to bottom the first time.

All tools live in `/opt/tools` and are on your `PATH` automatically in any new
terminal. To check:

```sh
yosys -V && openroad -version && klayout -v && verilator --version
```

The viewer helper is `croc-view` — run `croc-view list` to see every target.

> `croc-view` and everything under `/opt/tools` (including the
> `install-scripts/` mentioned below) belong to the workstation setup this
> guide was written on; they are **not** part of this repository. On another
> machine, open the same files directly in the OpenROAD GUI / KLayout.

---

## The flow in one picture

```
 sw/          Bender          Yosys            OpenROAD           KLayout
 C code   →   file list   →   netlist      →   layout         →   GDS (masks)
 .hex         croc.flist      croc_yosys.v     croc.def           croc.sealed.gds.gz
              ↓                                ↓                  ↓
              Verilator simulation             5 stages           fill + DRC
```

Runtimes on a 12-core machine: software seconds, simulation ~10 min,
synthesis ~10 min, **place & route ~2.5-3 h**, GDS ~1 min, DRC ~15 min
(minimal) or ~2 h (signoff). Measured numbers are in the table near the end.

---

## 0. Software — the program the chip will run

**What it does:** cross-compiles C to RISC-V and produces `.hex` files the
testbench loads into the chip's memory.

```sh
cd sw && make
```

**See the result**

```sh
ls bin/ bin/test/                 # helloworld.hex + 8 test programs
head -3 bin/helloworld.hex        # raw bytes the simulator loads
less bin/helloworld.dump          # human-readable disassembly
```

**Good sign:** `bin/helloworld.hex` exists and `.dump` shows RISC-V instructions.

---

## 1. Simulation — does the design actually work?

**What it does:** compiles the whole SoC into a fast C++ simulator, boots it over
JTAG, loads your program and runs it. This is the fastest way to know the design
is functionally correct — do this before any physical work.

```sh
cd verilator
./run_verilator.sh --build                       # ~10 min, only when RTL changes
./run_verilator.sh --run ../sw/bin/helloworld.hex
```

**See the result**

```sh
croc-view sim-log                 # the UART/JTAG transcript
croc-view waves                   # GTKWave: every signal over time
```

**Good sign:** `[UART] Hello World from Croc!` then
`[JTAG] Simulation finished: SUCCESS`.

Run the full test suite the way CI does:

```sh
.github/scripts/run_sim_flow.sh --rtl      # both phases + 8 unit tests
```
Expect `Summary: 8 passed, 0 failed`.

**In GTKWave:** pick signals in the left tree, drag into the wave window.
Start with `clk_i`, `rst_ni`, and the UART pins.

---

## 2. Synthesis — turn RTL into real logic gates

**What it does:** Yosys reads your SystemVerilog, optimises it, and maps it onto
actual IHP 130nm cells (NAND, flip-flops, …). Output is a *netlist*: a list of
cells and the wires between them.

```sh
cd yosys && ./run_synthesis.sh --synth        # ~10 min
```

**See the result**

```sh
croc-view area                    # total chip area + flop count
less reports/croc_area.rpt        # area per module and per cell type
grep -c tc_sram_blackbox out/croc_yosys.v    # MUST be 0
```

**Good sign:** `Found and reported 0 problems.` and **0 blackboxes**.
A blackbox means an SRAM size you asked for does not exist in the PDK.

**Numbers to remember for later comparison:** total area, flop count
(`sg13g2_dfrbpq_1`), and your module's own area line.

---

## 3. Place & route — arrange the gates and wire them up

**What it does:** OpenROAD turns the netlist into real geometry, in 5 stages.
This is the long one (~2.5-3 h).

| Stage | What happens |
| --- | --- |
| 01 floorplan | fix the die size, place the IO ring and SRAM macros, build the power grid |
| 02 placement | put every standard cell somewhere sensible, fix timing |
| 03 CTS | build the clock tree so the clock reaches all 4275 flops together |
| 04 routing | draw the actual metal wires; repair antenna + DRC violations |
| 05 finishing | add filler cells, connect power, write the final files |

```sh
cd openroad
./run_backend.sh --all            # all 5 stages
# or one at a time (each resumes from the previous checkpoint):
./run_backend.sh --floorplan
./run_backend.sh --placement
./run_backend.sh --cts
./run_backend.sh --routing
./run_backend.sh --finishing
```

**See the result — this is the interesting part**

```sh
croc-view floorplan     # stage 01
croc-view placed        # stage 02
croc-view cts           # stage 03
croc-view routed        # stage 04  (final geometry)
croc-view final         # stage 05  (the finished design)
croc-view images        # 20 PNGs the flow rendered, no GUI needed
```

**In the OpenROAD GUI:**
- left **Display Control** panel toggles layers — turn Metal2/3/4 on and off
- **Heat Maps** → *Placement Density* shows crowding, *Routing Congestion*
  shows where wires are fighting for space
- **Clock Tree Viewer** shows the clock distribution
- **Timing** → *Timing Report* lets you click a slow path and see it highlighted

**Good sign:**

```sh
grep -c '\[ERROR' logs/*.log             # all zero
wc -c reports/04_croc_route_drc.rpt      # 0 bytes = no routing violations
```

The `[WARNING RSZ-0062] Unable to repair all setup violations` message is
expected — the SDC asks for 100 MHz, which is aggressive for this technology.
The simulation testbench runs at 20 MHz.

---

## 4. GDS — the file the foundry actually receives

**What it does:** merges the layout with the real cell geometry and adds a seal
ring (a protective metal frame around the die edge).

```sh
cd klayout
./run_finishing.sh --gds --seal
```

**See the result**

```sh
croc-view sealed        # KLayout on the chip + seal ring (fill is added in step 5)
croc-view seal          # just the seal ring
ls -la out/
```

**In KLayout:** the layer list is on the right — click a layer to hide/show it.
Press `*` to fit the view, `F2` to zoom to selection. Drag a box to zoom.

**Good sign:** die is 1916 x 1916 um, with seal ring 2000 x 2000 um.

---

## 5. DRC — are the shapes legal to manufacture?

**What it does:** checks the layout against the foundry's geometric rules
(minimum widths, spacings, densities).

There are two levels, and the difference matters:

```sh
cd klayout
./run_drc.sh --minimal     # fast (~15 min). Angle + precheck only. CI uses this.
./run_drc.sh --full        # slow. FEOL, BEOL, offgrid, density, antenna.
```

**Minimal DRC is NOT a tapeout check.** It explicitly disables density, FEOL,
offgrid and recommended rules. Use it while iterating; use `--full` before
committing to silicon.

**See the result**

```sh
croc-view drc-log       # pass/fail summary
croc-view drc           # KLayout with violation markers loaded
```

**In KLayout with markers:** **Tools → Marker Browser** lists every violation;
double-click one and the view jumps straight to it.

**Good sign:** `KLayout DRC Check Passed: No DRC violations detected`.

### Before real fabrication: fill + signoff DRC

Density rules require *fill* — dummy metal added in empty areas so the wafer
polishes evenly — and minimal DRC (above) explicitly skips density, so
passing it tells you nothing about fill. Run:

```sh
cd klayout
./run_finishing.sh --gds --seal --fill-metal --fill-activ   # ~25 min -> out/croc.filled.gds.gz
./run_drc.sh --full --jobs 4                                # ~2 h signoff DRC
```

The fill step does the vendor's metal + activ fill, then a supplemental pass
that closes gaps the vendor's single-pass filler leaves in routing-dense areas
(a known, still-open upstream PDK limitation — see
**[doc/DRC_SIGNOFF.md](DRC_SIGNOFF.md)** for the full story, root cause, and
verification). `out/<PROJ_NAME>.filled.gds.gz` is the DRC-signoff GDS, the
file you would run LVS on and submit, and `run_drc.sh --full` checks it
automatically (`--minimal` keeps checking the unfilled `.sealed.gds.gz`, by
design).

**Expected signoff result:** zero *mandatory* violations. A block of
RECOMMENDED-only `Pad.*` findings (bond-pad exit length / spacing) remains by
design — they come from the vendor IO-cell pin geometry and cannot be changed
by place-and-route; see DRC_SIGNOFF.md.

**DRC is not the whole signoff.** This flow writes an LVS netlist
(`openroad/out/croc_lvs.v`) but does not run LVS; a foundry submission needs
it. See "Is this GDS ready to submit?" in DRC_SIGNOFF.md.

---

## What to expect: run times (measured, 12-core / 15 GB machine)

| Step | Time |
| --- | --- |
| Yosys synthesis | ~7 min |
| OpenROAD floorplan -> placement -> CTS | ~12 min |
| OpenROAD routing (global + detailed) | ~2.5 h |
| DEF -> GDS + seal ring | < 1 min |
| Minimal DRC (CI) | ~13 min |
| Fill (vendor + supplemental) | ~25 min |
| **Full signoff DRC** | **~2 h** |

**Don't kill detailed routing when it sits at "Number of violations = 2" for
an hour.** It converges 26606 -> ... -> 4 -> 2, plateaus for ~10 iterations,
then its final repair pass clears to 0. It is deterministic, not stuck. Use
`openroad/reports/04_croc_route_drc.rpt-*.rpt` to see what the remaining
markers are; the final `04_croc_route_drc.rpt` is empty when routing is clean.

The whole chain can be run unattended: sim/synth/P&R/GDS via
`.github/scripts/run_full_flow.sh`, then
`klayout/run_finishing.sh --fill-metal --fill-activ` and
`klayout/run_drc.sh --full --jobs 4`.

---

## Doing it all in one go

```sh
.github/scripts/run_sim_flow.sh --rtl      # simulation
.github/scripts/run_synth_flow.sh          # synthesis (both configs)
.github/scripts/run_full_flow.sh           # synth + P&R + GDS + minimal DRC
```

---

## After you add your own logic

Put your design in `rtl/user_domain/`, add the files to `Bender.yml`, then:

```sh
cd yosys     && ./run_synthesis.sh --flist
cd ../verilator && ./run_verilator.sh --flist
```

To see exactly what your logic changed versus the known-good baseline:

```sh
/opt/tools/install-scripts/golden-capture.sh mychange
/opt/tools/install-scripts/golden-diff.sh baseline mychange
```

**Should change:** area, netlist, DEF, GDS, images.
**Should NOT change:** tool versions, the 8 unit tests, software `.hex` files,
`croc_domain` module areas, DRC violation count.

If a `croc_domain` number moved, your change touched more than you intended.

---

## Quick troubleshooting

| Symptom | Cause |
| --- | --- |
| `No module named 'klayout'` | `PYTHONPATH` missing `/opt/tools/klayout/pymod` |
| GUI never opens | `QT_QPA_PLATFORM=offscreen` is set; unset it |
| `tc_sram_blackbox` in netlist | requested an SRAM size the PDK lacks |
| Routing violations stuck | let it finish — the count can sit flat then clear late |
| `flist-plus` unknown | bender too old (0.28.0 lacks it; 0.29.1 works) |
