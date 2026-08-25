# Recipe Intelligence Verticals

Recipe Intelligence uses shared extraction, evidence, dedupe, ranking, QA, observability, and reporting code while keeping each cooking-method population isolated at the data and execution layers.

## Air Fryer

The original production vertical remains rooted at the repository-level `data/`, `output/`, and `docs/` paths and uses `config/sources.yaml`, `config/model.yaml`, and `config/storage.yaml`.

## Slow Cooker

Slow Cooker is an independent production vertical.

### Configuration

- Sources: `config/verticals/slow_cooker/sources.yaml`
- Model: `config/verticals/slow_cooker/model.yaml`
- Storage: `config/verticals/slow_cooker/storage.yaml`
- Shared publication SLO policy: `config/slo.yaml`

### State and evidence

All Slow Cooker mutable/derived data lives below `verticals/slow_cooker/`:

- `verticals/slow_cooker/data/state.json`
- `verticals/slow_cooker/data/observations/`
- `verticals/slow_cooker/data/rankings/`
- `verticals/slow_cooker/data/coverage/`
- `verticals/slow_cooker/data/anomalies/`
- `verticals/slow_cooker/data/model/`
- `verticals/slow_cooker/data/benchmarks/`

No Air Fryer state, observations, priors, rank history, calibration history, or publication snapshots are read as Slow Cooker history.

### Outputs

Slow Cooker serving/research outputs live below `verticals/slow_cooker/output/`, including its own leaderboard, Top 50, QA reports, calibration results, publication gate, summary, Excel workbook, DuckDB analytical cache, and optional Parquet archive. Its generated dashboard lives under `verticals/slow_cooker/docs/`.

### Discovery semantics

The Slow Cooker registry uses a case-insensitive inclusion pattern covering `slow cooker`, `slow-cooker`, `slow cooked`, `crockpot`, and `crock-pot`. Verified vertical landing pages are supplied for publishers where a stable Slow Cooker category page is known; other configured publishers are discovered conservatively through their sitemaps.

Air Fryer retains its original Air Fryer-specific discovery pattern.

### Scheduling

`.github/workflows/slow-cooker.yml` is independent from the Air Fryer workflow and provides:

- hourly incremental refresh at minute 31;
- daily discovery/full-known-catalog refresh at 09:07 UTC;
- weekly deep refresh at 09:37 UTC on Sunday;
- manual hourly/daily/deep/backfill execution;
- bounded live Slow Cooker smoke crawling on pull requests.

The workflows share source code and quality tooling, but they do not share mutable vertical state.

## Adding another vertical

A future vertical should follow the Slow Cooker pattern: add one canonical `VerticalDefinition`, create its source/model/storage configuration, isolated working tree, benchmark ledgers, authority/mobile serving outputs, and a thin caller of `_vertical-refresh.yml`, then provide an explicit discovery pattern. Operators and validators should resolve paths through that definition rather than duplicating filenames in Python or shell code. It should not mix observations or prior distributions with another cooking method merely because the underlying scoring implementation is shared.
