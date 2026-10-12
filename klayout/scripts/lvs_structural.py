#!/usr/bin/env python3
"""Structural cross-check of a layout netlist against the schematic netlist.

KLayout's top-level matcher cannot tell apart cells of equal topology (buf_2,
buf_4, buf_8 are the same transistor graph) and large groups of interchangeable
instances, and reports unresolved pairs for them even when the two netlists are
identical. This check compares what KLayout leaves open: it refines a colouring
of every standard-cell instance and net (Weisfeiler-Leman) on both sides and
requires every colour class to have the same size on both sides, with the cell
*type* (so drive strength included), the pin names, the top-level ports and the
net names present on both sides as fixed anchors. A swapped, opened or shorted
connection changes the colouring at the first round and is reported.

Floating outputs (nets with a single connection) are opened on both sides. Hard
macros are left out: they are compared by KLayout, by pin name.

Usage: lvs_structural.py <layout.cir> <schematic.cdl> [top] [rounds]
Exit status 0 if identical, 1 if not.
"""
import collections
import hashlib
import re
import sys

MACRO_PREFIXES = ("sg13cmos5l_", "rm_ihpsg13", "bondpad", "sealring")


def logical_lines(path):
    out = []
    for raw in open(path, errors="replace"):
        line = raw.rstrip("\n")
        if not line or line.startswith("*"):
            continue
        if line.startswith("+") and out:
            out[-1] += " " + line[1:].strip()
        else:
            out.append(line.strip())
    return out


def parse(path, top):
    masters, insts, ports = {}, [], []
    cur = None
    for line in logical_lines(path):
        up = line.upper()
        if up.startswith(".SUBCKT"):
            tok = line.split()
            cur = tok[1]
            masters[cur.lower()] = [p.lower() for p in tok[2:]]
            if cur.lower() == top.lower():
                ports = tok[2:]
            continue
        if up.startswith(".ENDS"):
            cur = None
            continue
        if cur is not None and cur.lower() == top.lower() and line[0] in "Xx":
            tok = line.split()
            insts.append((tok[0], tok[-1], tok[1:-1]))
    return masters, insts, ports


def norm_net(name):
    return name.replace("\\", "").lower()


def is_auto(name):
    return name.startswith("$")


def build(path, top):
    masters, insts, ports = parse(path, top)
    out = []
    for name, master, nets in insts:
        m = re.sub(r"\$\d+$", "", master.lower())
        if m.startswith(MACRO_PREFIXES):
            continue
        pins = masters.get(master.lower()) or masters.get(m)
        if pins is None or len(pins) != len(nets):
            raise SystemExit(f"{path}: {name} ({master}): {len(nets)} nets for "
                             f"{'unknown master' if pins is None else str(len(pins)) + ' pins'}")
        out.append((name, m, list(zip(pins, [norm_net(n) for n in nets]))))
    return out, {norm_net(p) for p in ports}


def digest(obj):
    return hashlib.blake2b(repr(obj).encode(), digest_size=8).hexdigest()


def open_floating(insts, anchors):
    count = collections.Counter(net for _, _, pn in insts for _, net in pn)
    return [(n, m, [(p, x) for p, x in pn if count[x] > 1 or x in anchors]) for n, m, pn in insts]


def refine(insts, anchors, rounds):
    nets = collections.defaultdict(list)
    for i, (_, _, pn) in enumerate(insts):
        for pin, net in pn:
            nets[net].append((i, pin))
    col = [digest(m) for _, m, _ in insts]
    hist = [list(col)]
    for _ in range(rounds):
        ncol = {}
        for net, conns in nets.items():
            if net in anchors:
                ncol[net] = digest(("anchor", net))
            else:
                ncol[net] = digest(sorted((col[i], pin) for i, pin in conns))
        col = [digest((m, sorted((pin, ncol[net]) for pin, net in pn))) for _, m, pn in insts]
        hist.append(list(col))
    return hist


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    lay_path, sch_path = sys.argv[1], sys.argv[2]
    top = sys.argv[3] if len(sys.argv) > 3 else "croc_chip"
    rounds = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    L, lports = build(lay_path, top)
    S, sports = build(sch_path, top)
    named_l = {n for _, _, pn in L for _, n in pn if not is_auto(n)}
    named_s = {n for _, _, pn in S for _, n in pn if not is_auto(n)}
    anchors = (lports & sports) | (named_l & named_s)
    print(f"[INFO][LVS] structural check: {len(L)} layout / {len(S)} schematic standard-cell "
          f"instances, {len(anchors)} anchor nets, {rounds} rounds")
    if len(L) != len(S):
        print(f"[ERROR][LVS] instance count differs: layout {len(L)}, schematic {len(S)}")
        by_l = collections.Counter(m for _, m, _ in L)
        by_s = collections.Counter(m for _, m, _ in S)
        for m in sorted(set(by_l) | set(by_s)):
            if by_l[m] != by_s[m]:
                print(f"[ERROR][LVS]   {m}: layout {by_l[m]}, schematic {by_s[m]}")
        sys.exit(1)
    L, S = open_floating(L, anchors), open_floating(S, anchors)
    hl, hs = refine(L, anchors, rounds), refine(S, anchors, rounds)
    for k in range(rounds + 1):
        cl, cs = collections.Counter(hl[k]), collections.Counter(hs[k])
        dl, ds = cl - cs, cs - cl
        if dl or ds:
            n = sum(dl.values())
            print(f"[ERROR][LVS] netlists differ after round {k}: {n} layout instance(s) have no "
                  f"counterpart")
            shown = 0
            for tag, lst, hist, dd in (("layout", L, hl, dl), ("schematic", S, hs, ds)):
                for i, c in enumerate(hist[k]):
                    if dd[c] > 0 and shown < 6:
                        print(f"[ERROR][LVS]   {tag}: {lst[i][0]} {lst[i][1]}")
                        shown += 1
            sys.exit(1)
    print(f"[INFO][LVS] structural check: identical after {rounds} rounds")


if __name__ == "__main__":
    main()
