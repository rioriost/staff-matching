"""Deterministic, explicitly synthetic inputs and extension-derived evidence."""

import math

from backend.models import PairEvidence

SKILL_DIMENSIONS = ("office", "forklift", "packing", "rare")
FEATURE_VERSION = "synthetic-tokyo-structured-v1"


def skill_vector(skills: dict[str, int] | None) -> str:
    """A four-dimensional structured feature vector, not a language-model embedding."""
    values = [(skills or {}).get(skill, 0) for skill in SKILL_DIMENSIONS]
    return "[" + ",".join(str(value) for value in values) + "]"


def synthetic_point(index: int, *, site: bool = False) -> tuple[float, float]:
    """Fictional locations near Tokyo Station; never actual staff addresses."""
    return (
        139.7671 + (0.004 if site else -0.005) * (index + 1),
        35.6812 + 0.003 * (index + 1),
    )


def enriched_evidence(evidence: PairEvidence, feature: dict) -> PairEvidence:
    distance = feature["vector_distance"]
    # PostgreSQL cosine distance is NaN for a zero skill vector.
    similarity = 0.0 if distance is None or not math.isfinite(distance) else 1.0 - distance
    updates = {
        "skill_fit": round(0.9 * evidence.skill_fit + 0.1 * max(0.0, min(1.0, similarity)), 6),
        "history_fit": feature["history_fit"],
        "rated_assignments": feature["rated_assignments"],
    }
    if evidence.commute_verified and evidence.commute_minutes is not None:
        updates["commute_minutes"] = max(15, math.ceil(feature["spatial_distance_m"] / 350 + 5))
    return evidence.model_copy(update=updates)
