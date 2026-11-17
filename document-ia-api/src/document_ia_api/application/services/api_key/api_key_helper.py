import base64
import hashlib
import hmac
import re
import secrets

from argon2 import PasswordHasher

from document_ia_api.core.config import api_key_settings
from document_ia_infra.data.api_key.dto.api_key_dto import ApiKeyDTO


class ApiKeyHelper:
    ALLOWED_ENVS = ("dev", "staging", "sandbox", "prod")
    ALLOWED_VERSIONS = ("1", "2")

    def __init__(self) -> None:
        self.ph = PasswordHasher(time_cost=2, memory_cost=1024 * 64, parallelism=4)
        self.key_re = re.compile(
            r"^dia_(dev|staging|sandbox|prod)_(\d+)_[A-Z2-7]{8}_[A-Z2-7]{40,64}_[A-Z2-7]{4}\Z"
        )

    def _parse_and_validate(self, presented: object) -> tuple[str, int, str, str, str]:
        """Parse and validate structural, environment, version, and prefix invariants."""
        if not isinstance(presented, str) or not self.key_re.fullmatch(presented):
            raise ValueError("Invalid API key format")

        _, env, version_str, prefix, body, chk = presented.split("_", 5)

        if env != api_key_settings.APP_ENV:
            raise ValueError("API key environment mismatch")

        if version_str not in self.ALLOWED_VERSIONS:
            raise ValueError("Unsupported API key version")

        version = int(version_str)
        if version == 1 and not api_key_settings.API_KEY_ALLOW_V1:
            raise ValueError("Legacy v1 API keys are disabled")

        if prefix != body[:8]:
            raise ValueError("API key prefix mismatch")

        return env, version, prefix, body, chk

    def _get_signing_payload(
        self, env: str, version: int, prefix: str, body: str
    ) -> str:
        """Return the payload authenticated by checksum and pre-Argon2id MAC."""
        if version == 1:
            return body
        if version == 2:
            return f"dia_{env}_{version}_{prefix}_{body}"
        raise ValueError(f"Unsupported API key version: {version}")

    def _compute_checksum(self, signing_payload: str) -> str:
        return self._hmac_b32(api_key_settings.API_KEY_PEPPER_CHK, signing_payload)[:4]

    def _compute_mac(self, signing_payload: str) -> str:
        return hmac.new(
            api_key_settings.API_KEY_PEPPER_HASH.encode(),
            signing_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    def generate_new_api_key(self) -> tuple[str, str, str, str]:
        """Generates a new API key."""
        env = api_key_settings.APP_ENV
        if env not in self.ALLOWED_ENVS:
            raise ValueError(f"Unsupported APP_ENV: {env}")

        version = api_key_settings.API_KEY_VERSION
        if str(version) not in self.ALLOWED_VERSIONS:
            raise ValueError(f"Unsupported API_KEY_VERSION: {version}")

        raw = secrets.token_bytes(32)
        body = self._b32_no_pad(raw)
        prefix = body[:8]
        signing_payload = self._get_signing_payload(env, version, prefix, body)
        chk = self._compute_checksum(signing_payload)
        presented = f"dia_{env}_{version}_{prefix}_{body}_{chk}"
        mac = self._compute_mac(signing_payload)

        key_hash = self.ph.hash(mac)
        return presented, prefix, chk, key_hash

    def verify_api_key(self, presented: str, api_key_dto: ApiKeyDTO) -> bool:
        try:
            env, version, prefix, body, chk = self._parse_and_validate(presented)
        except ValueError:
            return False

        if prefix != api_key_dto.prefix:
            return False

        signing_payload = self._get_signing_payload(env, version, prefix, body)
        expected_chk = self._compute_checksum(signing_payload)
        if not hmac.compare_digest(expected_chk, chk):
            return False

        mac = self._compute_mac(signing_payload)
        try:
            ok = self.ph.verify(api_key_dto.key_hash, mac)
            return ok
        except Exception:
            return False

    def get_key_encoding(self, presented: str) -> tuple[str, str]:
        """This method is used for initialization of the database with an api key present in .env file.
        - Validates format, environment, version, prefix, and checksum
        - Computes the MAC hash and Argon2 hash for storage
        """
        env, version, prefix, body, chk = self._parse_and_validate(presented)
        signing_payload = self._get_signing_payload(env, version, prefix, body)

        expected_chk = self._compute_checksum(signing_payload)
        if not hmac.compare_digest(expected_chk, chk):
            raise ValueError("Invalid API key checksum")

        mac = self._compute_mac(signing_payload)
        key_hash = self.ph.hash(mac)

        return key_hash, prefix

    def get_key_prefix(self, presented: str) -> str:
        """Extracts the prefix from the presented API key."""
        _, _, prefix, _, _ = self._parse_and_validate(presented)
        return prefix

    def get_key_version(self, presented: str) -> int:
        """Extracts and validates the version from the presented API key."""
        _, version, _, _, _ = self._parse_and_validate(presented)
        return version

    def is_legacy_v1_key(self, presented: str) -> bool:
        """Returns True if the presented valid API key uses legacy version 1."""
        return self.get_key_version(presented) == 1

    def _b32_no_pad(self, data: bytes) -> str:
        """Encodes bytes to base32 without padding."""

        return base64.b32encode(data).decode("ascii").rstrip("=").upper()

    def _hmac_b32(self, pepper: str, msg: str) -> str:
        d = hmac.new(pepper.encode(), msg.encode(), hashlib.sha256).digest()
        return self._b32_no_pad(d)
