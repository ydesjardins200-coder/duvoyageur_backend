#!/usr/bin/env python3
"""
assistant_eval.py — regression / quality benchmark for the portal travel agent.

Runs 20 representative member requests through the real pipeline
(portal_assistant.answer_events) for one or more providers and scores each
answer with automatic checks: hotels recommended, structured results present,
prices only from Canadian wholesalers, no competitor agencies, questions asked,
sources cited, latency. Prints a table and writes a JSON report.

USAGE (needs the API keys, e.g. in Railway):
    railway run python tools/assistant_eval.py                 # provider from settings
    railway run python tools/assistant_eval.py anthropic openai
    railway run python tools/assistant_eval.py anthropic --only 1,5,9

Each case costs real API calls (web search included): ~20 answers per provider.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portal_assistant as pa  # noqa: E402
from config import settings  # noqa: E402

COMPETITORS = re.compile(r"(?i)\b(expedia|redtag|red tag|booking\.com|itravel2000|flight ?centre|"
                         r"sell ?off|kayak|trivago|hotels\.com|agoda|priceline)\b")

# (request, expectations) — hotels: minimum hotel cards expected (0 = none required)
CASES = [
    ("Je cherche un voyage au Mexique, côte Pacifique, départ le 31 décembre 2026, 7 jours, 2 adultes. "
     "Budget 5 000 à 7 000 $ pour deux. Bonne bouffe et plein d'activités.", {"hotels": 3}),
    ("Je cherche un tout-inclus au Mexique le 20 décembre pour deux adultes, départ YUL.", {"hotels": 3, "questions": True}),
    ("Tout-inclus luxe à Punta Cana pour notre lune de miel en février, 10 nuits.", {"hotels": 3}),
    ("Famille avec 2 enfants (4 et 8 ans), Riviera Maya, semaine de relâche, budget serré.", {"hotels": 3}),
    ("Cuba pas cher en novembre, 2 adultes, 7 nuits, départ Montréal.", {"hotels": 3}),
    ("On veut un adultes seulement tranquille en Jamaïque, mars, 2 personnes.", {"hotels": 3}),
    ("Je veux partir dans le Sud.", {"hotels": 0, "questions": True}),
    ("Quand partir au Mexique pour éviter les sargasses?", {"hotels": 0}),
    ("Quoi mettre dans ma valise pour une semaine à Cancún?", {"hotels": 0}),
    ("Quels documents pour aller en République dominicaine?", {"hotels": 0}),
    ("Meilleur tout-inclus pour la plongée à Cozumel?", {"hotels": 3}),
    ("Hôtel avec beaucoup de restos à la carte à Varadero, 2 adultes, janvier.", {"hotels": 3}),
    ("Costa Rica ou Mexique pour un couple actif en avril?", {"hotels": 0}),
    ("Budget 3 000 $ pour 2, 7 nuits, Noël, plage — c'est réaliste?", {"hotels": 2, "questions": True}),
    ("Groupe de 6 amis, 30 ans, fête et plage, Cancún, mai.", {"hotels": 3}),
    ("Puerto Plata ou Punta Cana pour des grands-parents avec petits-enfants?", {"hotels": 2}),
    ("Je veux un hôtel 5 étoiles adultes seulement à Los Cabos, prix pas important.", {"hotels": 3}),
    ("Sainte-Lucie pour un voyage de noces, est-ce que ça vaut le coût?", {"hotels": 2}),
    ("Tout-inclus avec glissades d'eau pour les enfants, Punta Cana, juillet.", {"hotels": 3}),
    ("Je pars le 2 janvier au lieu du 31 décembre, ça change quoi pour Puerto Vallarta?", {"hotels": 0}),
]


def run_case(provider: str, q: str) -> dict:
    saved: list = []
    t0 = time.monotonic()
    events = []
    for line in pa.answer_events(provider, -1, q, [], "", True, lambda u, a: saved.append(a),
                                 count_searches=False, debug=True):
        events.append(json.loads(line))
    dt = time.monotonic() - t0
    a = saved[0] if saved else {}
    text = a.get("text", "")
    cards = a.get("cards") or []
    return {
        "latency_s": round(dt, 1),
        "chars": len(text),
        "hotels": len(cards),
        "priced_hotels": sum(1 for c in cards if c.get("total") or c.get("prix_personne")),
        "prix_rows": len(a.get("prix") or []),
        "budget": bool(a.get("budget")),
        "questions": len(a.get("questions") or []),
        "sources": len(a.get("sources") or []),
        "steps": len(a.get("steps") or []),
        "competitor_mention": bool(COMPETITORS.search(text)),
        "non_wholesaler_price": any((c.get("total") or c.get("prix_personne")) and
                                    not pa._on(c.get("source_url") or "", pa.WHOLESALERS) for c in cards),
        "error": "" if a else next((e["v"][-300:] for e in events if e["t"] == "text"), "no answer"),
        "hotel_names": [c.get("hotel") for c in cards],
    }


def score(r: dict, exp: dict) -> list:
    fails = []
    if r["error"]:
        fails.append("erreur")
    if r["hotels"] < exp.get("hotels", 0):
        fails.append(f"hôtels {r['hotels']}<{exp['hotels']}")
    if exp.get("questions") and not r["questions"]:
        fails.append("pas de questions")
    if r["competitor_mention"]:
        fails.append("concurrent cité")
    if r["non_wholesaler_price"]:
        fails.append("prix hors grossiste")
    if exp.get("hotels") and not r["sources"]:
        fails.append("aucune source")
    return fails


def main(argv: list) -> None:
    only = None
    if "--only" in argv:
        i = argv.index("--only")
        only = {int(x) for x in argv[i + 1].split(",")}
        argv = argv[:i] + argv[i + 2:]
    providers = [p for p in argv if p in pa.PROVIDERS] or [settings.PORTAL_ASSISTANT_PROVIDER]
    report = {}
    for prov in providers:
        if not pa.provider_ready(prov):
            print(f"[{prov}] clé API manquante — ignoré")
            continue
        print(f"\n=== {prov} · {pa.model_name(prov)} ===")
        rows = []
        for n, (q, exp) in enumerate(CASES, 1):
            if only and n not in only:
                continue
            r = run_case(prov, q)
            r["fails"] = score(r, exp)
            r["case"], r["q"] = n, q
            rows.append(r)
            flag = "OK " if not r["fails"] else "KO "
            print(f"{flag}#{n:02d} {r['latency_s']:>5}s hôtels={r['hotels']} prix={r['priced_hotels']}"
                  f"/{r['prix_rows']} q={r['questions']} src={r['sources']} "
                  f"{'; '.join(r['fails'])}  — {q[:60]}")
        ok = sum(1 for r in rows if not r["fails"])
        lat = sorted(r["latency_s"] for r in rows)
        print(f"--> {ok}/{len(rows)} réussis · latence médiane {lat[len(lat) // 2] if lat else 0}s")
        report[prov] = {"model": pa.model_name(prov), "passed": ok, "total": len(rows), "rows": rows}
    out = Path(__file__).resolve().parent / f"eval_{time.strftime('%Y%m%d_%H%M')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nRapport : {out}  (coût et tokens par réponse : voir les logs « assistant[...] »)")


if __name__ == "__main__":
    main(sys.argv[1:])
