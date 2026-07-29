"""Canonical Substrate transaction-hash validation."""

from __future__ import annotations

import re

_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}\Z")


def validate_tx_hash(value: str) -> str:
    """Return a valid 32-byte, ``0x``-prefixed hexadecimal transaction hash."""
    if _TX_HASH_RE.fullmatch(value) is None:
        raise ValueError("transaction hash must be a 32-byte 0x-prefixed hexadecimal value")
    return value


__all__ = ["validate_tx_hash"]
