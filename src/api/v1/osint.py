"""Endpoint /v1/osint - integration outils OSINT (Holehe, Sherlock).

Permet a Marc de scanner son email/username contre 100+ services en un appel,
au lieu d'ouvrir un terminal et lancer la commande CLI manuellement.

Approche : subprocess sur les binaires installes via pip.
Si non installes, retourne 503 avec lien vers les instructions install.

Holehe : https://github.com/megadose/holehe (email -> 120+ services)
Sherlock : https://github.com/sherlock-project/sherlock (username -> 400+ networks)
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from src.core.rate_limit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/osint", tags=["osint"])


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


class HoleheRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError("Email invalide")
        return v.lower()


class SherlockRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        if not _USERNAME_RE.match(v):
            raise ValueError("Username invalide (caractères autorisés : A-Z, a-z, 0-9, . _ -)")
        return v


class OsintHit(BaseModel):
    """Un site/service ou l'email/username apparait."""

    service: str
    url: str | None = None
    status: Literal["found", "not_found", "rate_limited", "error"]
    extra: dict[str, str] = Field(default_factory=dict)


class OsintResponse(BaseModel):
    tool: Literal["holehe", "sherlock"]
    target: str
    duration_seconds: float
    total_checked: int
    found_count: int
    hits: list[OsintHit]


# ---------------------------------------------------------------------
# Helpers : detection des binaires + execution subprocess
# ---------------------------------------------------------------------


def _find_tool(tool: str) -> str | None:
    """Cherche le binaire dans le PATH ou les venvs courants."""
    return shutil.which(tool)


def _minimal_env() -> dict[str, str]:
    """Env minimal pour subprocess OSINT — pas de leak des secrets app.

    Ne passe que PATH (nécessaire pour résoudre les binaires), HOME, et les
    locales. PAS SECRET_KEY, GOOGLE_OAUTH_CLIENT_SECRET, DATABASE_URL etc.
    Si une version compromise de holehe/sherlock arrive via pip, elle ne peut
    pas exfiltrer les secrets de l'app.
    """
    import os

    keep = ("PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONIOENCODING")
    return {k: os.environ[k] for k in keep if k in os.environ}


async def _run_subprocess(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    """Run un subprocess async avec timeout. Retourne (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_minimal_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            f"OSINT tool timeout ({timeout}s) - le scan a depasse la limite",
        ) from None
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


# ---------------------------------------------------------------------
# Holehe
# ---------------------------------------------------------------------


# Format de sortie holehe (sans couleurs ANSI) :
#  [+] Twitter
#  [+] Spotify
#  [-] Adobe
#  [-] LinkedIn
# [+] = found, [-] = not found
_HOLEHE_LINE = re.compile(r"^\s*\[(\+|-|x)\]\s+(.+?)\s*$")


def _parse_holehe_output(stdout: str) -> list[OsintHit]:
    """Parse la sortie texte de holehe (pas de mode JSON officiel)."""
    hits: list[OsintHit] = []
    # Strip ANSI escape codes au cas ou
    cleaned = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", stdout)
    for line in cleaned.splitlines():
        m = _HOLEHE_LINE.match(line)
        if not m:
            continue
        marker, service = m.group(1), m.group(2).strip()
        if not service or service.lower() in ("twitter", "domain", "email"):
            # skip header noise
            if not service or len(service) < 2:
                continue
        match marker:
            case "+":
                hits.append(OsintHit(service=service, status="found"))
            case "-":
                hits.append(OsintHit(service=service, status="not_found"))
            case "x":
                hits.append(OsintHit(service=service, status="rate_limited"))
    return hits


@router.post("/holehe", response_model=OsintResponse, dependencies=[Depends(rate_limit(5, 60))])
async def scan_email_holehe(payload: HoleheRequest) -> OsintResponse:
    """Scan l'email contre 120+ services via Holehe.

    Holehe doit etre installe (`pip install holehe`).
    Le binaire est cherche dans le PATH.
    """
    binary = _find_tool("holehe")
    if not binary:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "holehe non installe. Lance: pip install holehe",
        )

    import time

    start = time.monotonic()
    rc, stdout, stderr = await _run_subprocess(
        [binary, "--only-used", str(payload.email)],
        timeout=180.0,
    )
    duration = time.monotonic() - start

    if rc != 0 and not stdout:
        logger.error("holehe_failed: rc=%d stderr=%s", rc, stderr[:500])
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"holehe a echoue (rc={rc}): {stderr[:200]}",
        )

    hits = _parse_holehe_output(stdout)
    found = [h for h in hits if h.status == "found"]
    return OsintResponse(
        tool="holehe",
        target=str(payload.email),
        duration_seconds=round(duration, 2),
        total_checked=len(hits),
        found_count=len(found),
        hits=hits,
    )


# ---------------------------------------------------------------------
# Sherlock
# ---------------------------------------------------------------------


@router.post("/sherlock", response_model=OsintResponse, dependencies=[Depends(rate_limit(5, 60))])
async def scan_username_sherlock(payload: SherlockRequest) -> OsintResponse:
    """Scan le username contre 400+ reseaux sociaux via Sherlock.

    Sherlock doit etre installe (`pip install sherlock-project`).
    """
    # sherlock CLI binary name varie : sherlock ou sherlock-project
    binary = _find_tool("sherlock") or _find_tool("sherlock-project")
    if not binary:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "sherlock non installe. Lance: pip install sherlock-project",
        )

    import json as json_mod
    import os
    import tempfile
    import time

    # Sherlock supporte --json <file> pour output structure
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json_path = tmp.name

    try:
        start = time.monotonic()
        rc, stdout, stderr = await _run_subprocess(
            [
                binary,
                "--json",
                json_path,
                "--print-found",
                "--no-color",
                "--timeout",
                "10",
                payload.username,
            ],
            timeout=240.0,
        )
        duration = time.monotonic() - start

        # Sherlock peut returncode != 0 meme en succes, on essaie de parser le JSON
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json_mod.load(f)
        except (FileNotFoundError, json_mod.JSONDecodeError) as e:
            logger.error(
                "sherlock_json_unreadable: rc=%d err=%s stderr=%s",
                rc,
                str(e),
                stderr[:500],
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"sherlock JSON illisible : {e}",
            ) from e

        # data : { "ServiceName": { "url_user": "...", "status": {...}, ... } }
        hits: list[OsintHit] = []
        for svc, info in data.items():
            url = info.get("url_user")
            status_obj = info.get("status", {})
            status_name = status_obj.get("status") if isinstance(status_obj, dict) else None
            if status_name == "Claimed":
                hits.append(OsintHit(service=svc, url=url, status="found"))
            elif status_name == "Available":
                hits.append(OsintHit(service=svc, status="not_found"))
            elif status_name == "Unknown":
                hits.append(OsintHit(service=svc, status="error"))

        found = [h for h in hits if h.status == "found"]
        return OsintResponse(
            tool="sherlock",
            target=payload.username,
            duration_seconds=round(duration, 2),
            total_checked=len(hits),
            found_count=len(found),
            hits=hits,
        )
    finally:
        try:
            os.unlink(json_path)
        except OSError:
            pass


# ---------------------------------------------------------------------
# Status : verifier rapidement quels outils OSINT sont installes
# ---------------------------------------------------------------------


class OsintStatusResponse(BaseModel):
    holehe_installed: bool
    sherlock_installed: bool
    install_instructions: dict[str, str]


@router.get("/status", response_model=OsintStatusResponse)
async def osint_status() -> OsintStatusResponse:
    return OsintStatusResponse(
        holehe_installed=_find_tool("holehe") is not None,
        sherlock_installed=(
            _find_tool("sherlock") is not None or _find_tool("sherlock-project") is not None
        ),
        install_instructions={
            "holehe": "pip install holehe",
            "sherlock": "pip install sherlock-project",
        },
    )
