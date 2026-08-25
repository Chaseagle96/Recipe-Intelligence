from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = REPO_ROOT / ".github/workflows"

PINNED_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/upload-pages-artifact": "fc324d3547104276b827a68afc52ff2a11cc49c9",
    "actions/deploy-pages": "cd2ce8fcbc39b97be8ca5fce6e763baed58fa128",
    "github/codeql-action/init": "ff2f1c621b7f889edc0d3c761ac2e6a3f8cdb0dd",
    "github/codeql-action/analyze": "ff2f1c621b7f889edc0d3c761ac2e6a3f8cdb0dd",
}


def _load(path: Path) -> dict[str, Any]:
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict)
    return payload


def _uses(payload: Any) -> Iterator[str]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "uses" and isinstance(value, str):
                yield value
            yield from _uses(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _uses(value)


def _step(payload: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    return next(step for step in payload["jobs"][job]["steps"] if step.get("name") == name)


def test_all_workflows_parse_and_external_actions_use_reviewed_shas() -> None:
    paths = sorted(WORKFLOW_ROOT.glob("*.yml"))
    assert len(paths) == 13

    for path in paths:
        payload = _load(path)
        assert payload.get("name"), path
        assert payload.get("jobs"), path
        for reference in _uses(payload):
            if reference.startswith("./"):
                continue
            action, separator, sha = reference.partition("@")
            assert separator and PINNED_ACTIONS.get(action) == sha, (path, reference)


def test_dependabot_groups_actions_that_must_advance_together() -> None:
    payload = _load(REPO_ROOT / ".github/dependabot.yml")
    actions = next(update for update in payload["updates"] if update["package-ecosystem"] == "github-actions")
    assert actions["groups"]["actions"]["patterns"] == ["actions/*"]
    assert actions["groups"]["codeql"]["patterns"] == ["github/codeql-action/*"]


def test_vertical_pull_requests_are_read_only_and_secret_free() -> None:
    for filename in ("hourly.yml", "slow-cooker.yml"):
        payload = _load(WORKFLOW_ROOT / filename)
        assert payload["permissions"] == {"contents": "read"}
        assert payload["jobs"]["validate"]["permissions"] == {"contents": "read"}
        assert payload["jobs"]["validate"]["with"]["production"] == "false"
        assert payload["jobs"]["refresh"]["permissions"] == {
            "contents": "write",
            "pages": "write",
            "id-token": "write",
        }
        assert payload["jobs"]["refresh"]["with"]["production"] == "true"
        assert "secrets" not in payload["jobs"]["validate"]
        assert "secrets" not in payload["jobs"]["refresh"]


def test_source_workflow_permissions_and_secret_scope_are_split() -> None:
    expansion = _load(WORKFLOW_ROOT / "source-expansion.yml")
    assert expansion["permissions"] == {"contents": "read"}
    assert expansion["jobs"]["validate"]["permissions"] == {"contents": "read"}
    assert expansion["jobs"]["expand"]["permissions"] == {"contents": "write"}
    assert "BRAVE_SEARCH_API_KEY" not in expansion.get("env", {})
    discover = _step(expansion, "expand", "Discover and qualify recipe publishers")
    assert {
        "BRAVE_SEARCH_API_KEY",
        "GOOGLE_CSE_API_KEY",
        "GOOGLE_CSE_ID",
    } <= set(discover["env"])
    assert {"EVENT_NAME", "MODE"} <= set(discover["env"])

    catalog = _load(WORKFLOW_ROOT / "source-catalog-sync.yml")
    assert catalog["permissions"] == {"contents": "read"}
    assert catalog["jobs"]["validate"]["permissions"] == {"contents": "read"}
    assert catalog["jobs"]["sync"]["permissions"] == {"contents": "write"}
    assert catalog["jobs"]["dispatch-invalidation"]["permissions"] == {
        "actions": "write",
        "contents": "read",
    }


def test_privileged_workflow_run_guards_reject_prs_and_foreign_heads() -> None:
    for filename in (
        "source-catalog-sync.yml",
        "authority-invalidate.yml",
        "authority-postcheck.yml",
        "authority-self-heal.yml",
    ):
        workflow = (WORKFLOW_ROOT / filename).read_text(encoding="utf-8")
        assert "github.event.workflow_run.event != 'pull_request'" in workflow
        assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow


def test_authority_postcheck_uses_canonical_decision_command() -> None:
    postcheck = _load(WORKFLOW_ROOT / "authority-postcheck.yml")
    freshness = _step(postcheck, "certify", "Check whether current main still matches completed ranking")
    script = freshness["run"]
    assert "airfryer_rankings.ops authority-decision" in script
    assert 'if [ "$decision" = "noop" ]' in script
    assert "from datetime import datetime" not in script
    assert "summary_catalog == current_catalog" not in script


def test_pages_failure_propagates_and_bounded_jobs_have_timeouts() -> None:
    shared = _load(WORKFLOW_ROOT / "_vertical-refresh.yml")
    deploy = shared["jobs"]["deploy-pages"]
    assert "continue-on-error" not in deploy
    assert deploy["timeout-minutes"] == "15"
    assert _load(WORKFLOW_ROOT / "codeql.yml")["jobs"]["analyze"]["timeout-minutes"] == "30"
    assert _load(WORKFLOW_ROOT / "release.yml")["jobs"]["release"]["timeout-minutes"] == "15"


def test_resource_scoped_concurrency_prevents_cross_vertical_cancellation() -> None:
    hourly = _load(WORKFLOW_ROOT / "hourly.yml")
    slow = _load(WORKFLOW_ROOT / "slow-cooker.yml")
    expansion = _load(WORKFLOW_ROOT / "source-expansion.yml")
    catalog = _load(WORKFLOW_ROOT / "source-catalog-sync.yml")
    mobile = _load(WORKFLOW_ROOT / "mobile-corpus-backfill.yml")
    invalidation = _load(WORKFLOW_ROOT / "authority-invalidate.yml")
    postcheck = _load(WORKFLOW_ROOT / "authority-postcheck.yml")

    assert "recipe-intelligence-air-fryer-state" in hourly["concurrency"]["group"]
    assert "recipe-intelligence-slow-cooker-state" in slow["concurrency"]["group"]
    assert expansion["concurrency"]["group"] == catalog["concurrency"]["group"]
    assert "recipe-intelligence-source-network-state" in expansion["concurrency"]["group"]
    assert catalog["jobs"]["sync"]["concurrency"]["group"] == ("recipe-intelligence-${{ matrix.vertical }}-state")
    assert catalog["jobs"]["sync"]["strategy"]["max-parallel"] == "1"
    assert mobile["jobs"]["backfill"]["concurrency"]["group"] == ("recipe-intelligence-${{ matrix.vertical }}-state")
    assert mobile["jobs"]["backfill"]["strategy"]["max-parallel"] == "1"
    assert invalidation["jobs"]["invalidate"]["concurrency"]["group"] == (
        "recipe-intelligence-${{ matrix.vertical }}-state"
    )
    assert invalidation["jobs"]["invalidate"]["strategy"]["max-parallel"] == "1"
    postcheck_group = postcheck["jobs"]["certify"]["concurrency"]["group"]
    assert "'air-fryer'" in postcheck_group
    assert "'slow-cooker'" in postcheck_group


def test_catalog_dispatches_invalidation_only_after_ready_sync() -> None:
    catalog = _load(WORKFLOW_ROOT / "source-catalog-sync.yml")
    dispatch_job = catalog["jobs"]["dispatch-invalidation"]
    dispatch = _step(catalog, "dispatch-invalidation", "Dispatch authority invalidation after ready catalog sync")
    assert "needs.sync.outputs.ready == 'true'" in dispatch_job["if"]
    assert "gh workflow run authority-invalidate.yml" in dispatch["run"]

    invalidation = (WORKFLOW_ROOT / "authority-invalidate.yml").read_text(encoding="utf-8")
    workflow_run_block = invalidation.split("workflow_run:", 1)[1].split("types:", 1)[0]
    assert "Recipe Intelligence Source Catalog Sync" not in workflow_run_block


def test_release_existing_tag_must_target_triggering_commit() -> None:
    release = _load(WORKFLOW_ROOT / "release.yml")
    script = _step(release, "release", "Create GitHub release if absent")["run"]
    assert 'git rev-parse "$TAG^{commit}"' in script
    assert 'if [ "$existing_target" != "$GITHUB_SHA" ]' in script
    assert "expected $GITHUB_SHA" in script


def test_self_heal_ignores_pull_request_smoke_runs() -> None:
    workflow = _load(WORKFLOW_ROOT / "authority-self-heal.yml")
    script = _step(workflow, "heal", "Restore any non-authoritative vertical")["run"]
    assert "--json event,status" in script
    assert '.event != "pull_request"' in script


def test_no_workflow_inherits_all_repository_secrets() -> None:
    for path in WORKFLOW_ROOT.glob("*.yml"):
        assert "secrets: inherit" not in path.read_text(encoding="utf-8"), path


def test_readme_uses_production_equivalent_vertical_entrypoints() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "airfryer_rankings.run" not in readme
    assert "airfryer_rankings.ops run-vertical --vertical air-fryer --mode hourly" in readme
    assert "airfryer_rankings.ops run-vertical --vertical slow-cooker --mode hourly" in readme
