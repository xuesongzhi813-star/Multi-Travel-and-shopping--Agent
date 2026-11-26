"""Authentication types and password hashing for the demo runtime."""

from dataclasses import dataclass
from hashlib import scrypt
from hmac import compare_digest
from secrets import token_bytes
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    token_digest: str


def derive_password(password: str, salt: bytes) -> bytes:
    return scrypt(
        password.encode("utf-8"), salt=salt, n=2**15, r=8, p=3,
        maxmem=64 * 1024 * 1024, dklen=32,
    )


def hash_password(password: str) -> str:
    salt = token_bytes(16)
    return f"scrypt-v1${salt.hex()}${derive_password(password, salt).hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        version, salt_hex, digest_hex = stored.split("$")
        if version != "scrypt-v1" or len(salt_hex) != 32 or len(digest_hex) != 64:
            return False
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if len(salt) != 16 or len(digest) != 32:
            return False
        return compare_digest(derive_password(password, salt), digest)
    except (ValueError, TypeError):
        return False
