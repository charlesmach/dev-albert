"""Build the deterministic small fixtures for the financial agent benchmark.

Each fixture is a self-contained runtime dataset root with at most one planted
defect. The generator records what it planted, so a grader compares an agent
against the generator's ground truth rather than against the quality checks
that are themselves under test.

Fixtures are committed once and then treated as frozen. Regenerating them with
a different pyarrow build changes the parquet bytes and invalidates the recorded
digests, so rebuild only when a fixture's content is intentionally revised.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from alphalab.dataio.runtime import RuntimeStore

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = REPO_ROOT / "benchmark" / "data"

SEED = 20261021
CALENDAR_START = "2025-01-02"
CALENDAR_PERIODS = 90
SYMBOLS = (
    "000001.SZ",
    "000002.SZ",
    "000063.SZ",
    "600000.SH",
    "600036.SH",
    "600519.SH",
    "601318.SH",
    "300750.SZ",
)
QUARTERS = ("2023q4", "2024q1", "2024q2", "2024q3", "2024q4")
FUNDAMENTAL_FIELDS = (
    "ep",
    "bp",
    "roe",
    "gross_margin",
    "leverage",
    "profit_growth",
    "revenue_growth",
)
DAILY_FACTOR_FIELDS = ("market_cap", "turnover")

# Each fixture is the clean base plus at most one planted defect.
FIXTURES = {
    "clean-small": None,
    "bar-gap-small": "bar_gap",
    "pit-leak-small": "pit_leak",
    "missing-field-small": "missing_field",
    "duplicate-keys-small": "duplicate_keys",
    "stale-small": "stale",
}

# The symbol and window every defect is planted against, so a task prompt can
# describe a scope without naming the defect.
DEFECT_SYMBOL = "600000.SH"
GAP_FIRST_INDEX = 20
GAP_LAST_INDEX = 49
LEAK_QUARTERS = ("2024q2", "2024q3")
STALE_TRAILING_SESSIONS = 15


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quarter_end(quarter: str) -> pd.Timestamp:
    year, index = quarter.split("q")
    return pd.Period(f"{year}Q{index}", freq="Q").end_time.normalize()


def _calendar() -> pd.DatetimeIndex:
    return pd.bdate_range(CALENDAR_START, periods=CALENDAR_PERIODS)


def _bars(dates: pd.DatetimeIndex) -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    frames = []
    for offset, symbol in enumerate(SYMBOLS):
        steps = rng.normal(loc=0.0004, scale=0.018, size=len(dates))
        close = 10.0 * (1.0 + offset * 0.5) * np.exp(np.cumsum(steps))
        spread = np.abs(rng.normal(loc=0.006, scale=0.002, size=len(dates))) * close
        open_ = close - rng.normal(scale=0.004, size=len(dates)) * close
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        volume = rng.integers(200_000, 4_000_000, size=len(dates)).astype(float)
        frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "raw_open": open_,
                    "raw_high": high,
                    "raw_low": low,
                    "raw_close": close,
                    "volume": volume,
                    "amount": volume * close,
                }
            )
        )
    return pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"], ignore_index=True)


def _market_state(dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    keys = pd.DataFrame(
        [(date, symbol) for date in dates for symbol in SYMBOLS], columns=["date", "symbol"]
    )
    return keys.assign(paused=False), keys.assign(is_st=False)


def _instruments(dates: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "snapshot_date": dates[-1],
            "symbol": list(SYMBOLS),
            "listed_date": pd.Timestamp("2015-01-05"),
            "de_listed_date": pd.NaT,
            "asset_type": "CS",
        }
    )


def _daily_factors(dates: pd.DatetimeIndex, bars: pd.DataFrame) -> pd.DataFrame:
    close = bars.pivot(index="date", columns="symbol", values="close")
    rows = []
    for field in DAILY_FACTOR_FIELDS:
        scale = 1.8e9 if field == "market_cap" else 0.02
        long = (close * scale).stack().rename("value").reset_index()
        rows.append(long.assign(field=field))
    frame = pd.concat(rows, ignore_index=True)
    return frame.loc[frame["date"].isin(dates), ["date", "symbol", "field", "value"]].sort_values(
        ["date", "symbol", "field"], ignore_index=True
    )


def _disclosures() -> pd.DataFrame:
    """Statement-level first disclosures: info_date is the real release date."""

    rng = np.random.default_rng(SEED + 1)
    rows = []
    for quarter in QUARTERS:
        end = _quarter_end(quarter)
        for symbol in SYMBOLS:
            lag = 45 if quarter.endswith("q4") else 30
            info_date = end + pd.Timedelta(f"{int(lag + rng.integers(0, 12))}D")
            rows.append(
                {
                    "symbol": symbol,
                    "quarter": quarter,
                    "info_date": info_date,
                    "if_adjusted": 0,
                    "revenue": float(rng.uniform(2e9, 4e10)),
                    "net_profit": float(rng.uniform(1e8, 6e9)),
                }
            )
    return pd.DataFrame(rows).sort_values(["symbol", "quarter"], ignore_index=True)


def _fundamentals(disclosures: pd.DataFrame) -> pd.DataFrame:
    """Canonical fundamentals. available_date mirrors the first disclosure."""

    rng = np.random.default_rng(SEED + 2)
    frame = disclosures[["symbol", "quarter", "info_date"]].copy()
    frame = frame.rename(columns={"info_date": "available_date"})
    frame["shares"] = rng.uniform(5e8, 9e9, size=len(frame))
    frame["market_cap"] = frame["shares"] * rng.uniform(6.0, 40.0, size=len(frame))
    for field in FUNDAMENTAL_FIELDS:
        frame[field] = rng.normal(loc=0.12, scale=0.06, size=len(frame))
    return frame.sort_values(["symbol", "quarter"], ignore_index=True)


def _write_runtime(root: Path, datasets: dict[str, pd.DataFrame]) -> None:
    store = RuntimeStore(root)
    for dataset, frame in datasets.items():
        store.write(dataset, frame)


def _write_direct(root: Path, dataset: str, frame: pd.DataFrame) -> None:
    """Write a dataset bypassing RuntimeStore key de-duplication.

    RuntimeStore.write drops duplicate primary keys, so a duplicate-key defect
    can only be planted by writing the partition file directly.
    """

    store = RuntimeStore(root)
    target = store.catalog.path(dataset)
    target.mkdir(parents=True, exist_ok=True)
    frame.reset_index(drop=True).to_parquet(target / "part-0000.parquet", index=False)


def build_fixture(root: Path, defect: str | None) -> dict:
    """Materialize one fixture under root and return its planted ground truth."""

    dates = _calendar()
    bars = _bars(dates)
    paused, is_st = _market_state(dates)
    instruments = _instruments(dates)
    disclosures = _disclosures()
    fundamentals = _fundamentals(disclosures)
    daily_factors = _daily_factors(dates, bars)
    planted: list[dict] = []
    duplicated_bars: pd.DataFrame | None = None

    if defect == "bar_gap":
        removed = dates[GAP_FIRST_INDEX : GAP_LAST_INDEX + 1]
        mask = bars["symbol"].eq(DEFECT_SYMBOL) & bars["date"].isin(removed)
        planted.append(
            {
                "kind": "coverage_gap",
                "dataset": "rq.bars",
                "symbol": DEFECT_SYMBOL,
                "first_missing_date": removed[0].strftime("%Y-%m-%d"),
                "last_missing_date": removed[-1].strftime("%Y-%m-%d"),
                "missing_keys": int(mask.sum()),
                "explained_by_suspension": False,
                "expected_issue_codes": ["bar_key_gap"],
                "detected_by": "validate_dataset('rq.bars', fail_on_gap=True)",
                "notes": (
                    "rq.daily_factors deliberately retains its rows for the missing sessions, "
                    "so the gap is attributable to one dataset only."
                ),
            }
        )
        bars = bars.loc[~mask].reset_index(drop=True)

    elif defect == "pit_leak":
        mask = fundamentals["symbol"].eq(DEFECT_SYMBOL) & fundamentals["quarter"].isin(
            LEAK_QUARTERS
        )
        leaked = []
        for row in fundamentals.loc[mask].itertuples(index=False):
            disclosed = disclosures.loc[
                disclosures["symbol"].eq(row.symbol) & disclosures["quarter"].eq(row.quarter),
                "info_date",
            ].iloc[0]
            early = _quarter_end(row.quarter) + pd.Timedelta("5D")
            leaked.append(
                {
                    "symbol": row.symbol,
                    "quarter": row.quarter,
                    "disclosed_on": disclosed.strftime("%Y-%m-%d"),
                    "available_date": early.strftime("%Y-%m-%d"),
                    "days_early": int((disclosed - early).days),
                }
            )
            fundamentals.loc[
                fundamentals["symbol"].eq(row.symbol) & fundamentals["quarter"].eq(row.quarter),
                "available_date",
            ] = early
        planted.append(
            {
                "kind": "point_in_time_leak",
                "dataset": "canonical.fundamentals",
                "rows": leaked,
                "expected_issue_codes": [],
                "detected_by": (
                    "cross-check canonical.fundamentals.available_date against "
                    "rq.financials.income.info_date"
                ),
                "notes": (
                    "validate_dataset reports passed: it only rejects a null available_date, "
                    "not one that precedes the first disclosure. The leak is visible only by "
                    "comparing the two datasets."
                ),
            }
        )

    elif defect == "missing_field":
        mask = daily_factors["field"].eq("turnover")
        planted.append(
            {
                "kind": "missing_field",
                "dataset": "rq.daily_factors",
                "absent_field": "turnover",
                "present_fields": ["market_cap"],
                "dropped_rows": int(mask.sum()),
                "expected_issue_codes": ["factor_key_gap"],
                "expected_field_key_coverage": {"market_cap": 1.0, "turnover": 0.0},
                "detected_by": (
                    "validate_dataset('rq.daily_factors', "
                    "required_fields=['market_cap', 'turnover'], fail_on_gap=True)"
                ),
            }
        )
        daily_factors = daily_factors.loc[~mask].reset_index(drop=True)

    elif defect == "duplicate_keys":
        source = bars.loc[
            bars["symbol"].eq(DEFECT_SYMBOL)
            & bars["date"].isin(dates[GAP_FIRST_INDEX : GAP_FIRST_INDEX + 5])
        ]
        duplicated_bars = pd.concat([bars, source], ignore_index=True).sort_values(
            ["date", "symbol"], ignore_index=True
        )
        planted.append(
            {
                "kind": "duplicate_execution",
                "dataset": "rq.bars",
                "symbol": DEFECT_SYMBOL,
                "duplicated_dates": [
                    date.strftime("%Y-%m-%d")
                    for date in dates[GAP_FIRST_INDEX : GAP_FIRST_INDEX + 5]
                ],
                "duplicate_rows": int(len(source)),
                "expected_issue_codes": ["duplicate_keys"],
                "detected_by": "validate_dataset('rq.bars')",
                "notes": (
                    "Written directly to the partition file: RuntimeStore.write would have "
                    "de-duplicated these keys."
                ),
            }
        )

    elif defect == "stale":
        kept = dates[:-STALE_TRAILING_SESSIONS]
        planted.append(
            {
                "kind": "stale_data",
                "dataset": "rq.bars",
                "last_session": kept[-1].strftime("%Y-%m-%d"),
                "expected_last_session": dates[-1].strftime("%Y-%m-%d"),
                "missing_sessions": STALE_TRAILING_SESSIONS,
                "expected_issue_codes": ["stale_end_date"],
                "detected_by": (
                    "validate_dataset('rq.bars', as_of_date=<calendar end>, fail_on_gap=True)"
                ),
            }
        )
        bars = bars.loc[bars["date"].isin(kept)].reset_index(drop=True)
        paused = paused.loc[paused["date"].isin(kept)].reset_index(drop=True)
        is_st = is_st.loc[is_st["date"].isin(kept)].reset_index(drop=True)
        daily_factors = daily_factors.loc[
            daily_factors["date"].isin(kept)
        ].reset_index(drop=True)

    datasets = {
        "rq.instruments": instruments,
        "rq.paused": paused,
        "rq.is_st": is_st,
        "rq.daily_factors": daily_factors,
        "rq.financials.income": disclosures,
        "canonical.fundamentals": fundamentals,
    }
    if duplicated_bars is None:
        datasets["rq.bars"] = bars
    _write_runtime(root, datasets)
    if duplicated_bars is not None:
        _write_direct(root, "rq.bars", duplicated_bars)

    return {
        "planted_defects": planted,
        "rows": {
            "rq.bars": int(len(duplicated_bars if duplicated_bars is not None else bars)),
            "rq.daily_factors": int(len(daily_factors)),
            "canonical.fundamentals": int(len(fundamentals)),
            "rq.financials.income": int(len(disclosures)),
        },
    }


def _collect(staging: Path, target: Path) -> dict:
    """Copy only the parquet partitions, so no sqlite state or lock is shipped."""

    if target.exists():
        shutil.rmtree(target)
    files = {}
    for source in sorted(staging.rglob("*.parquet")):
        relative = source.relative_to(staging)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files[relative.as_posix()] = {
            "bytes": destination.stat().st_size,
            "sha256": _sha256(destination),
        }
    return files


def build(target_root: Path, selected: tuple[str, ...]) -> dict:
    dates = _calendar()
    index = {}
    for fixture in selected:
        defect = FIXTURES[fixture]
        with TemporaryDirectory() as staging:
            ground_truth = build_fixture(Path(staging), defect)
            files = _collect(Path(staging), target_root / fixture)
        manifest = {
            "fixture": fixture,
            "profile": "runtime",
            "seed": SEED,
            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "generator": "scripts/build_benchmark_data.py",
            "source": "synthetic; seeded pseudo-random walk, no vendor data",
            "license": "same terms as this repository; contains no licensed market data",
            "calendar": {
                "start": dates[0].strftime("%Y-%m-%d"),
                "end": dates[-1].strftime("%Y-%m-%d"),
                "sessions": len(dates),
            },
            "universe": list(SYMBOLS),
            "quarters": list(QUARTERS),
            "caveat": (
                "Synthetic fixture for agent evaluation only. Price paths carry no economic "
                "meaning and must not be read as strategy evidence."
            ),
            **ground_truth,
            "files": files,
        }
        path = target_root / fixture / "fixture.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        index[fixture] = {
            "defect": defect,
            "files": len(files),
            "planted_defects": len(manifest["planted_defects"]),
        }
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-root", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--fixture",
        action="append",
        choices=sorted(FIXTURES),
        help="Build only the named fixture. Repeatable. Defaults to all fixtures.",
    )
    args = parser.parse_args()
    target_root = args.target_root.resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    selected = tuple(args.fixture) if args.fixture else tuple(FIXTURES)
    index = build(target_root, selected)
    print(json.dumps(index, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
