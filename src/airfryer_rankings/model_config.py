from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MODEL_VERSION = 5
DEFAULT_MODEL_SEMVER = "5.2.0"
DEFAULT_COMPONENT_VERSIONS = {
    "ranking_schema": 5,
    "evidence_model": 2,
    "dedupe_model": 2,
    "uncertainty_calibration": 2,
}


@dataclass(frozen=True)
class ModelParameters:
    max_source_bias: float = 0.15
    evidence_confidence_target: float = 0.80
    evidence_penalty_scale: float = 0.20
    uncertainty_cap: float = 0.25
    source_prior_strength: float = 20.0
    category_prior_strength: float = 20.0
    volume_prior_quantile: float = 0.60
    minimum_volume_prior: float = 50.0
    volume_prior_multiplier: float = 1.0

    def to_dict(self) -> dict[str, float]:
        return {key: float(value) for key, value in asdict(self).items()}

    def with_overrides(self, **overrides: float) -> "ModelParameters":
        known = {key: value for key, value in overrides.items() if hasattr(self, key)}
        return replace(self, **known)


DEFAULT_MODEL_PARAMETERS = ModelParameters()


def load_model_config(path: str | Path = "config/model.yaml") -> tuple[ModelParameters, dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return DEFAULT_MODEL_PARAMETERS, {
            "model_version": DEFAULT_MODEL_VERSION,
            "model_semver": DEFAULT_MODEL_SEMVER,
            "component_versions": dict(DEFAULT_COMPONENT_VERSIONS),
            "active_parameters": DEFAULT_MODEL_PARAMETERS.to_dict(),
        }

    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    payload.setdefault("model_version", DEFAULT_MODEL_VERSION)
    payload.setdefault("model_semver", f"{int(payload['model_version'])}.0.0")
    payload.setdefault("component_versions", dict(DEFAULT_COMPONENT_VERSIONS))
    # V5's canonical key is active_parameters. Accept the short-lived `active`
    # spelling as a backward-compatible alias so experimental configs and older
    # test fixtures remain readable without changing the frozen production config.
    raw = payload.get("active_parameters") or payload.get("active") or {}
    params = ModelParameters(
        max_source_bias=float(raw.get("max_source_bias", DEFAULT_MODEL_PARAMETERS.max_source_bias)),
        evidence_confidence_target=float(
            raw.get("evidence_confidence_target", DEFAULT_MODEL_PARAMETERS.evidence_confidence_target)
        ),
        evidence_penalty_scale=float(
            raw.get("evidence_penalty_scale", DEFAULT_MODEL_PARAMETERS.evidence_penalty_scale)
        ),
        uncertainty_cap=float(raw.get("uncertainty_cap", DEFAULT_MODEL_PARAMETERS.uncertainty_cap)),
        source_prior_strength=float(raw.get("source_prior_strength", DEFAULT_MODEL_PARAMETERS.source_prior_strength)),
        category_prior_strength=float(
            raw.get("category_prior_strength", DEFAULT_MODEL_PARAMETERS.category_prior_strength)
        ),
        volume_prior_quantile=float(raw.get("volume_prior_quantile", DEFAULT_MODEL_PARAMETERS.volume_prior_quantile)),
        minimum_volume_prior=float(raw.get("minimum_volume_prior", DEFAULT_MODEL_PARAMETERS.minimum_volume_prior)),
        volume_prior_multiplier=float(
            raw.get("volume_prior_multiplier", DEFAULT_MODEL_PARAMETERS.volume_prior_multiplier)
        ),
    )
    return params, payload
