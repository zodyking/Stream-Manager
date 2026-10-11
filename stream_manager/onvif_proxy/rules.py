"""Ordered source rules. No network."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Camera, Source


@dataclass(frozen=True)
class Decision:
    source_id: str | None
    rule_id: str | None
    reason: str


def _numeric(value: str) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def matches(operator: str, expected: str, state: str) -> bool:
    if operator == "eq":
        return state.strip().casefold() == expected.strip().casefold()
    if operator == "ne":
        return state.strip().casefold() != expected.strip().casefold()
    left = _numeric(state)
    right = _numeric(expected)
    if left is None or right is None:
        return False
    if operator == "gt":
        return left > right
    if operator == "lt":
        return left < right
    return False


def decide(camera: Camera, states: dict[str, str]) -> Decision:
    """Pick the first matching rule, otherwise the fallback source."""
    if not camera.enabled:
        return Decision(None, None, "disabled")
    known = camera.source_map()
    if camera.rules_enabled:
        for rule in camera.rules:
            if rule.source_id not in known:
                continue
            if rule.entity_id not in states:
                continue
            if matches(rule.operator, rule.value, states[rule.entity_id]):
                return Decision(rule.source_id, rule.id, "rule")
    if camera.fallback_source_id in known:
        return Decision(camera.fallback_source_id, None, "fallback")
    return Decision(None, None, "no_source")


def cycle_decision(camera: Camera, now: float) -> Decision | None:
    """Walk the sources in order. A matching entity rule is left to the caller."""
    if not camera.cycle_enabled or len(camera.sources) < 2:
        return None
    seconds = max(5, int(camera.cycle_seconds or 30))
    index = int(now // seconds) % len(camera.sources)
    return Decision(camera.sources[index].id, None, "cycle")


def selected_source(camera: Camera, decision: Decision) -> Source | None:
    if not decision.source_id:
        return None
    return camera.source_map().get(decision.source_id)
