"""Stable campaign IDs derived from save-persistent HOI4 campaign seeds.

Clausewitz exposes persistent numeric variables but no documented UUID/hash
effect. The mod writes four integer seeds once into global save state. HOI4's
log formatter can emit those variable values as signed 32-bit integers even
when the scripted random range has a positive lower bound, so the Bridge must
accept canonical non-zero signed values. Legacy UUID markers continue to be
parsed by the Bridge and are not rewritten.
"""

from __future__ import annotations

import hashlib
import re


MIN_SAVE_SEED = -(2 ** 31)
MAX_SAVE_SEED = (2 ** 31) - 1
SAVE_CAMPAIGN_IDENTITY_FORMAT = "signed-int32-four-tuple-v1"
_CANONICAL_SIGNED_SEED = r"-?(?:0|[1-9][0-9]*)"
SAVE_CAMPAIGN_TOKEN_RE = re.compile(
    rf"^({_CANONICAL_SIGNED_SEED})-({_CANONICAL_SIGNED_SEED})-"
    rf"({_CANONICAL_SIGNED_SEED})-({_CANONICAL_SIGNED_SEED})$"
)


def campaign_id_from_save_token(token: str) -> str:
    """Return a deterministic 128-bit hex ID for four canonical signed seeds.

    Positive legacy tokens retain the same digest because the exact token bytes
    remain the hash input. Zero is rejected because the game uses it as the
    missing-variable fallback in its log format.
    """
    match = SAVE_CAMPAIGN_TOKEN_RE.fullmatch(token) if isinstance(token, str) else None
    if match is None:
        raise ValueError("save campaign token must contain four canonical signed integers")
    values = tuple(int(value) for value in match.groups())
    if any(value == 0 or value < MIN_SAVE_SEED or value > MAX_SAVE_SEED for value in values):
        raise ValueError("save campaign token integer is zero or outside the signed HOI4 range")
    digest = hashlib.sha256(
        b"hoi4-eyes-save-campaign-v1\0" + token.encode("ascii")
    ).hexdigest()
    return digest[:32]
