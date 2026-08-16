from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.client import KomariClient
from .core.renderer import StatusRenderer


class KomariPlugin(Star):
    """Expose Komari status cards and transition notifications."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.poll_interval = max(30, int(config.get("poll_interval", 120)))
        self.targets = self._read_targets(config.get("notification_targets", []))
        self.notify_enabled = bool(config.get("notification_enabled", True))
        self.plugin_data_dir = Path(StarTools.get_data_dir(self.name))
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.plugin_data_dir / "monitor_state.json"
        self.client = KomariClient(
            base_url=str(config.get("base_url", "")),
            api_token=str(config.get("api_token", "")),
            cache_ttl=int(config.get("cache_ttl", 30)),
        )
        self.renderer = StatusRenderer(
            self.plugin_data_dir / "assets" / "komari_status.png"
        )
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
                    await self._check_transitions(snapshot)
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError) as exc:
                logger.warning("Komari monitor request failed: %s", exc)
            except Exception:
                logger.exception("Unexpected Komari monitor error")
            await asyncio.sleep(self.poll_interval)

    async def _check_transitions(self, snapshot: list[dict[str, Any]]) -> None:
        """Persist node states and notify configured sessions on transitions."""
        current = {str(node["uuid"]): bool(node.get("online")) for node in snapshot}
        previous = self._state.get("nodes", {})
        if not isinstance(previous, dict):
            previous = {}
        if not self._baseline_ready:
            self._state = {"baseline_ready": True, "nodes": current}
            self._baseline_ready = True
            self._save_state()
            return
        names = {
            str(node["uuid"]): str(node.get("name", node["uuid"])) for node in snapshot
        }
        messages: list[str] = []
        for uuid, online in current.items():
            if uuid not in previous:
                continue
            was_online = bool(previous[uuid])
            if was_online and not online:
                messages.append(f"节点离线：{names[uuid]}\nUUID：{uuid}")
            elif not was_online and online:
                messages.append(f"节点恢复在线：{names[uuid]}\nUUID：{uuid}")
        self._state = {"baseline_ready": True, "nodes": current}
        self._save_state()
        if self.notify_enabled and messages and self.targets:
            chain = MessageChain().message(
                "Komari 状态提醒\n\n" + "\n\n".join(messages)
            )
            for target in self.targets:
                try:
                    await self.context.send_message(target, chain)
                except Exception:
                    logger.exception("Failed to send Komari notification to target")

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
