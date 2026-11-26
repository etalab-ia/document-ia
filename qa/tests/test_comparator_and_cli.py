"""Unit tests for qa.reporter comparator and qa.cli subcommands."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.cli import main
from qa.models import ExecutionSnapshot, RunReport, ScenarioResult
from qa.reporter import (
    compare_reports,
    format_comparison_report,
    save_json_report,
    summarize_results,
)


def _make_report(env: str, results: list[ScenarioResult]) -> RunReport:
    return RunReport(
        env=env,
        profile="baseline" if env == "sandbox" else "develop",
        base_url=f"https://api.{env}.example.test",
        summary=summarize_results(results),
        results=results,
    )


def test_compare_reports_detects_regression_improvement_and_diffs() -> None:
    baseline = _make_report(
        "sandbox",
        [
            ScenarioResult(
                id="sc-regressed",
                status="PASS",
                snapshot=ExecutionSnapshot(total_processing_time_ms=1000),
            ),
            ScenarioResult(
                id="sc-improved",
                status="FAIL",
            ),
            ScenarioResult(
                id="sc-func-diff",
                status="PASS",
                snapshot=ExecutionSnapshot(
                    total_processing_time_ms=1000,
                    classification={"document_type": "cni", "confidence": 0.9},
                    extraction_type="cni",
                    extraction_properties={"nom": "DUPONT"},
                    barcodes_count=1,
                    barcode_types=["2D_DOC"],
                ),
            ),
            ScenarioResult(id="sc-removed", status="PASS"),
        ],
    )

    target = _make_report(
        "staging",
        [
            ScenarioResult(
                id="sc-regressed",
                status="FAIL",
                snapshot=ExecutionSnapshot(total_processing_time_ms=1200),
            ),
            ScenarioResult(
                id="sc-improved",
                status="PASS",
            ),
            ScenarioResult(
                id="sc-func-diff",
                status="PASS",
                snapshot=ExecutionSnapshot(
                    total_processing_time_ms=1500,
                    classification={"document_type": "cni", "confidence": 0.92},
                    extraction_type="cni",
                    extraction_properties={"nom": "DURAND"},
                    barcodes_count=0,
                    barcode_types=[],
                ),
            ),
            ScenarioResult(id="sc-added", status="PASS"),
        ],
    )

    cmp = compare_reports(baseline, target)
    assert cmp.has_regressions is True
    assert cmp.regressions == 1
    assert cmp.improvements == 1
    assert cmp.functional_diffs_count == 1
    assert cmp.added == 1
    assert cmp.removed == 1

    by_id = {item.scenario_id: item for item in cmp.items}
    assert by_id["sc-regressed"].verdict == "REGRESSION"
    assert by_id["sc-improved"].verdict == "IMPROVEMENT"
    assert by_id["sc-func-diff"].latency_delta_ms == 500
    assert by_id["sc-func-diff"].latency_delta_pct == 50.0
    assert any(
        "extraction.properties.nom" in d for d in by_id["sc-func-diff"].functional_diffs
    )
    assert any("barcodes.count" in d for d in by_id["sc-func-diff"].functional_diffs)

    rendered = format_comparison_report(cmp, use_color=False)
    assert "REGRESSION" in rendered
    assert "IMPROVEMENT" in rendered


def test_cli_list_gen_fixtures_compare_and_prod_guard(tmp_path: Path) -> None:
    # list command
    assert main(["list", "--env", "staging"]) == 0

    # prod guard returns exit code 2 without --allow-prod
    assert main(["run", "--env", "prod"]) == 2
    assert main(["load", "--env", "prod", "--allow-prod"]) == 2

    # gen-fixtures command
    out_dir = tmp_path / "syn"
    assert main(["gen-fixtures", "--output-dir", str(out_dir)]) == 0
    assert len(list(out_dir.iterdir())) == 7

    # compare command exit code 1 on regression, 0 when clean
    base_path = save_json_report(
        _make_report("sandbox", [ScenarioResult(id="s1", status="PASS")]),
        tmp_path / "base.json",
    )
    ok_path = save_json_report(
        _make_report("staging", [ScenarioResult(id="s1", status="PASS")]),
        tmp_path / "ok.json",
    )
    reg_path = save_json_report(
        _make_report("staging", [ScenarioResult(id="s1", status="FAIL")]),
        tmp_path / "reg.json",
    )

    assert main(["compare", str(base_path), str(ok_path)]) == 0
    assert main(["compare", str(base_path), str(reg_path)]) == 1


def test_cli_handles_invalid_load_args_path_traversal_and_bad_json_cleanly(
    tmp_path: Path,
) -> None:
    # ValueError (-n 0) -> exit code 2 without raw traceback
    assert main(["load", "--env", "local", "-n", "0"]) == 2
    # FixturePathTraversalError -> exit code 2
    assert main(["load", "--env", "local", "--files", "../etc/passwd"]) == 2
    # Malformed JSON report on compare -> exit code 2
    bad_json = tmp_path / "broken.json"
    bad_json.write_text("{not-valid-json", encoding="utf-8")
    assert main(["compare", str(bad_json), str(bad_json)]) == 2


def test_cli_run_rejects_unknown_scenario_id_or_empty_selection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Unknown --id returns exit code 2
    code_unknown_id = main(["run", "--env", "local", "--id", "non-existent-scenario"])
    assert code_unknown_id == 2
    err_id = capsys.readouterr().err
    assert "Unknown scenario/protocol ID(s): non-existent-scenario" in err_id

    # Filter matching 0 scenarios returns exit code 2
    code_empty_tags = main(["run", "--env", "local", "--tags", "non-existent-tag"])
    assert code_empty_tags == 2
    err_tags = capsys.readouterr().err
    assert "No scenarios matched the requested filters" in err_tags
