from airfryer_rankings.authority import evaluate_authority, ranking_is_current


def test_authoritative_generation_is_idempotent() -> None:
    assert evaluate_authority({"authoritative": True, "status": "authoritative"}) == "noop"


def test_authoritative_generation_is_invalidated_when_current_inputs_advance() -> None:
    authority = {"authoritative": True, "status": "authoritative"}
    assert evaluate_authority(authority, ranking_current=False) == "invalidate"


def test_non_authoritative_generation_requires_refresh() -> None:
    assert evaluate_authority({"authoritative": False, "status": "refresh_required"}) == "refresh_required"


def test_recovery_is_explicit_for_non_authoritative_state() -> None:
    authority = {"authoritative": False, "status": "refresh_required"}
    assert evaluate_authority(authority, recovery_requested=True) == "recover"


def test_ranking_current_requires_matching_catalog_and_ordered_timestamps() -> None:
    state = {"url_catalog": {"a": {}, "b": {}}}
    summary = {"catalog_urls": 2, "generated_at": "2026-01-02T00:00:00+00:00"}
    metrics = {"catalog_sync_generated_at": "2026-01-01T00:00:00+00:00"}
    assert ranking_is_current(state=state, summary=summary, metrics=metrics)


def test_ranking_current_rejects_catalog_regression() -> None:
    state = {"url_catalog": {"a": {}, "b": {}}}
    summary = {"catalog_urls": 1, "generated_at": "2026-01-02T00:00:00+00:00"}
    metrics = {"catalog_sync_generated_at": "2026-01-01T00:00:00+00:00"}
    assert not ranking_is_current(state=state, summary=summary, metrics=metrics)


def test_ranking_current_rejects_missing_timestamps() -> None:
    state = {"url_catalog": {"a": {}}}
    assert not ranking_is_current(state=state, summary={"catalog_urls": 1}, metrics={})


def test_authority_decision_blocks_unknown_state_without_recovery() -> None:
    assert evaluate_authority({}) == "refresh_required"
