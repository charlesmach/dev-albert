"""Recompute the recorded reference values for the agent benchmark.

Five reference files quote numbers produced by running AlphaLab directly, with
no agent involved. This script re-derives them so the recordings can be checked
rather than trusted:

    python -m scripts.record_benchmark_references --check   # fail on drift
    python -m scripts.record_benchmark_references            # print a recording

Drift means either the engine changed behaviour or a frozen input moved. Both
need a decision, so --check belongs in the quality gate while the task set is
being developed.

Everything here runs on the runtime profile, because that is the only profile
the agent's Project-Run tool ever requests. Reference values for the planted
defects are not recomputed here: those come from the generator's recorded
plant, which is independent of this code by construction.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = REPO_ROOT / "benchmark"
LEGACY_DB = REPO_ROOT / "tests" / "fixtures" / "legacy_app.db"
PROJECT_ID = "sdk-v1-default"
FACTOR_ID = "momentum_20d"
BACKTEST_WINDOW = ("2025-02-03", "2026-12-31")
RESEARCH_WINDOW = ("2025-02-03", "2027-12-01")
SHORT_WINDOW = ("2025-02-03", "2025-05-07")
RISK_EXPRESSION = ("defensive_tilt", "MKT - 0.5 * SMB")
TOLERANCE = 1e-9


def _runtime_root(tmp: Path, fixture: str, label: str) -> Path:
    root = tmp / f"{label}-runtime"
    shutil.copytree(BENCHMARK / "data" / fixture, root)
    (root / "fixture.json").unlink()
    return root


def _repository(path: Path):
    from alphalab import StrategyRepository

    shutil.copy2(LEGACY_DB, path)
    return StrategyRepository(path)


def _backtest(tmp: Path) -> dict:
    from alphalab import create_runtime_engine, run_strategy_backtest
    from alphalab.analytics import PerformanceMetrics

    root = _runtime_root(tmp, "research-small", "bt")
    repository = _repository(tmp / "bt.db")
    try:
        result = run_strategy_backtest(
            repository, PROJECT_ID, *BACKTEST_WINDOW, create_runtime_engine(root), seed=0
        )
    finally:
        repository.close()
    diagnostics = result.diagnostics
    return {
        "metrics_252": PerformanceMetrics.summarize(result.returns, periods_per_year=252),
        "metrics_12": PerformanceMetrics.summarize(result.returns, periods_per_year=12),
        "execution_reliable": diagnostics.get("execution_reliable"),
        "execution_data_policy": diagnostics.get("execution_data_policy"),
        "execution_summary": diagnostics.get("execution_summary"),
        "execution_fidelity": diagnostics.get("execution_fidelity"),
    }


def _research(tmp: Path, label: str, fixture: str, window: tuple[str, str]) -> dict:
    from alphalab import create_runtime_engine, evaluate_factor_research

    root = _runtime_root(tmp, fixture, label)
    repository = _repository(tmp / f"{label}.db")
    try:
        result = evaluate_factor_research(
            repository, PROJECT_ID, FACTOR_ID, create_runtime_engine(root),
            *window, quantiles=5, horizons=(1, 3, 6),
        )
    finally:
        repository.close()
    return {"status": result["status"], "periods": result["periods"], "summary": result["summary"]}


_RISK_SCRIPT = """
import json, sys
from apps.api.services import market_analytics_service
name, expression, start, end = sys.argv[1:5]
result = market_analytics_service.compute_custom_risk_factor(
    name, expression, "runtime", start, end
)
print(json.dumps({
    "dependencies": result["dependencies"],
    "summary": result["summary"],
    "warnings": result["warnings"],
    "observations": len(result["returns"]),
}))
"""


def _market_risk(tmp: Path) -> dict:
    """Record in a subprocess: the service binds the runtime root at import time."""

    root = _runtime_root(tmp, "research-small", "risk")
    name, expression = RISK_EXPRESSION
    environment = {**os.environ, "ALPHALAB_RUNTIME_DIR": str(root)}
    completed = subprocess.run(
        [sys.executable, "-c", _RISK_SCRIPT, name, expression, *RESEARCH_WINDOW],
        cwd=REPO_ROOT, env=environment, capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "Could not record the market-risk reference:\n" + (completed.stderr or "").strip()
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def record() -> dict:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        return {
            "runtime_backtest": _backtest(tmp),
            "runtime_research": _research(tmp, "research", "research-small", RESEARCH_WINDOW),
            "clean_research": _research(tmp, "clean", "clean-small", SHORT_WINDOW),
            "market_risk": _market_risk(tmp),
        }


def _expected(split: str, task_id: str) -> dict:
    path = BENCHMARK / "reference" / split / task_id / "expected.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _compare(label: str, actual: Any, expected: Any, drift: list[str]) -> None:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if abs(float(actual) - float(expected)) <= TOLERANCE:
            return
    elif actual == expected:
        return
    drift.append(f"{label}: recorded {expected!r}, recomputed {actual!r}")


def check(recorded: dict) -> list[str]:
    drift: list[str] = []

    backtest = recorded["runtime_backtest"]
    pinned = _expected("dev", "strategy-backtest-pinned-metrics")["ground_truth"]
    for key, value in pinned["metrics"].items():
        _compare(f"pinned/{key}", backtest["metrics_252"][key], value, drift)
    _compare("pinned/execution_reliable", backtest["execution_reliable"],
             pinned["execution_reliable"], drift)
    _compare("pinned/successful_trade_count",
             backtest["execution_summary"]["successful_trade_count"],
             pinned["diagnostics"]["execution_summary"]["successful_trade_count"], drift)
    _compare("pinned/cash_rejection_count",
             backtest["execution_summary"]["cash_rejection_count"],
             pinned["diagnostics"]["execution_summary"]["cash_rejection_count"], drift)

    convention = _expected("dev", "audit-return-convention")["ground_truth"]
    for key, value in convention["corrected"].items():
        _compare(f"convention/correct/{key}", backtest["metrics_252"][key], value, drift)
    for key, value in convention["as_reported_wrong"].items():
        _compare(f"convention/wrong/{key}", backtest["metrics_12"][key], value, drift)
    _compare("convention/corrected_volatility",
             backtest["metrics_252"]["annual_vol"],
             convention["risk_assessment"]["corrected_volatility"], drift)

    research = recorded["runtime_research"]
    rank_ic = _expected("dev", "factor-rank-ic-evidence")["ground_truth"]
    _compare("rank-ic/status", research["status"], rank_ic["status"], drift)
    _compare("rank-ic/periods", research["periods"], rank_ic["periods"], drift)
    for key in ("mean_rank_ic", "icir", "newey_west_t_stat", "positive_ic_ratio"):
        _compare(f"rank-ic/{key}", research["summary"][key], rank_ic["summary"][key], drift)
    for bound in ("lower", "upper"):
        _compare(f"rank-ic/bootstrap/{bound}",
                 research["summary"]["bootstrap_mean_ic_95"][bound],
                 rank_ic["summary"]["bootstrap_mean_ic_95"][bound], drift)

    short = recorded["clean_research"]
    honesty = _expected("dev", "factor-short-window-honesty")["ground_truth"]
    _compare("short-window/status", short["status"], honesty["status"], drift)
    _compare("short-window/periods", short["periods"], honesty["periods"], drift)
    for key, value in honesty["summary"].items():
        _compare(f"short-window/summary/{key}", short["summary"].get(key), value, drift)

    risk = recorded["market_risk"]
    derived = _expected("dev", "risk-derived-series-claim")["ground_truth"]
    _compare("risk/observations", risk["observations"], derived["observations"], drift)
    _compare("risk/dependencies", risk["dependencies"], derived["dependencies"], drift)
    for key, value in derived["summary"].items():
        _compare(f"risk/summary/{key}", risk["summary"].get(key), value, drift)
    if derived["endpoint_warning"] not in risk["warnings"]:
        drift.append(
            f"risk/warning: recorded {derived['endpoint_warning']!r} absent from {risk['warnings']!r}"
        )

    return drift


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Compare against the committed reference files; exit nonzero on drift.")
    parser.add_argument("--output", type=Path, default=None,
                        help="Write the fresh recording to this path as JSON.")
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
            raise SystemExit("Benchmark reference values drifted:\n  " + "\n  ".join(drift))
        print(json.dumps({"status": "current", "checked": sorted(recorded)}, indent=2))
        return
    print(json.dumps(recorded, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
