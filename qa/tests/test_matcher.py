"""Unit tests for qa.matcher covering all operators, anomalies, and profile rules."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qa.client import HttpResponse
from qa.matcher import (
    evaluate_expectations,
    evaluate_field_rule,
    normalize_text,
    resolve_json_path,
)
from qa.models import FieldRule, ScenarioExpect, ScenarioRequest


def _resp(
    status: int = 200,
    body: object = None,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers=headers or {},
        body_json=body,
        body_text="",
        elapsed_ms=12.0,
    )


def test_normalize_text_strips_accents_case_and_extra_spaces() -> None:
    assert normalize_text("  Éléonore   D'ARÇON  ") == "eleonore d'arcon"


def test_field_rule_all_operators() -> None:
    # equals / not_equals
    assert all(
        a.passed
        for a in evaluate_field_rule(
            "f", FieldRule(equals="abc", not_equals="xyz"), found=True, actual="abc"
        )
    )
    # iequals / contains / regex / one_of
    assert all(
        a.passed
        for a in evaluate_field_rule(
            "f",
            FieldRule(
                iequals="francaise",
                contains="fran",
                regex=r"^\s*FRAN",
                one_of=["francaise", "belge"],
                not_empty=True,
                exists=True,
            ),
            found=True,
            actual="  FRANÇAISE ",
        )
    )
    # numeric gte / lte / between
    assert all(
        a.passed
        for a in evaluate_field_rule(
            "score",
            FieldRule(gte=0.7, lte=1.0, between=(0.8, 0.95)),
            found=True,
            actual=0.92,
        )
    )
    failed_num = evaluate_field_rule(
        "score", FieldRule(between=(0.8, 0.9)), found=True, actual=0.95
    )
    assert not failed_num[0].passed

    # is_null + anomaly (#170)
    rule_anomaly = FieldRule.model_validate(
        {
            "value": None,
            "anomaly_code": "invalid_date",
            "anomaly_raw_value": "31/04/2026",
        }
    )
    res_ok = evaluate_field_rule(
        "date_expiration",
        rule_anomaly,
        found=True,
        actual=None,
        anomaly={"code": "invalid_date", "raw_value": "31/04/2026"},
    )
    assert len(res_ok) == 3
    assert all(a.passed for a in res_ok)

    res_ko = evaluate_field_rule(
        "date_expiration",
        rule_anomaly,
        found=True,
        actual="2026-04-30",
        anomaly=None,
    )
    assert any(not a.passed for a in res_ko)

    # exists: false succeeds when missing
    res_absent = evaluate_field_rule(
        "missing", FieldRule(exists=False), found=False, actual=None
    )
    assert len(res_absent) == 1 and res_absent[0].passed


def test_evaluate_expectations_full_execution_and_purged_profile() -> None:
    exec_payload = {
        "id": "exec-1",
        "status": "SUCCESS",
        "data": {
            "total_processing_time_ms": 1450,
            "result": {
                "classification": {
                    "document_type": "cni",
                    "confidence": 0.94,
                    "explanation": "French CNI",
                },
                "extraction": {
                    "type": "cni",
                    "properties": [
                        {"name": "nom", "value": "DUPONT", "type": "string"},
                        {"name": "prenom", "value": "Jean-Luc", "type": "string"},
                        {
                            "name": "date_expiration",
                            "value": None,
                            "type": "date",
                            "anomaly": {
                                "code": "invalid_date",
                                "raw_value": "31/04/2026",
                            },
                        },
                    ],
                },
                "barcodes": [{"type": "2D_DOC", "is_valid": True}],
            },
        },
    }

    http_resp = _resp(
        200,
        body={"status": "success", "data": {"execution_id": "exec-1"}},
        headers={"X-RateLimit-Remaining-Minute": "58"},
    )
    exec_resp = _resp(200, body=exec_payload)

    expect = ScenarioExpect.model_validate(
        {
            "http_status": 200,
            "execution_status": "SUCCESS",
            "purged": False,
            "classification": {
                "document_type": "CNI",
                "confidence": {"gte": 0.75},
                "explanation": {"not_empty": True, "contains": "french"},
            },
            "extraction": {
                "type": "cni",
                "properties": {
                    "nom": {"iequals": "dupont"},
                    "prenom": {"contains": "jean"},
                    "date_expiration": {
                        "value": None,
                        "anomaly_code": "invalid_date",
                    },
                },
            },
            "barcodes": {
                "min_count": 1,
                "max_count": 2,
                "contains_type": "2D_DOC",
            },
            "headers": {
                "exists": ["x-ratelimit-remaining-minute"],
                "not_exists": ["Idempotent-Replay"],
            },
            "json_path": {
                "data.total_processing_time_ms": {"lte": 5000},
            },
        }
    )

    # On baseline profile without 'purged' field, purged check is gracefully skipped
    res_baseline = evaluate_expectations(
        expect,
        http_response=http_resp,
        execution_response=exec_resp,
        target_profile="baseline",
    )
    assert all(a.passed for a in res_baseline)
    assert any(
        a.path == "classification.explanation.not_empty" and a.passed
        for a in res_baseline
    )

    # On develop profile, missing 'purged' fails
    res_develop_missing = evaluate_expectations(
        expect,
        http_response=http_resp,
        execution_response=exec_resp,
        target_profile="develop",
    )
    assert any(a.path == "purged" and not a.passed for a in res_develop_missing)

    # With purged=False present, develop profile passes completely
    exec_payload["purged"] = False
    res_develop_ok = evaluate_expectations(
        expect,
        http_response=http_resp,
        execution_response=_resp(200, body=exec_payload),
        target_profile="develop",
    )
    assert all(a.passed for a in res_develop_ok)


def test_resolve_json_path_root_and_nested_length_and_sensitive_header_redaction() -> (
    None
):
    assert resolve_json_path([1, 2, 3], "$length") == (True, 3)
    assert resolve_json_path({"data": [1, 2]}, "data.$length") == (True, 2)
    assert resolve_json_path({"data": [1, 2]}, "data.$length.invalid") == (False, None)
    assert resolve_json_path({"a": {"b": 42}}, "$.a.b") == (True, 42)
    assert resolve_json_path(123, "$length") == (False, None)

    secret_cookie = "session_id=super-secret-cookie-value"
    secret_auth = "Bearer super-secret-jwt-token"
    http_resp = _resp(
        200,
        body={"ok": True},
        headers={
            "Set-Cookie": secret_cookie,
            "Authorization": secret_auth,
        },
    )
    expect = ScenarioExpect.model_validate(
        {
            "http_status": 200,
            "headers": {
                "exists": ["Set-Cookie"],
                "not_exists": ["Authorization"],
                "equals": {"Authorization": "Bearer wrong-token"},
                "contains": {"Set-Cookie": "missing-part"},
            },
        }
    )

    results = evaluate_expectations(expect, http_response=http_resp)
    serialized = " ".join(f"{r.actual} {r.expected} {r.message}" for r in results)
    assert secret_cookie not in serialized
    assert secret_auth not in serialized
    assert "wrong-token" not in serialized
    assert "missing-part" not in serialized
    assert any(
        r.path == "headers.Set-Cookie.exists"
        and r.passed
        and r.actual == "***REDACTED***"
        for r in results
    )
    assert any(
        r.path == "headers.Authorization.not_exists"
        and not r.passed
        and r.actual == "***REDACTED***"
        for r in results
    )


def test_classification_explanation_and_invalid_field_rule_validation() -> None:
    # 1. classification.explanation fails when empty
    expect_expl = ScenarioExpect.model_validate(
        {
            "http_status": 200,
            "classification": {
                "document_type": "cni",
                "explanation": {"not_empty": True},
            },
        }
    )
    exec_empty_expl = _resp(
        200,
        body={
            "status": "SUCCESS",
            "data": {
                "result": {
                    "classification": {
                        "document_type": "cni",
                        "explanation": "   ",
                    }
                }
            },
        },
    )
    res_fail = evaluate_expectations(
        expect_expl,
        http_response=_resp(200),
        execution_response=exec_empty_expl,
    )
    assert any(
        a.path == "classification.explanation.not_empty" and not a.passed
        for a in res_fail
    )

    # 2. ScenarioRequest accepts poll_timeout_s alias and excludes api_key_override from repr/dump
    req = ScenarioRequest.model_validate(
        {
            "workflow_id": "document-classification-extraction-v2",
            "file": "synthetic:minimal_pdf",
            "poll_timeout_s": 180.0,
            "api_key_override": "dia_test_override_secret_key",
        }
    )
    assert req.max_timeout_s == 180.0
    assert "dia_test_override_secret_key" not in repr(req)
    assert "api_key_override" not in req.model_dump()
    assert "dia_test_override_secret_key" not in req.model_dump_json()

    # 3. FieldRule rejects malformed regex and inverted between bounds at validation time
    with pytest.raises(ValidationError, match="Invalid regex pattern"):
        FieldRule.model_validate({"regex": "[unclosed("})

    with pytest.raises(ValidationError, match="Invalid 'between' bounds"):
        FieldRule.model_validate({"between": [1.0, 0.5]})
