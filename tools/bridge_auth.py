"""Shared local Bridge-to-Flask authentication configuration."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from user_storage import runtime_dir
from typing import Optional


BRIDGE_TOKEN_HEADER = "X-HOI4-EYES-Bridge-Token"
BRIDGE_TOKEN_ENV = "EYES_BRIDGE_AUTH_TOKEN"
MIN_BRIDGE_TOKEN_LENGTH = 32


def default_bridge_token_path() -> Path:
    return runtime_dir() / "bridge_auth.token"


def load_bridge_token(explicit: Optional[str] = None) -> Optional[str]:
    """Load the process token from an explicit value, environment, or private runtime file.

    Missing configuration returns None so the caller can fail closed at the
    authenticated routes without preventing read-only Flask endpoints from
    starting. Malformed configured secrets are startup errors.
    """
    if explicit is not None:
        candidate = explicit
    else:
        candidate = os.getenv(BRIDGE_TOKEN_ENV)
        if candidate is None:
            explicit_file = os.getenv("EYES_BRIDGE_AUTH_FILE")
            try:
                path = Path(explicit_file) if explicit_file else default_bridge_token_path()
            except RuntimeError:
                # No determinable user profile is "no configuration", not a
                # startup crash: the authenticated routes still fail closed.
                return None
            try:
                candidate = path.read_text(encoding="ascii")
            except FileNotFoundError:
                return None
            except OSError as error:
                raise ValueError("Bridge authentication token file cannot be read") from error
    token = str(candidate).strip()
    if len(token) < MIN_BRIDGE_TOKEN_LENGTH or not token.isascii():
        raise ValueError("Bridge authentication token must be ASCII and contain at least 32 characters")
    return token


def bridge_auth_error(expected_token: Optional[str], supplied_token: Optional[str]):
    """Return (error code, HTTP status) when auth is absent or invalid."""
    if expected_token is None:
        return "bridge_auth_unconfigured", 503
    if (not isinstance(supplied_token, str) or not supplied_token.isascii()
            or not hmac.compare_digest(expected_token, supplied_token)):
        return "bridge_auth_required", 401
    return None
