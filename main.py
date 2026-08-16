from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools


class KomariPlugin(Star):
    """Query Komari monitoring data, render a status card, and notify transitions."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = str(config.get("base_url", "")).strip().rstrip("/")
        self.api_token = str(config.get("api_token", "")).strip()
        self.poll_interval = max(30, int(config.get("poll_interval", 120)))
        self.cache_ttl = max(0, int(config.get("cache_ttl", 30)))
        self.targets = self._read_targets(config.get("notification_targets", []))
        self.notify_enabled = bool(config.get("notification_enabled", True))
        self.plugin_data_dir = Path(StarTools.get_data_dir(self.name))
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.plugin_data_dir / "komari_status.png"
        self.state_path = self.plugin_data_dir / "monitor_state.json"
        self._font_cache: dict[int, ImageFont.FreeTypeFont] = {}
        self._snapshot_cache: tuple[float, list[dict[str, Any]]] | None = None
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

    @filter.command("komari", alias={"komari状态"})
    async def show_status(self, event: AstrMessageEvent):
        """Render and return the current Komari status image."""
        snapshot = await self._fetch_snapshot()
        if snapshot is None:
            yield event.plain_result(self._configuration_hint())
            return
        image_path = await asyncio.to_thread(self._render_snapshot, snapshot)
        yield event.image_result(image_path)

    @filter.command("komari刷新")
    async def refresh_status(self, event: AstrMessageEvent):
        """Refresh Komari data and return a newly rendered image."""
        snapshot = await self._fetch_snapshot(force=True)
        if snapshot is None:
            yield event.plain_result(
                "Komari 请求失败，请检查站点地址、Token 和网络连接。"
            )
            return
        image_path = await asyncio.to_thread(self._render_snapshot, snapshot)
        yield event.image_result(image_path)

    @filter.command("komari指令", alias={"komari帮助"})
    async def show_help(self, event: AstrMessageEvent):
        """List the available Komari commands and monitor configuration."""
        targets = "、".join(self.targets) if self.targets else "未配置"
        yield event.plain_result(
            "Komari 监控指令\n"
            "• /komari 或 /komari状态：查询节点状态图片\n"
            "• /komari刷新：跳过缓存并刷新状态图片\n"
            "• /komari指令：查看本帮助\n\n"
            f"监控地址：{self.base_url or '未配置'}\n"
            f"提醒会话：{targets}\n"
            f"轮询间隔：{self.poll_interval} 秒"
        )

    async def _monitor_loop(self) -> None:
        """Poll Komari and send one notification per state transition."""
        while True:
            try:
                snapshot = await self._fetch_snapshot(force=True)
                if snapshot is not None:
                    await self._check_transitions(snapshot)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.warning("Komari monitor request failed: %s", exc)
            except Exception:
                logger.exception("Unexpected Komari monitor error")
            await asyncio.sleep(self.poll_interval)

    async def _fetch_snapshot(self, force: bool = False) -> list[dict[str, Any]] | None:
        """Fetch and merge node metadata with the latest status records.

        Args:
            force: Ignore the short-lived in-memory cache when true.

        Returns:
            A list of normalized node records, or None when the request fails.
        """
        if not self.base_url:
            return None
        now = time.monotonic()
        if (
            not force
            and self._snapshot_cache
            and now - self._snapshot_cache[0] < self.cache_ttl
        ):
            return self._snapshot_cache[1]
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, headers=headers
            ) as session:
                nodes, statuses = await asyncio.gather(
                    self._rpc_call(session, "public:getNodesInformation", {}),
                    self._rpc_call(session, "common:getNodesLatestStatus", {}),
                )
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
        ) as exc:
            logger.warning("Komari API unavailable: %s", exc)
            return None
        if not isinstance(nodes, list) or not isinstance(statuses, dict):
            logger.warning("Komari API returned an unexpected response shape")
            return None
        snapshot: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            uuid = str(node.get("uuid", ""))
            if not uuid:
                continue
            status = statuses.get(uuid) or {}
            record = {**node, **status, "uuid": uuid}
            record["online"] = bool(status.get("online", False))
            snapshot.append(record)
        self._snapshot_cache = (now, snapshot)
        return snapshot

    async def _rpc_call(
        self, session: aiohttp.ClientSession, method: str, params: dict[str, Any]
    ) -> Any:
        """Call a Komari JSON-RPC method.

        Args:
            session: Shared HTTP session for the snapshot.
            method: JSON-RPC method name.
            params: JSON-RPC parameters.

        Returns:
            The method result.

        Raises:
            RuntimeError: If Komari returns a JSON-RPC error.
        """
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        async with session.post(f"{self.base_url}/api/rpc2", json=payload) as response:
            response.raise_for_status()
            body = await response.json(content_type=None)
        if body.get("error"):
            raise RuntimeError(str(body["error"]))
        return body.get("result")

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

    def _render_snapshot(self, snapshot: list[dict[str, Any]]) -> str:
        """Render a bounded status card image with Pillow."""
        width = 1400
        margin = 54
        gap = 24
        card_width = (width - margin * 2 - gap) // 2
        card_height = 292
        rows = max(1, math.ceil(len(snapshot) / 2))
        height = 350 + rows * (card_height + gap) + 78
        image = Image.new("RGB", (width, height), "#fff7fb")
        draw = ImageDraw.Draw(image)
        self._draw_background(draw, width, height)
        title_font = self._font(42)
        subtitle_font = self._font(20)
        draw.text((margin, 56), "Komari · 节点巡航", font=title_font, fill="#3d294c")
        online = sum(bool(node.get("online")) for node in snapshot)
        offline = max(0, len(snapshot) - online)
        draw.text(
            (margin, 116),
            f"清蒸云鸭的粉色监控舱  ·  {online} 在线 / {offline} 离线",
            font=subtitle_font,
            fill="#84627f",
        )
        draw.rounded_rectangle(
            (width - margin - 330, 58, width - margin, 174),
            radius=28,
            fill="#ffe2ee",
            outline="#f5b5cb",
            width=2,
        )
        draw.text(
            (width - margin - 300, 79), "巡航状态", font=self._font(18), fill="#9e6684"
        )
        draw.text(
            (width - margin - 300, 106),
            "稳定" if offline == 0 else "需要关注",
            font=self._font(30),
            fill="#e45d87" if offline else "#4caa83",
        )
        draw.text((margin, 206), "NODES", font=self._font(16), fill="#d57e9e")
        for index, node in enumerate(snapshot):
            column = index % 2
            row = index // 2
            x = margin + column * (card_width + gap)
            y = 238 + row * (card_height + gap)
            self._draw_node_card(draw, node, x, y, card_width, card_height)
        draw.text(
            (margin, height - 48),
            "数据来自 Komari · 图片缓存由插件数据目录管理",
            font=self._font(16),
            fill="#a98ca8",
        )
        image.save(self.cache_path, format="PNG", optimize=True)
        return str(self.cache_path)

    def _draw_background(
        self, draw: ImageDraw.ImageDraw, width: int, height: int
    ) -> None:
        """Paint the quiet pink background and restrained sakura accents."""
        draw.ellipse((width - 330, -150, width + 80, 260), fill="#ffe9f2")
        draw.ellipse((-130, height - 300, 230, height + 80), fill="#f1e8ff")
        for x, y, radius in (
            (128, 186, 8),
            (1120, 206, 11),
            (1250, 330, 7),
            (770, 116, 6),
        ):
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius), fill="#f8bed1"
            )

    def _draw_node_card(
        self,
        draw: ImageDraw.ImageDraw,
        node: dict[str, Any],
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        """Draw one node card while truncating all variable-length labels."""
        online = bool(node.get("online"))
        accent = "#4caf83" if online else "#e66787"
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=26,
            fill="#ffffff",
            outline="#f0dce8",
            width=2,
        )
        draw.ellipse((x + 28, y + 32, x + 48, y + 52), fill=accent)
        draw.text(
            (x + 62, y + 25),
            self._fit_text(str(node.get("name", "未命名节点")), width - 100, 24),
            font=self._font(24),
            fill="#432d4f",
        )
        meta = self._fit_text(
            f"{self._region_label(node.get('region'))}  "
            f"{node.get('os', '未知系统')}  {node.get('arch', '')}",
            width - 56,
            16,
        )
        draw.text((x + 28, y + 70), meta, font=self._font(16), fill="#987d9b")
        draw.line((x + 28, y + 104, x + width - 28, y + 104), fill="#f3e8f0", width=2)
        metrics = [
            ("CPU", self._percent(node.get("cpu"))),
            ("RAM", self._ratio_percent(node.get("ram"), node.get("ram_total"))),
            ("DISK", self._ratio_percent(node.get("disk"), node.get("disk_total"))),
            ("SWAP", self._ratio_percent(node.get("swap"), node.get("swap_total"))),
        ]
        metric_width = (width - 84) // 2
        for index, (label, value) in enumerate(metrics):
            column = index % 2
            row = index // 2
            mx = x + 28 + column * (metric_width + 28)
            my = y + 124 + row * 66
            draw.text((mx, my), label, font=self._font(15), fill="#ad8cae")
            draw.rounded_rectangle(
                (mx, my + 28, mx + metric_width, my + 40), radius=6, fill="#f2eaf2"
            )
            draw.rounded_rectangle(
                (mx, my + 28, mx + int(metric_width * value / 100), my + 40),
                radius=6,
                fill=accent if value < 80 else "#f1ae4b",
            )
            draw.text(
                (mx + metric_width - 64, my),
                f"{value:.1f}%",
                font=self._font(15),
                fill=accent,
            )
        uptime = self._format_uptime(node.get("uptime")) if online else "等待节点恢复"
        draw.text(
            (x + 28, y + height - 38),
            self._fit_text(uptime, width - 56, 15),
            font=self._font(15),
            fill="#9b819d",
        )

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        """Load a cross-platform font from a deterministic candidate list."""
        if size in self._font_cache:
            return self._font_cache[size]
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for candidate in candidates:
            try:
                font = ImageFont.truetype(candidate, size)
                self._font_cache[size] = font
                return font
            except OSError:
                continue
        font = ImageFont.load_default()
        self._font_cache[size] = font
        return font

    def _fit_text(self, text: str, max_width: int, size: int) -> str:
        """Truncate text by rendered pixel width, preserving an ellipsis."""
        font = self._font(size)
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        if probe.textlength(text, font=font) <= max_width:
            return text
        while text and probe.textlength(text + "…", font=font) > max_width:
            text = text[:-1]
        return text + "…"

    @staticmethod
    def _percent(value: Any) -> float:
        """Normalize a percentage-like metric into the 0..100 range."""
        try:
            return max(0.0, min(100.0, float(value or 0)))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _ratio_percent(cls, used: Any, total: Any) -> float:
        """Convert byte counters into a percentage with safe zero handling."""
        try:
            total_value = float(total or 0)
            return (
                cls._percent(float(used or 0) / total_value * 100)
                if total_value
                else 0.0
            )
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    @staticmethod
    def _format_uptime(seconds: Any) -> str:
        """Format uptime seconds into a compact Chinese label."""
        try:
            total = max(0, int(seconds or 0))
        except (TypeError, ValueError):
            total = 0
        days, remainder = divmod(total, 86400)
        hours, remainder = divmod(remainder, 3600)
        return f"已运行 {days}天 {hours}小时 {remainder // 60}分钟"

    @staticmethod
    def _region_label(value: Any) -> str:
        """Convert flag emoji to an ASCII country code for font portability."""
        region = str(value or "·").strip()
        if len(region) == 2 and all(0x1F1E6 <= ord(char) <= 0x1F1FF for char in region):
            return "".join(chr(ord(char) - 0x1F1E6 + ord("A")) for char in region)
        return region

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
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.exception("Failed to save Komari monitor state")

    def _configuration_hint(self) -> str:
        """Explain the minimum configuration required for a successful query."""
        return "Komari 尚未配置或请求失败。请在插件配置中填写 base_url（不带末尾斜杠）；api_token 可选。"
