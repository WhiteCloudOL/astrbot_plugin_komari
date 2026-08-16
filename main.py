from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.client import KomariClient
from .core.monitor import evaluate_alerts
from .core.renderer import StatusRenderer
from .core.settings import load_settings


class KomariPlugin(Star):
    """Expose Komari status cards and transition notifications."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        settings = load_settings(config)
        self.poll_interval = settings.poll_interval
        self.targets = settings.targets
        self.notify_enabled = settings.notify_enabled
        self.alert_policy = settings.alert_policy
        self.daily_report_enabled = settings.daily_report_enabled
        self.daily_report_time = settings.daily_report_time
        self.plugin_data_dir = Path(StarTools.get_data_dir(self.name))
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.plugin_data_dir / "monitor_state.json"
        self.client = KomariClient(
            base_url=settings.base_url,
            api_token=settings.api_token,
            cache_ttl=settings.cache_ttl,
        )
        self.renderer = StatusRenderer(self.plugin_data_dir)
        self._monitor_task: asyncio.Task[None] | None = None
        self._state = self._load_state()
        self._baseline_ready = bool(self._state.get("baseline_ready", False))

    async def initialize(self) -> None:
        """Start the background monitor after the plugin has loaded."""
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        if self.client.base_url:
            logger.info(
                "Komari monitor initialized: interval=%ss targets=%d",
                self.poll_interval,
                len(self.targets),
            )
        else:
            logger.warning("Komari monitor initialized without a site URL")

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
        logger.debug("Komari status command received")
        try:
            snapshot = await self.client.fetch_snapshot()
            if snapshot is None:
                yield event.plain_result(self._configuration_hint())
                return
            image_path = await asyncio.to_thread(self.renderer.render, snapshot)
        except (OSError, RuntimeError, ValueError):
            logger.exception("Failed to render Komari status command image")
            yield event.plain_result("Komari 状态图生成失败，请稍后重试并检查日志。")
            return
        except Exception:
            logger.exception("Unexpected Komari status command error")
            yield event.plain_result("Komari 状态查询发生异常，请稍后重试并检查日志。")
            return
        logger.debug("Komari status command rendered %d nodes", len(snapshot))
        yield event.image_result(image_path)

    async def _monitor_loop(self) -> None:
        """Poll Komari and send one notification per state transition."""
        while True:
            try:
                logger.debug("Starting Komari monitor poll")
                snapshot = await self.client.fetch_snapshot(force=True)
                if snapshot is not None:
                    await self._check_alerts(snapshot)
                    await self._send_daily_report_if_due(snapshot)
                    logger.debug(
                        "Completed Komari monitor poll: nodes=%d", len(snapshot)
                    )
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
        pending_value = self._state.get("pending_alerts")
        pending: list[dict[str, Any]] = []
        pending_count = len(pending_value) if isinstance(pending_value, list) else 0
        now = time.time()
        if isinstance(pending_value, list):
            for item in pending_value:
                if not isinstance(item, dict):
                    continue
                try:
                    created_at = float(item.get("created_at", 0))
                except (TypeError, ValueError):
                    continue
                if now - created_at < 86400:
                    pending.append(item)
        if len(pending) < pending_count:
            logger.debug(
                "Discarded %d expired or malformed Komari alerts",
                pending_count - len(pending),
            )
        if self.notify_enabled and self.targets:
            for alert in alerts:
                uuid = str(alert.node.get("uuid", "unknown"))
                if any(
                    item.get("uuid") == uuid and item.get("kind") == alert.kind
                    for item in pending
                ):
                    continue
                pending.append(
                    {
                        "id": f"{uuid}:{alert.kind}:{time.time_ns()}",
                        "uuid": uuid,
                        "node": alert.node,
                        "kind": alert.kind,
                        "value": alert.value,
                        "threshold": alert.threshold,
                        "created_at": now,
                        "delivered_targets": [],
                    }
                )
                logger.info(
                    "Komari alert detected: kind=%s node=%s",
                    alert.kind,
                    str(alert.node.get("name", uuid)),
                )
        elif alerts:
            logger.debug(
                "Suppressed %d Komari alerts because delivery is disabled or empty",
                len(alerts),
            )
        self._state.update(
            {
                "baseline_ready": True,
                "nodes": current,
                "resource_alerts": next_resources,
                "pending_alerts": pending,
            }
        )
        self._baseline_ready = True
        self._save_state()
        await self._deliver_pending_alerts()

    async def _deliver_pending_alerts(self) -> None:
        """Retry pending alert images until every current target succeeds."""
        pending = self._state.get("pending_alerts")
        if not (
            self.notify_enabled
            and self.targets
            and isinstance(pending, list)
            and pending
        ):
            return
        for item in list(pending):
            node = item.get("node")
            kind = item.get("kind")
            if not isinstance(node, dict) or not isinstance(kind, str):
                pending.remove(item)
                logger.warning("Discarded malformed pending Komari alert")
                self._save_state()
                continue
            delivered_value = item.get("delivered_targets")
            delivered = (
                {str(target) for target in delivered_value if target}
                if isinstance(delivered_value, list)
                else set()
            )
            pending_targets = [
                target for target in self.targets if target not in delivered
            ]
            if not pending_targets:
                pending.remove(item)
                self._save_state()
                continue
            try:
                image_path = await asyncio.to_thread(
                    self.renderer.render_alert,
                    node,
                    kind,
                    item.get("value"),
                    item.get("threshold"),
                )
            except (OSError, RuntimeError, ValueError):
                logger.exception("Failed to render pending Komari alert image")
                continue
            chain = MessageChain([Comp.Image.fromFileSystem(image_path)])
            for target in pending_targets:
                try:
                    await self.context.send_message(target, chain)
                    delivered.add(target)
                    item["delivered_targets"] = sorted(delivered)
                    self._save_state()
                    logger.info(
                        "Delivered Komari alert: kind=%s target=%s",
                        kind,
                        target,
                    )
                except Exception:
                    logger.exception("Failed to send Komari alert image to target")
            if all(target in delivered for target in self.targets):
                pending.remove(item)
                self._save_state()

    async def _send_daily_report_if_due(self, snapshot: list[dict[str, Any]]) -> None:
        """Send the all-node status card once after the configured local time."""
        if not (self.notify_enabled and self.daily_report_enabled and self.targets):
            return
        now = datetime.now().astimezone()
        today = now.date().isoformat()
        scheduled_time = self.daily_report_time.strftime("%H:%M")
        if now.time().replace(tzinfo=None) < self.daily_report_time:
            return
        delivery_state = self._state.get("daily_report_delivery")
        delivered: set[str] = set()
        if (
            isinstance(delivery_state, dict)
            and delivery_state.get("date") == today
            and delivery_state.get("time") == scheduled_time
            and isinstance(delivery_state.get("targets"), list)
        ):
            delivered = {str(target) for target in delivery_state["targets"] if target}
        pending_targets = [target for target in self.targets if target not in delivered]
        if not pending_targets:
            return
        try:
            image_path = await asyncio.to_thread(self.renderer.render, snapshot)
        except (OSError, RuntimeError, ValueError):
            logger.exception("Failed to render Komari daily report image")
            return
        chain = MessageChain([Comp.Image.fromFileSystem(image_path)])
        for target in pending_targets:
            try:
                await self.context.send_message(target, chain)
                logger.info("Sent Komari daily report to target=%s", target)
                delivered.add(target)
                self._state["daily_report_delivery"] = {
                    "date": today,
                    "time": scheduled_time,
                    "targets": sorted(delivered),
                }
                if all(item in delivered for item in self.targets):
                    self._state["last_daily_report_time"] = f"{today} {scheduled_time}"
                    self._state.pop("last_daily_report_date", None)
                self._save_state()
            except Exception:
                logger.exception("Failed to send Komari daily report to target")

    def _load_state(self) -> dict[str, Any]:
        """Load transition state while tolerating a missing or corrupt file."""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            logger.debug("Komari monitor state does not exist yet")
            return {}
        except json.JSONDecodeError:
            logger.warning("Komari monitor state is corrupt; rebuilding baseline")
            return {}
        except OSError:
            logger.exception("Failed to read Komari monitor state; rebuilding baseline")
            return {}

    def _save_state(self) -> None:
        """Persist transition state in the plugin data directory."""
        try:
            self.state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            logger.exception("Failed to save Komari monitor state")

    @staticmethod
    def _configuration_hint() -> str:
        """Explain the minimum configuration required for a successful query."""
        return "Komari 尚未配置或请求失败。请检查 base_url、api_token 与网络连接。"
