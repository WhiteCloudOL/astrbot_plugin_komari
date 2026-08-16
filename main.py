from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.client import KomariClient
from .core.monitor import AlertPolicy, evaluate_alerts
from .core.renderer import StatusRenderer


class KomariPlugin(Star):
    """Expose Komari status cards and transition notifications."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._migrate_flat_config()
        connection = self._config_group("connection")
        delivery = self._config_group("delivery")
        alerts = self._config_group("alerts")
        daily_report = self._config_group("daily_report")
        advanced = self._config_group("advanced")
        self.poll_interval = max(30, int(advanced.get("poll_interval", 120)))
        self.targets = self._read_targets(delivery.get("notification_targets", []))
        self.notify_enabled = bool(delivery.get("notification_enabled", True))
        cpu_alert_threshold = max(
            1.0, min(100.0, float(alerts.get("cpu_alert_threshold", 90)))
        )
        memory_alert_threshold = max(
            1.0, min(100.0, float(alerts.get("memory_alert_threshold", 90)))
        )
        self.alert_policy = AlertPolicy(
            offline_enabled=bool(alerts.get("offline_alert_enabled", True)),
            cpu_enabled=bool(alerts.get("cpu_alert_enabled", False)),
            cpu_alert=cpu_alert_threshold,
            memory_enabled=bool(alerts.get("memory_alert_enabled", False)),
            memory_alert=memory_alert_threshold,
        )
        self.daily_report_enabled = bool(daily_report.get("enabled", False))
        daily_report_time = str(daily_report.get("time", "09:00")).strip()
        try:
            self.daily_report_time = datetime.strptime(
                daily_report_time, "%H:%M"
            ).time()
        except ValueError:
            logger.warning(
                "Invalid Komari daily report time %r; using 09:00",
                daily_report_time,
            )
            self.daily_report_time = datetime.strptime("09:00", "%H:%M").time()
        self.plugin_data_dir = Path(StarTools.get_data_dir(self.name))
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.plugin_data_dir / "monitor_state.json"
        self.client = KomariClient(
            base_url=str(connection.get("base_url", "")),
            api_token=str(connection.get("api_token", "")),
            cache_ttl=int(advanced.get("cache_ttl", 30)),
        )
        self.renderer = StatusRenderer(self.plugin_data_dir)
        self._monitor_task: asyncio.Task[None] | None = None
        self._state = self._load_state()
        self._baseline_ready = bool(self._state.get("baseline_ready", False))

    async def initialize(self) -> None:
        """Start the background monitor after the plugin has loaded."""
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Komari monitor initialized")

    async def terminate(self) -> None:
        """Stop the background monitor and release its task."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

    @filter.command("komari")
    async def show_status(self, event: AstrMessageEvent):
        """Render and return the current Komari status image."""
        snapshot = await self.client.fetch_snapshot()
        if snapshot is None:
            yield event.plain_result(self._configuration_hint())
            return
        image_path = await asyncio.to_thread(self.renderer.render, snapshot)
        yield event.image_result(image_path)

    async def _monitor_loop(self) -> None:
        """Poll Komari and send one notification per state transition."""
        while True:
            try:
                snapshot = await self.client.fetch_snapshot(force=True)
                if snapshot is not None:
                    await self._check_alerts(snapshot)
                    await self._send_daily_report_if_due(snapshot)
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError) as exc:
                logger.warning("Komari monitor request failed: %s", exc)
            except Exception:
                logger.exception("Unexpected Komari monitor error")
            await asyncio.sleep(self.poll_interval)

    async def _check_alerts(self, snapshot: list[dict[str, Any]]) -> None:
        """Persist state and send one image for every alert edge."""
        previous = self._state.get("nodes", {})
        if not isinstance(previous, dict):
            previous = {}
        previous_resources = self._state.get("resource_alerts")
        resources_ready = isinstance(previous_resources, dict)
        if not resources_ready:
            previous_resources = {}
        current, next_resources, alerts = evaluate_alerts(
            snapshot,
            previous,
            previous_resources,
            self._baseline_ready,
            resources_ready,
            self.alert_policy,
        )
        self._state.update(
            {
                "baseline_ready": True,
                "nodes": current,
                "resource_alerts": next_resources,
            }
        )
        self._baseline_ready = True
        self._save_state()
        if self.notify_enabled and alerts and self.targets:
            for alert in alerts:
                try:
                    image_path = await asyncio.to_thread(
                        self.renderer.render_alert,
                        alert.node,
                        alert.kind,
                        alert.value,
                        alert.threshold,
                    )
                except (OSError, ValueError):
                    logger.exception("Failed to render Komari alert image")
                    continue
                chain = MessageChain([Comp.Image.fromFileSystem(image_path)])
                for target in self.targets:
                    try:
                        await self.context.send_message(target, chain)
                    except Exception:
                        logger.exception("Failed to send Komari alert image to target")

    async def _send_daily_report_if_due(self, snapshot: list[dict[str, Any]]) -> None:
        """Send the all-node status card once after the configured local time."""
        if not (self.notify_enabled and self.daily_report_enabled and self.targets):
            return
        now = datetime.now().astimezone()
        today = now.date().isoformat()
        if (
            now.time().replace(tzinfo=None) < self.daily_report_time
            or self._state.get("last_daily_report_date") == today
        ):
            return
        try:
            image_path = await asyncio.to_thread(self.renderer.render, snapshot)
        except OSError:
            logger.exception("Failed to render Komari daily report image")
            return
        self._state["last_daily_report_date"] = today
        self._save_state()
        chain = MessageChain([Comp.Image.fromFileSystem(image_path)])
        for target in self.targets:
            try:
                await self.context.send_message(target, chain)
            except Exception:
                logger.exception("Failed to send Komari daily report to target")

    def _migrate_flat_config(self) -> None:
        """Move v1.0 flat settings into the grouped v1.1 configuration."""
        mappings = {
            "connection": ("base_url", "api_token"),
            "delivery": ("notification_enabled", "notification_targets"),
            "alerts": (
                "offline_alert_enabled",
                "cpu_alert_enabled",
                "cpu_alert_threshold",
                "memory_alert_enabled",
                "memory_alert_threshold",
            ),
            "daily_report": ("daily_report_enabled", "daily_report_time"),
            "advanced": ("poll_interval", "cache_ttl"),
        }
        changed = False
        for group_name, legacy_keys in mappings.items():
            group = self.config.get(group_name)
            if not isinstance(group, dict):
                group = {}
            group_changed = False
            for key in legacy_keys:
                if key not in self.config:
                    continue
                nested_key = {
                    "daily_report_enabled": "enabled",
                    "daily_report_time": "time",
                }.get(key, key)
                group[nested_key] = self.config.pop(key)
                changed = True
                group_changed = True
            if group_changed:
                self.config[group_name] = group
        if changed:
            self.config.save_config()
            logger.info("Migrated Komari settings to grouped configuration")

    def _config_group(self, name: str) -> dict[str, Any]:
        """Return one configuration group with safe type handling.

        Args:
            name: Top-level configuration group name.

        Returns:
            Group mapping, or an empty mapping for invalid input.
        """
        value = self.config.get(name, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _read_targets(value: Any) -> list[str]:
        """Normalize list or newline-separated target configuration."""
        if isinstance(value, str):
            values = value.replace("，", "\n").splitlines()
        elif isinstance(value, list):
            values = value
        else:
            values = []
        return list(
            dict.fromkeys(str(item).strip() for item in values if str(item).strip())
        )

    def _load_state(self) -> dict[str, Any]:
        """Load transition state while tolerating a missing or corrupt file."""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        """Persist transition state in the plugin data directory."""
        try:
            self.state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("Failed to save Komari monitor state")

    @staticmethod
    def _configuration_hint() -> str:
        """Explain the minimum configuration required for a successful query."""
        return "Komari 尚未配置或请求失败。请检查 base_url、api_token 与网络连接。"
