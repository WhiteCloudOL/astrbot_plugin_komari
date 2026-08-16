from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


class StatusRenderer:
    """Render Komari snapshots as compact, readable status cards."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._font_cache: dict[int, ImageFont.FreeTypeFont] = {}

    def render(self, snapshot: list[dict[str, Any]]) -> str:
        """Render a bounded status card image with Pillow.

        Args:
            snapshot: Normalized Komari node records.

        Returns:
            Absolute path to the generated PNG image.
        """
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
        draw.text(
            (margin, 56),
            "Komari · 节点巡航",
            font=self._font(42),
            fill="#3d294c",
        )
        online = sum(bool(node.get("online")) for node in snapshot)
        offline = max(0, len(snapshot) - online)
        draw.text(
            (margin, 116),
            f"清蒸云鸭的粉色监控舱  ·  {online} 在线 / {offline} 离线",
            font=self._font(20),
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
            (width - margin - 300, 79),
            "巡航状态",
            font=self._font(18),
            fill="#9e6684",
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
        output_path = self._new_output_path("komari_status")
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_render_cache()
        return str(output_path)

    def render_alert(
        self,
        node: dict[str, Any],
        alert_kind: str,
        value: float | None = None,
        threshold: float | None = None,
    ) -> str:
        """Render one node transition or resource alert image.

        Args:
            node: Normalized Komari node record at alert time.
            alert_kind: Alert state identifier selected by the monitor.
            value: Current resource percentage for resource alerts.
            threshold: Configured resource threshold for context.

        Returns:
            Absolute path to a uniquely named PNG image.

        Raises:
            ValueError: If the alert state identifier is unsupported.
        """
        alert_styles = {
            "offline": ("节点离线", "连接已经中断", "#e66787", "OFFLINE"),
            "online": ("恢复在线", "节点已重新加入巡航", "#4caf83", "BACK ONLINE"),
            "cpu_high": (
                "CPU 占用预警",
                "资源压力超过设定阈值",
                "#ee8d52",
                "CPU ALERT",
            ),
            "memory_high": (
                "内存占用预警",
                "内存用量超过设定阈值",
                "#ee8d52",
                "MEMORY ALERT",
            ),
        }
        if alert_kind not in alert_styles:
            raise ValueError(f"Unsupported Komari alert kind: {alert_kind}")

        title, subtitle, accent, badge = alert_styles[alert_kind]
        width = 1080
        height = 1120
        margin = 56
        image = Image.new("RGB", (width, height), "#fff7fb")
        draw = ImageDraw.Draw(image)
        self._draw_background(draw, width, height)
        draw.text(
            (margin, 48), "Komari · 状态提醒", font=self._font(34), fill="#3d294c"
        )
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        draw.text(
            (width - margin, 59),
            timestamp,
            font=self._font(15),
            fill="#9b819d",
            anchor="ra",
        )

        panel_fill = "#fff0f5" if alert_kind == "offline" else "#fff4f7"
        draw.rounded_rectangle(
            (margin, 118, width - margin, 316),
            radius=34,
            fill=panel_fill,
            outline="#f2cbd9",
            width=2,
        )
        draw.ellipse((margin + 34, 162, margin + 86, 214), fill=accent)
        draw.ellipse((margin + 48, 176, margin + 72, 200), fill="#ffffff")
        draw.text((margin + 112, 146), title, font=self._font(42), fill="#432d4f")
        draw.text((margin + 114, 205), subtitle, font=self._font(18), fill="#896b88")
        draw.rounded_rectangle(
            (width - margin - 246, 246, width - margin - 28, 286),
            radius=20,
            fill=accent,
        )
        draw.text(
            (width - margin - 137, 266),
            badge,
            font=self._font(14),
            fill="#ffffff",
            anchor="mm",
        )
        if value is not None and threshold is not None:
            draw.text(
                (margin + 114, 253),
                f"当前 {value:.1f}%  ·  参考阈值 {threshold:.1f}%",
                font=self._font(17),
                fill=accent,
            )

        draw.text((margin, 354), "CURRENT NODE", font=self._font(15), fill="#d57e9e")
        draw.rounded_rectangle(
            (margin, 384, width - margin, 574),
            radius=28,
            fill="#ffffff",
            outline="#f0dce8",
            width=2,
        )
        name = self._fit_text(
            str(node.get("name", "未命名节点")), width - 2 * margin - 64, 28
        )
        draw.text((margin + 30, 410), name, font=self._font(28), fill="#432d4f")
        uuid = self._fit_text(
            f"UUID  {node.get('uuid', '未知')}", width - 2 * margin - 64, 16
        )
        draw.text((margin + 30, 457), uuid, font=self._font(16), fill="#987d9b")
        metadata = (
            f"{self._region_label(node.get('region'))}  ·  "
            f"{node.get('os', '未知系统')}  ·  {node.get('arch', '未知架构')}"
        )
        draw.text(
            (margin + 30, 501),
            self._fit_text(metadata, width - 2 * margin - 64, 17),
            font=self._font(17),
            fill="#765a77",
        )
        cpu_name = str(node.get("cpu_name") or "CPU 型号未上报")
        draw.text(
            (margin + 30, 535),
            self._fit_text(cpu_name, width - 2 * margin - 64, 15),
            font=self._font(15),
            fill="#a58ba5",
        )

        draw.text((margin, 612), "LIVE METRICS", font=self._font(15), fill="#d57e9e")
        metrics = [
            ("CPU", self._percent(node.get("cpu"))),
            ("RAM", self._ratio_percent(node.get("ram"), node.get("ram_total"))),
            ("DISK", self._ratio_percent(node.get("disk"), node.get("disk_total"))),
            ("SWAP", self._ratio_percent(node.get("swap"), node.get("swap_total"))),
        ]
        metric_width = (width - margin * 2 - 24) // 2
        for index, (label, metric_value) in enumerate(metrics):
            column = index % 2
            row = index // 2
            x = margin + column * (metric_width + 24)
            y = 646 + row * 112
            draw.rounded_rectangle(
                (x, y, x + metric_width, y + 90),
                radius=22,
                fill="#ffffff",
                outline="#f0dce8",
                width=2,
            )
            draw.text((x + 22, y + 18), label, font=self._font(15), fill="#a17fa2")
            draw.text(
                (x + metric_width - 22, y + 17),
                f"{metric_value:.1f}%",
                font=self._font(18),
                fill=accent if metric_value < 80 else "#e07e46",
                anchor="ra",
            )
            bar_width = metric_width - 44
            draw.rounded_rectangle(
                (x + 22, y + 56, x + 22 + bar_width, y + 68),
                radius=6,
                fill="#f2eaf2",
            )
            draw.rounded_rectangle(
                (
                    x + 22,
                    y + 56,
                    x + 22 + int(bar_width * metric_value / 100),
                    y + 68,
                ),
                radius=6,
                fill=accent if metric_value < 80 else "#ee9b52",
            )

        draw.rounded_rectangle(
            (margin, 886, width - margin, 1026),
            radius=26,
            fill="#f8edf5",
        )
        online = bool(node.get("online"))
        uptime = self._format_uptime(node.get("uptime")) if online else "节点当前离线"
        network = (
            f"累计流量 ↓ {self._format_bytes(node.get('net_total_down'))}  "
            f"↑ {self._format_bytes(node.get('net_total_up'))}"
        )
        draw.text((margin + 28, 911), uptime, font=self._font(18), fill="#684d6d")
        draw.text((margin + 28, 951), network, font=self._font(16), fill="#8f7191")
        draw.text(
            (margin + 28, 986),
            f"采样时间  {timestamp}",
            font=self._font(15),
            fill="#a58ba5",
        )
        draw.text(
            (margin, height - 46),
            "清蒸云鸭的粉色监控舱 · 本提醒仅在状态变化时发送一次",
            font=self._font(15),
            fill="#a98ca8",
        )

        safe_uuid = "".join(
            char
            for char in str(node.get("uuid", "node"))
            if char.isalnum() or char == "-"
        )[:48]
        output_path = self._new_output_path(f"komari_notice_{safe_uuid}_{alert_kind}")
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_render_cache()
        return str(output_path)

    def _new_output_path(self, prefix: str) -> Path:
        """Create a cache-busting output path in the plugin data directory.

        Args:
            prefix: Human-readable filename prefix.

        Returns:
            Unique PNG path based on a nanosecond timestamp.
        """
        return self.data_dir / f"{prefix}_{time.time_ns()}.png"

    def _cleanup_render_cache(self) -> None:
        """Bound generated image files by age and count."""
        now = time.time()
        candidates: list[tuple[float, Path]] = []
        for path in self.data_dir.glob("komari_*.png"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        for index, (modified_at, path) in enumerate(candidates):
            try:
                if index >= 40 or now - modified_at > 86400:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

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
                (x - radius, y - radius, x + radius, y + radius),
                fill="#f8bed1",
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
        """Draw one node card while truncating variable-length labels."""
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
        draw.line(
            (x + 28, y + 104, x + width - 28, y + 104),
            fill="#f3e8f0",
            width=2,
        )
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
                (mx, my + 28, mx + metric_width, my + 40),
                radius=6,
                fill="#f2eaf2",
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
    def _format_bytes(value: Any) -> str:
        """Format a byte counter with a compact binary unit."""
        try:
            amount = max(0.0, float(value or 0))
        except (TypeError, ValueError):
            amount = 0.0
        for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
            if amount < 1024 or unit == "PiB":
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return "0 B"
