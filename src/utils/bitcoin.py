"""Real Bitcoin address encodings (spec section 6 - synthetic data realism).

Synthetic addresses used to be random strings that merely *looked* like Bitcoin
addresses: the right alphabet, the right length, no checksum.  That is enough to
demo a graph and not enough to survive a validator: any Bitcoin library rejects
them, so "our data is modelled on real Bitcoin fields" was a claim with a hole in
it.

This module implements the real thing, from the specifications, with no external
dependency:

* **Base58Check** (P2PKH ``1...``, P2SH ``3...``) - version byte + 20-byte hash160
  + the first 4 bytes of ``SHA256(SHA256(version || payload))`` (BIP-13/BIP-16).
* **bech32** (P2WPKH ``bc1q...``, BIP-173) and **bech32m** (P2TR ``bc1p...``,
  BIP-350) - the BIP-173 polymod checksum over a converted 5-bit data stream.

Every generated address therefore passes an independent decoder, which is what
``scripts/audit_dataset.py`` proves on every run: it re-derives each checksum and
fails the dataset if a single address does not verify.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Optional, Tuple

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_CONST = 1
BECH32M_CONST = 0x2BC830A3

P2PKH_VERSION = 0x00          # mainnet "1..."
P2SH_VERSION = 0x05           # mainnet "3..."
BECH32_HRP = "bc"             # mainnet bech32 / bech32m


# --------------------------------------------------------------------------- #
# Base58Check
# --------------------------------------------------------------------------- #
def base58_encode(payload: bytes) -> str:
    number = int.from_bytes(payload, "big")
    encoded = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        encoded = B58_ALPHABET[remainder] + encoded
    leading_zeros = len(payload) - len(payload.lstrip(b"\x00"))
    return "1" * leading_zeros + encoded


def base58_decode(text: str) -> bytes:
    number = 0
    for char in text:
        if char not in B58_ALPHABET:
            raise ValueError(f"invalid base58 character {char!r}")
        number = number * 58 + B58_ALPHABET.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_ones = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_ones + body


def base58check_encode(version: int, payload: bytes) -> str:
    body = bytes([version]) + payload
    checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    return base58_encode(body + checksum)


def base58check_decode(address: str) -> Tuple[int, bytes]:
    """Return ``(version, payload)`` or raise ``ValueError`` if the checksum fails."""
    raw = base58_decode(address)
    if len(raw) < 5:
        raise ValueError("base58check payload too short")
    body, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != checksum:
        raise ValueError("base58check checksum mismatch")
    return body[0], body[1:]


def p2pkh_address(hash160: bytes) -> str:
    return base58check_encode(P2PKH_VERSION, hash160[:20])


def p2sh_address(hash160: bytes) -> str:
    return base58check_encode(P2SH_VERSION, hash160[:20])


# --------------------------------------------------------------------------- #
# bech32 / bech32m  (BIP-173, BIP-350)
# --------------------------------------------------------------------------- #
def _bech32_polymod(values: list) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index in range(5):
            if (top >> index) & 1:
                checksum ^= generator[index]
    return checksum


def _bech32_hrp_expand(hrp: str) -> list:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_create_checksum(hrp: str, data: list, spec: str) -> list:
    const = BECH32M_CONST if spec == "bech32m" else BECH32_CONST
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
    return [(polymod >> 5 * (5 - index)) & 31 for index in range(6)]


def _convertbits(data, from_bits: int, to_bits: int, pad: bool = True) -> list:
    accumulator = 0
    bits = 0
    result = []
    max_value = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise ValueError("invalid value in convertbits")
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)
    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        raise ValueError("convertbits: invalid padding")
    return result


def segwit_address(witness_version: int, program: bytes, hrp: str = BECH32_HRP) -> str:
    """Encode a segwit output address (v0 -> bech32, v1+ -> bech32m)."""
    if not 0 <= witness_version <= 16:
        raise ValueError("witness version out of range")
    spec = "bech32" if witness_version == 0 else "bech32m"
    data = [witness_version] + _convertbits(program, 8, 5)
    checksum = _bech32_create_checksum(hrp, data, spec)
    return hrp + "1" + "".join(BECH32_ALPHABET[value] for value in data + checksum)


def p2wpkh_address(hash160: bytes) -> str:
    return segwit_address(0, hash160[:20])


def p2tr_address(x_only_key: bytes) -> str:
    return segwit_address(1, x_only_key[:32])


def bech32_decode(address: str) -> Tuple[str, int, bytes, str]:
    """Return ``(hrp, witness_version, program, spec)`` or raise ``ValueError``."""
    if address.lower() != address and address.upper() != address:
        raise ValueError("mixed-case bech32 string")
    address = address.lower()
    position = address.rfind("1")
    if position < 1 or position + 7 > len(address):
        raise ValueError("bad bech32 separator position")
    hrp, payload = address[:position], address[position + 1:]
    if any(char not in BECH32_ALPHABET for char in payload):
        raise ValueError("invalid bech32 character")
    data = [BECH32_ALPHABET.index(char) for char in payload]
    if len(data) < 7:
        raise ValueError("bech32 payload too short")
    body, checksum = data[:-6], data[-6:]
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + body + checksum)
    if polymod == BECH32_CONST:
        spec = "bech32"
    elif polymod == BECH32M_CONST:
        spec = "bech32m"
    else:
        raise ValueError("bech32 checksum mismatch")
    witness_version = body[0]
    if witness_version > 16:
        raise ValueError("invalid witness version")
    program = bytes(_convertbits(body[1:], 5, 8, pad=False))
    if not 2 <= len(program) <= 40:
        raise ValueError("invalid witness program length")
    if witness_version == 0 and len(program) not in (20, 32):
        raise ValueError("invalid v0 witness program length")
    if spec == "bech32m" and witness_version == 0:
        raise ValueError("v0 program must use bech32, not bech32m")
    if spec == "bech32" and witness_version != 0:
        raise ValueError("v1+ program must use bech32m, not bech32")
    return hrp, witness_version, program, spec


# --------------------------------------------------------------------------- #
# one entry point for everything else
# --------------------------------------------------------------------------- #
def describe_address(address: Optional[str]) -> Dict[str, object]:
    """Decode an address and report whether it is a *valid* Bitcoin address.

    Returns a dict rather than raising, because the audit needs to report on
    thousands of addresses at once::

        {"address": ..., "valid": True, "kind": "p2wpkh", "checksum": "bech32"}

    ``kind`` is one of ``p2pkh`` / ``p2sh`` / ``p2wpkh`` / ``p2wsh`` / ``p2tr`` /
    ``segwit``, or ``unknown`` when the string does not decode at all.
    """
    result: Dict[str, object] = {
        "address": address, "valid": False, "kind": "unknown", "checksum": None,
    }
    if not address or not isinstance(address, str):
        return result
    try:
        if address[:1] in ("1", "3"):
            version, payload = base58check_decode(address)
            if len(payload) != 20:
                return result
            result.update(
                valid=True,
                kind="p2pkh" if version == P2PKH_VERSION else "p2sh",
                checksum="base58check",
            )
            return result
        hrp, witness_version, program, spec = bech32_decode(address)
        if hrp != BECH32_HRP:
            return result
        kind = {0: "p2wpkh" if len(program) == 20 else "p2wsh", 1: "p2tr"}.get(
            witness_version, "segwit"
        )
        result.update(valid=True, kind=kind, checksum=spec)
    except Exception:
        return result
    return result


def is_valid_address(address: Optional[str]) -> bool:
    return bool(describe_address(address)["valid"])
