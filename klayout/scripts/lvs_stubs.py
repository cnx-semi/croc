#!/usr/bin/env python3
"""Write empty (black-box) CDL subcircuits for hard macros from their LEF pins.

Hard macros (IO cells, SRAMs, bond pads) are verified at their pins, not
inside. For LVS the schematic only needs each macro's name and pin list, which
the LEF already has.

Usage: lvs_stubs.py <out.cdl> <macro.lef> [<macro.lef> ...]
"""
import re
import sys


def lef_macros(path):
    text = open(path).read()
    for m in re.finditer(r'^MACRO\s+(\S+)(.*?)^END\s+\1\s*$', text, re.S | re.M):
        pins = [p.group(1) for p in re.finditer(r'^\s*PIN\s+(\S+)', m.group(2), re.M)]
        yield m.group(1), pins


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    out, lefs = sys.argv[1], sys.argv[2:]
    seen = set()
    with open(out, 'w') as f:
        f.write("* Black-box stubs generated from LEF pin lists (no devices on purpose).\n")
        for lef in lefs:
            for name, pins in lef_macros(lef):
                if name in seen:
                    continue
                seen.add(name)
                f.write(f".SUBCKT {name}")
                width = len(name) + 8
                for pin in pins:
                    if width + len(pin) + 1 > 78:
                        f.write("\n+")
                        width = 1
                    f.write(" " + pin)
                    width += len(pin) + 1
                f.write(f"\n.ENDS {name}\n")
    print(f"[INFO][LVS] {len(seen)} macro stub(s) written to {out}")


if __name__ == "__main__":
    main()
