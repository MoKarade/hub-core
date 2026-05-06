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
    history: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Historique conversationnel [{role: 'user'|'assistant', content: str}]. "
            "Permet les questions de suivi (et avant ca ?, le mois suivant ?)."
        ),
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

-- Phase 2 : Localisation Google Maps Timeline (depuis 2013) -----------------

CREATE TABLE location_visits (       -- Lieux visites avec semantic type
    id UUID PRIMARY KEY,
    start_time TIMESTAMPTZ,          -- en UTC
    end_time TIMESTAMPTZ,
    tz_offset_minutes INT,           -- offset local au moment de la visite
    lat NUMERIC(10,7),               -- latitude en degres decimaux
    lng NUMERIC(10,7),               -- longitude en degres decimaux
    place_id TEXT,                   -- Google place_id si dispo
    semantic_type TEXT,              -- 'HOME', 'INFERRED_HOME', 'WORK', 'INFERRED_WORK',
                                     -- 'SEARCHED_ADDRESS', 'ALIASED_LOCATION', 'UNKNOWN'
    probability NUMERIC(5,4),        -- confiance Google [0,1]
    source TEXT                      -- 'google_timeline'
);

CREATE TABLE location_activities (   -- Trajets / segments de transport
    id UUID PRIMARY KEY,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    activity_type TEXT,              -- 'IN_PASSENGER_VEHICLE', 'WALKING', 'FLYING',
                                     -- 'IN_TRAIN', 'IN_SUBWAY', 'IN_BUS', 'CYCLING',
                                     -- 'IN_VEHICLE', 'RUNNING', 'SKIING'
    distance_meters NUMERIC,         -- distance parcourue en metres
    probability NUMERIC(5,4),
    start_lat NUMERIC(10,7), start_lng NUMERIC(10,7),
    end_lat NUMERIC(10,7),   end_lng NUMERIC(10,7),
    source TEXT
);

CREATE TABLE location_points (       -- Points GPS bruts (timelinePath)
    id UUID PRIMARY KEY,
    timestamp_utc TIMESTAMPTZ,
    latitude NUMERIC(10,7),
    longitude NUMERIC(10,7),
    accuracy_m INT,
    altitude_m INT,
    activity_type TEXT,              -- legacy lowercase pour ancien format
    source TEXT,                     -- 'google_timeline' uniquement actuellement
    source_file TEXT
);
-- Marc habite Levis QC (lat=46.738, lng=-71.243) depuis 2024.
-- Il a vecu en France (Hauts-de-France ~50.6,2.98) avant.
-- Pour detecter "voyages a l'etranger", filtrer les visites a >100km du domicile.
-- Pour "trajets en avion", filter activity_type='FLYING' et distance_meters > 200000.

-- Phase 3 : Emails Gmail --------------------------------------------------

CREATE TABLE emails (
    id UUID PRIMARY KEY,
    user_email TEXT,                 -- proprietaire ('marc.richard4@gmail.com')
    gmail_id TEXT UNIQUE,
    thread_id TEXT,
    subject TEXT,
    sender TEXT,                     -- "Display Name <email@domain>"
    sender_email TEXT,               -- juste l'email
    recipients TEXT[],               -- ARRAY destinataires
    sent_at TIMESTAMPTZ,
    snippet TEXT,                    -- preview ~200 chars
    body_text TEXT,                  -- corps en texte brut
    body_html TEXT,
    labels TEXT[],                   -- ['INBOX','UNREAD','IMPORTANT', custom...]
    has_attachments BOOLEAN,
    is_unread BOOLEAN,
    size_estimate INT
);
-- Pour "non-lus" : WHERE is_unread = TRUE.
-- Pour filter par expediteur : WHERE sender_email ILIKE '%domain%'.
-- Pour rechercher dans label : WHERE 'INBOX' = ANY(labels).

-- Phase 3 : Calendar Google ------------------------------------------------

CREATE TABLE calendar_events (
    id UUID PRIMARY KEY,
    user_email TEXT,
    gcal_id TEXT UNIQUE,
    calendar_id TEXT,
    summary TEXT,                    -- titre de l'event
    description TEXT,
    location TEXT,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    all_day BOOLEAN,
    organizer_email TEXT,
    attendees TEXT[],                -- ARRAY emails participants
    status TEXT,                     -- 'confirmed','tentative','cancelled'
    html_link TEXT,
    recurring_event_id TEXT          -- NULL si one-off
);

-- Phase 3c : Photos Google -------------------------------------------------

CREATE TABLE photos (
    id UUID PRIMARY KEY,
    user_email TEXT,
    media_id TEXT UNIQUE,
    filename TEXT,
    mime_type TEXT,                  -- 'image/jpeg', 'video/mp4', etc.
    description TEXT,
    creation_time TIMESTAMPTZ,
    width INT, height INT,
    is_video BOOLEAN,
    video_duration_ms INT,
    camera_make TEXT,                -- 'Apple', 'Google', 'samsung', NULL
    camera_model TEXT,               -- 'iPhone 14', 'Pixel 8', etc.
    base_url TEXT,                   -- expire ~60min, ne pas se fier en SQL
    product_url TEXT,
    latitude FLOAT, longitude FLOAT, -- NULL si non geolocalise
    location_name TEXT,
    faces_count INT
);

-- Phase 3c : Drive Google --------------------------------------------------

CREATE TABLE drive_files (
    id UUID PRIMARY KEY,
    user_email TEXT,
    drive_id TEXT UNIQUE,
    name TEXT,
    mime_type TEXT,                  -- 'application/pdf', 'application/vnd.google-apps.document', etc.
    size_bytes BIGINT,               -- NULL pour Google Docs/Sheets
    starred BOOLEAN, trashed BOOLEAN, is_shared BOOLEAN,
    owner_email TEXT,
    created_time TIMESTAMPTZ,
    modified_time TIMESTAMPTZ,
    web_view_link TEXT,
    parents TEXT                     -- IDs parents separes par virgules
);

-- Phase 4 : Sante / Fitness ------------------------------------------------

CREATE TABLE health_metrics (        -- 1 ligne par jour x metric x source
    id UUID PRIMARY KEY,
    user_email TEXT,
    date DATE,
    metric TEXT,                     -- 'steps','sleep_total_min','sleep_deep_min',
                                     -- 'sleep_rem_min','sleep_light_min','calories',
                                     -- 'distance_m','active_minutes','weight_kg',
                                     -- 'heart_rate_avg'
    value FLOAT,                     -- valeur (unite implicite par metric)
    source TEXT                      -- 'google_fit','garmin','apple_health','manual'
);

-- Phase 5 : Tasks Google ---------------------------------------------------

CREATE TABLE tasks (
    id UUID PRIMARY KEY,
    user_email TEXT,
    task_id TEXT UNIQUE,
    tasklist_id TEXT,
    tasklist_title TEXT,             -- nom de la liste ('Personnel','Boulot', etc.)
    title TEXT,
    notes TEXT,
    is_completed BOOLEAN,
    due_at TIMESTAMPTZ,              -- echeance, NULL si pas de date
    completed_at TIMESTAMPTZ,
    last_modified TIMESTAMPTZ
);
-- Pour "en retard" : is_completed = FALSE AND due_at < NOW().

-- Phase 5 : Contacts Google People ------------------------------------------

CREATE TABLE contacts (
    id UUID PRIMARY KEY,
    user_email TEXT,
    person_id TEXT UNIQUE,
    display_name TEXT,
    given_name TEXT, family_name TEXT,
    emails TEXT[], phones TEXT[], addresses TEXT[],
    organizations TEXT[],            -- 'NomEntreprise — Titre'
    birthday DATE,
    photo_url TEXT,
    notes TEXT,
    last_modified TIMESTAMPTZ
);

-- Phase 6 : YouTube --------------------------------------------------------

CREATE TABLE youtube_activities (
    id UUID PRIMARY KEY,
    user_email TEXT,
    activity_id TEXT UNIQUE,
    activity_type TEXT,              -- 'upload','like','favorite','subscription'
    video_id TEXT,
    video_title TEXT,
    channel_id TEXT, channel_title TEXT,
    description TEXT,
    thumbnail_url TEXT,
    published_at TIMESTAMPTZ
);

-- Annotations Marc ---------------------------------------------------------

CREATE TABLE named_places (          -- Lieux nommes (Maison parents, Chalet, Gym, ...)
    id UUID PRIMARY KEY,
    name TEXT,
    lat NUMERIC, lng NUMERIC,
    radius_m FLOAT,                  -- rayon de match (metres)
    semantic_type TEXT,
    notes TEXT
);

CREATE TABLE trip_notes (            -- Notes sur voyages (cle = start_date)
    id UUID PRIMARY KEY,
    start_date DATE UNIQUE,
    end_date DATE,
    title TEXT,
    content TEXT,
    rating INT                       -- 1-5 etoiles
);

-- Phase 6 : Actualites Google News RSS ------------------------------------

CREATE TABLE news_articles (         -- Articles RSS Google News (auto-sync 30 min)
    id UUID PRIMARY KEY,
    guid TEXT UNIQUE,
    title TEXT,
    link TEXT,
    summary TEXT,
    source TEXT,                     -- 'Le Devoir','Radio-Canada','TVA Nouvelles', etc.
    category TEXT,
    image_url TEXT,
    published_at TIMESTAMPTZ,
    feed_url TEXT
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

-- Localisation : exemples ----------------------------------------------------

Q: Combien de fois je suis alle en France en 2024 ?
SQL: SELECT COUNT(DISTINCT DATE(start_time)) AS jours_en_france
     FROM location_visits
     WHERE start_time BETWEEN '2024-01-01' AND '2024-12-31'
       AND lat BETWEEN 41 AND 51
       AND lng BETWEEN -5 AND 9;

Q: Combien de km j'ai parcourus en avion au total ?
SQL: SELECT SUM(distance_meters) / 1000 AS km_total, COUNT(*) AS nb_vols
     FROM location_activities
     WHERE activity_type = 'FLYING';

Q: Quels sont mes 5 voyages les plus longs hors du Quebec ?
SQL: WITH away AS (
       SELECT DATE(start_time) AS jour, lat, lng
       FROM location_visits
       WHERE NOT (lat BETWEEN 45 AND 49 AND lng BETWEEN -75 AND -69)  -- hors QC
         AND semantic_type IS DISTINCT FROM 'HOME'
     )
     SELECT jour, COUNT(*) AS nb_visites
     FROM away
     GROUP BY jour
     ORDER BY nb_visites DESC
     LIMIT 5;

Q: Combien de temps j'ai passe au travail en mars 2024 ?
SQL: SELECT SUM(EXTRACT(EPOCH FROM (end_time - start_time)) / 3600) AS heures
     FROM location_visits
     WHERE start_time BETWEEN '2024-03-01' AND '2024-03-31'
       AND semantic_type IN ('WORK', 'INFERRED_WORK');

Q: Ou etais-je le 15 aout 2024 ?
SQL: SELECT start_time, end_time, lat, lng, semantic_type
     FROM location_visits
     WHERE start_time::date = '2024-08-15'
     ORDER BY start_time;

Q: Combien j'ai marche en 2024 ?
SQL: SELECT SUM(distance_meters) / 1000 AS km, COUNT(*) AS sessions,
            SUM(EXTRACT(EPOCH FROM (end_time - start_time)) / 60) AS minutes
     FROM location_activities
     WHERE activity_type IN ('WALKING', 'RUNNING')
       AND start_time BETWEEN '2024-01-01' AND '2024-12-31';

Q: Mes 5 derniers voyages a l'etranger ?
SQL: SELECT DATE(start_time) AS jour, AVG(lat)::numeric(8,4) AS lat,
            AVG(lng)::numeric(8,4) AS lng, COUNT(*) AS visites
     FROM location_visits
     WHERE NOT (lat BETWEEN 45 AND 49 AND lng BETWEEN -75 AND -69)
       AND semantic_type IS DISTINCT FROM 'HOME'
     GROUP BY DATE(start_time)
     ORDER BY jour DESC LIMIT 5;

-- Finance : exemples additionnels --------------------------------------------

Q: Quels sont mes 10 plus gros achats par carte de credit cette annee ?
SQL: SELECT transaction_date, description, amount
     FROM credit_card_transactions
     WHERE amount > 0
       AND transaction_date >= DATE_TRUNC('year', CURRENT_DATE)
     ORDER BY amount DESC LIMIT 10;

Q: Mes abonnements recurrents (transactions mensuelles repetees) ?
SQL: SELECT description, COUNT(*) AS occurrences,
            ROUND(AVG(amount)::numeric, 2) AS montant_moyen,
            MIN(transaction_date) AS premiere, MAX(transaction_date) AS derniere
     FROM credit_card_transactions
     WHERE amount > 0
     GROUP BY description
     HAVING COUNT(*) >= 3
        AND COUNT(DISTINCT DATE_TRUNC('month', transaction_date)) >= 3
     ORDER BY occurrences DESC LIMIT 20;

Q: Combien j'ai depense en epicerie le mois dernier ?
SQL: SELECT SUM(amount) AS total
     FROM credit_card_transactions
     WHERE amount > 0
       AND transaction_date >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')
       AND transaction_date <  DATE_TRUNC('month', CURRENT_DATE)
       AND (description ILIKE '%iga%' OR description ILIKE '%metro%'
            OR description ILIKE '%maxi%' OR description ILIKE '%super c%'
            OR description ILIKE '%provigo%' OR description ILIKE '%loblaws%'
            OR description ILIKE '%walmart%' OR description ILIKE '%costco%');

Q: Mes 5 dernieres transactions ?
SQL: SELECT id, transaction_date, description, debit, credit, balance_after
     FROM transactions
     ORDER BY transaction_date DESC, created_at DESC LIMIT 5;

Q: Quelle est la tendance de mon portefeuille sur les 6 derniers releves ?
SQL: SELECT statement_date, currency, SUM(market_value) AS valeur_totale
     FROM investment_positions
     GROUP BY statement_date, currency
     ORDER BY statement_date DESC LIMIT 12;

Q: Mes positions Nvidia actuelles ?
SQL: SELECT statement_date, symbol, description, quantity, market_price, market_value, currency
     FROM investment_positions
     WHERE symbol ILIKE 'NVDA' OR description ILIKE '%nvidia%'
     ORDER BY statement_date DESC LIMIT 10;

Q: Combien de dividendes recus en 2025 ?
SQL: SELECT currency, SUM(amount) AS total_dividendes, COUNT(*) AS nb
     FROM investment_transactions
     WHERE operation ILIKE '%dividende%'
       AND transaction_date BETWEEN '2025-01-01' AND '2025-12-31'
     GROUP BY currency;

-- Emails : exemples ----------------------------------------------------------

Q: Combien d'emails non-lus ?
SQL: SELECT COUNT(*) AS non_lus FROM emails WHERE is_unread = TRUE;

Q: Mes 10 derniers emails recus ?
SQL: SELECT id, sent_at, sender_email, subject, snippet
     FROM emails
     WHERE 'INBOX' = ANY(labels)
     ORDER BY sent_at DESC LIMIT 10;

Q: Emails de Hydro Quebec cette annee ?
SQL: SELECT sent_at, subject, snippet
     FROM emails
     WHERE sender_email ILIKE '%hydroquebec%'
       AND sent_at >= DATE_TRUNC('year', CURRENT_DATE)
     ORDER BY sent_at DESC;

Q: Top 10 expediteurs qui m'envoient le plus d'emails ?
SQL: SELECT sender_email, COUNT(*) AS nb
     FROM emails
     GROUP BY sender_email
     ORDER BY nb DESC LIMIT 10;

Q: Combien d'emails recus la semaine derniere ?
SQL: SELECT COUNT(*) AS nb FROM emails
     WHERE sent_at >= DATE_TRUNC('week', CURRENT_DATE - INTERVAL '1 week')
       AND sent_at <  DATE_TRUNC('week', CURRENT_DATE);

-- Calendar : exemples --------------------------------------------------------

Q: Mes evenements aujourd'hui ?
SQL: SELECT id, start_at, end_at, summary, location, all_day
     FROM calendar_events
     WHERE start_at::date = CURRENT_DATE
       AND status IS DISTINCT FROM 'cancelled'
     ORDER BY start_at;

Q: Evenements de la semaine prochaine ?
SQL: SELECT start_at, summary, location
     FROM calendar_events
     WHERE start_at >= DATE_TRUNC('week', CURRENT_DATE + INTERVAL '1 week')
       AND start_at <  DATE_TRUNC('week', CURRENT_DATE + INTERVAL '2 week')
       AND status IS DISTINCT FROM 'cancelled'
     ORDER BY start_at;

Q: Combien d'heures de meetings j'ai eu en mars 2025 ?
SQL: SELECT SUM(EXTRACT(EPOCH FROM (end_at - start_at)) / 3600) AS heures
     FROM calendar_events
     WHERE start_at BETWEEN '2025-03-01' AND '2025-03-31'
       AND all_day = FALSE
       AND status IS DISTINCT FROM 'cancelled';

Q: Mes prochains anniversaires de contacts ?
SQL: SELECT display_name, birthday,
            (DATE_TRUNC('year', CURRENT_DATE) + (birthday - DATE_TRUNC('year', birthday)))::date AS prochain
     FROM contacts
     WHERE birthday IS NOT NULL
     ORDER BY (DATE_TRUNC('year', CURRENT_DATE) + (birthday - DATE_TRUNC('year', birthday)))
              - CURRENT_DATE
     LIMIT 10;

-- Tasks : exemples -----------------------------------------------------------

Q: Mes taches en retard ?
SQL: SELECT id, title, tasklist_title, due_at, notes
     FROM tasks
     WHERE is_completed = FALSE
       AND due_at IS NOT NULL
       AND due_at < NOW()
     ORDER BY due_at;

Q: Combien de taches j'ai terminees ce mois ?
SQL: SELECT COUNT(*) AS terminees FROM tasks
     WHERE is_completed = TRUE
       AND completed_at >= DATE_TRUNC('month', CURRENT_DATE);

Q: Mes taches a faire cette semaine ?
SQL: SELECT title, tasklist_title, due_at
     FROM tasks
     WHERE is_completed = FALSE
       AND due_at BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'
     ORDER BY due_at;

-- Photos : exemples ----------------------------------------------------------

Q: Combien de photos j'ai en tout ?
SQL: SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE is_video) AS videos
     FROM photos;

Q: Photos prises en juillet 2024 ?
SQL: SELECT id, creation_time, filename, camera_model, location_name
     FROM photos
     WHERE creation_time BETWEEN '2024-07-01' AND '2024-07-31'
     ORDER BY creation_time DESC LIMIT 50;

Q: Mes photos avec GPS prises a l'etranger ?
SQL: SELECT creation_time, filename, latitude, longitude, location_name
     FROM photos
     WHERE latitude IS NOT NULL
       AND NOT (latitude BETWEEN 45 AND 49 AND longitude BETWEEN -75 AND -69)
     ORDER BY creation_time DESC LIMIT 50;

Q: Photos prises avec mon iPhone ?
SQL: SELECT COUNT(*) AS total, MIN(creation_time) AS premiere, MAX(creation_time) AS derniere
     FROM photos
     WHERE camera_make ILIKE '%apple%';

-- Drive : exemples -----------------------------------------------------------

Q: Mes 10 fichiers Drive les plus recents ?
SQL: SELECT name, mime_type, size_bytes, modified_time, web_view_link
     FROM drive_files
     WHERE trashed = FALSE
     ORDER BY modified_time DESC NULLS LAST LIMIT 10;

Q: Combien de PDF j'ai sur Drive ?
SQL: SELECT COUNT(*) AS nb, COALESCE(SUM(size_bytes), 0) AS taille_totale_bytes
     FROM drive_files
     WHERE mime_type = 'application/pdf' AND trashed = FALSE;

-- Sante : exemples -----------------------------------------------------------

Q: Combien de pas hier ?
SQL: SELECT SUM(value) AS pas
     FROM health_metrics
     WHERE metric = 'steps' AND date = CURRENT_DATE - INTERVAL '1 day';

Q: Moyenne de pas par jour ce mois-ci ?
SQL: SELECT ROUND(AVG(value)::numeric, 0) AS pas_moyens, COUNT(DISTINCT date) AS jours
     FROM health_metrics
     WHERE metric = 'steps' AND date >= DATE_TRUNC('month', CURRENT_DATE);

Q: Combien d'heures j'ai dormi en moyenne la semaine derniere ?
SQL: SELECT ROUND(AVG(value)::numeric / 60, 1) AS heures_par_nuit
     FROM health_metrics
     WHERE metric = 'sleep_total_min'
       AND date >= CURRENT_DATE - INTERVAL '7 days';

Q: Mon poids sur les 30 derniers jours ?
SQL: SELECT date, value AS kg
     FROM health_metrics
     WHERE metric = 'weight_kg'
       AND date >= CURRENT_DATE - INTERVAL '30 days'
     ORDER BY date;

-- YouTube : exemples ---------------------------------------------------------

Q: Mes 10 dernieres videos likees ?
SQL: SELECT published_at, video_title, channel_title
     FROM youtube_activities
     WHERE activity_type = 'like'
     ORDER BY published_at DESC LIMIT 10;

Q: Mes chaines YouTube les plus regardees (par nombre d'activites) ?
SQL: SELECT channel_title, COUNT(*) AS nb
     FROM youtube_activities
     WHERE channel_title IS NOT NULL
     GROUP BY channel_title
     ORDER BY nb DESC LIMIT 10;

-- Actualites : exemples -----------------------------------------------------

Q: Les actualites du jour ?
SQL: SELECT id, published_at, source, title, summary
     FROM news_articles
     WHERE published_at >= CURRENT_DATE
     ORDER BY published_at DESC LIMIT 20;

Q: Articles de Radio-Canada cette semaine ?
SQL: SELECT id, published_at, title, summary, link
     FROM news_articles
     WHERE source ILIKE '%radio-canada%'
       AND published_at >= CURRENT_DATE - INTERVAL '7 days'
     ORDER BY published_at DESC LIMIT 30;

-- Contacts : exemples --------------------------------------------------------

Q: Combien de contacts dans mon carnet ?
SQL: SELECT COUNT(*) AS total,
            COUNT(*) FILTER (WHERE birthday IS NOT NULL) AS avec_anniversaire,
            COUNT(*) FILTER (WHERE array_length(phones, 1) > 0) AS avec_tel
     FROM contacts;

Q: Mes contacts qui travaillent chez Google ?
SQL: SELECT display_name, organizations, emails
     FROM contacts
     WHERE EXISTS (SELECT 1 FROM unnest(organizations) AS o WHERE o ILIKE '%google%');
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
    "Toujours utiliser ILIKE pour les comparaisons texte (insensible a la casse). "
    "Pour les listes de lignes (non agregees), inclus toujours `id` en PREMIERE colonne. "
    "INTERDIT : UNION et UNION ALL (les tables ont des schemas differents). "
    "Routage : 'emails' parle du contenu de la table emails (Gmail), pas des champs "
    "emails dans contacts. Exemple : 'mes emails' -> FROM emails. "
    "Si la question est trop vague pour cibler UNE seule table (ex: 'tout mon data', "
    "'liste tout'), retourne EXACTEMENT ce SQL : "
    "SELECT 'Question trop vague, precise une categorie : transactions, comptes, "
    "investissements, trajets, etc.' AS message"
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
    # Phase 1 : Finance
    "accounts",
    "transactions",
    "credit_card_transactions",
    "investment_transactions",
    "investment_positions",
    # Phase 2 : Localisation
    "location_visits",
    "location_activities",
    "location_points",
    "location_addresses",
    # Phase 3 : Emails + Calendar
    "emails",
    "calendar_events",
    # Phase 3c : Photos + Drive
    "photos",
    "drive_files",
    # Phase 4 : Sante
    "health_metrics",
    # Phase 5 : Tasks + Contacts
    "tasks",
    "contacts",
    # Phase 6 : YouTube
    "youtube_activities",
    # Annotations Marc
    "named_places",
    "trip_notes",
    # Phase 6 : News
    "news_articles",
}

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|VACUUM|REINDEX|CLUSTER|EXPLAIN|ANALYZE|LOCK|"
    r"MERGE|CALL|DO|LOAD|"
    r"SET\s+ROLE|SET\s+SESSION|"
    r"pg_read_file|pg_ls_dir|pg_sleep|pg_terminate_backend|dblink)\b",
    re.IGNORECASE,
)


def _extract_sql(text_in: str) -> str:
    """Extrait le SQL d'une reponse LLM qui peut contenir explication + markdown.

    Strategie defensive : Qwen 14B suit pas toujours l'instruction "juste le SQL,
    aucune explication". Il prefixe parfois "Pour obtenir X, voici la requete :",
    wrap dans ``` ou ajoute du blabla apres. On extrait robustement :
      1. Strip les fences markdown ```sql / ```
      2. Strip les prefixes 'SQL:' / 'Q:' / 'Query:' / 'Requete:'
      3. Si le texte contient SELECT ou WITH, on tronque tout ce qui precede
         le premier match (case-insensitive). Ca elimine les "Pour obtenir..."
      4. Si le texte contient ';' apres un mot-cle SQL, on coupe a ce ';'
         pour eviter le blabla post-SQL.

    Retourne le SQL nettoye (peut encore etre invalide, _validate_sql tranchera).
    """
    s = text_in.strip()
    # 1. Markdown fences
    s = re.sub(r"```(?:sql|postgres|postgresql)?", "", s, flags=re.IGNORECASE)
    s = s.replace("```", "").strip()
    # 2. Prefixes en debut de ligne
    s = re.sub(r"^(?:SQL|Q|Query|Requete|Reponse)\s*:\s*", "", s, flags=re.IGNORECASE).strip()
    # 3. Tronque tout ce qui precede le premier SELECT/WITH (case-insensitive,
    #    ancre sur word-boundary pour eviter de matcher "selecting" dans une phrase).
    m = re.search(r"\b(WITH|SELECT)\b", s, flags=re.IGNORECASE)
    if m:
        s = s[m.start() :]
    # 4. Coupe au premier ';' (fin d'instruction SQL standard) -> elimine le
    #    blabla post-SQL ("Cette requete selectionne...").
    semi = s.find(";")
    if semi != -1:
        s = s[: semi + 1]
    return s.strip()


def _validate_sql(sql: str) -> str:
    """Verifie que le SQL est read-only et touche seulement les tables autorisees."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("SQL vide")
    # Strip les commentaires SQL (line + block) AVANT validation, sinon le LLM
    # pourrait planquer des mots-cles interdits dedans (ex: "-- DROP TABLE").
    cleaned = re.sub(r"--[^\n]*", "", cleaned)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned = cleaned.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("SQL vide apres suppression des commentaires")
    # Mot-cles interdits VERIFIES EN PREMIER : message d erreur plus parlant
    # quand le LLM genere un INSERT/DELETE/etc.
    if _FORBIDDEN_KEYWORDS.search(cleaned):
        raise ValueError("Mot-cle interdit detecte (INSERT/UPDATE/DELETE/...)")
    if not cleaned.lower().lstrip("(").startswith(("select", "with")):
        raise ValueError("Le SQL doit commencer par SELECT ou WITH")

    # Extraction des noms de CTE pour les ajouter aux tables autorisees.
    # Pattern : "WITH [RECURSIVE] foo AS (...)" ou ", bar AS (...)" en cascade.
    cte_names = {
        m.group(1).lower()
        for m in re.finditer(r"\bWITH\s+(?:RECURSIVE\s+)?(\w+)\s+AS\b", cleaned, re.IGNORECASE)
    }
    cte_names |= {
        m.group(1).lower() for m in re.finditer(r",\s*(\w+)\s+AS\s*\(", cleaned, re.IGNORECASE)
    }
    allowed = _ALLOWED_TABLES | cte_names

    # Detection naive des tables. On extrait tout ce qui ressemble a un identifiant
    # apres FROM ou JOIN, et on verifie qu'il est whitelistee.
    table_refs = re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", cleaned, re.IGNORECASE)
    for tbl in table_refs:
        if tbl.lower() not in allowed:
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
    """Questionne le hub en francais. LLM -> SQL -> exec -> LLM -> reponse.

    Mode conversationnel : si payload.history est fourni, le LLM voit les Q/R
    precedentes pour resoudre les references contextuelles (et avant ?, le suivant ?).
    """
    # 1. Pass 1 LLM : genere le SQL avec contexte historique.
    history_block = ""
    if payload.history:
        # Garde les 6 derniers tours (3 Q/R) pour ne pas exploser le prompt
        recent = payload.history[-6:]
        lines = []
        for msg in recent:
            role = msg.get("role", "user")
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                lines.append(f"Q precedente: {content}")
            elif role == "assistant":
                lines.append(f"Reponse precedente: {content[:200]}")
        if lines:
            history_block = (
                "Contexte de la conversation (pour resoudre 'et avant ?', 'le suivant ?', etc.) :\n"
                + "\n".join(lines)
                + "\n\n"
            )

    # Format prompt avec separateur fort entre examples et la VRAIE question
    # pour empecher Qwen de "continuer" la liste d'exemples au lieu de repondre.
    sql_prompt = (
        f"{_DB_SCHEMA}\n\n{_FEW_SHOT_EXAMPLES}\n\n"
        f"--- FIN DES EXEMPLES ---\n"
        f"Maintenant reponds UNIQUEMENT a la question suivante avec UN SEUL SQL "
        f"(pas d'explication, pas d'exemples additionnels) :\n\n"
        f"{history_block}Question: {payload.question}\nSQL:"
    )

    try:
        # Timeout 180s : le qwen2.5:14b peut prendre 60-120s sur prompt long (cold start).
        async with httpx.AsyncClient(timeout=180.0) as client:
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
        # type(e).__name__ est crucial : httpx.TimeoutException a str() vide
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Generation LLM echouee : {type(e).__name__}: {e!r}",
        ) from e

    # Nettoyage robuste : explication FR + markdown + prefixes que le LLM ajoute
    # parfois meme avec temperature=0 (Qwen 14B sans QLoRA fine-tune).
    generated = _extract_sql(generated)

    try:
        sql = _validate_sql(generated)
    except ValueError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"SQL genere refuse : {e}. Genere : {generated[:200]!r}",
        ) from e

    # 2. Execute en lecture seule avec timeout serveur.
    # Note : SET LOCAL statement_timeout est Postgres-only ; skip sur SQLite.
    try:
        dialect_name = db.bind.dialect.name if db.bind else ""
        if dialect_name == "postgresql":
            await db.execute(text("SET LOCAL statement_timeout = 5000"))  # 5 secondes
        result = await db.execute(text(sql))
        rows = [dict(r._mapping) for r in result]
    except Exception as e:
        # Fallback gracieux : log + retourne une reponse sans crasher.
        logger.error(
            "ai_sql_execution_failed: sql=%r error_type=%s error=%r",
            sql[:300],
            type(e).__name__,
            e,
        )
        return AskResponse(
            answer=(
                "Je n'ai pas pu trouver une reponse precise dans tes donnees. "
                "Essaie une question plus specifique (ex: 'mes 10 dernieres transactions', "
                "'depenses en restos en mars'), ou utilise le mode 'Discussion'."
            ),
            sql=sql,
            rows=[],
            row_count=0,
        )

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


# ---------------------------------------------------------------------
# Streaming /ask via SSE — meme logique que /ask mais avec progressive disclosure
# ---------------------------------------------------------------------


@router.post("/ask/stream", summary="Streaming SSE de la generation AI ask")
async def ask_stream(
    payload: AskRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Version streaming de /ask : emet des events SSE en cours de generation.

    Events emis :
    - stage     : indique l'etape courante (sql_generation, sql_validation,
                  sql_execution, answer_generation, done)
    - sql       : le SQL valide (apres pass 1 LLM + validation)
    - rows      : les rows de la DB (apres execution)
    - token     : chaque token de la reponse finale (pass 2 LLM streaming)
    - error     : si quelque chose casse
    - done      : payload final identique a AskResponse

    Frontend consomme via fetch + ReadableStream OU EventSource.
    """
    import json as _json

    from fastapi.responses import StreamingResponse

    async def event_stream():
        def _emit(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {_json.dumps(data, default=str)}\n\n"

        # Build sql_prompt avec history (reuse logique de /ask)
        history_block = ""
        if payload.history:
            recent = payload.history[-6:]
            lines = []
            for msg in recent:
                role = msg.get("role", "user")
                content = (msg.get("content") or "").strip()
                if not content:
                    continue
                if role == "user":
                    lines.append(f"Q precedente: {content}")
                elif role == "assistant":
                    lines.append(f"Reponse precedente: {content[:200]}")
            if lines:
                history_block = "Contexte de la conversation :\n" + "\n".join(lines) + "\n\n"

        sql_prompt = (
            f"{_DB_SCHEMA}\n\n{_FEW_SHOT_EXAMPLES}\n\n"
            f"--- FIN DES EXEMPLES ---\n"
            f"Maintenant reponds UNIQUEMENT a la question suivante avec UN SEUL SQL "
            f"(pas d'explication, pas d'exemples additionnels) :\n\n"
            f"{history_block}"
            f"Question: {payload.question}\nSQL:"
        )

        # ── Stage 1 : SQL generation
        yield _emit("stage", {"stage": "sql_generation", "label": "Generation SQL..."})

        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
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
            yield _emit(
                "error",
                {"stage": "sql_generation", "error": f"{type(e).__name__}: {e!r}"},
            )
            return

        # Cleanup robuste : explication FR + markdown + prefixes
        generated = _extract_sql(generated)

        # ── Stage 2 : Validation
        yield _emit("stage", {"stage": "sql_validation", "label": "Validation..."})
        try:
            sql = _validate_sql(generated)
        except ValueError as e:
            yield _emit(
                "error",
                {
                    "stage": "sql_validation",
                    "error": str(e),
                    "generated": generated[:200],
                },
            )
            return

        yield _emit("sql", {"sql": sql})

        # ── Stage 3 : Execution
        yield _emit("stage", {"stage": "sql_execution", "label": "Execution DB..."})
        try:
            dialect_name = db.bind.dialect.name if db.bind else ""
            if dialect_name == "postgresql":
                await db.execute(text("SET LOCAL statement_timeout = 5000"))
            result = await db.execute(text(sql))
            rows = [dict(r._mapping) for r in result]
        except Exception as e:
            logger.error("ai_stream_sql_failed: sql=%r error=%r", sql[:300], e)
            yield _emit(
                "error",
                {"stage": "sql_execution", "error": f"{type(e).__name__}: {e!r}"},
            )
            yield _emit(
                "done",
                {
                    "answer": (
                        "Je n'ai pas pu trouver une reponse precise. "
                        "Essaie une question plus specifique."
                    ),
                    "sql": sql,
                    "rows": [],
                    "row_count": 0,
                },
            )
            return

        yield _emit("rows", {"rows": rows[:50], "row_count": len(rows)})

        # ── Stage 4 : Reformulation streaming
        yield _emit(
            "stage",
            {"stage": "answer_generation", "label": "Generation reponse..."},
        )

        rendered_rows = "\n".join(str(r) for r in rows[:20]) if rows else "(aucun resultat)"
        answer_prompt = (
            f"Question : {payload.question}\n\n"
            f"Resultat SQL ({len(rows)} ligne(s)) :\n{rendered_rows}\n\n"
            "Repond en francais, en 1-3 phrases courtes et factuelles. "
            "Cite les chiffres exacts."
        )

        full_answer = ""
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{settings.ollama_base_url}/api/generate",
                    json={
                        "model": settings.ollama_model,
                        "system": _ANSWER_SYSTEM_PROMPT,
                        "prompt": answer_prompt,
                        "stream": True,
                        "options": {"temperature": 0.2},
                    },
                ) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = _json.loads(line)
                        except Exception:
                            continue
                        token = chunk.get("response", "")
                        if token:
                            full_answer += token
                            yield _emit("token", {"token": token})
                        if chunk.get("done"):
                            break
        except Exception as e:
            logger.error("ai_stream_answer_failed: %r", e)
            full_answer = full_answer or f"(LLM indisponible : {type(e).__name__}: {e!r})"

        # ── Done : payload final identique a AskResponse
        yield _emit(
            "done",
            {
                "answer": full_answer.strip(),
                "sql": sql,
                "rows": rows,
                "row_count": len(rows),
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Pour proxies type Nginx
        },
    )


# ---------------------------------------------------------------------
# Chat libre (sans SQL/DB)
# ---------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(
        default_factory=list,
        description="Historique [{role: 'user'|'assistant', content: str}, ...]",
    )


class ChatResponse(BaseModel):
    answer: str
    model: str


_CHAT_SYSTEM_PROMPT = (
    "Tu es l'assistant personnel de Marc, integre dans son hub de donnees personnel. "
    "Tu reponds en francais, naturel, concis et utile. "
    "Si Marc te pose une question sur ses donnees personnelles "
    "(transactions, depenses, trajets, mails, etc.), suggere-lui d'utiliser le mode "
    "'Recherche dans mes donnees' qui execute du SQL sur sa DB. "
    "Sinon reponds directement comme un assistant LLM normal."
)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChatResponse:
    """Discussion libre avec l'IA, sans toucher la DB."""
    # Construit le prompt avec l'historique de conversation
    convo_lines = []
    for msg in payload.history[-10:]:  # garde les 10 derniers tours max
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content:
            continue
        prefix = "Marc" if role == "user" else "Assistant"
        convo_lines.append(f"{prefix}: {content}")
    convo_lines.append(f"Marc: {payload.message}")
    convo_lines.append("Assistant:")
    prompt = "\n\n".join(convo_lines)

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "system": _CHAT_SYSTEM_PROMPT,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.7},
                },
            )
            r.raise_for_status()
            answer = r.json().get("response", "").strip()
    except Exception as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Chat LLM echoue : {type(e).__name__}: {e!r}",
        ) from e

    return ChatResponse(answer=answer, model=settings.ollama_model)
