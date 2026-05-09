"""Logging structuré via structlog."""

import logging
import os
import re
import sys
from typing import Any

import structlog

# Cles de log dont la valeur doit etre redactee (matching case-insensitive)
_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"authorization|cookie|jwt|fernet|encrypted)",
    re.IGNORECASE,
)


def _redact_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact les valeurs des cles qui ressemblent a des secrets.

    Defense en profondeur : si un developpeur fait `logger.info("oauth", token=t)`
    par erreur, le token n'apparait pas en clair dans les logs.
    """
    for key in list(event_dict.keys()):
        if _SENSITIVE_KEY_RE.search(key):
            event_dict[key] = "***REDACTED***"
    return event_dict


def setup_logging(level: str = "INFO") -> None:
    """Configure logging globalement.

    En dev, les logs sont colorés et lisibles (ConsoleRenderer).
    En prod (APP_ENV=prod/production), JSON structuré pour agrégateurs (Loki, etc.).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    app_env = os.getenv("APP_ENV", "dev").strip().lower()
    is_production = app_env in ("prod", "production")
    renderer = (
        structlog.processors.JSONRenderer() if is_production else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


logger = structlog.get_logger()


def mask_email(email: str) -> str:
    """Masque l'email pour les logs — évite de persister du PII en clair.

    'marc.richard4@gmail.com' → 'mar***@gmail.com'
    """
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    prefix = local[:3] if len(local) >= 3 else local
    return f"{prefix}***@{domain}"
