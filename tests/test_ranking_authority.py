from __future__ import annotations

import json
from pathlib import Path

import pytest

import airfryer_rankings.authority as authority_module
from airfryer_rankings.authority import (
    AuthorityError,
    evaluate_authority,
    invalidate_authority,
    publish_authority,
    ranking_is_current,
)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _manifest_payload() -> dict:
    return {
        "generated_at": "2026-08-19T10:10:00+00:00",
        "vertical": {"id": "air_fryer", "name": "Air Fryer", "source_count": 1},
        "catalog_url_count": 1,
        "recipe_count": 1,
        "ranked_recipe_count": 1,
        "pages": [{"index": 1, "path": "recipes/0001.json", "count": 1}],
        "corpus_recipe_count": 1,
        "corpus_pages": [{"index": 1, "path": "corpus/0001.json", "count": 1}],
    }


def _fixture(tmp_path: Path) -> dict[str, Path]:
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources:\n  - domain: trusted.example\n", encoding="utf-8")
    state = tmp_path / "state.json"
    _write(
        state,
        {
            "recipes": {},
            "url_catalog": {
                "https://trusted.example/air-fryer-chicken": {
                    "url": "https://trusted.example/air-fryer-chicken",
                    "source": "trusted.example",
                }
            },
            "effective_source_domains": ["trusted.example"],
            "source_history": [],
            "schema_version": 5,
        },
    )
    registry = tmp_path / "registry.json"
    _write(
        registry,
        {
            "schema_version": 1,
            "source_gate_version": 2,
            "vertical": "air_fryer",
            "candidates": {},
            "manual_overrides": {},
            "audit": [],
        },
    )
    metrics = tmp_path / "source_expansion.json"
    _write(
        metrics,
        {
            "generated_at": "2026-08-19T10:00:00+00:00",
            "catalog_sync_generated_at": "2026-08-19T10:05:00+00:00",
            "source_gate_version": 2,
            "catalog_url_count": 1,
            "effective_source_count": 1,
        },
    )
    summary = tmp_path / "summary.json"
    _write(
        summary,
        {
            "generated_at": "2026-08-19T10:10:00+00:00",
            "mode": "daily",
            "configured_sources": 1,
            "catalog_urls": 1,
            "targets_this_run": 1,
            "ranked_recipes": 1,
            "model_version": 5,
            "model_semver": "5.2.0",
        },
    )
    leaderboard = tmp_path / "leaderboard.csv"
    leaderboard.write_text(
        "rank,recipe_id,title,source,url\n"
        "1,recipe-1,Air Fryer Chicken,trusted.example,https://trusted.example/air-fryer-chicken\n",
        encoding="utf-8",
    )
    authority = tmp_path / "authority.json"
    public_authority = tmp_path / "docs" / "api" / "authority.json"
    manifest = tmp_path / "docs" / "api" / "manifest.json"
    _write(manifest, _manifest_payload())
    dashboard = tmp_path / "docs" / "index.html"
    dashboard.write_text("<html><body>old leaderboard</body></html>\n", encoding="utf-8")
    return {
        "sources": sources,
        "state": state,
        "registry": registry,
        "metrics": metrics,
        "summary": summary,
        "leaderboard": leaderboard,
        "authority": authority,
        "public_authority": public_authority,
        "manifest": manifest,
        "dashboard": dashboard,
    }


def _publish(paths: dict[str, Path]) -> dict:
    return publish_authority(
        vertical="air_fryer",
        sources_path=paths["sources"],
        state_path=paths["state"],
        registry_path=paths["registry"],
        metrics_path=paths["metrics"],
        summary_path=paths["summary"],
        leaderboard_path=paths["leaderboard"],
        authority_path=paths["authority"],
        public_authority_path=paths["public_authority"],
        manifest_path=paths["manifest"],
    )


def test_publish_authority_certifies_matching_generation(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    payload = _publish(paths)

    assert payload["authoritative"] is True
    assert payload["authority_contract_version"] == 2
    assert payload["effective_source_count"] == 1
    assert payload["leaderboard_sources"] == ["trusted.example"]
    assert payload["effective_catalog_url_count"] == 1
    assert payload["ranking_mode"] == "daily"
    assert payload["full_catalog_baseline"] is True
    assert len(payload["generation_fingerprint_sha256"]) == 64
    assert json.loads(paths["summary"].read_text(encoding="utf-8"))["authority"]["authoritative"] is True
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["authority"]["authoritative"] is True
    assert manifest["ranked_serving_available"] is True
    assert manifest["ranked_serving_status"] == "authoritative"
    assert manifest["ranked_recipe_count"] == 1
    assert manifest["pages"]


def test_publish_authority_rejects_ranking_older_than_catalog_sync(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    summary["generated_at"] = "2026-08-19T10:04:00+00:00"
    _write(paths["summary"], summary)

    with pytest.raises(AuthorityError, match="predates"):
        _publish(paths)


def test_publish_authority_rejects_catalog_count_drift(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    state["url_catalog"]["https://trusted.example/air-fryer-potatoes"] = {
        "url": "https://trusted.example/air-fryer-potatoes",
        "source": "trusted.example",
    }
    _write(paths["state"], state)

    with pytest.raises(AuthorityError, match="catalog mismatch"):
        _publish(paths)


def test_publish_authority_rejects_ranking_scope_drift(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    state["effective_source_domains"] = ["retired.example"]
    _write(paths["state"], state)

    with pytest.raises(AuthorityError, match="ranking source scope"):
        _publish(paths)


def test_publish_authority_rejects_non_effective_leaderboard_source(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["leaderboard"].write_text(
        "rank,recipe_id,title,source,url\n"
        "1,old-1,Air Fryer Old,suspended.example,https://suspended.example/air-fryer-old\n",
        encoding="utf-8",
    )

    with pytest.raises(AuthorityError, match="non-effective source"):
        _publish(paths)


def test_publish_authority_rejects_strict_vertical_contamination(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["sources"].write_text(
        "defaults:\n"
        "  include_pattern: '(?:slow[-_ ]?cook(?:er|ing|ed)|crock[-_ ]?pot)'\n"
        "  allow_unmatched_discovery_links: false\n"
        "sources:\n"
        "  - domain: trusted.example\n",
        encoding="utf-8",
    )
    paths["leaderboard"].write_text(
        "rank,recipe_id,title,source,url\n"
        "1,taco-1,Birria Tacos,trusted.example,https://trusted.example/birria-tacos/\n",
        encoding="utf-8",
    )

    with pytest.raises(AuthorityError, match="strict vertical policy"):
        _publish(paths)


def test_publish_authority_rejects_leaderboard_count_mismatch(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["leaderboard"].write_text(
        "rank,recipe_id,title,source,url\n"
        "1,chicken-1,Air Fryer Chicken,trusted.example,https://trusted.example/air-fryer-chicken\n"
        "2,potato-1,Air Fryer Potatoes,trusted.example,https://trusted.example/air-fryer-potatoes\n",
        encoding="utf-8",
    )

    with pytest.raises(AuthorityError, match="row count"):
        _publish(paths)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("generated_at", "2026-08-19T10:09:59+00:00", "manifest generation"),
        ("ranked_recipe_count", 0, "manifest ranked count"),
        ("catalog_url_count", 0, "manifest catalog count"),
    ],
)
def test_publish_authority_rejects_stale_public_manifest(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    paths = _fixture(tmp_path)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest[field] = value
    _write(paths["manifest"], manifest)

    with pytest.raises(AuthorityError, match=message):
        _publish(paths)


def test_publish_authority_rejects_public_manifest_source_drift(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["vertical"]["source_count"] = 2
    _write(paths["manifest"], manifest)

    with pytest.raises(AuthorityError, match="manifest source count"):
        _publish(paths)


@pytest.mark.parametrize("mode", ["hourly", "backfill"])
def test_new_generation_requires_true_full_refresh_before_certification(tmp_path: Path, mode: str) -> None:
    paths = _fixture(tmp_path)
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    summary["mode"] = mode
    _write(paths["summary"], summary)

    with pytest.raises(AuthorityError, match="daily or deep"):
        _publish(paths)


def test_new_generation_rejects_partial_daily_catalog_coverage(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    state["url_catalog"]["https://trusted.example/air-fryer-potatoes"] = {
        "url": "https://trusted.example/air-fryer-potatoes",
        "source": "trusted.example",
    }
    _write(paths["state"], state)
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    metrics["catalog_url_count"] = 2
    _write(paths["metrics"], metrics)
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    summary["catalog_urls"] = 2
    summary["targets_this_run"] = 1
    _write(paths["summary"], summary)
    manifest = _manifest_payload()
    manifest["catalog_url_count"] = 2
    _write(paths["manifest"], manifest)

    with pytest.raises(AuthorityError, match="complete effective catalog"):
        _publish(paths)


def test_non_effective_catalog_rows_do_not_expand_authority_universe(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    state["url_catalog"]["https://suspended.example/air-fryer-old"] = {
        "url": "https://suspended.example/air-fryer-old",
        "source": "suspended.example",
    }
    _write(paths["state"], state)
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    metrics["catalog_url_count"] = 2
    _write(paths["metrics"], metrics)
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    summary["catalog_urls"] = 2
    _write(paths["summary"], summary)
    manifest = _manifest_payload()
    manifest["catalog_url_count"] = 2
    _write(paths["manifest"], manifest)

    payload = _publish(paths)
    assert payload["effective_catalog_url_count"] == 1
    assert payload["raw_catalog_url_count"] == 2


def test_hourly_refresh_inherits_authority_when_inputs_are_unchanged(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    baseline = _publish(paths)
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    summary.pop("authority", None)
    summary["generated_at"] = "2026-08-19T11:10:00+00:00"
    summary["mode"] = "hourly"
    summary["targets_this_run"] = 0
    _write(paths["summary"], summary)
    paths["leaderboard"].write_text(
        "rank,recipe_id,title,source,url\n"
        "1,potato-1,Air Fryer Potatoes,trusted.example,https://trusted.example/air-fryer-potatoes\n",
        encoding="utf-8",
    )
    manifest = _manifest_payload()
    manifest["generated_at"] = "2026-08-19T11:10:00+00:00"
    _write(paths["manifest"], manifest)

    hourly = _publish(paths)
    assert hourly["authoritative"] is True
    assert hourly["full_catalog_baseline"] is False
    assert hourly["input_fingerprint_sha256"] == baseline["input_fingerprint_sha256"]
    assert hourly["leaderboard_fingerprint_sha256"] != baseline["leaderboard_fingerprint_sha256"]


def test_invalidation_removes_public_ranked_serving_pointers(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    payload = invalidate_authority(
        vertical="air_fryer",
        metrics_path=paths["metrics"],
        summary_path=paths["summary"],
        authority_path=paths["authority"],
        public_authority_path=paths["public_authority"],
        manifest_path=paths["manifest"],
        invalidated_at="2026-08-19T10:06:00+00:00",
    )

    assert payload["authoritative"] is False
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["ranked_serving_available"] is False
    assert manifest["ranked_serving_status"] == "refresh_required"
    assert manifest["recipe_count"] == 0
    assert manifest["ranked_recipe_count"] == 0
    assert manifest["pages"] == []
    assert manifest["corpus_recipe_count"] == 1
    assert manifest["corpus_pages"]
    dashboard = paths["dashboard"].read_text(encoding="utf-8")
    assert "Rankings temporarily unavailable" in dashboard
    assert "refresh_required" in dashboard
    assert "old leaderboard" not in dashboard


def test_invalidation_is_fail_closed_but_ignores_late_race(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    first = invalidate_authority(
        vertical="air_fryer",
        metrics_path=paths["metrics"],
        summary_path=paths["summary"],
        authority_path=paths["authority"],
        public_authority_path=paths["public_authority"],
        manifest_path=paths["manifest"],
        invalidated_at="2026-08-19T10:06:00+00:00",
    )
    assert first["authoritative"] is False

    # A real ranking run regenerates the public feed before authority publication.
    _write(paths["manifest"], _manifest_payload())
    _publish(paths)
    late = invalidate_authority(
        vertical="air_fryer",
        metrics_path=paths["metrics"],
        summary_path=paths["summary"],
        authority_path=paths["authority"],
        invalidated_at="2026-08-19T10:11:00+00:00",
    )
    assert late["authoritative"] is True


@pytest.mark.parametrize(
    ("authoritative", "current", "recover", "expected"),
    [
        (True, True, False, "noop"),
        (True, False, False, "invalidate"),
        (False, False, False, "refresh_required"),
        (False, False, True, "recover"),
    ],
)
def test_evaluate_authority_covers_every_lifecycle_outcome(
    authoritative: bool,
    current: bool,
    recover: bool,
    expected: str,
) -> None:
    assert (
        evaluate_authority(
            {"authoritative": authoritative},
            ranking_current=current,
            recovery_requested=recover,
        )
        == expected
    )


def test_ranking_current_requires_matching_catalog_and_sources() -> None:
    summary = {
        "generated_at": "2026-08-19T10:10:00+00:00",
        "catalog_urls": 2,
        "configured_sources": 1,
    }
    metrics = {
        "generated_at": "2026-08-19T10:00:00+00:00",
        "catalog_sync_generated_at": "2026-08-19T10:05:00+00:00",
        "catalog_url_count": 1,
        "effective_source_count": 1,
    }
    assert ranking_is_current(summary, metrics) is False


def test_authority_rejects_corrupt_persisted_inputs(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["metrics"].write_text("{", encoding="utf-8")

    with pytest.raises(AuthorityError, match="invalid authority input"):
        invalidate_authority(
            vertical="air_fryer",
            metrics_path=paths["metrics"],
            summary_path=paths["summary"],
            authority_path=paths["authority"],
        )


def test_authority_rejects_incomplete_leaderboard_identity(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["leaderboard"].write_text(
        "rank,recipe_id,title,source,url\n"
        "1,,Air Fryer Chicken,trusted.example,https://trusted.example/air-fryer-chicken\n",
        encoding="utf-8",
    )

    with pytest.raises(AuthorityError, match="missing identity"):
        _publish(paths)


def test_invalidation_disables_manifest_before_later_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _fixture(tmp_path)
    real_write = authority_module.atomic_write_json

    def fail_public(path: str | Path, payload: object, **kwargs: object) -> None:
        if Path(path) == paths["public_authority"]:
            raise OSError("simulated public authority failure")
        real_write(path, payload, **kwargs)

    monkeypatch.setattr(authority_module, "atomic_write_json", fail_public)
    with pytest.raises(OSError, match="simulated public authority failure"):
        invalidate_authority(
            vertical="air_fryer",
            metrics_path=paths["metrics"],
            summary_path=paths["summary"],
            authority_path=paths["authority"],
            public_authority_path=paths["public_authority"],
            manifest_path=paths["manifest"],
        )

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["ranked_serving_available"] is False
    assert manifest["pages"] == []
