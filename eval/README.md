# Éval du parser (`parse_trip`)

Mesure la **précision champ par champ** du parser IA contre des exemples étiquetés à la main
(« golden set ») — la bonne façon de mesurer l'exactitude (pas du volume aléatoire).

## Lancer
```bash
cd duvoyageur_backend
ANTHROPIC_API_KEY=sk-... python eval/run_eval.py            # rapport lisible
ANTHROPIC_API_KEY=sk-... python eval/run_eval.py --json out.json
ANTHROPIC_API_KEY=sk-... python eval/run_eval.py --strict   # dates exactes (année) + noms exacts
```

## Ajouter des cas
1. Ajoute une ligne à `eval/golden.jsonl` (un objet JSON par ligne).
2. Pour une capture : dépose l'image (anonymisée) dans `eval/images/` et mets `"image": "fichier.png"`.
3. Dans `"expect"`, ne mets **que les champs qui comptent**. `null` = le parser doit laisser **vide**.

```json
{"id": "mon-cas", "image": "deal1.png",
 "expect": {"destination": "Punta Cana", "hotel_name_raw": "Riu Bambu",
            "departure_date": "2026-02-14", "num_adults": 2,
            "price_amount": 1450, "price_basis": "per_person"}}
```

## Notes
- Les **dates ignorent l'année** par défaut (l'inférence d'année dépend de la date du jour) ; `--strict` la compare.
- Les **champs texte** (destination, hôtel, voyagiste) sont comparés en *normalisé* + *contains* ; `--strict` exige l'égalité exacte.
- `price_basis` : `per_person` | `total` | `unknown`.
- Quand un champ baisse, ajoute 2-3 **exemples few-shot** au prompt du parser pour ce type d'erreur, puis relance.
