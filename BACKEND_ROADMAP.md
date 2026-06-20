# BACKEND_ROADMAP.md — Ownership, suivi & performance pipeline

Source de vérité pour le chantier "productivité admin / vélocité de pipeline".
On suit ça de session en session (comme IMAGE_AUDIT.md côté frontend).

## Problème
Pipeline en place (`new → needs_info → quoted → booked → closed`, flag
`awaiting_reply`, timeline `Interaction`), mais :
1. **Pas de responsable** — un dossier qui sort de "new" devient orphelin.
2. **Aucune visibilité de relance** — rien ne dit quoi relancer aujourd'hui ni ce
   qui traîne ; les "quoted" refroidissent → bookings perdus.
3. **Tabs organisés par entité** (Demandes/Clients/Réservées), pas par action.

But réel : **vélocité de pipeline** (amener new → quoted → booked plus vite),
pas juste l'assignation.

## Principe directeur (décidé avec Yan)
Plus d'un admin viendra. On dessine le **modèle de données une seule fois** :
table `staff` + `owner_id` (FK) sur `cases` dès la Phase 0. L'ownership "léger"
(login partagé, on choisit son nom) et les "vrais comptes" (Phase 4) partagent
le même schéma. Quand les logins individuels arrivent, `owner_id` passe de
"choisi manuellement" à "auto depuis la session". Zéro repeinturage.

## Phases

### Phase 0 — Fondations (schéma) — invisible, débloque tout
- [ ] Table `staff` (name, initials, email, role admin/agent, active,
      password_hash nullable pour Phase 4). Seedée avec l'admin actuel.
- [ ] Colonnes `cases` : `owner_id` (FK staff, null = pool partagé),
      `next_follow_up_at`, `last_activity_at`.
- [ ] `last_activity_at` backfillé depuis la timeline `Interaction`
      (fallback created_at).
- [ ] Migration via le pattern `ADD COLUMN IF NOT EXISTS` (PG) / PRAGMA (SQLite).
- [ ] Étendre `ACTIVITY_KINDS` : `follow_up`, `claim`, `assign`.
- Additif et rétrocompatible : colonnes nullables, nouvelle table → safe à
  pousser même si non encore utilisé par l'UI.

### Phase 1 — Moteur de relance ("À relancer") — le plus gros levier
- [ ] Au passage à `needs_info`/`quoted` : fixer `next_follow_up_at` par défaut
      (+2 jours ouvrables, éditable).
- [ ] Vue "À relancer" : dossiers en cours dont la relance est due,
      triés par retard, owner affiché.
- [ ] Action "Relancé" → repousse la date + log `follow_up` dans la timeline.
- [ ] Flag "à risque" si un quoted dort depuis N jours sans activité.
- [ ] `last_activity_at` mis à jour à chaque event.

### Phase 2 — Ownership (modèle "Réclamer")
- [ ] Bouton "Réclamer" → set `owner_id`.
- [ ] Pool "À réclamer" (dossiers en cours, owner null).
- [ ] Vue "Mes dossiers" (filtre owner courant — choisi dans menu staff tant que
      login partagé).
- [ ] Réassignation manuelle (admin) + log `assign`.

### Phase 3 — Board pipeline + métriques (performance d'affaire)
- [ ] Board par statut (chips owner + âge du dossier).
- [ ] Bande métriques : conversion new→quoted→booked, temps moyen par étape,
      nb en retard, classement par owner.

### Phase 4 — Vrais comptes admin (quand le 2e admin arrive)
- [ ] Login individuel par staff (cookie `dv_admin`, séparé de `dv_portal`),
      hash par staff, rôles admin/agent.
- [ ] `owner_id` auto depuis la session → "Mes dossiers" + perf par agent fiables.
- Le schéma Phase 0 le supporte déjà (password_hash existe).

## Séquence
0 → 1 → 2 → 3, puis 4 le jour où le 2e admin embarque.
Phases 1-3 livrent de la valeur tout de suite sur le login partagé actuel.

## Notes techniques
- Auth admin actuelle : compte unique partagé (`ADMIN_USER`/`ADMIN_PASSWORD`,
  HTTP Basic). Donc `owner` = convention/étiquette jusqu'à la Phase 4.
- Tests : SQLite local (`sqlite:///./dev.db`) + Starlette TestClient in-process.
- Migration : suivre `_ensure_columns()` dans db.py (branches PG + SQLite).

## Journal
- (Phase 0) en cours.
