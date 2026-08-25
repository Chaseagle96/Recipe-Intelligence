from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from .runtime import vertical_output_path


def _frame(rows) -> pd.DataFrame:
    if isinstance(rows, dict):
        rows = [rows]
    frame = pd.DataFrame(list(rows or []))
    if frame.empty:
        return frame
    for column in frame.columns:
        if frame[column].map(lambda value: isinstance(value, (dict, list, tuple, set))).any():
            frame[column] = frame[column].map(
                lambda value: (
                    json.dumps(value, sort_keys=True, default=str)
                    if isinstance(value, (dict, list, tuple, set))
                    else value
                )
            )
    return frame


def _replace_table(connection, name: str, rows) -> None:
    frame = _frame(rows)
    connection.execute(f'DROP TABLE IF EXISTS "{name}"')
    if frame.empty:
        connection.execute(f'CREATE TABLE "{name}" (empty INTEGER)')
        return
    relation = f"_{name}_frame"
    connection.register(relation, frame)
    connection.execute(f'CREATE TABLE "{name}" AS SELECT * FROM {relation}')
    connection.unregister(relation)


def write_duckdb_cache(
    path: str | Path,
    *,
    ranked: list[dict],
    observations: list[dict],
    ranking_records: list[dict],
    source_health: list[dict],
    source_reliability: list[dict],
    anomalies: list[dict],
    calibration: dict[str, dict],
    robustness: dict,
    dedupe_summary: dict,
    dedupe_results: list[dict],
    pipeline_metrics: list[dict] | None = None,
    backtest: dict | None = None,
    evidence_calibration: dict[str, dict] | None = None,
    evidence_label_results: list[dict] | None = None,
    quality_gate: dict | None = None,
    storage_health: dict | None = None,
    contracts: dict | None = None,
    dedupe_label_queue: list[dict] | None = None,
) -> str:
    target = vertical_output_path(path, "air_fryer_analytics.duckdb", "analytics.duckdb")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(target))
    try:
        _replace_table(connection, "current_rankings", ranked)
        _replace_table(connection, "observations", observations)
        _replace_table(connection, "ranking_history", ranking_records)
        _replace_table(connection, "source_health", source_health)
        _replace_table(connection, "source_reliability", source_reliability)
        _replace_table(connection, "anomalies", anomalies)
        _replace_table(connection, "uncertainty_calibration", list(calibration.values()))
        _replace_table(connection, "dedupe_benchmark", dedupe_results)
        _replace_table(connection, "dedupe_benchmark_summary", dedupe_summary)
        _replace_table(connection, "dedupe_label_queue", dedupe_label_queue or [])
        robustness_summary = {key: value for key, value in robustness.items() if key != "simulations"}
        _replace_table(connection, "ranking_robustness_summary", robustness_summary)
        _replace_table(connection, "ranking_robustness_simulations", robustness.get("simulations", []))
        _replace_table(connection, "pipeline_metrics", pipeline_metrics or [])
        backtest = backtest or {}
        _replace_table(connection, "historical_backtest_windows", backtest.get("windows", []))
        _replace_table(connection, "hyperparameter_evaluation", backtest.get("configurations", []))
        _replace_table(
            connection,
            "historical_backtest_summary",
            {key: value for key, value in backtest.items() if key not in {"windows", "configurations"}},
        )
        _replace_table(connection, "evidence_calibration", list((evidence_calibration or {}).values()))
        _replace_table(connection, "evidence_label_results", evidence_label_results or [])
        _replace_table(connection, "publication_quality_gate", quality_gate or {})
        _replace_table(connection, "storage_health", storage_health or {})
        _replace_table(connection, "data_contracts", (contracts or {}).get("contracts", []))
        connection.execute(
            "CREATE OR REPLACE VIEW top50 AS SELECT * FROM current_rankings WHERE rank <= 50 ORDER BY rank"
        )
        connection.execute(
            "CREATE OR REPLACE VIEW top10 AS SELECT * FROM current_rankings WHERE rank <= 10 ORDER BY rank"
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    return str(target)
