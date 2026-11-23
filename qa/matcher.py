"""Assertion and matching engine tolerant to OCR/LLM semi-determinism."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from qa.client import (
    HttpResponse,
    extract_error_code,
    extract_execution_id,
    extract_rate_limit_headers,
)
from qa.config import SENSITIVE_HEADER_NAMES
from qa.models import (
    AssertionResult,
    EnvProfile,
    ExecutionSnapshot,
    FieldRule,
    ScenarioExpect,
)

_WHITESPACE_RE = re.compile(r"\s+")
_INDEX_RE = re.compile(r"^([^\[\]]+)\[(\d+)\]$")


def normalize_text(value: str) -> str:
    """Normalize text for accent/case/whitespace-insensitive comparison."""
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    collapsed = _WHITESPACE_RE.sub(" ", without_accents).strip()
    return collapsed.casefold()


def resolve_json_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted/indexed JSON path against a decoded JSON structure.

    Special tokens:
    - `$length` at root or as a final segment returns `len(current)` if sized.
    - Supports `a.b[0].c` as well as `a.b.0.c`.
    """
    if payload is None:
        return False, None

    if path in ("$", ""):
        return True, payload

    if path == "$length":
        if isinstance(payload, (list, dict, str)):
            return True, len(payload)
        return False, None

    cleaned_path = path.removeprefix("$.")
    current: Any = payload
    segments = [seg for seg in cleaned_path.split(".") if seg]

    for seg_idx, segment in enumerate(segments):
        if segment == "$length":
            if seg_idx == len(segments) - 1 and isinstance(current, (list, dict, str)):
                return True, len(current)
            return False, None

        index_match = _INDEX_RE.match(segment)
        if index_match is not None:
            key = index_match.group(1)
            idx = int(index_match.group(2))
            if not isinstance(current, dict) or key not in current:
                return False, None
            current = current[key]
            if not isinstance(current, list) or idx >= len(current):
                return False, None
            current = current[idx]
            continue

        if isinstance(current, dict):
            if segment not in current:
                return False, None
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isdigit():
                return False, None
            idx = int(segment)
            if idx >= len(current):
                return False, None
            current = current[idx]
        else:
            return False, None

    return True, current


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def evaluate_field_rule(
    path: str,
    rule: FieldRule,
    *,
    found: bool,
    actual: Any,
    anomaly: dict[str, Any] | None = None,
) -> list[AssertionResult]:
    """Evaluate all configured operators on a single FieldRule."""
    results: list[AssertionResult] = []

    if rule.exists is not None:
        passed = found is rule.exists
        results.append(
            AssertionResult(
                path=f"{path}.exists",
                passed=passed,
                expected=rule.exists,
                actual=found,
                message=(
                    ""
                    if passed
                    else f"Expected path '{path}' existence={rule.exists}, got {found}"
                ),
            )
        )
        if not found and not rule.exists:
            return results

    if not found and (
        rule.has_equals
        or rule.has_not_equals
        or rule.iequals is not None
        or rule.contains is not None
        or rule.regex is not None
        or rule.one_of is not None
        or rule.gte is not None
        or rule.lte is not None
        or rule.between is not None
        or rule.is_null is not None
        or rule.not_empty is not None
        or rule.anomaly_code is not None
        or rule.anomaly_raw_value is not None
    ):
        results.append(
            AssertionResult(
                path=path,
                passed=False,
                expected="present",
                actual="<missing>",
                message=f"Expected field '{path}' to be present in response",
            )
        )
        return results

    if rule.is_null is not None:
        is_none = actual is None
        passed = is_none is rule.is_null
        results.append(
            AssertionResult(
                path=f"{path}.is_null",
                passed=passed,
                expected=rule.is_null,
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' is_null={rule.is_null}, got {actual!r}"
                ),
            )
        )

    if rule.not_empty is not None:
        non_empty = actual is not None and (
            (isinstance(actual, str) and actual.strip() != "")
            or (isinstance(actual, (list, dict)) and len(actual) > 0)
            or (not isinstance(actual, (str, list, dict)))
        )
        passed = non_empty is rule.not_empty
        results.append(
            AssertionResult(
                path=f"{path}.not_empty",
                passed=passed,
                expected=rule.not_empty,
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' not_empty={rule.not_empty}, got {actual!r}"
                ),
            )
        )

    if rule.has_equals:
        passed = actual == rule.equals
        results.append(
            AssertionResult(
                path=f"{path}.equals",
                passed=passed,
                expected=rule.equals,
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' == {rule.equals!r}, got {actual!r}"
                ),
            )
        )

    if rule.has_not_equals:
        passed = actual != rule.not_equals
        results.append(
            AssertionResult(
                path=f"{path}.not_equals",
                passed=passed,
                expected=f"!= {rule.not_equals!r}",
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' != {rule.not_equals!r}, got {actual!r}"
                ),
            )
        )

    if rule.iequals is not None:
        passed = isinstance(actual, str) and normalize_text(actual) == normalize_text(
            rule.iequals
        )
        results.append(
            AssertionResult(
                path=f"{path}.iequals",
                passed=passed,
                expected=rule.iequals,
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' iequals {rule.iequals!r}, got {actual!r}"
                ),
            )
        )

    if rule.contains is not None:
        if isinstance(actual, str):
            passed = normalize_text(rule.contains) in normalize_text(actual)
        elif isinstance(actual, list):
            passed = rule.contains in actual
        else:
            passed = False
        results.append(
            AssertionResult(
                path=f"{path}.contains",
                passed=passed,
                expected=rule.contains,
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' to contain {rule.contains!r}, got {actual!r}"
                ),
            )
        )

    if rule.regex is not None:
        passed = isinstance(actual, str) and re.search(rule.regex, actual) is not None
        results.append(
            AssertionResult(
                path=f"{path}.regex",
                passed=passed,
                expected=rule.regex,
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' to match /{rule.regex}/, got {actual!r}"
                ),
            )
        )

    if rule.one_of is not None:
        if actual in rule.one_of:
            passed = True
        elif isinstance(actual, str):
            norm_actual = normalize_text(actual)
            passed = any(
                isinstance(opt, str) and normalize_text(opt) == norm_actual
                for opt in rule.one_of
            )
        else:
            passed = False
        results.append(
            AssertionResult(
                path=f"{path}.one_of",
                passed=passed,
                expected=rule.one_of,
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' in {rule.one_of!r}, got {actual!r}"
                ),
            )
        )

    if rule.gte is not None:
        num = _to_float(actual)
        passed = num is not None and num >= rule.gte
        results.append(
            AssertionResult(
                path=f"{path}.gte",
                passed=passed,
                expected=f">= {rule.gte}",
                actual=actual,
                message=(
                    "" if passed else f"Expected '{path}' >= {rule.gte}, got {actual!r}"
                ),
            )
        )

    if rule.lte is not None:
        num = _to_float(actual)
        passed = num is not None and num <= rule.lte
        results.append(
            AssertionResult(
                path=f"{path}.lte",
                passed=passed,
                expected=f"<= {rule.lte}",
                actual=actual,
                message=(
                    "" if passed else f"Expected '{path}' <= {rule.lte}, got {actual!r}"
                ),
            )
        )

    if rule.between is not None:
        low, high = rule.between
        num = _to_float(actual)
        passed = num is not None and low <= num <= high
        results.append(
            AssertionResult(
                path=f"{path}.between",
                passed=passed,
                expected=f"[{low}, {high}]",
                actual=actual,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' in [{low}, {high}], got {actual!r}"
                ),
            )
        )

    if rule.anomaly_code is not None:
        actual_code = anomaly.get("code") if isinstance(anomaly, dict) else None
        passed = actual_code == rule.anomaly_code
        results.append(
            AssertionResult(
                path=f"{path}.anomaly.code",
                passed=passed,
                expected=rule.anomaly_code,
                actual=actual_code,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' anomaly.code == {rule.anomaly_code!r}, got {actual_code!r}"
                ),
            )
        )

    if rule.anomaly_raw_value is not None:
        actual_raw = anomaly.get("raw_value") if isinstance(anomaly, dict) else None
        passed = isinstance(actual_raw, str) and normalize_text(
            actual_raw
        ) == normalize_text(rule.anomaly_raw_value)
        results.append(
            AssertionResult(
                path=f"{path}.anomaly.raw_value",
                passed=passed,
                expected=rule.anomaly_raw_value,
                actual=actual_raw,
                message=(
                    ""
                    if passed
                    else f"Expected '{path}' anomaly.raw_value == {rule.anomaly_raw_value!r}, got {actual_raw!r}"
                ),
            )
        )

    return results


def _as_rule(value: str | FieldRule) -> FieldRule:
    if isinstance(value, FieldRule):
        return value
    return FieldRule(iequals=value)


def _extract_result_section(execution_payload: Any) -> dict[str, Any]:
    if not isinstance(execution_payload, dict):
        return {}
    data = execution_payload.get("data")
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    return result if isinstance(result, dict) else {}


def build_execution_snapshot(
    http_response: HttpResponse | None,
    execution_response: HttpResponse | None = None,
) -> ExecutionSnapshot:
    """Build a secret-free, comparable snapshot from HTTP and execution responses."""
    snapshot = ExecutionSnapshot()
    if http_response is not None:
        snapshot.http_status = http_response.status_code
        snapshot.rate_limit_headers = extract_rate_limit_headers(http_response)
        snapshot.execution_id = extract_execution_id(http_response.body_json)
        snapshot.error_code = extract_error_code(http_response.body_json)

    exec_resp = execution_response or http_response
    if exec_resp is None or not isinstance(exec_resp.body_json, dict):
        return snapshot

    payload = exec_resp.body_json
    if "status" in payload and isinstance(payload["status"], str):
        status_upper = payload["status"].upper()
        if status_upper in ("STARTED", "SUCCESS", "FAILED"):
            snapshot.execution_status = status_upper
    if "id" in payload and not snapshot.execution_id:
        snapshot.execution_id = str(payload["id"])
    if isinstance(payload.get("purged"), bool):
        snapshot.purged = payload["purged"]

    data = payload.get("data")
    if isinstance(data, dict):
        proc_ms = data.get("total_processing_time_ms")
        if isinstance(proc_ms, int):
            snapshot.total_processing_time_ms = proc_ms
        result = data.get("result")
        if isinstance(result, dict):
            classification = result.get("classification")
            if isinstance(classification, dict):
                snapshot.classification = {
                    "document_type": classification.get("document_type"),
                    "confidence": classification.get("confidence"),
                }
            extraction = result.get("extraction")
            if isinstance(extraction, dict):
                ext_type = extraction.get("type")
                if isinstance(ext_type, str):
                    snapshot.extraction_type = ext_type
                props = extraction.get("properties")
                if isinstance(props, list):
                    prop_map: dict[str, Any] = {}
                    for item in props:
                        if isinstance(item, dict) and isinstance(item.get("name"), str):
                            entry: Any = item.get("value")
                            if "anomaly" in item and item["anomaly"] is not None:
                                entry = {
                                    "value": item.get("value"),
                                    "anomaly": item.get("anomaly"),
                                }
                            prop_map[item["name"]] = entry
                    snapshot.extraction_properties = prop_map
            barcodes = result.get("barcodes")
            if isinstance(barcodes, list):
                snapshot.barcodes_count = len(barcodes)
                snapshot.barcode_types = [
                    str(bc["type"])
                    for bc in barcodes
                    if isinstance(bc, dict) and bc.get("type") is not None
                ]

    return snapshot


def evaluate_expectations(
    expect: ScenarioExpect,
    *,
    http_response: HttpResponse,
    execution_response: HttpResponse | None = None,
    execution_status_override: str | None = None,
    target_profile: EnvProfile = "baseline",
) -> list[AssertionResult]:
    """Evaluate declarative ScenarioExpect against actual HTTP and execution responses."""
    results: list[AssertionResult] = []

    # 1. HTTP status
    expected_statuses = (
        [expect.http_status]
        if isinstance(expect.http_status, int)
        else list(expect.http_status)
    )
    status_ok = http_response.status_code in expected_statuses
    results.append(
        AssertionResult(
            path="http_status",
            passed=status_ok,
            expected=expected_statuses
            if len(expected_statuses) > 1
            else expected_statuses[0],
            actual=http_response.status_code,
            message=(
                ""
                if status_ok
                else f"Expected HTTP status in {expected_statuses}, got {http_response.status_code}"
            ),
        )
    )

    def _safe_header_for_report(hdr_name: str, val: str | None) -> str | None:
        if val is None:
            return None
        if hdr_name.lower() in SENSITIVE_HEADER_NAMES:
            return "***REDACTED***"
        return val

    # 2. HTTP headers (always evaluated against primary http_response)
    if expect.headers is not None:
        for hdr in expect.headers.exists:
            val = http_response.get_header(hdr)
            safe_val = _safe_header_for_report(hdr, val)
            results.append(
                AssertionResult(
                    path=f"headers.{hdr}.exists",
                    passed=val is not None,
                    expected="present",
                    actual=safe_val,
                    message="" if val is not None else f"Header '{hdr}' missing",
                )
            )
        for hdr in expect.headers.not_exists:
            val = http_response.get_header(hdr)
            safe_val = _safe_header_for_report(hdr, val)
            results.append(
                AssertionResult(
                    path=f"headers.{hdr}.not_exists",
                    passed=val is None,
                    expected="absent",
                    actual=safe_val,
                    message=""
                    if val is None
                    else f"Header '{hdr}' unexpectedly present",
                )
            )
        for hdr, exp_val in expect.headers.equals.items():
            val = http_response.get_header(hdr)
            safe_val = _safe_header_for_report(hdr, val)
            safe_exp = _safe_header_for_report(hdr, exp_val)
            passed = val is not None and val.strip().lower() == exp_val.strip().lower()
            results.append(
                AssertionResult(
                    path=f"headers.{hdr}.equals",
                    passed=passed,
                    expected=safe_exp,
                    actual=safe_val,
                    message=(
                        ""
                        if passed
                        else f"Header '{hdr}': expected {safe_exp!r}, got {safe_val!r}"
                    ),
                )
            )
        for hdr, substr in expect.headers.contains.items():
            val = http_response.get_header(hdr)
            safe_val = _safe_header_for_report(hdr, val)
            safe_sub = _safe_header_for_report(hdr, substr)
            passed = val is not None and substr.lower() in val.lower()
            results.append(
                AssertionResult(
                    path=f"headers.{hdr}.contains",
                    passed=passed,
                    expected=safe_sub,
                    actual=safe_val,
                    message=(
                        ""
                        if passed
                        else f"Header '{hdr}': expected to contain {safe_sub!r}, got {safe_val!r}"
                    ),
                )
            )

    # Determine payload for execution-level and json_path assertions
    exec_payload: Any = (
        execution_response.body_json
        if execution_response is not None
        else http_response.body_json
    )

    # 3. execution_status
    if expect.execution_status is not None:
        actual_exec_status = execution_status_override
        if actual_exec_status is None and isinstance(exec_payload, dict):
            raw_status = exec_payload.get("status")
            if isinstance(raw_status, str):
                actual_exec_status = raw_status.upper()

        expected_exec_statuses = (
            [expect.execution_status.upper()]
            if isinstance(expect.execution_status, str)
            else [s.upper() for s in expect.execution_status]
        )
        passed = (
            actual_exec_status is not None
            and actual_exec_status.upper() in expected_exec_statuses
        )
        results.append(
            AssertionResult(
                path="execution_status",
                passed=passed,
                expected=(
                    expected_exec_statuses
                    if len(expected_exec_statuses) > 1
                    else expected_exec_statuses[0]
                ),
                actual=actual_exec_status,
                message=(
                    ""
                    if passed
                    else f"Expected execution_status in {expected_exec_statuses}, got {actual_exec_status!r}"
                ),
            )
        )

    # 4. purged (#156) — only evaluated on develop profile OR when field is present
    if expect.purged is not None and isinstance(exec_payload, dict):
        has_purged = "purged" in exec_payload
        if target_profile == "develop" or has_purged:
            actual_purged = exec_payload.get("purged")
            passed = actual_purged is expect.purged
            results.append(
                AssertionResult(
                    path="purged",
                    passed=passed,
                    expected=expect.purged,
                    actual=actual_purged,
                    message=(
                        ""
                        if passed
                        else f"Expected purged={expect.purged!r}, got {actual_purged!r}"
                    ),
                )
            )

    result_section = _extract_result_section(exec_payload)

    # 5. classification
    if expect.classification is not None:
        classification = result_section.get("classification")
        if not isinstance(classification, dict):
            results.append(
                AssertionResult(
                    path="classification",
                    passed=False,
                    expected="object",
                    actual=classification,
                    message="Expected 'data.result.classification' object in execution result",
                )
            )
        else:
            if expect.classification.document_type is not None:
                rule = _as_rule(expect.classification.document_type)
                results.extend(
                    evaluate_field_rule(
                        "classification.document_type",
                        rule,
                        found="document_type" in classification,
                        actual=classification.get("document_type"),
                    )
                )
            if expect.classification.confidence is not None:
                results.extend(
                    evaluate_field_rule(
                        "classification.confidence",
                        expect.classification.confidence,
                        found="confidence" in classification,
                        actual=classification.get("confidence"),
                    )
                )
            if expect.classification.explanation is not None:
                results.extend(
                    evaluate_field_rule(
                        "classification.explanation",
                        expect.classification.explanation,
                        found="explanation" in classification,
                        actual=classification.get("explanation"),
                    )
                )

    # 6. extraction
    if expect.extraction is not None:
        extraction = result_section.get("extraction")
        if not isinstance(extraction, dict):
            results.append(
                AssertionResult(
                    path="extraction",
                    passed=False,
                    expected="object",
                    actual=extraction,
                    message="Expected 'data.result.extraction' object in execution result",
                )
            )
        else:
            if expect.extraction.type is not None:
                rule = _as_rule(expect.extraction.type)
                results.extend(
                    evaluate_field_rule(
                        "extraction.type",
                        rule,
                        found="type" in extraction,
                        actual=extraction.get("type"),
                    )
                )

            raw_props = extraction.get("properties")
            props_by_name: dict[str, dict[str, Any]] = {}
            if isinstance(raw_props, list):
                for item in raw_props:
                    if isinstance(item, dict) and isinstance(item.get("name"), str):
                        props_by_name[item["name"]] = item

            for prop_name, prop_rule in expect.extraction.properties.items():
                prop_obj = props_by_name.get(prop_name)
                if prop_obj is None:
                    results.extend(
                        evaluate_field_rule(
                            f"extraction.properties.{prop_name}",
                            prop_rule,
                            found=False,
                            actual=None,
                            anomaly=None,
                        )
                    )
                else:
                    raw_anomaly = prop_obj.get("anomaly")
                    anomaly_dict = (
                        raw_anomaly if isinstance(raw_anomaly, dict) else None
                    )
                    results.extend(
                        evaluate_field_rule(
                            f"extraction.properties.{prop_name}",
                            prop_rule,
                            found=True,
                            actual=prop_obj.get("value"),
                            anomaly=anomaly_dict,
                        )
                    )

    # 7. barcodes
    if expect.barcodes is not None:
        barcodes = result_section.get("barcodes")
        if not isinstance(barcodes, list):
            results.append(
                AssertionResult(
                    path="barcodes",
                    passed=False,
                    expected="list",
                    actual=barcodes,
                    message="Expected 'data.result.barcodes' list in execution result",
                )
            )
        else:
            bc_count = len(barcodes)
            if expect.barcodes.min_count is not None:
                passed = bc_count >= expect.barcodes.min_count
                results.append(
                    AssertionResult(
                        path="barcodes.min_count",
                        passed=passed,
                        expected=f">= {expect.barcodes.min_count}",
                        actual=bc_count,
                        message=(
                            ""
                            if passed
                            else f"Expected at least {expect.barcodes.min_count} barcodes, got {bc_count}"
                        ),
                    )
                )
            if expect.barcodes.max_count is not None:
                passed = bc_count <= expect.barcodes.max_count
                results.append(
                    AssertionResult(
                        path="barcodes.max_count",
                        passed=passed,
                        expected=f"<= {expect.barcodes.max_count}",
                        actual=bc_count,
                        message=(
                            ""
                            if passed
                            else f"Expected at most {expect.barcodes.max_count} barcodes, got {bc_count}"
                        ),
                    )
                )
            if expect.barcodes.contains_type is not None:
                expected_types = (
                    [expect.barcodes.contains_type]
                    if isinstance(expect.barcodes.contains_type, str)
                    else list(expect.barcodes.contains_type)
                )
                actual_types = [
                    str(bc.get("type", "")) for bc in barcodes if isinstance(bc, dict)
                ]
                actual_norm = {t.upper() for t in actual_types}
                missing = [t for t in expected_types if t.upper() not in actual_norm]
                passed = len(missing) == 0
                results.append(
                    AssertionResult(
                        path="barcodes.contains_type",
                        passed=passed,
                        expected=expected_types,
                        actual=actual_types,
                        message=(
                            ""
                            if passed
                            else f"Missing expected barcode types {missing}; found {actual_types}"
                        ),
                    )
                )

    # 8. Generic json_path assertions
    for jp, jp_rule in expect.json_path.items():
        found, value = resolve_json_path(exec_payload, jp)
        if not found and execution_response is not None:
            # Fallback to initial HTTP payload (e.g. checking submit response fields)
            found, value = resolve_json_path(http_response.body_json, jp)
        results.extend(
            evaluate_field_rule(
                f"json_path.{jp}",
                jp_rule,
                found=found,
                actual=value,
            )
        )

    return results
