# hub-core

Backend & API centrale du Personal Data Hub de Marc.

## Stack

- Python 3.13
- FastAPI + Uvicorn
- PostgreSQL 16 + pgvector (vecteurs pour RAG)
- SQLAlchemy 2 + Alembic (migrations)
- Pydantic v2 (validation)
- Ollama (LLM local, via HTTP)

## Démarrer en local

Tu n'as **pas** besoin d'installer Python directement — tout tourne en Docker via `hub-deploy`.

Si tu veux lancer juste hub-core (sans Docker, en mode dev rapide) :

```powershell
# depuis le dossier hub-core
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
# éditer .env avec tes valeurs
uvicorn src.main:app --reload
```

Ouvre ensuite http://localhost:8000/health → tu dois voir `{"status":"ok"}`.

## Structure

```
src/
├── api/
│   └── v1/
│       ├── health.py         # GET /health, /ready
│       └── __init__.py
├── core/
│   ├── config.py             # settings (lit .env via Pydantic)
│   └── logging.py
├── db/
│   ├── session.py            # session SQLAlchemy
│   └── models/
└── main.py                   # entry point FastAPI
```

## Tests

```powershell
pytest
```

## Migration DB (Alembic)

```powershell
# créer une nouvelle migration
alembic revision --autogenerate -m "description"

# appliquer
alembic upgrade head
```

## TODO Phase 0

- [x] Skeleton FastAPI avec health check
- [x] Config via Pydantic + .env
- [ ] Connexion PostgreSQL via SQLAlchemy
- [ ] Première migration Alembic
- [ ] Endpoint `GET /v1/ai/ping` qui appelle Ollama
- [ ] Tests d'intégration de base

Détails dans `../../02_phasing.md` du projet global.
