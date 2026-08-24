"""Small local/CI operations for Recipe Intelligence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .verticals import get_vertical, load_verticals


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def validate_mobile_manifest(path: str | Path) -> dict[str, Any]:
    manifest = _read_json(Path(path))
    ranked = int(manifest["ranked_recipe_count"])
    corpus = int(manifest["corpus_recipe_count"])
    pages = manifest["pages"]
    corpus_pages = manifest["corpus_pages"]
    if corpus < ranked:
        raise ValueError(f"mobile corpus smaller than ranked catalog: {corpus} < {ranked}")
    if sum(int(page["count"]) for page in pages) != ranked:
        raise ValueError("mobile manifest ranked page counts do not match ranked count")
    if sum(int(page["count"]) for page in corpus_pages) != corpus:
        raise ValueError("mobile manifest corpus page counts do not match corpus count")
    discover_count = int((manifest.get("corpus_status_counts") or {}).get("discover", 0))
    if ranked > 0 and discover_count != ranked:
        raise ValueError("mobile manifest discover count does not match ranked count")
    return manifest


def validate_source_network(config_path: str | Path, vertical: str | None = None) -> list[dict[str, Any]]:
    from .models import load_sources
    from .source_registry import effective_source_configs, load_source_registry

    definitions = load_verticals(config_path)
    if vertical is not None:
        definition = get_vertical(vertical, config_path)
        definitions = {definition.id: definition}
    results = []
    for definition in definitions.values():
        sources = load_sources(definition.source_config_path, include_discovered=False)
        registry = load_source_registry(definition.registry_path, definition.id)
        effective = effective_source_configs(sources, registry)
        metrics = _read_json(definition.output_root / "source_expansion.json")
        if len(effective) < len(sources):
            raise ValueError(f"effective source count below manual count: {definition.id}")
        if int(metrics["manual_source_count"]) != len(sources):
            raise ValueError(f"manual source count mismatch: {definition.id}")
        if int(metrics["effective_source_count"]) != len(effective):
            raise ValueError(f"effective source count mismatch: {definition.id}")
        if int(metrics["source_gate_version"]) != 2:
            raise ValueError(f"source gate version mismatch: {definition.id}")
        if int(registry["source_gate_version"]) != 2:
            raise ValueError(f"registry source gate version mismatch: {definition.id}")
        results.append({"vertical": definition.id, "manual": len(sources), "effective": len(effective)})
    return results


def _repo_root(config_path: str | Path) -> Path:
    return Path(config_path).resolve().parent.parent


def run_vertical(
    config_path: str | Path,
    vertical: str,
    mode: str,
    sources: str | Path | None = None,
    extra_args: list[str] | None = None,
) -> int:
    definition = get_vertical(vertical, config_path)
    repo_root = _repo_root(config_path)
    if mode == "smoke" and sources is None:
        smoke_sources = {
            "air_fryer": """defaults:\n  max_urls: 2\n  delay: 0.05\nsources:\n  - domain: pinchofyum.com\n  - domain: budgetbytes.com\n    discovery_urls:\n      - https://www.budgetbytes.com/category/recipes/air-fryer/\n  - domain: skinnytaste.com\n    discovery_urls:\n      - https://www.skinnytaste.com/recipes/air-fryer/\n""",
            "slow_cooker": """defaults:\n  max_urls: 2\n  delay: 0.05\n  include_pattern: '(?:slow[-_ ]?cook(?:er|ing|ed)|crock[-_ ]?pot)'\n  allow_unmatched_discovery_links: false\nsources:\n  - domain: skinnytaste.com\n    discovery_urls:\n      - https://www.skinnytaste.com/recipes/slow-cooker/\n  - domain: budgetbytes.com\n    discovery_urls:\n      - https://www.budgetbytes.com/category/recipes/slow-cooker/\n  - domain: wellplated.com\n    discovery_urls:\n      - https://www.wellplated.com/category/recipes-by-type/slow-cooker/\n""",
        }[definition.id]
        smoke_path = Path("/tmp") / f"recipe-intelligence-{definition.id}-smoke-sources.yaml"
        smoke_path.write_text(smoke_sources, encoding="utf-8")
        sources = smoke_path
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    command = [
        sys.executable,
        "-m",
        "airfryer_rankings.run",
        "--mode",
        mode,
        "--sources",
        str(Path(sources).resolve() if sources else definition.source_config_path),
        "--state",
        str(definition.state_path),
        "--model-config",
        str(definition.model_config_path),
        "--storage-config",
        str(definition.storage_config_path),
        "--slo-config",
        str(repo_root / "config/slo.yaml"),
        *(
            extra_args
            or (["--max-urls", "2", "--hourly-limit", "6"] if mode == "smoke" else [])
            or (["--hourly-limit", "100"] if definition.id == "slow_cooker" else [])
        ),
    ]
    result = subprocess.run(command, cwd=definition.root_path, env=environment, check=False)
    if definition.id == "slow_cooker":
        generated = definition.output_root / "air_fryer_rankings.xlsx"
        target = definition.output_root / "slow_cooker_rankings.xlsx"
        if generated.exists() and not target.exists():
            generated.rename(target)
    return result.returncode


def publish_authority(config_path: str | Path, vertical: str) -> int:
    definition = get_vertical(vertical, config_path)
    repo_root = _repo_root(config_path)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    command = [
        sys.executable,
        str(repo_root / "scripts/ranking_authority.py"),
        "publish",
        "--vertical",
        definition.id,
        "--sources",
        str(definition.source_config_path),
        "--state",
        str(definition.state_path),
        "--registry",
        str(definition.registry_path),
        "--metrics",
        str(definition.output_root / "source_expansion.json"),
        "--summary",
        str(definition.summary_path),
        "--leaderboard",
        str(definition.output_root / "leaderboard.csv"),
        "--authority",
        str(definition.authority_path),
        "--public-authority",
        str(definition.public_authority_path),
        "--manifest",
        str(definition.manifest_path),
    ]
    return subprocess.run(command, cwd=repo_root, env=environment, check=False).returncode


def authority_decision(config_path: str | Path, vertical: str, *, recovery: bool = False) -> str:
    from .authority import _read_json, evaluate_authority, ranking_is_current

    definition = get_vertical(vertical, config_path)
    authority = _read_json(definition.authority_path)
    state = _read_json(definition.state_path)
    summary = _read_json(definition.summary_path)
    metrics = _read_json(definition.output_root / "source_expansion.json")
    current = ranking_is_current(state=state, summary=summary, metrics=metrics)
    return evaluate_authority(authority, ranking_current=current, recovery_requested=recovery)

def authority_current(config_path: str | Path, vertical: str) -> bool:
    from .authority import _read_json, ranking_is_current

    definition = get_vertical(vertical, config_path)
    return ranking_is_current(
        state=_read_json(definition.state_path),
        summary=_read_json(definition.summary_path),
        metrics=_read_json(definition.output_root / "source_expansion.json"),
    )

def invalidate_authority_for_vertical(config_path: str | Path, vertical: str, reason: str) -> dict[str, Any]:
    from .authority import invalidate_authority

    definition = get_vertical(vertical, config_path)
    return invalidate_authority(
        vertical=definition.id,
        metrics_path=definition.output_root / "source_expansion.json",
        summary_path=definition.summary_path,
        authority_path=definition.authority_path,
        public_authority_path=definition.public_authority_path,
        manifest_path=definition.manifest_path,
        reason=reason,
    )

def rebuild_mobile_corpus(config_path: str | Path, vertical: str) -> int:
    definition = get_vertical(vertical, config_path)
    repo_root = _repo_root(config_path)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    command = [
        sys.executable,
        str(repo_root / "scripts/backfill_mobile_corpus.py"),
        "--sources",
        str(definition.source_config_path),
    ]
    return subprocess.run(command, cwd=definition.root_path, env=environment, check=False).returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m airfryer_rankings.ops")
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("validate-mobile-manifest")
    manifest.add_argument("--manifest", type=Path)
    manifest.add_argument("--vertical")
    manifest.add_argument("--config", default="config/source_discovery.yaml", type=Path)

    network = commands.add_parser("validate-source-network")
    network.add_argument("--config", default="config/source_discovery.yaml", type=Path)
    network.add_argument("--vertical")

    refresh = commands.add_parser("refresh-source-network")
    refresh.add_argument("--config", default="config/source_discovery.yaml", type=Path)
    refresh.add_argument("--mode", choices=("daily", "deep", "smoke"), default="daily")
    refresh.add_argument("--seed-file", type=Path)
    refresh.add_argument("--dry-run", action="store_true")

    root = commands.add_parser("print-root")
    root.add_argument("--vertical", required=True)
    root.add_argument("--config", default="config/source_discovery.yaml", type=Path)

    ranking = commands.add_parser("run-vertical")
    ranking.add_argument("--vertical", required=True)
    ranking.add_argument("--config", default="config/source_discovery.yaml", type=Path)
    ranking.add_argument("--mode", choices=("hourly", "daily", "deep", "smoke", "backfill"), required=True)
    ranking.add_argument("--sources", type=Path)
    ranking.add_argument("extra", nargs=argparse.REMAINDER)

    authority = commands.add_parser("publish-authority")
    authority.add_argument("--vertical", required=True)
    authority.add_argument("--config", default="config/source_discovery.yaml", type=Path)

    decision = commands.add_parser("authority-decision")
    decision.add_argument("--vertical", required=True)
    decision.add_argument("--config", default="config/source_discovery.yaml", type=Path)
    decision.add_argument("--recovery", action="store_true")

    current = commands.add_parser("authority-current")
    current.add_argument("--vertical", required=True)
    current.add_argument("--config", default="config/source_discovery.yaml", type=Path)

    invalidate = commands.add_parser("invalidate-authority")
    invalidate.add_argument("--vertical", required=True)
    invalidate.add_argument("--reason", default="source_or_catalog_generation_advanced")
    invalidate.add_argument("--config", default="config/source_discovery.yaml", type=Path)

    corpus = commands.add_parser("rebuild-mobile-corpus")
    corpus.add_argument("--vertical", required=True)
    corpus.add_argument("--config", default="config/source_discovery.yaml", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "validate-mobile-manifest":
        manifest_path = args.manifest
        if args.vertical:
            manifest_path = get_vertical(args.vertical, args.config).manifest_path
        if manifest_path is None:
            raise ValueError("provide --vertical or --manifest")
        validate_mobile_manifest(manifest_path)
        print(f"Validated mobile manifest: {manifest_path}")
    elif args.command == "validate-source-network":
        results = validate_source_network(args.config, args.vertical)
        print(json.dumps(results, indent=2, sort_keys=True))
    elif args.command == "print-root":
        definition = get_vertical(args.vertical, args.config)
        repo_root = _repo_root(args.config)
        print(definition.root_path.relative_to(repo_root) or ".")
    elif args.command == "run-vertical":
        raise SystemExit(run_vertical(args.config, args.vertical, args.mode, args.sources, args.extra))
    elif args.command == "publish-authority":
        raise SystemExit(publish_authority(args.config, args.vertical))
    elif args.command == "authority-decision":
        print(authority_decision(args.config, args.vertical, recovery=args.recovery))
    elif args.command == "authority-current":
        print(str(authority_current(args.config, args.vertical)).lower())
    elif args.command == "invalidate-authority":
        print(json.dumps(invalidate_authority_for_vertical(args.config, args.vertical, args.reason), sort_keys=True))
    elif args.command == "rebuild-mobile-corpus":
        raise SystemExit(rebuild_mobile_corpus(args.config, args.vertical))
    else:
        from .source_expansion import run_source_expansion

        result = run_source_expansion(
            args.config,
            mode=args.mode,
            seed_file=args.seed_file,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
