from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd
import yaml


def load_storage_policy(path: str | Path = "config/storage.yaml") -> dict:
    target = Path(path)
    if not target.exists():
        return {}
    return yaml.safe_load(target.read_text(encoding="utf-8")) or {}


def history_storage_health(
    roots: Iterable[str | Path],
    policy: dict | None = None,
) -> dict:
    policy = policy or load_storage_policy()
    archive = policy.get("history", {}).get("archive_policy", {}) or {}
    record_threshold = int(archive.get("recommendation_record_threshold", 750000))
    bytes_threshold = int(archive.get("recommendation_bytes_threshold", 536870912))
    records = 0
    total_bytes = 0
    files = 0
    by_root = []
    for root_value in roots:
        root = Path(root_value)
        root_records = 0
        root_bytes = 0
        root_files = 0
        if root.exists():
            for path in root.rglob("*.ndjson"):
                try:
                    size = path.stat().st_size
                    root_bytes += size
                    root_files += 1
                    with path.open("r", encoding="utf-8") as handle:
                        root_records += sum(1 for line in handle if line.strip())
                except OSError:
                    continue
        records += root_records
        total_bytes += root_bytes
        files += root_files
        by_root.append({"root": str(root), "records": root_records, "bytes": root_bytes, "files": root_files})
    archive_uri = os.environ.get(str(archive.get("object_storage_uri_env") or "AIRFRYER_HISTORY_ARCHIVE_URI"), "")
    recommended = records >= record_threshold or total_bytes >= bytes_threshold
    return {
        "ndjson_records": records,
        "ndjson_bytes": total_bytes,
        "ndjson_files": files,
        "record_threshold": record_threshold,
        "bytes_threshold": bytes_threshold,
        "archive_recommended": recommended,
        "object_storage_configured": bool(archive_uri),
        "automatic_upload_enabled": bool(archive.get("upload_enabled", False)),
        "by_root": by_root,
    }


def write_history_parquet(path: str | Path, observations: list[dict]) -> str | None:
    if not observations:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(observations)
    for column in frame.columns:
        if frame[column].map(lambda value: isinstance(value, (dict, list, tuple, set))).any():
            frame[column] = frame[column].map(
                lambda value: (
                    json.dumps(value, sort_keys=True, default=str)
                    if isinstance(value, (dict, list, tuple, set))
                    else value
                )
            )
    connection = duckdb.connect()
    try:
        connection.register("history_frame", frame)
        escaped = str(target).replace("'", "''")
        connection.execute(f"COPY history_frame TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        connection.unregister("history_frame")
    finally:
        connection.close()
    return str(target)
