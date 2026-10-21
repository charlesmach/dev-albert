"""Recompute the recorded reference values for the agent benchmark.

Four reference files quote numbers produced by running the SDK directly, with
no agent involved. This script re-derives them so the recordings can be checked
rather than trusted:

    python -m scripts.record_benchmark_references --check   # fail on drift
    python -m scripts.record_benchmark_references            # print a fresh recording

A drift means either the engine changed behavior or a frozen input moved. Both
need a decision, so --check belongs in the quality gate while the task set is
being developed.

Reference values for the deterministic fixtures are not recomputed here: those
come from the generator's recorded plant, which is independent of this code by
construction. See benchmark/README.md.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from alphalab import (
    StrategyRepository,
    create_default_engine,
    create_runtime_engine,
    evaluate_factor_research,
    run_strategy_backtest,
)
from alphalab.analytics import PerformanceMetrics

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = REPO_ROOT / "benchmark"
LEGACY_DB = REPO_ROOT / "tests" / "fixtures" / "legacy_app.db"
PROJECT_ID = "sdk-v1-default"
FACTOR_ID = "momentum_20d"
BACKTEST_WINDOW = ("2025-01-01", "2025-06-30")
RESEARCH_WINDOW = ("2021-08-02", "2026-06-30")
SHORT_WINDOW = ("2025-02-03", "2025-05-07")
TOLERANCE = 1e-9


def _repository(path: Path) -> StrategyRepository:
    shutil.copy2(LEGACY_DB, path)
    return StrategyRepository(path)


def _demo_backtest(tmp: Path, policy: str) -> dict:
    repository = _repository(tmp / f"{policy}.db")
    try:
        result = run_strategy_backtest(
            repository,
            PROJECT_ID,
            *BACKTEST_WINDOW,
            create_default_engine(),
            seed=0,
            execution_data_policy=policy,
        )
    finally:
        repository.close()
    diagnostics = result.diagnostics
    return {
        "metrics_252": PerformanceMetrics.summarize(result.returns, periods_per_year=252),
        "metrics_12": PerformanceMetrics.summarize(result.returns, periods_per_year=12),
        "return_rows": len(result.returns),
        "revision": diagnostics.get("revision"),
        "source_sha256": diagnostics.get("source_sha256"),
        "execution_reliable": diagnostics.get("execution_reliable"),
        "execution_invalid_reasons": diagnostics.get("execution_invalid_reasons"),
        "execution_fidelity": diagnostics.get("execution_fidelity"),
        "execution_summary": diagnostics.get("execution_summary"),
        "execution_data_exclusions": diagnostics.get("execution_data_exclusions"),
    }


def _factor_research(tmp: Path, label: str, fixture: str | None, window: tuple[str, str]) -> dict:
    repository = _repository(tmp / f"{label}.db")
    if fixture is None:
        engine = create_default_engine()
    else:
        root = tmp / f"{label}-runtime"
        shutil.copytree(BENCHMARK / "data" / fixture, root)
        (root / "fixture.json").unlink()
        engine = create_runtime_engine(root)
    try:
        result = evaluate_factor_research(
            repository,
            PROJECT_ID,
            FACTOR_ID,
            engine,
            *window,
            quantiles=5,
            horizons=(1, 3, 6),
        )
    finally:
        repository.close()
    return {
        "status": result["status"],
        "periods": result["periods"],
        "summary": result["summary"],
        "revision": result["revision"],
        "source_sha256": result["source_sha256"],
    }


def record() -> dict:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        return {
            "demo_backtest_illustrative": _demo_backtest(tmp, "illustrative"),
            "demo_backtest_strict": _demo_backtest(tmp, "strict"),
            "demo_factor_research": _factor_research(tmp, "demo", None, RESEARCH_WINDOW),
            "clean_factor_research": _factor_research(
                tmp, "clean", "clean-small", SHORT_WINDOW
            ),
        }


def _expected(split: str, task_id: str) -> dict:
    path = BENCHMARK / "reference" / split / task_id / "expected.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _close(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(expected)) <= TOLERANCE
    return actual == expected


def _compare(label: str, actual: Any, expected: Any, drift: list[str]) -> None:
    if not _close(actual, expected):
        drift.append(f"{label}: recorded {expected!r}, recomputed {actual!r}")


def check(recorded: dict) -> list[str]:
    drift: list[str] = []

    illustrative = recorded["demo_backtest_illustrative"]
    pinned = _expected("dev", "strategy-backtest-pinned-metrics")["ground_truth"]
    for key, value in pinned["metrics"].items():
        _compare(f"pinned-metrics/{key}", illustrative["metrics_252"][key], value, drift)
    _compare(
        "pinned-metrics/return_rows", illustrative["return_rows"], pinned["return_rows"], drift
    )
    _compare(
        "pinned-metrics/successful_trade_count",
        illustrative["execution_summary"]["successful_trade_count"],
        pinned["diagnostics"]["execution_summary"]["successful_trade_count"],
        drift,
    )

    convention = _expected("dev", "audit-return-convention")["ground_truth"]
    for key, value in convention["corrected"].items():
        _compare(f"return-convention/correct/{key}", illustrative["metrics_252"][key], value, drift)
    for key, value in convention["as_reported_wrong"].items():
        _compare(f"return-convention/wrong/{key}", illustrative["metrics_12"][key], value, drift)

    strict = recorded["demo_backtest_strict"]
    zero = _expected("holdout", "strategy-zero-execution-report")["ground_truth"]
    _compare(
        "zero-execution/successful_trade_count",
        strict["execution_summary"]["successful_trade_count"],
        zero["diagnostics"]["execution_summary"]["successful_trade_count"],
        drift,
    )
    _compare(
        "zero-execution/fidelity_mean",
        strict["execution_fidelity"]["mean"],
        zero["diagnostics"]["execution_fidelity"]["mean"],
        drift,
    )
    _compare(
        "zero-execution/total_return",
        strict["metrics_252"]["total_return"],
        zero["metrics"]["total_return"],
        drift,
    )

    fidelity = _expected("dev", "risk-execution-fidelity-explain")["ground_truth"]
    _compare(
        "fidelity/illustrative_fills",
        illustrative["execution_summary"]["successful_trade_count"],
        fidelity["illustrative"]["successful_trade_count"],
        drift,
    )
    _compare(
        "fidelity/strict_exclusions",
        strict["execution_data_exclusions"]["symbol_date_count"],
        fidelity["strict"]["exclusions_symbol_date_count"],
        drift,
    )

    research = recorded["demo_factor_research"]
    rank_ic = _expected("dev", "factor-rank-ic-evidence")["ground_truth"]
    _compare("rank-ic/status", research["status"], rank_ic["status"], drift)
    _compare("rank-ic/periods", research["periods"], rank_ic["periods"], drift)
    for key in ("mean_rank_ic", "icir", "newey_west_t_stat", "positive_ic_ratio"):
        _compare(f"rank-ic/{key}", research["summary"][key], rank_ic["summary"][key], drift)

    short = recorded["clean_factor_research"]
    honesty = _expected("dev", "factor-short-window-honesty")["ground_truth"]
    _compare("short-window/status", short["status"], honesty["status"], drift)
    _compare("short-window/periods", short["periods"], honesty["periods"], drift)
    for key, value in honesty["summary"].items():
        _compare(f"short-window/summary/{key}", short["summary"].get(key), value, drift)

    return drift


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare against the committed reference files and exit nonzero on drift.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the fresh recording to this path as JSON.",
    )
    args = parser.parse_args()
    recorded = record()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(recorded, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if args.check:
        drift = check(recorded)
        if drift:
            raise SystemExit(
                "Benchmark reference values drifted:\n  " + "\n  ".join(drift)
            )
        print(json.dumps({"status": "current", "checked": sorted(recorded)}, indent=2))
        return
    print(json.dumps(recorded, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
