from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import airfryer_rankings.ops as ops
import airfryer_rankings.storage as storage
from airfryer_rankings.ops import authority_decision, run_vertical, validate_mobile_manifest


def _manifest() -> dict:
    return {
        "ranked_recipe_count": 2,
        "corpus_recipe_count": 3,
        "pages": [{"count": 2}],
        "corpus_pages": [{"count": 3}],
        "corpus_status_counts": {"discover": 2},
    }


def test_validate_mobile_manifest_accepts_consistent_counts(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    assert validate_mobile_manifest(path)["corpus_recipe_count"] == 3


def test_validate_mobile_manifest_rejects_inconsistent_counts(tmp_path) -> None:
    payload = _manifest()
    payload["corpus_pages"] = [{"count": 2}]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus page counts"):
        validate_mobile_manifest(path)


def test_validate_mobile_manifest_allows_unranked_corpus(tmp_path) -> None:
    payload = _manifest()
    payload["ranked_recipe_count"] = 0
    payload["pages"] = []
    payload["corpus_status_counts"] = {"discover": 3}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_mobile_manifest(path)["ranked_recipe_count"] == 0


def _vertical(tmp_path, vertical: str):
    root = tmp_path / vertical
    output = root / "output"
    output.mkdir(parents=True)
    return SimpleNamespace(
        id=vertical,
        root_path=root,
        output_root=output,
        source_config_path=tmp_path / "sources.yaml",
        state_path=output / "state.json",
        model_config_path=tmp_path / "model.yaml",
        storage_config_path=tmp_path / "storage.yaml",
        authority_path=output / "authority.json",
        summary_path=output / "summary.json",
    )


def test_run_vertical_resolves_slow_cooker_paths(monkeypatch, tmp_path) -> None:
    calls = []
    definition = _vertical(tmp_path, "slow_cooker")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        (definition.output_root / "air_fryer_rankings.xlsx").write_text("new", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ops, "get_vertical", lambda *_: definition)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_vertical("config/source_discovery.yaml", "slow-cooker", "hourly") == 0

    command, kwargs = calls[0]
    assert "--model-config" in command
    assert command[command.index("--model-config") + 1] == str(definition.model_config_path)
    assert command[-2:] == ["--hourly-limit", "100"]
    assert kwargs["cwd"].name == "slow_cooker"
    assert (definition.output_root / "slow_cooker_rankings.xlsx").read_text(encoding="utf-8") == "new"


def test_run_vertical_smoke_sources_are_private_and_cleaned(monkeypatch, tmp_path) -> None:
    definition = _vertical(tmp_path, "air_fryer")
    source_paths = []

    def fake_run(command, **kwargs):
        source = command[command.index("--sources") + 1]
        source_paths.append(source)
        assert Path(source).is_file()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ops, "get_vertical", lambda *_: definition)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_vertical("config/source_discovery.yaml", "air-fryer", "smoke") == 0
    assert run_vertical("config/source_discovery.yaml", "air-fryer", "smoke") == 0
    assert len(set(source_paths)) == 2
    assert all(not Path(path).exists() for path in source_paths)


def test_failed_slow_cooker_run_preserves_last_good_workbook(monkeypatch, tmp_path) -> None:
    definition = _vertical(tmp_path, "slow_cooker")
    target = definition.output_root / "slow_cooker_rankings.xlsx"
    generated = definition.output_root / "air_fryer_rankings.xlsx"
    target.write_text("last-good", encoding="utf-8")
    generated.write_text("stale", encoding="utf-8")

    monkeypatch.setattr(ops, "get_vertical", lambda *_: definition)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
    )
    assert run_vertical("config/source_discovery.yaml", "slow-cooker", "hourly") == 1
    assert target.read_text(encoding="utf-8") == "last-good"
    assert not generated.exists()


def test_authority_decision_uses_current_state_and_shared_lifecycle(monkeypatch, tmp_path) -> None:
    definition = _vertical(tmp_path, "air_fryer")
    definition.authority_path.write_text('{"authoritative": true}', encoding="utf-8")
    definition.summary_path.write_text(
        '{"generated_at":"2026-08-24T10:10:00Z","catalog_urls":1,"configured_sources":1}',
        encoding="utf-8",
    )
    (definition.output_root / "source_expansion.json").write_text(
        '{"generated_at":"2026-08-24T10:00:00Z",'
        '"catalog_sync_generated_at":"2026-08-24T10:05:00Z",'
        '"catalog_url_count":1,"effective_source_count":1}',
        encoding="utf-8",
    )
    state = {"url_catalog": {"https://example.test/recipe": {}}}
    monkeypatch.setattr(ops, "get_vertical", lambda *_: definition)
    monkeypatch.setattr(storage, "load_state", lambda *_: state)

    assert authority_decision("config/source_discovery.yaml", "air-fryer") == {
        "decision": "noop",
        "ranking_current": True,
    }
    state["url_catalog"]["https://example.test/new"] = {}
    assert authority_decision("config/source_discovery.yaml", "air-fryer")["decision"] == "invalidate"
