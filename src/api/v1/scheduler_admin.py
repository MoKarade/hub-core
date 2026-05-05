"""Endpoint /v1/scheduler - status + manual trigger des jobs auto-sync.

Permet a Marc :
  - GET /v1/scheduler/status   : liste les jobs + prochain run
  - POST /v1/scheduler/run/{job} : lance un job tout de suite

Note : pas de protection auth ici, on assume que Cloudflare Access protege
deja l'instance derriere le tunnel. En dev local c'est ouvert.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from src.scheduler import list_jobs_status, run_job_now

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.get("/status")
def scheduler_status() -> dict[str, Any]:
    """Liste les jobs auto-sync configures et leur prochain run."""
    jobs = list_jobs_status()
    return {
        "running": len(jobs) > 0,
        "jobs": jobs,
    }


@router.post("/run/{job_id}")
async def trigger_job(job_id: str) -> dict[str, Any]:
    """Force l'execution immediate d'un job (utile pour tester)."""
    try:
        return await run_job_now(job_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
