"""Dependency-free validation for canonical Bittensor SS58 account addresses."""

from __future__ import annotations

from hashlib import blake2b
from typing import Final

BITTENSOR_SS58_FORMAT: Final[int] = 42
BITTENSOR_SS58_LENGTH: Final[int] = 48
_ACCOUNT_ID_LENGTH: Final[int] = 32
_CHECKSUM_LENGTH: Final[int] = 2
_SS58_PREFIX: Final[bytes] = b"SS58PRE"
_BASE58_ALPHABET: Final[str] = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_VALUES: Final[dict[str, int]] = {
    character: index for index, character in enumerate(_BASE58_ALPHABET)
}


def _base58_decode(value: str) -> bytes:
    """Decode one canonical Bitcoin-base58 string without a runtime dependency."""
    number = 0
    for character in value:
        try:
            digit = _BASE58_VALUES[character]
        except KeyError as exc:
            raise ValueError("address contains a non-base58 character") from exc
        number = number * 58 + digit

    payload = number.to_bytes((number.bit_length() + 7) // 8, byteorder="big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\0" * leading_zeroes + payload


def validate_bittensor_ss58(value: str) -> str:
    """Return *value* when it is a canonical format-42, 32-byte SS58 account.

    Bittensor mainnet and local development chains both use SS58 format 42.
    Shape checks alone are insufficient: the final two bytes are a Blake2b
    checksum over the format byte and 32-byte account id.
    """
    message = (
        "expected a canonical Bittensor SS58 address "
        "(format 42, 32-byte account id, valid checksum)"
    )
    if len(value) != BITTENSOR_SS58_LENGTH or not value.startswith("5"):
        raise ValueError(message)
    try:
        decoded = _base58_decode(value)
    except ValueError as exc:
        raise ValueError(message) from exc

    expected_length = 1 + _ACCOUNT_ID_LENGTH + _CHECKSUM_LENGTH
    if len(decoded) != expected_length or decoded[0] != BITTENSOR_SS58_FORMAT:
        raise ValueError(message)

    payload = decoded[:-_CHECKSUM_LENGTH]
    checksum = decoded[-_CHECKSUM_LENGTH:]
    expected_checksum = blake2b(_SS58_PREFIX + payload, digest_size=64).digest()[:_CHECKSUM_LENGTH]
    if checksum != expected_checksum:
        raise ValueError(message)
    return value


__all__ = [
    "BITTENSOR_SS58_FORMAT",
    "BITTENSOR_SS58_LENGTH",
    "validate_bittensor_ss58",
]
