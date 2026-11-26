"""Console reporting, JSON export, and cross-environment report comparison."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal

from qa.models import (
    ComparisonDiffItem,
    ComparisonReport,
    ExecutionSnapshot,
    LatencyStats,
    LoadReport,
    RunReport,
    RunSummary,
    ScenarioResult,
    ScenarioStatus,
)


class _Ansi:
    """Minimal ANSI color helper respecting NO_COLOR and TTY."""

    def __init__(self, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.enabled = enabled

    def wrap(self, text: str, code: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def green(self, text: str) -> str:
        return self.wrap(text, "32")

    def red(self, text: str) -> str:
        return self.wrap(text, "31")

    def yellow(self, text: str) -> str:
        return self.wrap(text, "33")

    def cyan(self, text: str) -> str:
        return self.wrap(text, "36")

    def bold(self, text: str) -> str:
        return self.wrap(text, "1")

    def dim(self, text: str) -> str:
        return self.wrap(text, "2")


def summarize_results(results: list[ScenarioResult]) -> RunSummary:
    """Compute aggregated PASS/FAIL/SKIPPED/ERROR counts."""
    return RunSummary(
        total=len(results),
        passed=sum(1 for r in results if r.status == "PASS"),
        failed=sum(1 for r in results if r.status == "FAIL"),
        skipped=sum(1 for r in results if r.status == "SKIPPED"),
        errors=sum(1 for r in results if r.status == "ERROR"),
    )


def save_json_report(
    report: RunReport | LoadReport | ComparisonReport,
    output_path: Path,
) -> Path:
    """Persist any QA report model as indented UTF-8 JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        report.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def load_run_report(path: Path) -> RunReport:
    """Load a previously saved RunReport JSON file."""
    return RunReport.model_validate_json(path.read_text(encoding="utf-8"))


def _diff_snapshots(
    base_snap: ExecutionSnapshot | None,
    target_snap: ExecutionSnapshot | None,
) -> list[str]:
    """Compare functional outputs between two ExecutionSnapshots."""
    if base_snap is None or target_snap is None:
        return []

    diffs: list[str] = []

    base_cls = (
        base_snap.classification.get("document_type")
        if base_snap.classification
        else None
    )
    target_cls = (
        target_snap.classification.get("document_type")
        if target_snap.classification
        else None
    )
    if base_cls != target_cls:
        diffs.append(f"classification.document_type: {base_cls!r} -> {target_cls!r}")

    if base_snap.extraction_type != target_snap.extraction_type:
        diffs.append(
            f"extraction.type: {base_snap.extraction_type!r} -> {target_snap.extraction_type!r}"
        )

    all_prop_keys = sorted(
        set(base_snap.extraction_properties.keys())
        | set(target_snap.extraction_properties.keys())
    )
    for key in all_prop_keys:
        b_val: Any = base_snap.extraction_properties.get(key, "<missing>")
        t_val: Any = target_snap.extraction_properties.get(key, "<missing>")
        if b_val != t_val:
            diffs.append(f"extraction.properties.{key}: {b_val!r} -> {t_val!r}")

    if base_snap.barcodes_count != target_snap.barcodes_count:
        diffs.append(
            f"barcodes.count: {base_snap.barcodes_count!r} -> {target_snap.barcodes_count!r}"
        )
    elif sorted(base_snap.barcode_types) != sorted(target_snap.barcode_types):
        diffs.append(
            f"barcodes.types: {base_snap.barcode_types!r} -> {target_snap.barcode_types!r}"
        )

    return diffs


DiffVerdict = Literal["REGRESSION", "IMPROVEMENT", "UNCHANGED", "ADDED", "REMOVED"]


def _classify_status_transition(
    base_status: ScenarioStatus | None,
    target_status: ScenarioStatus | None,
) -> tuple[DiffVerdict, bool]:
    """Return (verdict, is_regression) for a scenario status transition."""
    if base_status is None:
        return "ADDED", False
    if target_status is None:
        return "REMOVED", False
    if base_status == "PASS" and target_status in ("FAIL", "ERROR"):
        return "REGRESSION", True
    if base_status in ("FAIL", "ERROR") and target_status == "PASS":
        return "IMPROVEMENT", False
    return "UNCHANGED", False


def compare_reports(
    baseline: RunReport,
    target: RunReport,
    *,
    baseline_file: str = "",
    target_file: str = "",
) -> ComparisonReport:
    """Compare a baseline RunReport (e.g. sandbox) against a target RunReport (e.g. staging)."""
    base_by_id = {r.id: r for r in baseline.results}
    target_by_id = {r.id: r for r in target.results}
    all_ids = sorted(set(base_by_id.keys()) | set(target_by_id.keys()))

    items: list[ComparisonDiffItem] = []
    regressions = 0
    improvements = 0
    functional_diffs_count = 0
    unchanged = 0
    added = 0
    removed = 0

    for sid in all_ids:
        b_res = base_by_id.get(sid)
        t_res = target_by_id.get(sid)
        b_status = b_res.status if b_res else None
        t_status = t_res.status if t_res else None

        verdict_raw, is_reg = _classify_status_transition(b_status, t_status)
        func_diffs = (
            _diff_snapshots(b_res.snapshot, t_res.snapshot)
            if (b_res is not None and t_res is not None)
            else []
        )
        if func_diffs:
            functional_diffs_count += 1

        b_ms = (
            b_res.snapshot.total_processing_time_ms
            if (b_res is not None and b_res.snapshot is not None)
            else None
        )
        t_ms = (
            t_res.snapshot.total_processing_time_ms
            if (t_res is not None and t_res.snapshot is not None)
            else None
        )
        delta_ms: int | None = None
        delta_pct: float | None = None
        if b_ms is not None and t_ms is not None:
            delta_ms = t_ms - b_ms
            if b_ms > 0:
                delta_pct = round((delta_ms / b_ms) * 100.0, 1)

        if is_reg:
            regressions += 1
        elif verdict_raw == "IMPROVEMENT":
            improvements += 1
        elif verdict_raw == "ADDED":
            added += 1
        elif verdict_raw == "REMOVED":
            removed += 1
        else:
            unchanged += 1

        items.append(
            ComparisonDiffItem(
                scenario_id=sid,
                baseline_status=b_status,
                target_status=t_status,
                verdict=verdict_raw,
                functional_diffs=func_diffs,
                baseline_processing_ms=b_ms,
                target_processing_ms=t_ms,
                latency_delta_ms=delta_ms,
                latency_delta_pct=delta_pct,
            )
        )

    return ComparisonReport(
        baseline_env=baseline.env,
        target_env=target.env,
        baseline_file=baseline_file,
        target_file=target_file,
        regressions=regressions,
        improvements=improvements,
        functional_diffs_count=functional_diffs_count,
        unchanged=unchanged,
        added=added,
        removed=removed,
        has_regressions=regressions > 0,
        items=items,
    )


def _color_status(status: str, ansi: _Ansi) -> str:
    if status == "PASS":
        return ansi.green(f"{status:<7}")
    if status in ("FAIL", "ERROR", "REGRESSION"):
        return ansi.red(f"{status:<7}")
    if status == "SKIPPED":
        return ansi.yellow(f"{status:<7}")
    return ansi.cyan(f"{status:<7}")


def format_run_report(
    report: RunReport,
    *,
    verbose: bool = False,
    use_color: bool | None = None,
) -> str:
    """Format a RunReport for concise, readable terminal output."""
    ansi = _Ansi(enabled=use_color)
    lines: list[str] = [
        "=" * 80,
        ansi.bold(
            f"Document IA QA Run | env={report.env} ({report.profile}) | {report.base_url}"
        ),
        (
            f"Tags: {', '.join(report.tags_filter) if report.tags_filter else 'all'} | "
            f"Duration: {report.duration_s:.2f}s"
        ),
        "=" * 80,
    ]

    for res in report.results:
        badge = _color_status(res.status, ansi)
        kind_tag = "[P]" if res.kind == "protocol" else "[S]"
        duration_str = f"{res.duration_ms:7.0f}ms"
        lines.append(f"  {badge} {kind_tag} {res.id:<36} {duration_str}")
        if res.message and (res.status != "PASS" or verbose):
            lines.append(f"          {ansi.dim(res.message)}")
        if verbose or res.status in ("FAIL", "ERROR"):
            for assertion in res.assertions:
                if not assertion.passed or verbose:
                    mark = ansi.green("ok") if assertion.passed else ansi.red("KO")
                    detail = (
                        assertion.message
                        or f"expected={assertion.expected!r} actual={assertion.actual!r}"
                    )
                    lines.append(f"            [{mark}] {assertion.path}: {detail}")

    s = report.summary
    lines.append("-" * 80)
    summary_line = (
        f"Summary: total={s.total} | "
        f"{ansi.green(f'passed={s.passed}')} | "
        f"{ansi.red(f'failed={s.failed}')} | "
        f"{ansi.yellow(f'skipped={s.skipped}')} | "
        f"{ansi.red(f'errors={s.errors}')}"
    )
    lines.append(summary_line)
    lines.append("=" * 80)
    return "\n".join(lines)


def _fmt_latency(label: str, stats: LatencyStats) -> str:
    if stats.count == 0:
        return f"  {label:<10}: no samples"
    return (
        f"  {label:<10}: n={stats.count} | min={stats.min_ms:.0f}ms | "
        f"avg={stats.avg_ms:.0f}ms | p50={stats.p50_ms:.0f}ms | "
        f"p95={stats.p95_ms:.0f}ms | p99={stats.p99_ms:.0f}ms | max={stats.max_ms:.0f}ms"
    )


def format_load_report(
    report: LoadReport,
    *,
    use_color: bool | None = None,
) -> str:
    """Format a LoadReport with ledger metrics, chaos audit, and latency percentiles."""
    ansi = _Ansi(enabled=use_color)
    a = report.assertions
    verdict = ansi.green("PASS") if a.all_passed else ansi.red("FAIL")
    lines: list[str] = [
        "=" * 80,
        ansi.bold(
            f"Document IA Load & Chaos Test | {verdict} | env={report.env} | workflow={report.workflow_id}"
        ),
        (
            f"Requests={report.total_requests} | Concurrency={report.concurrency} | "
            f"Idempotency={report.use_idempotency_key} | Chaos={report.chaos_mode} | "
            f"Duration={report.duration_s:.2f}s"
        ),
        "=" * 80,
        f"  Accepted     : {report.accepted_count} / {report.total_requests}",
        f"  SUCCESS      : {report.success_count} ({a.success_rate_pct:.1f}%)",
        f"  FAILED       : {report.failed_count}",
        f"  TIMEOUT/DROP : {report.timeout_count}",
        f"  Unsubmitted  : {report.unsubmitted_count}",
        f"  Backpressure : 503={report.backpressure_503_total} | 429={report.rate_limit_429_total} | 500={report.http_500_total}",
        "-" * 80,
        "Latencies:",
        _fmt_latency("Submit", report.submit_latency),
        _fmt_latency("Process", report.process_latency),
        _fmt_latency("End-to-End", report.e2e_latency),
    ]

    if report.chaos_event is not None:
        ce = report.chaos_event
        lines.append("-" * 80)
        lines.append(
            f"Chaos Event: mode={ce.mode} target={ce.target} downtime={ce.downtime_s:.2f}s | {ce.details}"
        )
        if ce.redis_read_during_outage_ok is not None:
            lines.append(
                f"  Redis outage GET /executions readable (200) : {ce.redis_read_during_outage_ok}"
            )
        if ce.redis_write_fail_closed_503 is not None:
            lines.append(
                f"  Redis outage POST /execute fail-closed (503): {ce.redis_write_fail_closed_503}"
            )

    lines.append("-" * 80)
    lines.append("Reliability Invariants:")
    lines.append(
        f"  Zero drop                 : {'OK' if a.zero_drop else f'FAIL ({len(a.dropped_execution_ids)} dropped)'}"
    )
    lines.append(
        f"  Zero idempotency dupes    : {'OK' if a.zero_idempotency_duplicates else f'FAIL ({a.duplicate_idempotency_keys})'}"
    )
    lines.append(
        f"  Zero HTTP 500             : {'OK' if a.zero_http_500 else f'FAIL ({a.http_500_count} x 500)'}"
    )
    lines.append("=" * 80)
    return "\n".join(lines)


def format_comparison_report(
    report: ComparisonReport,
    *,
    use_color: bool | None = None,
) -> str:
    """Format a ComparisonReport (baseline vs target) for terminal output."""
    ansi = _Ansi(enabled=use_color)
    lines: list[str] = [
        "=" * 80,
        ansi.bold(
            f"QA Comparison: {report.baseline_env} (baseline) -> {report.target_env} (target)"
        ),
        "=" * 80,
    ]
    for item in report.items:
        verdict = item.verdict
        if verdict == "REGRESSION":
            badge = ansi.red(f"{verdict:<11}")
        elif verdict == "IMPROVEMENT":
            badge = ansi.green(f"{verdict:<11}")
        elif verdict in ("ADDED", "REMOVED"):
            badge = ansi.cyan(f"{verdict:<11}")
        else:
            badge = ansi.dim(f"{verdict:<11}")

        lat_str = ""
        if item.latency_delta_ms is not None:
            sign = "+" if item.latency_delta_ms >= 0 else ""
            pct_str = (
                f" ({sign}{item.latency_delta_pct:.1f}%)"
                if item.latency_delta_pct is not None
                else ""
            )
            lat_str = f" | proc: {item.baseline_processing_ms}ms -> {item.target_processing_ms}ms ({sign}{item.latency_delta_ms}ms{pct_str})"

        lines.append(
            f"  {badge} {item.scenario_id:<36} "
            f"{item.baseline_status or '-'!s:<7} -> {item.target_status or '-'!s:<7}{lat_str}"
        )
        for diff in item.functional_diffs:
            lines.append(f"              {ansi.yellow('DIFF')} {diff}")

    lines.append("-" * 80)
    lines.append(
        f"Summary: regressions={report.regressions} | improvements={report.improvements} | "
        f"functional_diffs={report.functional_diffs_count} | unchanged={report.unchanged} | "
        f"added={report.added} | removed={report.removed}"
    )
    lines.append("=" * 80)
    return "\n".join(lines)
