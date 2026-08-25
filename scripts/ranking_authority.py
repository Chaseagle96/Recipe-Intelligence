from __future__ import annotations

import argparse
import json

from airfryer_rankings.authority import AuthorityError, invalidate_authority, publish_authority


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish or invalidate Recipe Intelligence serving authority")
    subparsers = parser.add_subparsers(dest="command", required=True)

    publish = subparsers.add_parser("publish", help="Fail closed unless ranking inputs match current production state")
    publish.add_argument("--vertical", required=True)
    publish.add_argument("--sources", required=True)
    publish.add_argument("--state", required=True)
    publish.add_argument("--registry", required=True)
    publish.add_argument("--metrics", required=True)
    publish.add_argument("--summary", required=True)
    publish.add_argument("--leaderboard", required=True)
    publish.add_argument("--authority", required=True)
    publish.add_argument("--public-authority")
    publish.add_argument("--manifest")

    invalidate = subparsers.add_parser("invalidate", help="Mark a serving generation as requiring refresh")
    invalidate.add_argument("--vertical", required=True)
    invalidate.add_argument("--metrics", required=True)
    invalidate.add_argument("--summary", required=True)
    invalidate.add_argument("--authority", required=True)
    invalidate.add_argument("--public-authority")
    invalidate.add_argument("--manifest")
    invalidate.add_argument("--reason", default="source_or_catalog_generation_advanced")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "publish":
            payload = publish_authority(
                vertical=args.vertical,
                sources_path=args.sources,
                state_path=args.state,
                registry_path=args.registry,
                metrics_path=args.metrics,
                summary_path=args.summary,
                leaderboard_path=args.leaderboard,
                authority_path=args.authority,
                public_authority_path=args.public_authority,
                manifest_path=args.manifest,
            )
        else:
            payload = invalidate_authority(
                vertical=args.vertical,
                metrics_path=args.metrics,
                summary_path=args.summary,
                authority_path=args.authority,
                public_authority_path=args.public_authority,
                manifest_path=args.manifest,
                reason=args.reason,
            )
    except AuthorityError as exc:
        raise SystemExit(f"AUTHORITY_CHECK_FAILED: {exc}") from exc
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
