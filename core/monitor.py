from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    """Define enabled alert types and their resource thresholds."""

    offline_enabled: bool
    cpu_enabled: bool
    cpu_alert: float
    memory_enabled: bool
    memory_alert: float


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """Describe one alert edge that should produce an image."""

    node: dict[str, Any]
    kind: str
    value: float | None = None
    threshold: float | None = None


def evaluate_alerts(
    snapshot: list[dict[str, Any]],
    previous_nodes: dict[str, Any],
    previous_resources: dict[str, Any],
    baseline_ready: bool,
    resources_ready: bool,
    policy: AlertPolicy,
) -> tuple[dict[str, bool], dict[str, dict[str, bool]], list[AlertEvent]]:
    """Evaluate node and resource state edges without performing IO.

    Args:
        snapshot: Current normalized Komari node records.
        previous_nodes: Persisted online states keyed by node UUID.
        previous_resources: Persisted resource alert states keyed by node UUID.
        baseline_ready: Whether node online states have an established baseline.
        resources_ready: Whether resource alert states have an established baseline.
        policy: Enabled alert types and configured thresholds.

    Returns:
        Current online states, next resource states, and newly triggered events.
    """
    current_nodes = {str(node["uuid"]): bool(node.get("online")) for node in snapshot}
    next_resources: dict[str, dict[str, bool]] = {}
    events: list[AlertEvent] = []
    for node in snapshot:
        uuid = str(node["uuid"])
        online = bool(node.get("online"))
        if baseline_ready and uuid in previous_nodes and policy.offline_enabled:
            was_online = bool(previous_nodes[uuid])
            if was_online and not online:
                events.append(AlertEvent(node=node, kind="offline"))
            elif not was_online and online:
                events.append(AlertEvent(node=node, kind="online"))

        cpu, memory = _resource_usage(node)
        old_resource = previous_resources.get(uuid, {})
        if not isinstance(old_resource, dict):
            old_resource = {}
        cpu_active = bool(old_resource.get("cpu", cpu >= policy.cpu_alert))
        memory_active = bool(old_resource.get("memory", memory >= policy.memory_alert))
        if online and resources_ready:
            if not cpu_active and cpu >= policy.cpu_alert:
                cpu_active = True
                if policy.cpu_enabled:
                    events.append(AlertEvent(node, "cpu_high", cpu, policy.cpu_alert))
            elif cpu_active and cpu < policy.cpu_alert:
                cpu_active = False
            if not memory_active and memory >= policy.memory_alert:
                memory_active = True
                if policy.memory_enabled:
                    events.append(
                        AlertEvent(
                            node,
                            "memory_high",
                            memory,
                            policy.memory_alert,
                        )
                    )
            elif memory_active and memory < policy.memory_alert:
                memory_active = False
        next_resources[uuid] = {
            "cpu": cpu_active,
            "memory": memory_active,
        }
    return current_nodes, next_resources, events


def _resource_usage(node: dict[str, Any]) -> tuple[float, float]:
    """Normalize CPU and memory usage percentages for alert evaluation."""
    try:
        cpu = max(0.0, min(100.0, float(node.get("cpu") or 0)))
        ram_total = float(node.get("ram_total") or 0)
        memory = (
            max(
                0.0,
                min(
                    100.0,
                    float(node.get("ram") or 0) / ram_total * 100,
                ),
            )
            if ram_total
            else 0.0
        )
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0, 0.0
    return cpu, memory
