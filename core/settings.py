from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from astrbot.api import AstrBotConfig, logger

from .monitor import AlertPolicy


@dataclass(frozen=True, slots=True)
class PluginSettings:
    """Store validated Komari plugin settings."""

    base_url: str
    api_token: str
    cache_ttl: int
    poll_interval: int
    targets: list[str]
    notify_enabled: bool
    alert_policy: AlertPolicy
    daily_report_enabled: bool
    daily_report_time: time


def load_settings(config: AstrBotConfig) -> PluginSettings:
    """Validate and normalize the grouped plugin configuration.

    Args:
        config: AstrBot-managed plugin configuration.

    Returns:
        Validated settings for the plugin runtime.
    """
    connection = _config_group(config, "connection")
    delivery = _config_group(config, "delivery")
    alerts = _config_group(config, "alerts")
    daily_report = _config_group(config, "daily_report")
    advanced = _config_group(config, "advanced")

    cpu_alert_threshold = _number_setting(
        alerts.get("cpu_alert_threshold", 90),
        default=90,
        minimum=1,
        maximum=100,
        name="cpu_alert_threshold",
    )
    memory_alert_threshold = _number_setting(
        alerts.get("memory_alert_threshold", 90),
        default=90,
        minimum=1,
        maximum=100,
        name="memory_alert_threshold",
    )
    daily_time_value = str(daily_report.get("time", "09:00")).strip()
    try:
        parsed_daily_time = datetime.strptime(daily_time_value, "%H:%M").time()
    except ValueError:
        logger.warning(
            "Invalid Komari daily report time %r; using 09:00",
            daily_time_value,
        )
        parsed_daily_time = time(9, 0)

    return PluginSettings(
        base_url=str(connection.get("base_url", "")),
        api_token=str(connection.get("api_token", "")),
        cache_ttl=int(
            _number_setting(
                advanced.get("cache_ttl", 30),
                default=30,
                minimum=0,
                maximum=None,
                name="cache_ttl",
            )
        ),
        poll_interval=int(
            _number_setting(
                advanced.get("poll_interval", 30),
                default=30,
                minimum=30,
                maximum=None,
                name="poll_interval",
            )
        ),
        targets=_read_targets(delivery.get("notification_targets", [])),
        notify_enabled=bool(delivery.get("notification_enabled", True)),
        alert_policy=AlertPolicy(
            offline_enabled=bool(alerts.get("offline_alert_enabled", True)),
            cpu_enabled=bool(alerts.get("cpu_alert_enabled", False)),
            cpu_alert=cpu_alert_threshold,
            memory_enabled=bool(alerts.get("memory_alert_enabled", False)),
            memory_alert=memory_alert_threshold,
        ),
        daily_report_enabled=bool(daily_report.get("enabled", False)),
        daily_report_time=parsed_daily_time,
    )


def _config_group(config: AstrBotConfig, name: str) -> dict[str, Any]:
    """Return one configuration group with safe type handling.

    Args:
        config: AstrBot-managed plugin configuration.
        name: Top-level configuration group name.

    Returns:
        Group mapping, or an empty mapping for invalid input.
    """
    value = config.get(name, {})
    if isinstance(value, dict):
        return value
    logger.warning("Ignoring invalid Komari configuration group: %s", name)
    return {}


def _number_setting(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float | None,
    name: str,
) -> float:
    """Parse and clamp one numeric setting without failing plugin load.

    Args:
        value: Raw configuration value.
        default: Fallback value for invalid input.
        minimum: Smallest accepted value.
        maximum: Largest accepted value, or None when unbounded.
        name: Safe setting name used in logs.

    Returns:
        Parsed value constrained to the configured range.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid Komari setting %s; using %s", name, default)
        parsed = default
    constrained = max(minimum, parsed)
    if maximum is not None:
        constrained = min(maximum, constrained)
    if constrained != parsed:
        logger.warning(
            "Komari setting %s was outside its allowed range; using %s",
            name,
            constrained,
        )
    return constrained


def _read_targets(value: Any) -> list[str]:
    """Normalize list or newline-separated target configuration.

    Args:
        value: Raw target list or newline-separated text.

    Returns:
        Deduplicated non-empty UMO target strings.
    """
    if isinstance(value, str):
        values = value.replace("，", "\n").splitlines()
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return list(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )
