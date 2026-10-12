#!/usr/bin/env python3
"""Summarise a KLayout LVS database (.lvsdb): per-circuit match status.

Usage: python3 lvs_report.py <file.lvsdb> [max_circuits_to_detail]
"""
import sys
import collections
import klayout.db as db


def name_of(obj):
    if obj is None:
        return "-"
    try:
        return obj.expanded_name()
    except Exception:
        return getattr(obj, "name", str(obj))


def main():
    path = sys.argv[1]
    max_detail = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    lvs = db.LayoutVsSchematic()
    lvs.read(path)
    xref = lvs.xref()
    if xref is None:
        print("no cross-reference in database (extraction-only run?)")
        return
    S = db.NetlistCrossReference
    status_name = {S.Match: "match", S.MatchWithWarning: "match-with-warning",
                   S.Mismatch: "MISMATCH", S.NoMatch: "NO-MATCH", S.Skipped: "skipped"}
    totals = collections.Counter()
    bad = []
    for cp in xref.each_circuit_pair():
        st = status_name.get(cp.status(), str(cp.status()))
        totals[st] += 1
        if st in ("match", "match-with-warning"):
            continue
        a, b = cp.first(), cp.second()
        detail = collections.Counter()
        ok = (S.Match, S.MatchWithWarning)
        for kind, it in (("nets", xref.each_net_pair(cp)), ("devices", xref.each_device_pair(cp)),
                         ("pins", xref.each_pin_pair(cp)), ("subcircuits", xref.each_subcircuit_pair(cp))):
            for p in it:
                detail[(kind, "ok" if p.status() in ok else "bad")] += 1
        bad.append((st, a.name if a else "-", b.name if b else "-", detail, cp))
    print("circuit pairs:", dict(totals))
    for st, la, sc, detail, cp in bad[:max_detail]:
        d = ", ".join(f"{k} {detail[(k, 'bad')]} bad / {detail[(k, 'ok')]} ok"
                      for k in ("nets", "devices", "pins", "subcircuits"))
        print(f"  {st:9s} layout={la}  schematic={sc}")
        print(f"            {d}")
        shown = 0
        for kind, it in (("net", xref.each_net_pair(cp)), ("device", xref.each_device_pair(cp)),
                         ("pin", xref.each_pin_pair(cp)), ("subckt", xref.each_subcircuit_pair(cp))):
            for p in it:
                if p.status() in (S.Match, S.MatchWithWarning):
                    continue
                if shown >= 6:
                    break
                print(f"            {kind}: layout={name_of(p.first())}  schematic={name_of(p.second())}")
                shown += 1
    if len(bad) > max_detail:
        print(f"  ... {len(bad) - max_detail} more non-matching circuit pairs")


if __name__ == "__main__":
    main()
