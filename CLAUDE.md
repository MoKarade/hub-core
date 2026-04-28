# hub-core — Contexte pour Claude Code

> **Avant de commencer, lis aussi :** `../../CLAUDE.md` (handoff projet global) et `~/.claude/CLAUDE.md` (profil Marc + règles).

## Rôle du repo

Backend FastAPI du Personal Data Hub. Sert l'API publique versionnée (`/v1/...`), gère la DB, appelle Ollama pour l'IA.

## Stack

- Python 3.13
- FastAPI + Uvicorn
- PostgreSQL 16 + pgvector (via Docker, voir hub-deploy)
- SQLAlchemy 2 async + Alembic (migrations)
- Pydantic v2 (validation + settings)
- Ollama via HTTP (host natif Windows pour GPU RTX 5080)
- structlog (logging structuré)

## État actuel (2026-04-28)

✅ Skeleton FastAPI fonctionnel
✅ Endpoints `/v1/health` et `/v1/ready` (health + check DB + Ollama)
✅ Settings via Pydantic + .env
✅ Logging structlog
✅ Session SQLAlchemy async configurée
✅ Alembic configuré (migrations vides au démarrage)
✅ Dockerfile multi-stage
✅ Tests pytest async basiques

❌ Pas encore lancé en vrai (jamais de `docker compose up` réussi)
❌ Pas encore d'endpoint `/v1/ai/ask` (TODO Phase 1)
❌ Pas encore de modèles SQLAlchemy (TODO Phase 1+ avec les premières tables)

## Conventions de code

- **Linting :** ruff (config dans pyproject.toml, line-length 100)
- **Type hints :** partout, mypy strict idéalement
- **Imports :** ordre standard (stdlib, third-party, local)
- **Fonctions async** par défaut quand on touche DB/HTTP
- **Settings :** lus uniquement via `get_settings()` (cached), JAMAIS d'env var direct dans le code applicatif
- **Logs :** `logger.info("event_name", key=value)` style structlog
- **Tests :** dans `tests/`, fichiers `test_*.py`, async via `pytest-asyncio`

## Convention API versionnée

L'API publique du hub est versionnée. Actuellement `/v1`. Quand on doit casser un contrat → bump à `/v2` et garder `/v1` en parallèle.

Les apps embarquées (app-trajets, app-finance) consomment cette API. Elles ne touchent JAMAIS la DB directement.

## Règles spécifiques pour ce repo

- ❌ Ne JAMAIS hardcoder des credentials ou secrets en clair
- ❌ Ne JAMAIS commit `.env` (le `.gitignore` le bloque)
- ❌ Ne JAMAIS créer de fixtures avec data fictive (règle no-fake)
- ✅ Toujours via `get_settings()` pour la config
- ✅ Toute nouvelle migration = `alembic revision --autogenerate -m "<description>"`
- ✅ Pour les nouveaux endpoints : ajouter dans `src/api/v1/<feature>.py` + tests

## TODO Phase 0 (avant de passer à Phase 1)

- [ ] Vérifier que le build Docker passe (`docker build -t hub-core:dev .`)
- [ ] Vérifier que `/v1/health` et `/v1/ready` répondent une fois la stack démarrée
- [ ] Premier endpoint `/v1/ai/ping` qui appelle Ollama et renvoie le modèle utilisé

## TODO Phase 1 (banque + IA basique)

- [ ] Modèle SQLAlchemy : `account`, `transaction`, `category`, `merchant`
- [ ] Migration Alembic correspondante
- [ ] Endpoint `POST /v1/finance/transactions` (insertion via hub-ingest)
- [ ] Endpoint `GET /v1/finance/transactions` avec filtres (date, category, account)
- [ ] Endpoint `POST /v1/ai/ask` :
  - Reçoit `{question: str}`
  - LLM génère SQL depuis schema connu (prompt engineering)
  - Validation SQL (whitelist tables, no DELETE/UPDATE/DROP)
  - Execute en read-only avec timeout
  - LLM formule la réponse en français à partir du résultat
  - Retourne `{answer, sql, results, sources}`
- [ ] Endpoint `GET /v1/insights` (anomalies, doublons, patterns)

## Démarrer en dev (sans Docker)

```powershell
cd C:\hub\hub-core
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
# édite .env avec DATABASE_URL pointant sur ta postgres locale
uvicorn src.main:app --reload
```

## Démarrer via Docker (recommandé)

Voir `../hub-deploy/CLAUDE.md` — la stack complète passe par docker-compose.

## Liens

- Phasing global : `../../02_phasing.md`
- Master plan : `../../04_master_plan.md`
- Architecture : `../hub-docs/02-architecture.md`
