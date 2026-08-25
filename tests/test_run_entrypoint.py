from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import airfryer_rankings.run as run_module
from airfryer_rankings.models import SourceConfig


def test_main_serializes_the_complete_state_update(monkeypatch, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    events: list[tuple[str, object]] = []

    @contextmanager
    def fake_lock(path: str | Path):
        events.append(("lock", Path(path)))
        yield
        events.append(("unlock", Path(path)))

    def fake_run(args) -> None:
        events.append(("run", args.mode))
        assert events[0] == ("lock", state)

    monkeypatch.setattr(run_module, "exclusive_lock", fake_lock)
    monkeypatch.setattr(run_module, "_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["airfryer-rankings", "--state", str(state), "--mode", "smoke"])

    run_module.main()

    assert events == [("lock", state), ("run", "smoke"), ("unlock", state)]


def _run_contract(monkeypatch, tmp_path: Path, *, mode: str, publishable: bool):
    state = {
        "migration": {},
        "url_catalog": {"https://example.test/recipe": {}},
        "recipes": {},
        "source_history": [],
        "anomaly_history": [],
    }
    writes: list[str] = []
    saved: list[dict] = []
    source = SourceConfig("example.test")
    model_params = SimpleNamespace(to_dict=lambda: {})
    method = {
        "model_version": 5,
        "model_semver": "5.0.0",
        "deduplicated_count": 0,
        "duplicate_groups": [],
        "robustness": {},
    }
    replacements = {
        "now_iso": lambda: "2026-08-24T10:00:00Z",
        "load_previous_serving_snapshot": lambda *_: ({}, []),
        "load_state": lambda *_: state,
        "load_sources": lambda *_: [source],
        "load_model_config": lambda *_: (model_params, {"model_version": 5}),
        "load_storage_policy": lambda *_: {},
        "load_quality_gate_policy": lambda *_: {},
        "discover_source_urls": lambda cfg, *_args, **_kwargs: {"source": cfg.domain, "status": "ok"},
        "select_refresh_targets": lambda *_args, **_kwargs: [],
        "crawl_targets": lambda *_: ([], [{"source": source.domain, "status": "ok"}], []),
        "enrich_ambiguous_perceptual_hashes": lambda *_args, **_kwargs: {},
        "evaluate_evidence_labels": lambda *_args, **_kwargs: ({}, []),
        "apply_evidence_calibration": lambda rows, _calibration: rows,
        "merge_observations": lambda *_: [],
        "validate_records": lambda *_: None,
        "detect_anomalies": lambda *_: [],
        "read_recent_records": lambda *_args, **_kwargs: [],
        "build_empirical_uncertainty": lambda *_: {},
        "build_historical_metrics": lambda *_: {},
        "bayesian_rank": lambda *_args, **_kwargs: ([], method),
        "temporal_anomalies": lambda *_: [],
        "source_reliability": lambda *_: {},
        "source_health_summary": lambda *_: ([], {}),
        "build_pipeline_metrics": lambda *_: ({}, [], []),
        "evaluate_dedupe_benchmark": lambda *_: ({}, []),
        "build_dedupe_label_queue": lambda *_args, **_kwargs: [],
        "evaluate_publish_gate": lambda *_args, **_kwargs: {"publishable": publishable},
        "write_run_records": lambda folder, *_: str(folder),
        "contract_manifest": lambda: {},
        "write_contract_manifest": lambda *_: writes.append("contracts"),
        "write_quality_gate": lambda *_: writes.append("quality_gate"),
        "write_csv_outputs": lambda *_args, **_kwargs: writes.append("csv"),
        "write_workbook": lambda *_args, **_kwargs: writes.append("workbook"),
        "write_dashboard": lambda *_args, **_kwargs: writes.append("dashboard"),
        "write_duckdb_cache": lambda *_args, **_kwargs: "analytics.duckdb",
        "save_state": lambda _path, payload: saved.append(payload),
    }
    for name, replacement in replacements.items():
        monkeypatch.setattr(run_module, name, replacement)
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(
        sources="sources.yaml",
        state="state.json",
        model_config="model.yaml",
        storage_config="storage.yaml",
        slo_config="slo.yaml",
        mode=mode,
        max_urls=None,
        hourly_limit=10,
        stale_days=14,
    )
    return args, writes, saved


def test_smoke_entrypoint_wires_the_complete_artifact_pipeline(monkeypatch, tmp_path: Path) -> None:
    args, writes, saved = _run_contract(monkeypatch, tmp_path, mode="smoke", publishable=True)

    run_module._run(args)

    summary = json.loads((tmp_path / "output/summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "smoke"
    assert summary["configured_sources"] == 1
    assert writes == ["quality_gate", "contracts", "csv", "workbook", "dashboard"]
    assert len(saved) == 1


def test_failed_quality_gate_writes_no_artifacts_or_state(monkeypatch, tmp_path: Path) -> None:
    args, writes, saved = _run_contract(monkeypatch, tmp_path, mode="hourly", publishable=False)
    monkeypatch.setattr(
        run_module,
        "assert_publishable",
        lambda *_: (_ for _ in ()).throw(RuntimeError("quality gate failed")),
    )

    with pytest.raises(RuntimeError, match="quality gate failed"):
        run_module._run(args)

    assert writes == []
    assert saved == []
    assert not (tmp_path / "output/summary.json").exists()
