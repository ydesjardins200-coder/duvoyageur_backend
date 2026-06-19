#!/usr/bin/env python3
"""Field-level accuracy harness for the trip parser (parse_trip).

This is how you MEASURE the AI parser's accuracy — not by sending random volume,
but by scoring it against hand-labeled "golden" examples.

Each example in golden.jsonl is one line of JSON:
  {
    "id": "punta-sunwing",
    "message": "Forfait tout inclus Punta Cana ...",   # optional text
    "image": "deal1.png",                               # optional, under eval/images/
    "expect": {                                         # only the fields you care about
      "destination": "Punta Cana",
      "hotel_name_raw": "Riu Bambu",
      "departure_date": "2026-02-14",                   # year is ignored by default
      "return_date": "2026-02-21",
      "num_adults": 2,
      "num_children": null,                             # null = expect EMPTY
      "operator": "Sunwing",
      "price_amount": 1450,
      "price_basis": "per_person"                       # per_person | total | unknown
    }
  }

Run it (needs ANTHROPIC_API_KEY set, same as prod):
    cd duvoyageur_backend
    ANTHROPIC_API_KEY=sk-... python eval/run_eval.py
    ANTHROPIC_API_KEY=sk-... python eval/run_eval.py --json report.json
    ANTHROPIC_API_KEY=sk-... python eval/run_eval.py --strict   # exact dates+names

Add cases by appending lines to golden.jsonl and dropping screenshots in images/.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
GOLDEN = HERE / "golden.jsonl"
IMAGES = HERE / "images"
FIELDS = ["destination", "hotel_name_raw", "departure_date", "return_date",
          "num_adults", "num_children", "operator", "price_amount", "price_basis"]


def _norm(s):
    if s is None:
        return None
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def _load_cases():
    if not GOLDEN.exists():
        sys.exit(f"Pas de golden set : {GOLDEN}")
    cases = []
    for i, line in enumerate(GOLDEN.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as e:
            sys.exit(f"golden.jsonl ligne {i} invalide : {e}")
    return cases


def _predicted(trip) -> dict:
    ps = getattr(trip, "price_seen", None)
    basis = getattr(ps, "basis", None) if ps else None
    return {
        "destination": trip.destination,
        "hotel_name_raw": trip.hotel_name_raw,
        "departure_date": trip.departure_date,
        "return_date": trip.return_date,
        "num_adults": trip.num_adults,
        "num_children": trip.num_children,
        "operator": trip.operator,
        "price_amount": ps.amount if ps else None,
        "price_basis": getattr(basis, "value", basis),
    }


def _match(field, exp, got, strict=False) -> bool:
    # Expected explicitly empty -> the parser should not have invented a value.
    if exp is None or exp == "":
        return got in (None, "")
    if got in (None, ""):
        return False
    if field in ("num_adults", "num_children"):
        return int(exp) == int(got)
    if field == "price_amount":
        try:
            return abs(float(exp) - float(got)) < 0.5
        except (TypeError, ValueError):
            return False
    if field in ("departure_date", "return_date"):
        e, g = str(exp), str(got)
        return e == g if strict else e[5:] == g[5:]   # default: ignore the year
    # text fields: normalized; lenient allows contains either way
    e, g = _norm(exp), _norm(got)
    return e == g if strict else (e == g or e in g or g in e)


def evaluate(parse_fn, strict=False):
    cases = _load_cases()
    field_stats = {f: [0, 0] for f in FIELDS}      # [correct, total]
    case_stats = []
    misses = []
    for c in cases:
        images = []
        if c.get("image"):
            p = IMAGES / c["image"]
            if not p.exists():
                sys.exit(f"Image manquante : {p}")
            media = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            images = [(p.read_bytes(), media)]
        trip = parse_fn(c.get("message", "") or "", images=images)
        pred = _predicted(trip)
        cok = ctot = 0
        for f, exp in c.get("expect", {}).items():
            got = pred.get(f)
            ok = _match(f, exp, got, strict=strict)
            field_stats[f][0] += int(ok)
            field_stats[f][1] += 1
            cok += int(ok)
            ctot += 1
            if not ok:
                misses.append((c["id"], f, exp, got))
        case_stats.append((c["id"], cok, ctot))
    return field_stats, case_stats, misses


def _print_report(field_stats, case_stats, misses):
    tot_ok = sum(s[0] for s in field_stats.values())
    tot = sum(s[1] for s in field_stats.values())
    print("=" * 56)
    print(f"  PRÉCISION GLOBALE : {tot_ok}/{tot} = {100*tot_ok/max(tot,1):.1f}%")
    print(f"  Cas : {len(case_stats)}")
    print("=" * 56)
    print("\nPar champ :")
    for f in FIELDS:
        ok, n = field_stats[f]
        if n:
            print(f"  {f:16s} {ok}/{n}  ({100*ok/n:.0f}%)")
    perfect = sum(1 for _, ok, n in case_stats if ok == n)
    print(f"\nCas parfaits : {perfect}/{len(case_stats)}")
    if misses:
        print(f"\nErreurs ({len(misses)}) — [champ] attendu ≠ obtenu :")
        for cid, f, exp, got in misses:
            print(f"  {cid:20s} [{f}] {exp!r} ≠ {got!r}")
    else:
        print("\nAucune erreur 🎉")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="FILE", help="écrit le rapport en JSON")
    ap.add_argument("--strict", action="store_true",
                    help="dates exactes (année incl.) + noms exacts (pas de 'contains')")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("⚠️  ANTHROPIC_API_KEY non défini — le parser ne peut pas tourner.\n"
                 "   Lance avec :  ANTHROPIC_API_KEY=sk-... python eval/run_eval.py")
    from parser import parse_trip

    field_stats, case_stats, misses = evaluate(parse_trip, strict=args.strict)
    _print_report(field_stats, case_stats, misses)

    if args.json:
        out = {
            "fields": {f: {"correct": s[0], "total": s[1]} for f, s in field_stats.items() if s[1]},
            "cases": [{"id": cid, "correct": ok, "total": n} for cid, ok, n in case_stats],
            "misses": [{"id": cid, "field": f, "expected": exp, "got": got}
                       for cid, f, exp, got in misses],
        }
        pathlib.Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\nRapport JSON écrit : {args.json}")


if __name__ == "__main__":
    main()
