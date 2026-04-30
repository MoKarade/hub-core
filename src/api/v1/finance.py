"""Endpoints de gestion des comptes et transactions financieres.

Sprint 1 du master plan Phase 1 :
- Accounts : CRUD basique des comptes (banque, type, devise...)
- Transactions : creation avec idempotence (dedup_hash) + lecture filtree

Les imports depuis hub-ingest passeront par ces endpoints. Aucun acces DB direct
hors hub-core (regle architecture du projet).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.events import broadcast
from src.db.models import (
    Account,
    CreditCardTransaction,
    InvestmentPosition,
    InvestmentTransaction,
    Transaction,
)
from src.db.session import get_db

router = APIRouter(prefix="/finance", tags=["finance"])


# ============================================================================
# Schemas Pydantic
# ============================================================================


class AccountCreate(BaseModel):
    """Payload pour creer un compte."""

    institution: str = Field(..., examples=["Desjardins"])
    account_type: str = Field(
        ...,
        examples=["checking", "savings", "credit_card", "investment"],
    )
    account_number_masked: str = Field(..., examples=["377646-EOP"])
    nickname: str | None = Field(default=None, examples=["Mon compte courant"])
    currency: str = Field(..., min_length=3, max_length=3, examples=["CAD", "USD"])
    is_active: bool = True


class AccountRead(BaseModel):
    """Compte tel que renvoye par l'API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    institution: str
    account_type: str
    account_number_masked: str
    nickname: str | None
    currency: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TransactionCreate(BaseModel):
    """Payload pour creer une transaction. Au moins un de debit/credit est requis."""

    account_id: UUID
    transaction_date: date
    description: str
    debit: Decimal | None = Field(default=None, ge=0)
    credit: Decimal | None = Field(default=None, ge=0)
    balance_after: Decimal | None = None
    source_format: str = Field(..., examples=["desjardins_csv_eop"])
    source_file: str | None = Field(default=None, examples=["janv2026.csv"])
    source_seq_num: int | None = Field(default=None, ge=0)
    dedup_hash: str = Field(..., min_length=64, max_length=64)

    @model_validator(mode="after")
    def _check_debit_xor_credit(self) -> "TransactionCreate":
        """Une transaction est soit un debit, soit un credit. Pas les deux."""
        if self.debit is None and self.credit is None:
            raise ValueError("Au moins un de debit ou credit doit etre fourni")
        if self.debit is not None and self.credit is not None:
            raise ValueError("Une transaction ne peut pas etre debit ET credit")
        return self


class TransactionRead(BaseModel):
    """Transaction telle que renvoyee par l'API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    transaction_date: date
    description: str
    debit: Decimal | None
    credit: Decimal | None
    balance_after: Decimal | None
    source_format: str
    source_file: str | None
    source_seq_num: int | None
    dedup_hash: str
    created_at: datetime


class CreditCardTransactionCreate(BaseModel):
    """Payload pour creer une transaction de carte de credit."""

    account_id: UUID
    card_number_masked: str
    transaction_date: date
    posting_date: date
    description: str
    amount: Decimal = Field(..., description="Positif=achat, negatif=paiement/remboursement")
    cashback_rate: Decimal | None = Field(default=None, ge=0, le=1)
    section: str = Field(..., examples=["transactions_courantes", "operations_au_compte"])
    source_format: str = Field(..., examples=["desjardins_mastercard_pdf"])
    source_file: str | None = None
    statement_date: date | None = None
    dedup_hash: str = Field(..., min_length=64, max_length=64)


class CreditCardTransactionRead(BaseModel):
    """Transaction de carte de credit telle que renvoyee par l'API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    card_number_masked: str
    transaction_date: date
    posting_date: date
    description: str
    amount: Decimal
    cashback_rate: Decimal | None
    section: str
    source_format: str
    source_file: str | None
    statement_date: date | None
    dedup_hash: str
    created_at: datetime


class InvestmentTransactionCreate(BaseModel):
    """Payload pour creer une transaction d'investissement (Disnat)."""

    account_id: UUID
    sub_account_code: str | None = None
    transaction_date: date
    settlement_date: date | None = None
    operation: str = Field(..., examples=["TRANSFERT RECU", "ACHAT", "FRAIS", "DIVIDENDE"])
    description: str
    symbol: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    statement_date: date
    source_format: str = Field(..., examples=["desjardins_disnat_pdf"])
    source_file: str | None = None
    dedup_hash: str = Field(..., min_length=64, max_length=64)


class InvestmentTransactionRead(BaseModel):
    """Transaction d'investissement renvoyee par l'API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    sub_account_code: str | None
    transaction_date: date
    settlement_date: date | None
    operation: str
    description: str
    symbol: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None
    currency: str | None
    statement_date: date
    source_format: str
    source_file: str | None
    dedup_hash: str
    created_at: datetime


class InvestmentPositionCreate(BaseModel):
    """Payload pour creer un snapshot de position (Disnat)."""

    account_id: UUID
    sub_account_code: str
    statement_date: date
    description: str
    symbol: str | None = None
    quantity: Decimal
    average_unit_cost: Decimal | None = None
    book_cost: Decimal | None = None
    market_price: Decimal
    market_value: Decimal
    currency: str = Field(..., min_length=3, max_length=3)
    portfolio_pct: Decimal | None = None
    source_format: str = Field(..., examples=["desjardins_disnat_pdf"])
    source_file: str | None = None
    dedup_hash: str = Field(..., min_length=64, max_length=64)


class InvestmentPositionRead(BaseModel):
    """Position renvoyee par l'API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    sub_account_code: str
    statement_date: date
    description: str
    symbol: str | None
    quantity: Decimal
    average_unit_cost: Decimal | None
    book_cost: Decimal | None
    market_price: Decimal
    market_value: Decimal
    currency: str
    portfolio_pct: Decimal | None
    source_format: str
    source_file: str | None
    dedup_hash: str
    created_at: datetime


# ============================================================================
# Routes Accounts
# ============================================================================


@router.post(
    "/accounts",
    response_model=AccountRead,
    status_code=status.HTTP_201_CREATED,
    summary="Creer un compte (idempotent par institution + account_number_masked)",
)
async def create_account(
    payload: AccountCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Account:
    # Idempotence : si un compte existe deja avec ces (institution, account_number_masked),
    # on retourne l'existant plutot que de creer un doublon.
    existing_q = select(Account).where(
        Account.institution == payload.institution,
        Account.account_number_masked == payload.account_number_masked,
    )
    existing = (await db.execute(existing_q)).scalar_one_or_none()
    if existing is not None:
        return existing

    account = Account(**payload.model_dump())
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@router.get(
    "/accounts",
    response_model=list[AccountRead],
    summary="Lister les comptes",
)
async def list_accounts(
    db: Annotated[AsyncSession, Depends(get_db)],
    is_active: bool | None = Query(default=None, description="Filtrer sur actif/inactif"),
) -> list[Account]:
    q = select(Account).order_by(Account.institution, Account.account_number_masked)
    if is_active is not None:
        q = q.where(Account.is_active == is_active)
    return list((await db.execute(q)).scalars().all())


@router.get(
    "/accounts/{account_id}",
    response_model=AccountRead,
    summary="Lire un compte par id",
)
async def get_account(
    account_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Account:
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compte introuvable")
    return account


# ============================================================================
# Routes Transactions
# ============================================================================


@router.post(
    "/transactions",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Creer une transaction (idempotent par dedup_hash)",
)
async def create_transaction(
    payload: TransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Transaction:
    # Verifie que le compte existe.
    account = await db.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Compte {payload.account_id} introuvable",
        )

    # Idempotence : si une transaction avec ce dedup_hash existe, on la retourne.
    existing_q = select(Transaction).where(Transaction.dedup_hash == payload.dedup_hash)
    existing = (await db.execute(existing_q)).scalar_one_or_none()
    if existing is not None:
        return existing

    txn = Transaction(**payload.model_dump())
    db.add(txn)
    await db.commit()
    await db.refresh(txn)
    await broadcast(
        "new_transaction",
        {
            "account_id": str(txn.account_id),
            "description": (txn.description or "")[:60],
            "amount": str(txn.debit or txn.credit or 0),
            "currency": "CAD",
        },
    )
    return txn


@router.get(
    "/transactions",
    response_model=list[TransactionRead],
    summary="Lister les transactions, avec filtres optionnels",
)
async def list_transactions(
    db: Annotated[AsyncSession, Depends(get_db)],
    account_id: UUID | None = Query(default=None),
    start_date: date | None = Query(default=None, description="Date de debut (incluse)"),
    end_date: date | None = Query(default=None, description="Date de fin (incluse)"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[Transaction]:
    q = (
        select(Transaction)
        .order_by(Transaction.transaction_date.desc(), Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    if start_date is not None:
        q = q.where(Transaction.transaction_date >= start_date)
    if end_date is not None:
        q = q.where(Transaction.transaction_date <= end_date)

    return list((await db.execute(q)).scalars().all())


# ============================================================================
# Routes Credit Card Transactions
# ============================================================================


@router.post(
    "/credit-card-transactions",
    response_model=CreditCardTransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Creer une transaction de carte de credit (idempotent par dedup_hash)",
)
async def create_credit_card_transaction(
    payload: CreditCardTransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreditCardTransaction:
    account = await db.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Compte {payload.account_id} introuvable",
        )

    existing_q = select(CreditCardTransaction).where(
        CreditCardTransaction.dedup_hash == payload.dedup_hash
    )
    existing = (await db.execute(existing_q)).scalar_one_or_none()
    if existing is not None:
        return existing

    txn = CreditCardTransaction(**payload.model_dump())
    db.add(txn)
    await db.commit()
    await db.refresh(txn)
    await broadcast(
        "new_transaction",
        {
            "account_id": str(txn.account_id),
            "description": (txn.description or "")[:60],
            "amount": str(txn.amount or 0),
            "currency": "CAD",
        },
    )
    return txn


@router.get(
    "/credit-card-transactions",
    response_model=list[CreditCardTransactionRead],
    summary="Lister les transactions de carte de credit, avec filtres",
)
async def list_credit_card_transactions(
    db: Annotated[AsyncSession, Depends(get_db)],
    account_id: UUID | None = Query(default=None),
    card_number_masked: str | None = Query(default=None),
    section: str | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    statement_date: date | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[CreditCardTransaction]:
    q = (
        select(CreditCardTransaction)
        .order_by(
            CreditCardTransaction.transaction_date.desc(),
            CreditCardTransaction.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    if account_id is not None:
        q = q.where(CreditCardTransaction.account_id == account_id)
    if card_number_masked is not None:
        q = q.where(CreditCardTransaction.card_number_masked == card_number_masked)
    if section is not None:
        q = q.where(CreditCardTransaction.section == section)
    if start_date is not None:
        q = q.where(CreditCardTransaction.transaction_date >= start_date)
    if end_date is not None:
        q = q.where(CreditCardTransaction.transaction_date <= end_date)
    if statement_date is not None:
        q = q.where(CreditCardTransaction.statement_date == statement_date)

    return list((await db.execute(q)).scalars().all())


# ============================================================================
# Routes Investment Transactions
# ============================================================================


@router.post(
    "/investment-transactions",
    response_model=InvestmentTransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Creer une transaction d'investissement (idempotent par dedup_hash)",
)
async def create_investment_transaction(
    payload: InvestmentTransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvestmentTransaction:
    account = await db.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Compte {payload.account_id} introuvable",
        )

    existing = (
        await db.execute(
            select(InvestmentTransaction).where(
                InvestmentTransaction.dedup_hash == payload.dedup_hash
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    txn = InvestmentTransaction(**payload.model_dump())
    db.add(txn)
    await db.commit()
    await db.refresh(txn)
    return txn


@router.get(
    "/investment-transactions",
    response_model=list[InvestmentTransactionRead],
)
async def list_investment_transactions(
    db: Annotated[AsyncSession, Depends(get_db)],
    account_id: UUID | None = Query(default=None),
    sub_account_code: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    statement_date: date | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[InvestmentTransaction]:
    q = (
        select(InvestmentTransaction)
        .order_by(InvestmentTransaction.transaction_date.desc())
        .limit(limit)
        .offset(offset)
    )
    if account_id is not None:
        q = q.where(InvestmentTransaction.account_id == account_id)
    if sub_account_code is not None:
        q = q.where(InvestmentTransaction.sub_account_code == sub_account_code)
    if symbol is not None:
        q = q.where(InvestmentTransaction.symbol == symbol)
    if operation is not None:
        q = q.where(InvestmentTransaction.operation == operation)
    if statement_date is not None:
        q = q.where(InvestmentTransaction.statement_date == statement_date)

    return list((await db.execute(q)).scalars().all())


# ============================================================================
# Routes Investment Positions (snapshots mensuels)
# ============================================================================


@router.post(
    "/investment-positions",
    response_model=InvestmentPositionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Creer un snapshot de position (idempotent par dedup_hash)",
)
async def create_investment_position(
    payload: InvestmentPositionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvestmentPosition:
    account = await db.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Compte {payload.account_id} introuvable",
        )

    existing = (
        await db.execute(
            select(InvestmentPosition).where(InvestmentPosition.dedup_hash == payload.dedup_hash)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    pos = InvestmentPosition(**payload.model_dump())
    db.add(pos)
    await db.commit()
    await db.refresh(pos)
    return pos


@router.get(
    "/investment-positions",
    response_model=list[InvestmentPositionRead],
)
async def list_investment_positions(
    db: Annotated[AsyncSession, Depends(get_db)],
    account_id: UUID | None = Query(default=None),
    sub_account_code: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    statement_date: date | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[InvestmentPosition]:
    q = (
        select(InvestmentPosition)
        .order_by(InvestmentPosition.statement_date.desc(), InvestmentPosition.market_value.desc())
        .limit(limit)
        .offset(offset)
    )
    if account_id is not None:
        q = q.where(InvestmentPosition.account_id == account_id)
    if sub_account_code is not None:
        q = q.where(InvestmentPosition.sub_account_code == sub_account_code)
    if symbol is not None:
        q = q.where(InvestmentPosition.symbol == symbol)
    if statement_date is not None:
        q = q.where(InvestmentPosition.statement_date == statement_date)

    return list((await db.execute(q)).scalars().all())
