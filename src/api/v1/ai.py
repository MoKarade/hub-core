"""Endpoint /v1/ai/ask : interroge le hub en francais via Qwen + SQL.

Workflow :
  1. Marc envoie une question en francais (ex: 'Combien j'ai depense en
     restos en mars ?')
  2. Le LLM (Qwen 2.5 14B) recoit le schema DB + la question + des exemples
     few-shot, et genere une requete SQL read-only.
  3. On valide le SQL (commence par SELECT, pas de mot interdit, regarde uniquement
     les tables whitelistees).
  4. On execute en lecture seule avec un timeout court.
  5. Le LLM recoit le resultat brut + la question + reformule une reponse en
     francais.
  6. On retourne `{answer, sql, results, sources}`.

Sprint Phase 1 fin : version minimale viable. Pas de retry, pas de feedback
loop, pas de safety-net sophistique. Pour Marc seul, en local, c'est OK.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        max_length=500,
        examples=["Combien j'ai depense en restos en mars 2026 ?"],
    )


class AskResponse(BaseModel):
    answer: str
    sql: str
    rows: list[dict[str, Any]]
    row_count: int


class PingResponse(BaseModel):
    status: str
    model: str
    backend: str
    sample_response: str | None = None


# ---------------------------------------------------------------------
# Schema DB transmis au LLM (en commentaires)
# ---------------------------------------------------------------------

# Garde uniquement les tables et colonnes utiles pour les questions de Marc.
# Les noms et types correspondent a la migration phase1.
_DB_SCHEMA = """\
-- Schema PostgreSQL du Personal Data Hub (Phase 1)

CREATE TABLE accounts (
    id UUID PRIMARY KEY,
    institution TEXT,            -- 'Desjardins'
    account_type TEXT,           -- 'checking', 'savings', 'credit_card', 'investment'
    account_number_masked TEXT,  -- ex: '377646-EOP', '5598 22** **** 5004'
    nickname TEXT,
    currency CHAR(3),            -- 'CAD', 'USD'
    is_active BOOLEAN
);

CREATE TABLE transactions (         -- Compte courant + epargne
    id UUID PRIMARY KEY,
    account_id UUID REFERENCES accounts(id),
    transaction_date DATE,
    description TEXT,
    debit NUMERIC(12,2),            -- montant sortant (NULL si credit)
    credit NUMERIC(12,2),           -- montant entrant (NULL si debit)
    balance_after NUMERIC(12,2)
);

CREATE TABLE credit_card_transactions (
    id UUID PRIMARY KEY,
    account_id UUID REFERENCES accounts(id),
    card_number_masked TEXT,
    transaction_date DATE,
    posting_date DATE,
    description TEXT,                -- inclut le marchand + ville/province
    amount NUMERIC(12,2),            -- positif=achat, negatif=paiement/remboursement
    cashback_rate NUMERIC(5,4),      -- 0.0050 ou 0.0200, NULL si paiement
    section TEXT,                    -- 'transactions_courantes' ou 'operations_au_compte'
    statement_date DATE              -- date du releve mensuel
);

CREATE TABLE investment_transactions (   -- Disnat (CAD + USD)
    id UUID PRIMARY KEY,
    account_id UUID REFERENCES accounts(id),
    sub_account_code TEXT,           -- '5NFL7A3' (CAD), '5NFL7B1' (USD)
    transaction_date DATE,
    settlement_date DATE,
    operation TEXT,                  -- 'TRANSFERT REÇU', 'ACHAT', 'VENTE', 'FRAIS',
                                     -- 'DIVIDENDE', 'DÉPÔT REÇU D''UNE CAISSE', etc.
    description TEXT,
    symbol TEXT,                     -- ticker boursier ou NULL
    quantity NUMERIC(20,6),
    unit_price NUMERIC(20,6),
    amount NUMERIC(15,2),
    currency CHAR(3),
    statement_date DATE
);

CREATE TABLE investment_positions (      -- Snapshots mensuels Disnat
    id UUID PRIMARY KEY,
    account_id UUID REFERENCES accounts(id),
    sub_account_code TEXT,
    statement_date DATE,             -- date de fin de mois
    description TEXT,                -- 'NVIDIA CORP', 'AMUNDI MSCI WORLD UCITS', etc.
    symbol TEXT,
    quantity NUMERIC(20,6),
    average_unit_cost NUMERIC(20,6),
    book_cost NUMERIC(15,2),
    market_price NUMERIC(20,6),
    market_value NUMERIC(15,2),
    currency CHAR(3),                -- 'CAD' ou 'USD'
    portfolio_pct NUMERIC(5,2)
);
"""

_FEW_SHOT_EXAMPLES = """\
Exemples de questions possibles et SQL correspondant :

Q: Combien j'ai depense en restos en mars 2026 ?
SQL: SELECT SUM(amount) AS total FROM credit_card_transactions
     WHERE transaction_date BETWEEN '2026-03-01' AND '2026-03-31'
       AND amount > 0
       AND (description ILIKE '%restaurant%' OR description ILIKE '%mcdonald%'
            OR description ILIKE '%tim hortons%' OR description ILIKE '%sushi%'
            OR description ILIKE '%boustan%' OR description ILIKE '%poulet%');

Q: Quel est le solde de mon compte courant fin mars ?
SQL: SELECT balance_after FROM transactions t
     JOIN accounts a ON a.id = t.account_id
     WHERE a.account_number_masked = '377646-EOP'
       AND t.transaction_date <= '2026-03-31'
     ORDER BY t.transaction_date DESC, t.created_at DESC LIMIT 1;

Q: Quelle est la valeur de mon portefeuille au 31 janvier 2026 ?
SQL: SELECT sub_account_code, SUM(market_value) AS total, currency
     FROM investment_positions
     WHERE statement_date = '2026-01-31'
     GROUP BY sub_account_code, currency;

Q: Combien j'ai recu en paie en fevrier 2026 ?
SQL: SELECT SUM(credit) AS total FROM transactions
     WHERE transaction_date BETWEEN '2026-02-01' AND '2026-02-29'
       AND description ILIKE '%paie%';
"""

_SYSTEM_PROMPT = (
    "Tu es l'assistant de Marc pour son Personal Data Hub. Marc parle francais. "
    "Marc habite au Quebec, sa banque est Desjardins (AccesD + Disnat). "
    "Sa devise principale est le DOLLAR CANADIEN (CAD, $). Le compte d'investissement "
    "USD (sub_account_code = '5NFL7B1') est en USD ; tout le reste est en CAD. "
    "Ne dis JAMAIS 'euros'. "
    "On t'envoie une question, tu generes UNE seule requete SQL PostgreSQL en lecture "
    "seule (SELECT uniquement). Aucune explication, juste le SQL, sans markdown ni "
    "delimiteur. Si la question est ambigue, fais ta meilleure interpretation. "
    "Toujours utiliser ILIKE pour les comparaisons texte (insensible a la casse)."
)


_ANSWER_SYSTEM_PROMPT = (
    "Tu es l'assistant de Marc, francophone du Quebec. La devise par defaut est "
    "le dollar CANADIEN (CAD, $). Le compte Disnat USD (sub_account_code = '5NFL7B1') "
    "est en USD. Ne dis JAMAIS 'euros'. Reponds en 1 a 3 phrases courtes, "
    "factuelles, en citant les chiffres EXACTS et la BONNE devise."
)


# ---------------------------------------------------------------------
# Garde-fou SQL
# ---------------------------------------------------------------------

_ALLOWED_TABLES = {
    "accounts",
    "transactions",
    "credit_card_transactions",
    "investment_transactions",
    "investment_positions",
}

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|VACUUM|REINDEX|CLUSTER|EXPLAIN|ANALYZE|LOCK)\b",
    re.IGNORECASE,
)


def _validate_sql(sql: str) -> str:
    """Verifie que le SQL est read-only et touche seulement les tables autorisees."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("SQL vide")
    if not cleaned.lower().lstrip("(").startswith(("select", "with")):
        raise ValueError("Le SQL doit commencer par SELECT ou WITH")
    if _FORBIDDEN_KEYWORDS.search(cleaned):
        raise ValueError("Mot-cle interdit detecte (INSERT/UPDATE/DELETE/...)")
    # Detection naive des tables. On extrait tout ce qui ressemble a un identifiant
    # apres FROM ou JOIN, et on verifie qu'il est whitelistee.
    table_refs = re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", cleaned, re.IGNORECASE)
    for tbl in table_refs:
        if tbl.lower() not in _ALLOWED_TABLES:
            raise ValueError(f"Table non autorisee : {tbl}")
    return cleaned


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------


@router.get("/ping", response_model=PingResponse)
async def ping(settings: Annotated[Settings, Depends(get_settings)]) -> PingResponse:
    """Smoke-test Ollama : envoie un prompt court et retourne la reponse."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": "Reponds en 1 mot : Bonjour ?",
                    "stream": False,
                },
            )
            r.raise_for_status()
            data = r.json()
            return PingResponse(
                status="ok",
                model=settings.ollama_model,
                backend=settings.ollama_base_url,
                sample_response=data.get("response", "").strip(),
            )
    except Exception as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Ollama indisponible : {e}",
        ) from e


@router.post("/ask", response_model=AskResponse)
async def ask(
    payload: AskRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AskResponse:
    """Questionne le hub en francais. LLM -> SQL -> exec -> LLM -> reponse."""
    # 1. Pass 1 LLM : genere le SQL.
    sql_prompt = (
        f"{_DB_SCHEMA}\n\n{_FEW_SHOT_EXAMPLES}\n\n"
        f"Q: {payload.question}\nSQL:"
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "system": _SYSTEM_PROMPT,
                    "prompt": sql_prompt,
                    "stream": False,
                    "options": {"temperature": 0.0},
                },
            )
            r.raise_for_status()
            generated = r.json().get("response", "").strip()
    except Exception as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"Generation LLM echouee : {e}"
        ) from e

    # Nettoyage des markdown delimiters au cas ou le LLM en ajoute
    generated = re.sub(r"```(?:sql)?", "", generated, flags=re.IGNORECASE).strip()

    try:
        sql = _validate_sql(generated)
    except ValueError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"SQL genere refuse : {e}. Genere : {generated[:200]!r}",
        ) from e

    # 2. Execute en lecture seule avec timeout serveur.
    try:
        await db.execute(text("SET LOCAL statement_timeout = 5000"))  # 5 secondes
        result = await db.execute(text(sql))
        rows = [dict(r._mapping) for r in result]
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Execution SQL echouee : {e}. SQL : {sql[:300]!r}",
        ) from e

    # 3. Pass 2 LLM : reformule en francais.
    rendered_rows = "\n".join(str(r) for r in rows[:20]) if rows else "(aucun resultat)"
    answer_prompt = (
        f"Question : {payload.question}\n\n"
        f"Resultat SQL ({len(rows)} ligne(s)) :\n{rendered_rows}\n\n"
        "Repond en francais, en 1-3 phrases courtes et factuelles. "
        "Cite les chiffres exacts."
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "system": _ANSWER_SYSTEM_PROMPT,
                    "prompt": answer_prompt,
                    "stream": False,
                    "options": {"temperature": 0.2},
                },
            )
            r.raise_for_status()
            answer = r.json().get("response", "").strip()
    except Exception as e:
        # On a au moins le SQL et les rows, on retourne quand meme.
        answer = f"(LLM indisponible pour la reformulation : {e}). Resultat brut ci-dessus."

    return AskResponse(answer=answer, sql=sql, rows=rows, row_count=len(rows))
