# Signoff DRC: known gap and the fix

**Status: every MANDATORY rule passes on the flow's own output; a residual
set of RECOMMENDED-only pad findings remains, root-caused and not fixable by
the P&R flow (see the last sections).**

`run_drc.sh --minimal` (what CI runs) passing is **not** enough to know a GDS
is fabricable. Full signoff DRC exercises rules minimal mode explicitly
disables, and on the stock flow output it found 9 real, mandatory metal-density
violations (`M1Fil.h`/`M2Fil.h`/`M3Fil.h`) plus a block of non-blocking
recommended pad findings. The density violations are now fixed **inside the
official flow**: `run_finishing.sh --fill-metal --fill-activ` writes
`klayout/out/<PROJ_NAME>.filled.gds.gz`, and `run_drc.sh --full` checks that
file automatically.

```sh
cd klayout
./run_finishing.sh --gds --seal --fill-metal --fill-activ   # -> out/croc.filled.gds.gz
./run_drc.sh --full --jobs 4                                # signoff DRC, ~2 h
```

(`scripts/run_signoff_fill.sh` is just a convenience wrapper around the first
line.)

## Symptom

Running full signoff DRC (density, antenna, and all `sg13cmos5l_maximal.drc`
rules) against the output of the *stock* `run_finishing.sh --fill-metal
--fill-activ` reports:

```
Violated rules are: {'M1Fil.h', 'Pad.fR_M4', 'M3Fil.h', 'M2Fil.h', 'Pad.d1R',
                      'Pad.fR_M3', 'Pad.fR_TM1', 'Pad.fR_M2', 'Pad.dR'}
```

Two very different classes of thing are bundled in that:

| Rules | Count | Class | Blocking? |
| --- | --- | --- | --- |
| `Pad.fR_*`, `Pad.d1R`, `Pad.dR` | 873 | **RECOMMENDED** (`if RECOMMENDED` / `if $recommendedRules` in the rule decks) | No: pad guidelines, not hard rules |
| `M1Fil.h`, `M2Fil.h`, `M3Fil.h` | 9 | **MANDATORY**, ungated | **Yes** |

## What `MxFil.h` checks

`5.18. MxFil.h: Min. MetalN and MetalN:filler coverage ratio for any
800.0 x 800.0 um2 chip area: 25.00%`. This is a sliding-window density check.
The design's *global* density passes on every layer (M1 39.8%, M2 38.4%, M3
48.0%, all against a 35-60% band) but a handful of 800x800 um windows,
clustered in the routing-dense middle of the die, dip under 25% coverage.

## Root cause: confirmed upstream, still open

This is not a croc bug. It is a documented, unfixed limitation in the IHP PDK:

**[IHP-Open-PDK #438, "Relax metal filler rules"](https://github.com/IHP-GmbH/IHP-Open-PDK/issues/438)**
(open as of this writing) describes the exact failure:

> Each routing layer has a preferred direction, which makes it difficult to
> insert square filler shapes. [...] I had to make five passes of the smallest
> fill shape with different offsets to get it to meet the density limit.

`ihp13/sg13cmos5l/libs.tech/klayout/tech/macros/sg13cmos5l_filler_Metal.lym`
(the vendor filler `--fill-metal` calls) does **exactly one** grid-aligned fill
pass per layer (a "medium" cell, then one grid pass of the 1x1 um minimum cell
over the leftover). In a routing-dense digital design that single fixed-origin
grid cannot tile irregular leftover slivers, leaving the gaps this rule catches.

Two comments on #438 discuss an upstream fix (a differently-sized filler tier,
as Magic does with 3 size tiers), not yet merged. The reporter's own practical
workaround, **repeating the smallest-cell fill at different grid offsets until
the leftover closes, is what is implemented here**: it needs no PDK change and
no waiting on upstream.

Two adjacent-looking issues were checked and ruled out: **#911** (nofill margin)
produces `MxFil.c` spacing-to-macro violations, not `MxFil.h`; **#1076**
(exclude density fill from device) is analog-specific.

## The fix (all in the official flow)

1. **`klayout/run_finishing.sh`**: `fill_metal()`/`fill_activ()` now set
   `PDK_ROOT`/`PDK` for the vendor filler macro. Upstream builds a path
   `$PDK_ROOT/$PDK/libs.tech/klayout/...` that only exists under ETH's
   cockpit layout; with the public `ihp13/pdk` mirror `--fill-metal` fails
   outright without this. After the vendor fill (`croc.vendorfilled.gds.gz`),
   `fill_activ()` calls the supplemental pass below and writes
   **`croc.filled.gds.gz`**, the signoff-ready file.
2. **`klayout/scripts/extra_metal_fill.py`**: re-derives the same "fillable"
   region the vendor macro computes (`EdgeSeal.holes` minus metal/filler +0.42 um
   minus nofill/slit/nomet) and tiles the 1x1 um minimum cell into it across up
   to 8 grid origins, stopping early once nothing is left. Each pass re-derives
   its exclusion from scratch **including a 0.42 um margin around everything
   placed by earlier passes**, so filler-to-filler spacing (`MxFil.b`) holds
   across different grid origins. New shapes go on the same (layer, 22) filler
   layer the vendor script uses, so DRC treats them identically. Only M1/M2/M3
   need it (M4 and TopMetal1 already pass). The vendored PDK is untouched.
3. **`klayout/run_drc.sh`**: in `--full` mode it now picks
   `out/<PROJ_NAME>.filled.gds.gz` automatically (warning loudly and falling back
   to the unfilled sealed GDS if it does not exist). `--minimal` is unchanged
   (it disables density by design).

Runtime of the supplemental pass: about 8 minutes for all three layers (it
prints elapsed time per phase and per pass). Do not add an in-script
`Region.space_check()` self-check: an earlier version had one, it over-reports
(merge-boundary notches the real rule does not flag), and on M1 (about 775k um^2
of fill) it never returns, which stalled the flow for hours.

### Leftover area closed, per layer (this design)

| Layer | Before | After |
| --- | --- | --- |
| M1 | 1,066,942 um^2 | **0** |
| M2 | 1,006,053 um^2 | 469,053 um^2 (reduced, not exhausted; enough to clear the density rule) |
| M3 | 986,103 um^2 | **0** |

(Measured on a fresh-clone run, see below. The whole supplemental pass takes
about 8 minutes; the vendor fill before it about 20.)

M2's leftover is not fully closed, but the acceptance test is the real density
rule, not leftover area, and it passes.

## Verification: real DRC engine, not a proxy

The exact rule code was pulled from `sg13cmos5l_maximal.drc` and run standalone
through KLayout's real DRC engine against the filled GDS:

```ruby
Metal1_filler.width(1.0.um)    # M1Fil.a1   (same for Metal2/Metal3)
Metal1_filler.space(0.42.um)   # M1Fil.b
```

Result: **0 violations** for `.a1` and `.b` on all three layers, and the density
table (`run_drc.py --table density`) passes. Full signoff (`run_drc.py
--antenna`, every table incl. the `maximal` deck):

| | Total violations | Mandatory (`MxFil.*`) | Recommended (`Pad.*`) |
| --- | --- | --- | --- |
| Stock flow output | 882 | 9 | 873 |
| **With the fix** | **873** | **0** | 873 |

Precisely the 9 mandatory density violations removed, and no new violation
anywhere else in the deck.

## Fresh-clone verification

The claim "a fresh checkout gives a clean result" was tested for real: a
pristine copy of the repo (tracked files + the changes above, **no** generated
outputs, stock `04_routing.tcl`) was run end to end with only the official
scripts: Yosys, OpenROAD (`run_backend.sh --all`), `run_finishing.sh --gds
--seal`, minimal DRC, `run_finishing.sh --fill-metal --fill-activ`,
`run_drc.sh --full --jobs 4`.

| Stage | Result | Wall time |
| --- | --- | --- |
| Yosys synthesis | OK | 7 min |
| OpenROAD floorplan -> finishing | OK, 0 `[ERROR]` in any log, **0 detailed-route violations** (final `04_croc_route_drc.rpt` is empty) | 160 min |
| DEF -> GDS + seal ring | OK | < 1 min |
| `run_drc.sh --minimal` (what CI runs) | **Passed**, no violations | 13 min |
| Vendor fill + supplemental fill -> `croc.filled.gds.gz` | OK | 24 min |
| `run_drc.sh --full` (all tables incl. `maximal`, antenna) | **0 mandatory violations**; 873 recommended `Pad.*` | 122 min |

Full-DRC violation breakdown on that fresh run (identical to the earlier
verified run):

| Rule | Count | Class |
| --- | --- | --- |
| `Pad.fR_M2` / `_M3` / `_M4` / `_TM1` | 581 / 64 / 64 / 88 | recommended |
| `Pad.d1R` | 72 (18 per die side) | recommended |
| `Pad.dR` | 4 (one repeated location, south edge) | recommended |
| **Anything else** (density, spacing, width, antenna, FEOL, offgrid, ...) | **0** | mandatory |

Notes from that run worth knowing:

- **Detailed routing plateaus, then clears.** The router goes 26606 -> 16356 ->
  15130 -> 539 -> 61 -> 10 -> 4 -> 2 violations, then sits at 2 (a Metal2
  signal-vs-clock spacing marker at the same spot) for about ten iterations
  (~1 h) before its final repair pass clears it to 0. This is deterministic
  (the same trajectory occurred in two independent runs) and the run is not
  stuck: leave it alone. Total routing is about 2.5 h.
- The supplemental fill finished in 8 minutes for M1+M2+M3.


## The remaining recommended `Pad.*` findings: not fixable by P&R

| Rule | Count | What it is |
| --- | --- | --- |
| `Pad.fR_M2` / `_M3` / `_M4` / `_TM1` | 581 / 64 / 64 / 88 | recommended 7 um straight metal exit length into the bond-pad opening |
| `Pad.d1R` | 72 | recommended pad-to-Activ (inside chip) space of 11.2 um |
| `Pad.dR` | 4 | recommended pad-to-EdgeSeal space of 25 um |

All of these are guidelines (wire-bond mechanical reliability and bonding
procedure), gated behind `RECOMMENDED` in the rule decks. The mandatory
neighbours (`Pad.d` pad-to-seal 7.5 um, `Pad.a1`, ...) pass.

### Root cause (measured, not assumed)

- Counts are exactly **16 / 16 / 16 / 16 per padring side** for M3 and M4 (22
  per side for TopMetal1): a fixed construction detail, one finding per pad.
- Every sg13cmos5l IO cell's own `pad` LEF pin is a **fixed 70 x 3 um
  rectangle** (`RECT 5.0 0.0 75.0 3.0`, `ihp13/pdk/lef/sg13cmos5l_io.lef`) on
  Metal2/Metal3/Metal4/TopMetal1. The bond pad cell (`bondpad5l_70x70`, a solid
  70x70 um square) is merely *placed* on it by `place_bondpad`
  (`openroad/src/padring.tcl`); no connector is generated by any croc script.
  A 3 um exit can never satisfy a 7 um exit-length rule, and a router cannot
  lengthen a hard macro's own pin. The M2 count additionally includes the IO
  cell's internal M2 routing.

- `Pad.d1R` is exactly 18 per die side on all four sides, with the pad edge
  about 8 um from the IO cell's own Activ (rule wants 11.2 um). Same
  signature: fixed vendor-cell geometry. Moving the bond pad outward to gain
  the 3 um would detach it from the cell's fixed 70 x 3 um `pad` pin, so it is
  not a usable knob. (Inferred from the violation coordinates and the
  `place_bondpad -offset {5.0 -70.0}` geometry; not separately experimented.)
- `Pad.dR` is a single location on the south edge reported four times: the pad
  opening is 7.7 um from the seal ring's inner edge against the recommended
  25 um. The *mandatory* `Pad.d` (7.5 um) passes, but with only 0.2 um of
  margin. Keep that in mind if anything near the padring or the
  `SEAL_RING_SPACE` / die-size parameters in `run_finishing.sh` is changed.

### What was tried: pad-net NDR on Metal2 (no effect)

The plausible router-side lever is a non-default rule giving the nets that
touch `pad_*` instances a wider Metal2 (`create_ndr` + `assign_ndr` in
`04_routing.tcl`, before `global_route`). It was implemented and measured:

- At 2.0 um width it left 7 detailed-route "Minimum Cut" violations: the tech
  LEF has `MINIMUMCUT 2 WIDTH 1.4` on Metal2/3/4 (anything >= 1.4 um wide needs
  2-cut vias) and this PDK defines no named VIARULE, so `create_ndr -via` fails
  (ODB-1008).
- At 1.2 um width (below the multi-cut threshold) routing converged to 0
  violations, the NDR landed on 109 routed pad nets (confirmed in the DEF), and
  full re-route, finishing and the pad DRC table were re-run.
  **`Pad.fR_M2/M3/M4/TM1` came out 581 / 64 / 64 / 88, identical to the no-NDR
  baseline to the digit.**

So the violations are fully determined by the vendor cell geometry and the
router has no influence on them. The NDR was therefore **removed again**: it
adds routing risk and runtime for zero measured benefit. `04_routing.tcl` is
stock.

### Realistic ways to close them (none are P&R tuning)

- Use a bond-pad / IO-cell variant whose pad pin is at least 7 um long
  (a PDK/IP change; worth raising upstream: the 3 um pin vs the 7 um
  recommended exit rule is inconsistent within the PDK itself).
- Accept them: the foundry rule is a recommendation, and this pad ring
  construction (fixed-offset bond pad on the vendor IO cell) is what the
  upstream croc flow ships unmodified. Whether a given shuttle accepts them is
  the foundry's decision; see "Is this GDS ready to submit?" below.
- A GDS-level patch (adding metal under each bond pad) is possible in principle
  but would have to be spacing-checked 873 times next to power rails, ESD
  structures and neighbouring pads; poor risk/benefit for a non-blocking rule.

## Is this GDS ready to submit?

**All three of IHP's physical-verification steps have a result; whether the
`Pad.*` recommended findings are acceptable is a question for the shuttle
organiser.**

IHP's submission guidance for its Open-Silicon-MPW runs
([slides, 19 March 2026](https://ieee-cas.org/files/ieeecass/slides/sumbissiontoopen-silicon-mpw-ihp.pdf),
repository <https://github.com/IHP-GmbH/Open-Silicon-MPW>) names SG13CMOS5L as
a supported process and lists three physical-verification steps, all using the
PDK's own runners:

| IHP step | What it runs | Status of this layout |
| --- | --- | --- |
| Minimal pre-check DRC | `run_drc.py --precheck_drc --no_offgrid --no_angle --disable_extra_rules --no_recommended` | **Passed, 0 violations**, run with exactly these flags on `croc.filled.gds.gz` (71 min with 6 workers), on the final layout including the SRAM power fix. croc's `run_drc.sh --minimal`, which uses similar but not identical flags, also passes. |
| Regular DRC | `run_drc.py` with all rules, recommended ones included | 873 findings, every one a recommended `Pad.*` rule (this document) |
| Regular LVS | `run_lvs.py --layout=<gds> --netlist=<cdl>` | **Passed**, with a caveat: 65 of 65 circuit types match in KLayout and a structural cross-check finds the standard-cell connectivity identical, but KLayout's own top-level matcher leaves about 1% of pairs unresolved (interchangeable cells). `klayout/run_lvs.sh`; details in [LVS.md](LVS.md). |

What that means for the 873 recommended findings:

- The pre-check **turns recommended rules off** (`--no_recommended`; the PDK
  describes `--precheck_drc` as "a minimal set of DRC checks typically required
  for foundry prechecks"), so the `Pad.*` findings do not appear in it.
- They do appear in the regular DRC. The guidance does not say whether
  recommended-rule findings there are tolerated, so **ask the foundry or shuttle
  organiser before relying on it**. They cannot be removed from this flow
  anyway (see the previous section): closing them means different IO / bond pad
  cells, not more place-and-route.

Two things to know about the LVS result before relying on it. IHP's guidance
runs the PDK's `run_lvs.py` against a schematic netlist; this flow adds a
wrapper (macros compared by pin name, fill and via cells left out of the view)
because the deck does not finish on this design otherwise, and the verdict
rests on a second check where KLayout's matcher stops. Both are explained in
[LVS.md](LVS.md); if the shuttle runs its own LVS with the deck as shipped,
expect that run to need the same help.

Other things a submission needs that are outside this document: the run's
metadata (PDK commit, process, top cell name, seal ring size, paths to the final
GDS and the LVS netlist) and an open-source licence. Other routes to silicon
have their own acceptance checks.

## If you touch the flow

- Fill depends on the actual routing pattern. After any change that alters the
  layout (floorplan, routing, added `user_domain` logic), re-run
  `run_finishing.sh --gds --seal --fill-metal --fill-activ` and then
  `run_drc.sh --full`.
- For another project variant: `PROJ_NAME=croc_idma ./run_finishing.sh ...`
  (not yet run for that variant).
- Plain `run_drc.sh --full` without the fill step will warn and check the
  unfilled GDS, and then report `MxFil.h` failures. That is expected.
