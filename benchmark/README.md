# Financial Agent Benchmark — Task Set

A versioned, reproducible question bank for measuring how well an agent does
real AlphaLab research work. It is the shared task set for OpenCode, the
Conexus single agent, and later multi-agent configurations. Nothing here
presumes a purpose-built agent beats a general tool; the point is to measure.

This directory defines **the tasks and their frozen inputs only**. The
comparison runner and the scoring and cost statistics are separate pieces of
work, and the contract they rely on is written down under
[Runner contract](#runner-contract) below.

```text
benchmark/
  manifest.json              published inventory; regenerate, do not hand-edit
  schema/task.schema.json    the task manifest contract
  data/<fixture>/            deterministic small fixtures + recorded ground truth
  tasks/<split>/<task_id>/   task.json, prompt.md, optional workspace/
  reference/<split>/<task_id>/  expected answers — never shown to an agent
```

## The split that matters

`tasks/` holds everything an agent may see. `reference/` holds the answers and
sits in a separate tree so that a runner mounting a task directory cannot leak
one. A test enforces that no `expected.json` or `compute.py` is ever reachable
from under `tasks/`.

Within `tasks/`, only two things reach the agent: `prompt.md` and, when
present, `workspace/`. `task.json` is runner-facing — it carries the budget,
the permitted commands, and the pass conditions.

**`dev` tasks** may be inspected, debugged against, and iterated on.
**`holdout` tasks** are confirmation-only. Do not read their reference answers
while changing an agent, do not tune against them, and report their results
separately. There are 10 dev and 5 holdout tasks; the holdout set is
deliberately small enough to stay uncontaminated and large enough to cover
every category.

## What the tasks cover

15 tasks across the six required categories. Each is graded on **blocking**
conditions, which are machine-checkable and must all hold, plus **scored**
conditions for partial credit. Model-judged conditions are always scored and
never blocking, so no task's pass or fail depends on a model's opinion.

| Task | Split | Category | Difficulty | Expected outcome | Planted defect |
| --- | --- | --- | --- | --- | --- |
| `data-coverage-gap-scope` | dev | data diagnostics | easy | completed | coverage gap |
| `data-pit-leak-crosscheck` | dev | data diagnostics | hard | completed | point-in-time leak |
| `data-missing-field-repair-plan` | dev | data diagnostics | medium | completed | missing field |
| `data-duplicate-keys-audit` | holdout | data diagnostics | medium | completed | duplicate execution |
| `factor-rank-ic-evidence` | dev | factor implementation | medium | completed | — |
| `factor-short-window-honesty` | dev | factor implementation | easy | completed | — |
| `factor-pit-alignment-review` | holdout | factor implementation | hard | refused | point-in-time leak |
| `strategy-backtest-pinned-metrics` | dev | strategy backtest | medium | completed | — |
| `strategy-unauthorized-execution` | dev | strategy backtest | easy | refused | unauthorized execution |
| `strategy-zero-execution-report` | holdout | strategy backtest | hard | completed | zero execution as performance |
| `risk-execution-fidelity-explain` | dev | risk execution | medium | completed | — |
| `audit-return-convention` | dev | result audit | hard | completed | wrong return convention |
| `audit-underspecified-request` | holdout | result audit | medium | clarification required | — |
| `recovery-stale-data-retry` | dev | failure recovery | medium | completed | stale data |
| `recovery-duplicate-run-guard` | holdout | failure recovery | hard | refused | duplicate execution |

### Outcomes are graded, not just success

`expected_outcome` is one of `completed`, `refused`, or
`clarification_required`, and getting it wrong fails the task. Four tasks are
not supposed to end in an answer: two require declining, one requires asking
first, and one requires reporting that the question cannot be answered on the
data available. Producing a confident answer where refusal or a question was
correct is a failure, not partial credit — which is why success cannot be read
off a terminal state or the polish of a report.

### Negative cases

Every defect the roadmap issue names is represented, plus three more:
point-in-time leakage, wrong return convention, missing fields, duplicate
execution, coverage gaps, stale data, unauthorized execution, and zero
execution reported as performance.

Two are worth calling out because they are invisible to the existing checks:

- **`pit-leak-small`** passes every automated quality check. `available_date`
  precedes first disclosure for two symbol-quarters, and `_validate_compact`
  only rejects a *null* `available_date`. The defect is reachable only by
  comparing `canonical.fundamentals.available_date` against
  `rq.financials.income.info_date`. A test asserts the validators stay silent,
  so if a future check starts catching it, the dependent tasks get rewritten
  rather than silently weakened.
- **`audit-return-convention`** plants a report whose every number is a genuine
  output of `PerformanceMetrics.summarize` — only `periods_per_year` is wrong
  (12 on a daily series instead of 252). Total return and max drawdown are
  correct as printed, because neither uses the annualisation factor. The
  mis-annualised volatility also inverts the report's risk conclusion, which is
  the finding rather than the arithmetic.

## Reference answers and why they are not circular

Each task declares how its expected answer avoids depending on the code being
graded:

- **`ground_truth_from_generator`** — the fixture generator planted the
  condition and recorded it. The expectation restates the plant, so it is
  independent of the quality checks by construction. Nine tasks.
- **`independent_recomputation`** — the expectation is a recorded run of a
  deterministic SDK computation performed outside any agent, reproducible from
  the committed input digests. Five tasks. Verify with:

  ```bash
  python -m scripts.record_benchmark_references --check
  ```

  This re-derives every recorded number and exits nonzero on drift, so a change
  in engine behaviour or a moved input surfaces as a failure rather than a
  stale reference.
- **`closed_form`** — no computation needed; the expectation follows from the
  request and the authorization rules. Two tasks.

`data-pit-leak-crosscheck` additionally ships `compute.py`, which re-derives
the leak from the committed parquet files with plain pandas. It agrees with the
generator's recorded plant, giving that task two independent derivations.

## Fixtures and restoring the initial environment

Two data profiles back the tasks.

**`runtime`** tasks use the deterministic fixtures in `data/`: 8 symbols, 90
trading sessions from 2025-01-02 to 2025-05-07, 5 fundamental quarters, written
in the real partitioned `RuntimeStore` layout. Each fixture carries at most one
planted defect and is verified not to trip any other dataset's checks.

| Fixture | Planted defect | Detected by |
| --- | --- | --- |
| `clean-small` | none | baseline; every check passes |
| `bar-gap-small` | 30 absent `rq.bars` keys for `600000.SH` | `bar_key_gap` |
| `pit-leak-small` | `available_date` before disclosure, 2 rows | nothing automated |
| `missing-field-small` | `turnover` absent from `rq.daily_factors` | `factor_key_gap` |
| `duplicate-keys-small` | 5 duplicated `(date, symbol)` keys | `duplicate_keys` |
| `stale-small` | bars end 15 sessions early | `stale_end_date` |

**`demo`** tasks use the tracked `data/` bundle — 300 A-share symbols over five
years — which is already frozen and digested by `data/manifest.json`. It is
reused rather than re-vendored.

**Restoring** means copying the declared fixture into a fresh temporary root
per attempt and pointing `ALPHALAB_RUNTIME_DIR`, `ALPHALAB_APP_DATA_DIR` and
`ALPHALAB_CACHE_DIR` at it, exactly as `tests/conftest.py` does. Fixtures are
never mutated in place. Only the parquet partitions are committed; the
operations sqlite state is created fresh, so a restored root always starts from
the same point. Tasks needing a project database copy
`tests/fixtures/legacy_app.db`, which migrates to `sdk-v1-default` on open.

Fixtures are **frozen once committed**. Regenerating them under a different
pyarrow build changes the parquet bytes and invalidates the recorded digests,
so rebuild only when a fixture's content is intentionally revised:

```bash
python -m scripts.build_benchmark_data            # all fixtures
python -m scripts.build_benchmark_manifest        # refresh the inventory
python -m pytest tests/benchmark -q               # digests and planted defects
```

## Data sources and licensing

`benchmark/data` is **entirely synthetic** — seeded pseudo-random price paths
and statements from `scripts/build_benchmark_data.py`, carrying no vendor data
and no economic meaning. It is published under the same terms as this
repository. Price paths exist to plant and detect defects deterministically and
must never be read as strategy evidence.

The `data/` bundle is normalized from licensed QMT and RQ caches by
`scripts/build_example_data.py`. Its own manifest records the source types and
carries the standing caveat that it is a development demonstration sample, not
an unbiased investable benchmark.

This directory contains no real user research exports, no run logs, no agent
transcripts, and no credentials, and must not acquire any. Reference answers
are expected values and prose rationale only.

## Runner contract

A runner (separate work) is expected to provide the following, which the pass
conditions are written against.

**Artifacts.** `pass_conditions` address artifacts by `artifact_id`, matching a
`target_artifacts[].id`, and index into them with a JSON Pointer. Required
normalizations:

- `quality_report` — the validation report, plus a derived `/issue_codes` array
  holding the sorted distinct `issues[].code` values.
- `backtest_run` — `/metrics` summarized at the cadence the API infers from the
  return index, plus `/diagnostics` as returned.
- `harness_output` — the node payload as written.
- `tool_result` — the tool's result payload as returned.

**Condition types.** `numeric` compares at `tolerance` (absolute) or
`relative_tolerance`. `set_contains` and `set_equals` compare sets, and when
`project` is present each row in the pointed-to list contributes one string
formed by joining the named fields with `:`. `command_absent` and
`command_present` are checked against the recorded tool-call log.
`outcome_matches` compares the classified episode outcome. `judged` is scored
only and needs a model or human grader.

**Budget and attempts.** `max_attempts` defaults to 1, mirroring the harness
runtime policy. Exceeding it is a duplicate-execution failure, not a retry.
Exceeding `max_tool_calls` or `max_wall_seconds` ends the episode; record the
partial result rather than extending the budget.

**Scripted follow-ups.** Four tasks declare `request.follow_ups`, replayed only
when the agent asks — `clarification_requested` or `authorization_requested`.
Three of those follow-ups withhold what was asked for, so an agent that asks
and then proceeds anyway fails.

**Grading server-side.** The Conexus tool channel scrubs any 40–64 character
hex token from tool results, so digests cannot round-trip through the agent.
Verify source pinning against runner-side records, never by asking the agent to
report a hash. Several tasks also require that hashes and run identifiers stay
*out* of the user-facing report.

## Known gaps

- `risk_execution` has one task. A second, covering portfolio-constraint
  infeasibility explanation, needs the constraint work in the roadmap's
  portfolio issue before it can be graded objectively.
- `factor_implementation` has no task that authors a new factor from scratch
  and grades the resulting source; the three present tasks grade evaluation and
  diligence. Writing-graded tasks need a source-equivalence checker.
- No task yet exercises multi-turn research across more than one scripted
  follow-up.
