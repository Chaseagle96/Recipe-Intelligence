# Recipe Intelligence

[![Air Fryer](https://github.com/Chaseagle96/Recipe-Intelligence/actions/workflows/hourly.yml/badge.svg)](https://github.com/Chaseagle96/Recipe-Intelligence/actions/workflows/hourly.yml)
[![Slow Cooker](https://github.com/Chaseagle96/Recipe-Intelligence/actions/workflows/slow-cooker.yml/badge.svg)](https://github.com/Chaseagle96/Recipe-Intelligence/actions/workflows/slow-cooker.yml)
[![CodeQL](https://github.com/Chaseagle96/Recipe-Intelligence/actions/workflows/codeql.yml/badge.svg)](https://github.com/Chaseagle96/Recipe-Intelligence/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/Chaseagle96/Recipe-Intelligence)](https://github.com/Chaseagle96/Recipe-Intelligence/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Current release: 5.2.0**  
**Production verticals: Air Fryer · Slow Cooker**

Recipe Intelligence is an auditable, evidence-driven recipe research and ranking platform. It verifies public recipe-rating evidence, normalizes publisher behavior, applies Bayesian ranking with explicit uncertainty, detects duplicate/syndicated recipes, tracks longitudinal changes, and publishes reproducible ranking artifacts.

Air Fryer and Slow Cooker are independent production verticals. They reuse the same extraction, evidence, dedupe, ranking, QA, observability, and reporting implementation, but they do **not** share mutable state, observations, ranking history, priors, serving snapshots, or discovery semantics. See [`VERTICALS.md`](VERTICALS.md) for the isolation contract.

## Platform architecture

Recipe Intelligence treats recipe ranking as a research and production data pipeline with four explicit contracts:

1. **Raw evidence**: immutable vertical-local observations are the longitudinal source of truth.
2. **Clean state**: validated current recipe evidence is stored independently for each vertical.
3. **Model outputs**: immutable ranking snapshots are generated from frozen, versioned model configuration within each vertical population.
4. **Serving outputs**: CSV, Excel, DuckDB, JSON, and dashboard artifacts are generated independently per vertical.

Derived layers can be regenerated. Raw observation history is never rewritten to make it agree with a later model.

### Vertical model

The platform currently has two production populations:

- **Air Fryer** uses the repository-root `data/`, `output/`, and `docs/` trees with `config/sources.yaml`, `config/model.yaml`, and `config/storage.yaml`.
- **Slow Cooker** uses `verticals/slow_cooker/data/`, `verticals/slow_cooker/output/`, and `verticals/slow_cooker/docs/` with configuration under `config/verticals/slow_cooker/`.

Each vertical preserves its own:

- discovery/source configuration and inclusion pattern;
- URL catalog and crawl state;
- recipe population and category/source baselines;
- uncertainty calibration history;
- rank history and predictive backtests;
- evidence-label and dedupe-adjudication ledgers;
- publication snapshot and serving/output namespace.

This prevents Slow Cooker observations from influencing Air Fryer priors or publisher/category expectations while avoiding duplicated ranking infrastructure.

### Ranking model

Production parameters never self-modify. Air Fryer is configured in `config/model.yaml`; Slow Cooker has an independent model configuration in `config/verticals/slow_cooker/model.yaml`.

For each eligible recipe, the current model:

1. normalizes the publisher rating to a five-star scale;
2. estimates a square-root-volume-weighted global prior within the active vertical;
3. estimates partially pooled category baselines;
4. estimates publisher rating-system residuals after category expectations;
5. partially pools and caps publisher adjustment;
6. computes a Bayesian posterior using rating volume;
7. subtracts histogram, empirical-history, or conservative theoretical uncertainty;
8. subtracts an evidence-quality penalty when evidence is below the preferred tier;
9. calculates rank provenance and robustness diagnostics.

Conceptually:

`hierarchical_score = BayesianPosterior(category-aware source-adjusted rating) - uncertainty - evidence penalty`

Popularity growth is descriptive and does not directly boost the primary quality score.

### Ranking robustness

Every leaderboard is stress-tested across 36 nearby parameter configurations. The system reports:

- Top-200 Spearman correlation;
- Top-100 Kendall correlation;
- Top-10 and Top-50 overlap;
- per-recipe rank standard deviation;
- likely rank range;
- Top-10 and Top-50 frequency;
- `rank_confidence` from 0 to 1.

A deterministic golden-ranking fixture ensures scoring changes create a reviewable CI diff rather than silent rank drift.

### Historical predictive backtesting

Daily/deep runs can evaluate frozen candidate configurations over 30-, 60-, and 90-day horizons against later high-volume evidence and report future-quality rank correlation, posterior/final-score error, and future Top-10 overlap.

Backtesting remains disabled independently for a vertical until that vertical has enough longitudinal history. `automatic_parameter_promotion` remains false; recommendations are advisory until changed through a reviewed model-version update.

### Time-aware diagnostics

Observation history supports review growth, rating/review slopes, velocity, acceleration, material page changes, change-point detection, peak rank, time in Top 10/Top 50, and rank volatility. These signals aid interpretation and anomaly detection without turning virality into quality.

## Evidence integrity

Primary extraction uses Schema.org `Recipe` / `AggregateRating` JSON-LD and independently visible/microdata evidence when available.

Evidence states include:

- `verified`
- `schema_only`
- `visible_only`
- `conflict`
- `legacy_unverified`

Conflicted/sub-threshold evidence is quarantined. Legacy evidence is explicitly downgraded and force-refetched instead of inheriting an obsolete favorable default.

### Structural publisher contracts

Every fetched page records its page content hash, structural DOM fingerprint, JSON-LD schema signature, and visible rating-evidence shape. Publisher markup changes therefore generate QA/observability events even when the HTTP request itself succeeds.

### Reviewed real-page fixtures

`tests/fixtures/real_pages/` contains sanitized structural snapshots tied to real publisher pages. Deep runs can capture candidate fixtures into Actions artifacts, but candidates never overwrite checked-in fixtures automatically.

### Evidence-confidence calibration

Air Fryer uses `data/benchmarks/evidence_labels.json`. Slow Cooker maintains its independent review ledger under `verticals/slow_cooker/data/benchmarks/evidence_labels.json`. Empirical evidence confidence activates only after the configured minimum reviewed sample size is reached, so small seed samples cannot masquerade as calibrated probabilities.

## Duplicate detection

Cross-site dedupe is deliberately precision-oriented. Signals include canonical URL, normalized title, ingredient overlap, instruction similarity/SimHash, author agreement, image URL fingerprint, and bounded perceptual image hashing for ambiguous candidates.

Cross-site review counts are **never summed** because syndicated pages may share a review population.

Air Fryer and Slow Cooker keep separate adjudicated dedupe ledgers and candidate queues so similarity performance can be evaluated against each vertical's real corpus rather than assuming one appliance population transfers perfectly to another.

## Discovery semantics

Discovery is configured per source rather than hard-coded globally.

- **Air Fryer** retains the existing Air Fryer-specific inclusion pattern.
- **Slow Cooker** recognizes `slow cooker`, `slow-cooker`, `slow cooked`, `crockpot`, and `crock-pot` URLs/text through `config/verticals/slow_cooker/sources.yaml`.

Slow Cooker has verified category entry points for selected high-value publishers and uses conservative sitemap discovery for the remaining configured publishers. The Slow Cooker URL catalog is stored only inside its own state tree.

## Observability and fail-closed publishing

Pipeline metrics include crawl/extraction success, ranking eligibility, evidence-conflict rate, robots denials, HTTP 403/429 counts, fetch latency, source freshness, structural publisher changes, legacy-evidence backlog, and anomaly volume.

Before a production result is committed, a versioned publication gate checks for catastrophic regressions such as an empty leaderboard, major corpus collapse, inability to produce a Top 50, severe evidence conflicts, unexplained rank collapse, or implausible dedupe expansion.

Warnings expose degraded-but-publishable runs. Failures stop that vertical's production publication and preserve its prior serving state while diagnostic artifacts remain available.

## Historical storage

Each vertical has an independent storage policy and history tree.

### Air Fryer

- authoritative NDJSON: repository-root `data/`
- analytical cache: `output/air_fryer_analytics.duckdb`
- storage policy: `config/storage.yaml`

### Slow Cooker

- authoritative NDJSON: `verticals/slow_cooker/data/`
- analytical cache: `verticals/slow_cooker/output/slow_cooker_analytics.duckdb`
- storage policy: `config/verticals/slow_cooker/storage.yaml`
- optional external archive environment variable: `SLOW_COOKER_HISTORY_ARCHIVE_URI`

Weekly deep runs can generate compressed Parquet history archives. External object-storage upload remains disabled unless explicitly configured.

## Serving outputs

### Air Fryer

Current Air Fryer serving outputs remain at the repository root:

- `output/top50.csv`
- `output/leaderboard.csv`
- `output/air_fryer_rankings.xlsx` as an Actions artifact
- `output/air_fryer_analytics.duckdb` as an Actions artifact
- `output/summary.json`
- `data/state.json`
- immutable `data/observations/`, `data/rankings/`, `data/coverage/`, and `data/anomalies/`
- generated dashboard in `docs/`

### Slow Cooker

Slow Cooker mirrors the serving contract without sharing files:

- `verticals/slow_cooker/output/top50.csv`
- `verticals/slow_cooker/output/leaderboard.csv`
- `verticals/slow_cooker/output/slow_cooker_rankings.xlsx` as an Actions artifact
- `verticals/slow_cooker/output/slow_cooker_analytics.duckdb` as an Actions artifact
- `verticals/slow_cooker/output/summary.json`
- `verticals/slow_cooker/data/state.json`
- immutable `verticals/slow_cooker/data/observations/`, `rankings/`, `coverage/`, and `anomalies/`
- generated dashboard in `verticals/slow_cooker/docs/`

Both workbooks include Top 50, all rankings, rank explainability, source coverage/health/reliability, rating history/trends, uncertainty/evidence calibration, robustness simulations, historical backtests, hyperparameter evaluation, pipeline metrics, publication gate, storage health, data contracts, movers, entrants, QA anomalies, duplicate groups, dedupe benchmark/label queue, methodology, and category leaderboards.

## Continuous integration and supply-chain controls

Recipe Intelligence has independent production workflows for Air Fryer and Slow Cooker. Both run the shared quality toolchain; the Air Fryer invocation audits their shared dependency set once per run:

1. pinned dependency installation;
2. shared-dependency vulnerability audit;
3. Ruff linting;
4. mypy static analysis;
5. pytest with branch coverage gate;
6. bounded live vertical-specific publisher smoke crawl;
7. Excel/DuckDB generation;
8. publication-gate evaluation.

GitHub Actions are pinned to exact commit SHAs. CodeQL runs independently. Dependabot monitors Python dependencies and Actions references.

The test suite combines deterministic unit/regression tests, reviewed real-page fixture tests, Hypothesis property tests, benchmark quality floors, golden model-output tests, and explicit vertical-isolation tests.

## Refresh cadence

### Air Fryer

- `17 * * * *`: hourly incremental refresh
- `43 8 * * *`: daily discovery/full-known-catalog refresh plus backtest evaluation
- `13 9 * * 0`: weekly deep discovery/refresh, storage archive, and candidate fixture capture

### Slow Cooker

- `31 * * * *`: hourly incremental refresh
- `7 9 * * *`: daily discovery/full-known-catalog refresh
- `37 9 * * 0`: weekly deep discovery/full refresh

Both workflows also support manual `hourly`, `daily`, `deep`, or `backfill` execution. Pull requests run bounded live smoke crawls with read-only tokens and without repository secrets or production writes.

### Authority lifecycle

Source expansion marks both serving generations `refresh_required`. A ready Source Catalog Sync then dispatches authority invalidation, and only after both vertical authority files are committed does it dispatch full daily refreshes. Ranking publication uses the canonical `publish-authority` operation and is fail-closed: an hourly run may defer publication until a daily/deep refresh, while unexpected certification failures fail the workflow. Authority Postcheck certifies only a completed production generation that still matches current `main`; Authority Self-Heal dispatches a missing daily recovery without treating pull-request smoke runs as active production work.

## Running locally

The distribution is branded **Recipe Intelligence**. The internal `airfryer_rankings` Python namespace remains for 5.2.x compatibility while the platform transitions from its original single-vertical implementation.

Install and validate the shared engine:

```bash
python -m pip install -r requirements-dev.txt
ruff check src tests
mypy src/airfryer_rankings
PYTHONPATH=src pytest --cov=airfryer_rankings
```

Run Air Fryer from the repository root:

```bash
PYTHONPATH=src python -m airfryer_rankings.ops run-vertical --vertical air-fryer --mode hourly
```

Run Slow Cooker from the repository root:

```bash
PYTHONPATH=src python -m airfryer_rankings.ops run-vertical --vertical slow-cooker --mode hourly
```

The GitHub workflow additionally namespaces the Slow Cooker Excel artifact as `slow_cooker_rankings.xlsx` and validates that all generated state and serving files stay inside the Slow Cooker tree.

## Scope and caveat

No crawler can prove complete coverage of every recipe on the public internet. Publishers can block crawlers, change markup, remove recipes, expose incomplete ratings, or use rating systems with different behavioral biases.

Recipe Intelligence therefore reports coverage, evidence confidence, source health, uncertainty, model robustness, benchmark quality, historical validation, and explicit data-quality gates alongside each production leaderboard. The objective is not to erase uncertainty; it is to make assumptions, evidence, failure modes, and model behavior measurable and reproducible.
