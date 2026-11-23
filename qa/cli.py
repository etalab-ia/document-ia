"""Command-line interface for the Document IA QA toolkit."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from qa.client import DocumentIAClient
from qa.config import ConfigError, ProdGuardError, ResolvedEnvConfig, load_env_config
from qa.fixtures import (
    DEFAULT_SCENARIOS_DIR,
    FixturePathTraversalError,
    discover_scenario_files,
    list_synthetic_fixtures,
    write_synthetic_fixtures,
)
from qa.models import ChaosMode, RunReport, utc_now_iso
from qa.reporter import (
    compare_reports,
    format_comparison_report,
    format_load_report,
    format_run_report,
    load_run_report,
    save_json_report,
    summarize_results,
)
from qa.runners.chaos import ChaosConfigError
from qa.runners.load_runner import LoadConfig, run_load_test
from qa.runners.protocol_runner import (
    PROTOCOL_SCENARIOS,
    filter_protocol_scenarios,
    run_protocol_scenarios,
)
from qa.runners.scenario_runner import (
    ScenarioLoadError,
    filter_scenarios,
    load_scenarios,
    run_scenarios,
)

DEFAULT_BASELINE_TAGS = frozenset({"baseline", "smoke", "functional", "negative"})
DEFAULT_DEVELOP_TAGS = frozenset(
    {
        "baseline",
        "smoke",
        "functional",
        "negative",
        "security",
        "idempotency",
        "resilience",
        "develop",
    }
)
DEFAULT_EXCLUDED_TAGS = frozenset({"ratelimit-burst", "webhook", "load"})


def _parse_csv_set(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    items = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return items or None


def _parse_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _resolve_default_tags(
    env_config: ResolvedEnvConfig,
    explicit_tags: set[str] | None,
    explicit_exclude: set[str] | None,
    explicit_ids: set[str] | None,
) -> tuple[set[str] | None, set[str] | None]:
    if explicit_ids is not None:
        return None, explicit_exclude
    if explicit_tags is not None:
        return explicit_tags, explicit_exclude

    include = (
        set(DEFAULT_DEVELOP_TAGS)
        if env_config.profile == "develop"
        else set(DEFAULT_BASELINE_TAGS)
    )
    exclude = set(DEFAULT_EXCLUDED_TAGS)
    if explicit_exclude:
        exclude |= explicit_exclude
    return include, exclude


def _add_common_env_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env",
        default="local",
        help="Target environment: local, sandbox, staging (alias: develop), prod (default: local)",
    )
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help="Explicitly allow targeting production environment (still blocks load/chaos).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Optional override for the target API base URL.",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=None,
        help="Directory containing local real documents and optional local scenarios/.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m qa",
        description="Document IA QA, non-regression, security, load and chaos toolkit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- list ---
    p_list = sub.add_parser(
        "list",
        help="List discovered YAML scenarios, protocol scenarios, and synthetic fixtures.",
    )
    _add_common_env_args(p_list)
    p_list.add_argument(
        "--scenarios",
        type=Path,
        nargs="*",
        default=None,
        help="Explicit YAML scenario files or directories.",
    )
    p_list.add_argument(
        "--tags",
        default=None,
        help="Comma-separated tags to filter displayed scenarios.",
    )
    p_list.add_argument(
        "--exclude-tags",
        default=None,
        help="Comma-separated tags to exclude.",
    )

    # --- run ---
    p_run = sub.add_parser(
        "run",
        help="Run declarative YAML scenarios and multi-step protocol tests.",
    )
    _add_common_env_args(p_run)
    p_run.add_argument(
        "--scenarios",
        type=Path,
        nargs="*",
        default=None,
        help="Explicit YAML scenario files or directories.",
    )
    p_run.add_argument(
        "--tags",
        default=None,
        help="Comma-separated tags to run (overrides profile defaults).",
    )
    p_run.add_argument(
        "--exclude-tags",
        default=None,
        help="Comma-separated tags to exclude.",
    )
    p_run.add_argument(
        "--id",
        dest="scenario_ids",
        default=None,
        help="Comma-separated exact scenario/protocol IDs to run.",
    )
    p_run.add_argument(
        "--strict-fixtures",
        action="store_true",
        help="Fail instead of SKIPPED when a local fixture file is missing.",
    )
    p_run.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=2,
        help="Max concurrent declarative scenarios (default: 2).",
    )
    p_run.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Path to write JSON RunReport.",
    )
    p_run.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show all individual assertions.",
    )

    # --- load ---
    p_load = sub.add_parser(
        "load",
        help="Run concurrent load test with optional chaos injection and 0-drop verification.",
    )
    _add_common_env_args(p_load)
    p_load.add_argument(
        "-n",
        "--requests",
        type=int,
        default=10,
        help="Total number of workflow executions to submit (default: 10).",
    )
    p_load.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=5,
        help="Maximum concurrent in-flight submissions (default: 5).",
    )
    p_load.add_argument(
        "--workflow-id",
        default="document-classification-extraction-v2",
        help="Target v2 workflow ID (default: document-classification-extraction-v2).",
    )
    p_load.add_argument(
        "--files",
        default="synthetic:minimal_pdf",
        help="Comma-separated local paths or synthetic:<name> fixtures (round-robin).",
    )
    p_load.add_argument(
        "--override",
        default=None,
        help="Optional JSON string for workflow v2 override payload.",
    )
    p_load.add_argument(
        "--use-idempotency-key",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Send a unique Idempotency-Key per request and reuse on retry (default: true).",
    )
    p_load.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds (default: 2.0).",
    )
    p_load.add_argument(
        "--max-timeout",
        type=float,
        default=None,
        help="Max polling timeout in seconds per execution (default: 180s, or 360s with chaos).",
    )
    p_load.add_argument(
        "--chaos-mode",
        choices=("none", "manual", "docker", "scalingo"),
        default="none",
        help="Chaos fault-injection provider (default: none).",
    )
    p_load.add_argument(
        "--chaos-after",
        type=int,
        default=3,
        help="Number of accepted submissions before triggering chaos (default: 3).",
    )
    p_load.add_argument(
        "--chaos-target",
        default=None,
        help="Target container name (docker) or container type (scalingo, e.g. worker).",
    )
    p_load.add_argument(
        "--scalingo-app",
        default=None,
        help="Scalingo application name (required for --chaos-mode scalingo).",
    )
    p_load.add_argument(
        "--chaos-downtime-s",
        type=float,
        default=5.0,
        help="Downtime duration in seconds for docker stop/start (default: 5.0).",
    )
    p_load.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Path to write JSON LoadReport.",
    )

    # --- compare ---
    p_cmp = sub.add_parser(
        "compare",
        help="Compare two JSON RunReports (e.g. sandbox vs staging) for regressions.",
    )
    p_cmp.add_argument("baseline", type=Path, help="Baseline RunReport JSON file.")
    p_cmp.add_argument("target", type=Path, help="Target RunReport JSON file.")
    p_cmp.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional path to write JSON ComparisonReport.",
    )
    p_cmp.add_argument(
        "--fail-on-functional-diff",
        action="store_true",
        help="Also exit 1 when functional outputs differ even if statuses stayed PASS.",
    )

    # --- gen-fixtures ---
    p_gen = sub.add_parser(
        "gen-fixtures",
        help="Materialize all pure-Python synthetic fixtures onto disk.",
    )
    p_gen.add_argument(
        "--output-dir",
        type=Path,
        default=Path("qa/data/synthetic"),
        help="Destination directory for synthetic fixtures (default: qa/data/synthetic).",
    )

    return parser


def _cmd_list(args: argparse.Namespace) -> int:
    env_config = load_env_config(
        args.env,
        allow_prod=args.allow_prod,
        base_url_override=args.base_url,
        fixtures_dir_override=args.fixtures_dir,
    )
    include_tags = _parse_csv_set(args.tags)
    exclude_tags = _parse_csv_set(args.exclude_tags)

    scenario_files = discover_scenario_files(
        fixtures_dir=env_config.fixtures_dir,
        extra_paths=args.scenarios,
        default_scenarios_dir=DEFAULT_SCENARIOS_DIR,
    )
    scenarios = load_scenarios(scenario_files)
    selected_scenarios = filter_scenarios(
        scenarios,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        target_profile=None,
    )
    selected_protocols = filter_protocol_scenarios(
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        target_profile="develop",
        include_opt_in=(include_tags is None),
    )

    print(
        f"Environment: {env_config.name} (profile={env_config.profile}, url={env_config.base_url})"
    )
    print(f"Fixtures dir: {env_config.fixtures_dir}")
    print("\nDeclarative Scenarios:")
    for sc in selected_scenarios:
        tags_str = ",".join(sc.tags)
        print(f"  [S] {sc.id:<36} profile={sc.min_profile:<8} tags=[{tags_str}]")
    print("\nProtocol Scenarios:")
    for pr in selected_protocols:
        tags_str = ",".join(pr.tags)
        opt = " (opt-in)" if pr.opt_in else ""
        print(f"  [P] {pr.id:<36} profile={pr.min_profile:<8} tags=[{tags_str}]{opt}")
    print("\nSynthetic Fixtures:")
    for fx in list_synthetic_fixtures():
        print(f"  - {fx}")
    return 0


async def _cmd_run_async(args: argparse.Namespace) -> int:
    env_config = load_env_config(
        args.env,
        allow_prod=args.allow_prod,
        base_url_override=args.base_url,
        fixtures_dir_override=args.fixtures_dir,
    )
    explicit_tags = _parse_csv_set(args.tags)
    explicit_exclude = _parse_csv_set(args.exclude_tags)
    explicit_ids = (
        {part.strip() for part in args.scenario_ids.split(",") if part.strip()}
        if args.scenario_ids
        else None
    )

    include_tags, exclude_tags = _resolve_default_tags(
        env_config, explicit_tags, explicit_exclude, explicit_ids
    )

    scenario_files = discover_scenario_files(
        fixtures_dir=env_config.fixtures_dir,
        extra_paths=args.scenarios,
        default_scenarios_dir=DEFAULT_SCENARIOS_DIR,
    )
    all_scenarios = load_scenarios(scenario_files)
    if explicit_ids is not None:
        known_ids = {s.id for s in all_scenarios} | set(PROTOCOL_SCENARIOS.keys())
        unknown_ids = explicit_ids - known_ids
        if unknown_ids:
            raise ConfigError(
                f"Unknown scenario/protocol ID(s): {', '.join(sorted(unknown_ids))}"
            )

    selected_scenarios = filter_scenarios(
        all_scenarios,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        scenario_ids=explicit_ids,
        target_profile=env_config.profile,
    )
    selected_protocols = filter_protocol_scenarios(
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        scenario_ids=explicit_ids,
        target_profile=env_config.profile,
    )
    if not selected_scenarios and not selected_protocols:
        raise ConfigError("No scenarios matched the requested filters.")

    started_at = utc_now_iso()
    t0 = time.perf_counter()

    async with DocumentIAClient(
        env_config, concurrency=max(args.concurrency, 4)
    ) as client:
        scenario_results = await run_scenarios(
            client,
            selected_scenarios,
            fixtures_dir=env_config.fixtures_dir,
            strict_fixtures=args.strict_fixtures,
            concurrency=args.concurrency,
        )
        protocol_results = await run_protocol_scenarios(client, selected_protocols)

    all_results = [*scenario_results, *protocol_results]
    summary = summarize_results(all_results)
    report = RunReport(
        env=env_config.name,
        profile=env_config.profile,
        base_url=env_config.base_url,
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_s=round(time.perf_counter() - t0, 3),
        tags_filter=sorted(include_tags) if include_tags else [],
        summary=summary,
        results=all_results,
    )

    print(format_run_report(report, verbose=args.verbose))
    if args.report is not None:
        saved = save_json_report(report, args.report)
        print(f"Report written to {saved}")

    return 0 if (summary.failed == 0 and summary.errors == 0) else 1


async def _cmd_load_async(args: argparse.Namespace) -> int:
    chaos_mode: ChaosMode = args.chaos_mode
    env_config = load_env_config(
        args.env,
        allow_prod=args.allow_prod,
        for_load=True,
        for_chaos=(chaos_mode != "none"),
        base_url_override=args.base_url,
        fixtures_dir_override=args.fixtures_dir,
    )

    override_dict: dict[str, Any] | None = None
    if args.override:
        try:
            parsed = json.loads(args.override)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid --override JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError("--override must be a JSON object.")
        override_dict = parsed

    files_list = _parse_csv_list(args.files) or ["synthetic:minimal_pdf"]
    default_timeout = (
        360.0 if chaos_mode != "none" else env_config.default_poll_timeout_s
    )
    max_timeout = args.max_timeout if args.max_timeout is not None else default_timeout

    load_cfg = LoadConfig(
        requests=args.requests,
        concurrency=args.concurrency,
        workflow_id=args.workflow_id,
        files=files_list,
        override=override_dict,
        use_idempotency_key=args.use_idempotency_key,
        poll_interval_s=args.poll_interval,
        max_timeout_s=max_timeout,
        chaos_mode=chaos_mode,
        chaos_after=args.chaos_after,
        chaos_target=args.chaos_target,
        scalingo_app=args.scalingo_app,
        chaos_downtime_s=args.chaos_downtime_s,
    )

    async with DocumentIAClient(
        env_config, concurrency=max(args.concurrency, 10)
    ) as client:
        report = await run_load_test(
            client,
            load_cfg,
            fixtures_dir=env_config.fixtures_dir,
        )

    print(format_load_report(report))
    if args.report is not None:
        saved = save_json_report(report, args.report)
        print(f"Load report written to {saved}")

    return 0 if report.assertions.all_passed else 1


def _cmd_compare(args: argparse.Namespace) -> int:
    baseline = load_run_report(args.baseline)
    target = load_run_report(args.target)
    comparison = compare_reports(
        baseline,
        target,
        baseline_file=str(args.baseline),
        target_file=str(args.target),
    )
    print(format_comparison_report(comparison))
    if args.report is not None:
        saved = save_json_report(comparison, args.report)
        print(f"Comparison report written to {saved}")

    if comparison.has_regressions:
        return 1
    if args.fail_on_functional_diff and comparison.functional_diffs_count > 0:
        return 1
    return 0


def _cmd_gen_fixtures(args: argparse.Namespace) -> int:
    written = write_synthetic_fixtures(args.output_dir)
    print(f"Generated {len(written)} synthetic fixtures in {args.output_dir}:")
    for path in written:
        size_kb = path.stat().st_size / 1024.0
        print(f"  - {path.name:<28} ({size_kb:.1f} KB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _cmd_list(args)
        if args.command == "run":
            return asyncio.run(_cmd_run_async(args))
        if args.command == "load":
            return asyncio.run(_cmd_load_async(args))
        if args.command == "compare":
            return _cmd_compare(args)
        if args.command == "gen-fixtures":
            return _cmd_gen_fixtures(args)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except (
        ConfigError,
        ProdGuardError,
        ScenarioLoadError,
        ChaosConfigError,
        FixturePathTraversalError,
        FileNotFoundError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
