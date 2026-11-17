import logging
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from document_ia_api.application.services.api_key.api_key_helper import ApiKeyHelper
from document_ia_api.application.services.api_key.api_key_service import ApiKeyService
from document_ia_api.core.config import api_key_settings
from document_ia_infra.data.api_key.dto.api_key_dto import ApiKeyDTO
from document_ia_infra.data.api_key.enum.api_key_status import ApiKeyStatus

_REAL_GET_FROM_PRESENTED_KEY = ApiKeyService.get_from_presented_key

LEGACY_V1_KEY = "dia_dev_1_WMBUIIAK_WMBUIIAK2QQFKWQ5S7YPQZIUTFHQ2KKNFKPESOLLCQVME2NJFZUQ_BYMQ"  # gitleaks:allow
LEGACY_V1_HASH = "$argon2id$v=19$m=65536,t=2,p=4$dag2me5gKU9k/N65qZm1lA$edWO9fTYKHBSVbRmV3MnVGdIUPf1ULk+6gJZ66pWjgk"


def _make_record(prefix: str, key_hash: str) -> ApiKeyDTO:
    return ApiKeyDTO(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        organization=None,
        key_hash=key_hash,
        prefix=prefix,
        status=ApiKeyStatus.ACTIVE,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


def test_generate_and_verify_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_VERSION", 2)
    api_key_helper = ApiKeyHelper()

    presented, prefix, chk, key_hash = api_key_helper.generate_new_api_key()

    assert isinstance(presented, str) and presented
    assert isinstance(prefix, str) and len(prefix) == 8
    assert isinstance(chk, str) and len(chk) == 4
    assert isinstance(key_hash, str) and key_hash

    assert api_key_helper.key_re.match(presented) is not None
    assert presented.startswith("dia_dev_2_")
    assert api_key_helper.get_key_version(presented) == 2
    assert not api_key_helper.is_legacy_v1_key(presented)

    record = _make_record(prefix=prefix, key_hash=key_hash)
    assert api_key_helper.verify_api_key(presented, record) is True


def test_verify_api_key_with_valid_v1_legacy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", True)
    api_key_helper = ApiKeyHelper()
    record = _make_record(prefix="WMBUIIAK", key_hash=LEGACY_V1_HASH)

    is_valid = api_key_helper.verify_api_key(LEGACY_V1_KEY, record)

    assert is_valid is True
    assert api_key_helper.is_legacy_v1_key(LEGACY_V1_KEY) is True


def test_verify_api_key_with_invalid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", True)
    api_key_helper = ApiKeyHelper()
    checked_api_key = "dia_dev_1_WMBUIIAK_WMBUIIAK2AAFKWQ5S7YPQZIUTFHQ2KKNFKPESOLLCQVME2NJFZUQ_BYMQ"  # gitleaks:allow
    record = _make_record(prefix="WMBUIIAK", key_hash=LEGACY_V1_HASH)

    is_valid = api_key_helper.verify_api_key(checked_api_key, record)

    assert is_valid is False


def test_internal_get_key_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", True)
    api_key_helper = ApiKeyHelper()
    presented = "dia_dev_1_ID5RKLRX_ID5RKLRXQZM52XFUNGUHOBL7MAU3XQDRYN3R4HZH24DGGTFROBLA_5YTN"  # gitleaks:allow

    key_hash, prefix = api_key_helper.get_key_encoding(presented)
    record = _make_record(prefix=prefix, key_hash=key_hash)

    assert api_key_helper.verify_api_key(presented, record) is True


def test_v2_cryptographic_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", True)
    api_key_helper = ApiKeyHelper()

    _, _, _, prefix, body, v1_chk = LEGACY_V1_KEY.split("_", 5)
    v2_payload = api_key_helper._get_signing_payload("dev", 2, prefix, body)
    v1_payload = api_key_helper._get_signing_payload("dev", 1, prefix, body)

    assert v2_payload == f"dia_dev_2_{prefix}_{body}"
    assert v1_payload == body

    v2_chk = api_key_helper._compute_checksum(v2_payload)
    assert v2_chk != v1_chk
    assert api_key_helper._compute_mac(v2_payload) != api_key_helper._compute_mac(
        v1_payload
    )

    v2_key = f"dia_dev_2_{prefix}_{body}_{v2_chk}"
    v2_hash, v2_prefix = api_key_helper.get_key_encoding(v2_key)
    assert v2_prefix == prefix
    assert api_key_helper.verify_api_key(v2_key, _make_record(v2_prefix, v2_hash))


def test_v2_and_v1_encoding_bootstrap_coherence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "staging")
    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", True)
    api_key_helper = ApiKeyHelper()

    for version in (1, 2):
        monkeypatch.setattr(api_key_settings, "API_KEY_VERSION", version)
        presented, prefix, _, key_hash = api_key_helper.generate_new_api_key()
        assert presented.startswith(f"dia_staging_{version}_")
        assert api_key_helper.get_key_prefix(presented) == prefix
        assert api_key_helper.get_key_version(presented) == version

        record = _make_record(prefix=prefix, key_hash=key_hash)
        assert api_key_helper.verify_api_key(presented, record) is True

        bootstrapped_hash, bootstrapped_prefix = api_key_helper.get_key_encoding(
            presented
        )
        assert bootstrapped_prefix == prefix
        assert api_key_helper.verify_api_key(
            presented, _make_record(bootstrapped_prefix, bootstrapped_hash)
        )


def test_reject_env_mutation_on_v1_and_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_VERSION", 2)
    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", True)
    api_key_helper = ApiKeyHelper()

    v2_dev_key, prefix, _, v2_hash = api_key_helper.generate_new_api_key()
    v2_record = _make_record(prefix=prefix, key_hash=v2_hash)
    v1_record = _make_record(prefix="WMBUIIAK", key_hash=LEGACY_V1_HASH)

    # Mutate env segment to prod while APP_ENV is dev
    mutated_v2_to_prod = v2_dev_key.replace("dia_dev_2_", "dia_prod_2_", 1)
    mutated_v1_to_prod = LEGACY_V1_KEY.replace("dia_dev_1_", "dia_prod_1_", 1)

    for mutated in (mutated_v2_to_prod, mutated_v1_to_prod):
        with pytest.raises(ValueError):
            api_key_helper.get_key_prefix(mutated)
        with pytest.raises(ValueError):
            api_key_helper.get_key_encoding(mutated)

    assert api_key_helper.verify_api_key(mutated_v2_to_prod, v2_record) is False
    assert api_key_helper.verify_api_key(mutated_v1_to_prod, v1_record) is False

    # Present valid dev keys against APP_ENV="prod"
    monkeypatch.setattr(api_key_settings, "APP_ENV", "prod")
    for dev_key in (v2_dev_key, LEGACY_V1_KEY):
        with pytest.raises(ValueError):
            api_key_helper.get_key_prefix(dev_key)
    assert api_key_helper.verify_api_key(v2_dev_key, v2_record) is False
    assert api_key_helper.verify_api_key(LEGACY_V1_KEY, v1_record) is False


def test_reject_version_mutation_and_v_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_VERSION", 2)
    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", True)
    api_key_helper = ApiKeyHelper()

    v2_key, prefix, _, v2_hash = api_key_helper.generate_new_api_key()
    v2_record = _make_record(prefix=prefix, key_hash=v2_hash)
    v1_record = _make_record(prefix="WMBUIIAK", key_hash=LEGACY_V1_HASH)

    invalid_versions = [
        v2_key.replace("dia_dev_2_", "dia_dev_v2_", 1),
        v2_key.replace("dia_dev_2_", "dia_dev_v1_", 1),
        v2_key.replace("dia_dev_2_", "dia_dev_0_", 1),
        v2_key.replace("dia_dev_2_", "dia_dev_3_", 1),
        v2_key.replace("dia_dev_2_", "dia_dev_99_", 1),
        v2_key.replace("dia_dev_2_", "dia_dev_02_", 1),
        LEGACY_V1_KEY.replace("dia_dev_1_", "dia_dev_v1_", 1),
    ]
    for bad_key in invalid_versions:
        with pytest.raises(ValueError):
            api_key_helper.get_key_prefix(bad_key)
        assert api_key_helper.verify_api_key(bad_key, v2_record) is False
        assert api_key_helper.verify_api_key(bad_key, v1_record) is False

    # Cross-version downgrade attack: v2 hash in DB, forged v1 key with valid v1 checksum
    _, _, _, v2_prefix, v2_body, _ = v2_key.split("_", 5)
    forged_v1_chk = api_key_helper._compute_checksum(v2_body)
    forged_v1_key = f"dia_dev_1_{v2_prefix}_{v2_body}_{forged_v1_chk}"
    assert api_key_helper.verify_api_key(forged_v1_key, v2_record) is False

    # Cross-version upgrade attack: v1 hash in DB, forged v2 key with valid v2 checksum
    _, _, _, v1_prefix, v1_body, _ = LEGACY_V1_KEY.split("_", 5)
    forged_v2_payload = api_key_helper._get_signing_payload(
        "dev", 2, v1_prefix, v1_body
    )
    forged_v2_chk = api_key_helper._compute_checksum(forged_v2_payload)
    forged_v2_key = f"dia_dev_2_{v1_prefix}_{v1_body}_{forged_v2_chk}"
    assert api_key_helper.verify_api_key(forged_v2_key, v1_record) is False


def test_reject_prefix_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_VERSION", 2)
    api_key_helper = ApiKeyHelper()

    v2_key, prefix, _, v2_hash = api_key_helper.generate_new_api_key()
    v2_record = _make_record(prefix=prefix, key_hash=v2_hash)

    wrong_prefix = "AAAAAAAA" if prefix != "AAAAAAAA" else "BBBBBBBB"
    mutated_key = v2_key.replace(f"_{prefix}_", f"_{wrong_prefix}_", 1)

    with pytest.raises(ValueError):
        api_key_helper.get_key_prefix(mutated_key)
    with pytest.raises(ValueError):
        api_key_helper.get_key_encoding(mutated_key)
    assert api_key_helper.verify_api_key(mutated_key, v2_record) is False


def test_disable_v1_keys_via_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_VERSION", 2)
    api_key_helper = ApiKeyHelper()

    v2_key, v2_prefix, _, v2_hash = api_key_helper.generate_new_api_key()
    v2_record = _make_record(prefix=v2_prefix, key_hash=v2_hash)
    v1_record = _make_record(prefix="WMBUIIAK", key_hash=LEGACY_V1_HASH)

    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", False)

    with pytest.raises(ValueError):
        api_key_helper.get_key_prefix(LEGACY_V1_KEY)
    with pytest.raises(ValueError):
        api_key_helper.get_key_encoding(LEGACY_V1_KEY)
    assert api_key_helper.verify_api_key(LEGACY_V1_KEY, v1_record) is False

    # v2 key remains accepted
    assert api_key_helper.get_key_prefix(v2_key) == v2_prefix
    assert api_key_helper.verify_api_key(v2_key, v2_record) is True


async def test_legacy_v1_logs_warning_without_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(api_key_settings, "APP_ENV", "dev")
    monkeypatch.setattr(api_key_settings, "API_KEY_ALLOW_V1", True)
    monkeypatch.setattr(api_key_settings, "API_KEY_VERSION", 2)

    monkeypatch.setattr(
        ApiKeyService, "get_from_presented_key", _REAL_GET_FROM_PRESENTED_KEY
    )
    service = ApiKeyService(db_session=MagicMock())
    v1_record = _make_record(prefix="WMBUIIAK", key_hash=LEGACY_V1_HASH)
    service.api_key_repository.get_by_prefix_with_relation = AsyncMock(
        return_value=v1_record
    )

    _, _, _, _, v1_body, _ = LEGACY_V1_KEY.split("_", 5)

    with caplog.at_level(logging.WARNING):
        result = await service.get_from_presented_key(LEGACY_V1_KEY)

    assert result == v1_record
    warning_messages = [
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING
    ]
    assert len(warning_messages) == 1
    msg = warning_messages[0]
    assert str(v1_record.id) in msg
    assert v1_record.prefix in msg
    assert str(v1_record.organization_id) in msg
    assert LEGACY_V1_KEY not in msg
    assert v1_body not in msg

    # v2 key must not emit legacy deprecation warning
    caplog.clear()
    v2_key, v2_prefix, _, v2_hash = service.api_key_helper.generate_new_api_key()
    v2_record = _make_record(prefix=v2_prefix, key_hash=v2_hash)
    service.api_key_repository.get_by_prefix_with_relation = AsyncMock(
        return_value=v2_record
    )

    with caplog.at_level(logging.WARNING):
        result_v2 = await service.get_from_presented_key(v2_key)

    assert result_v2 == v2_record
    assert not [rec for rec in caplog.records if rec.levelno == logging.WARNING]
