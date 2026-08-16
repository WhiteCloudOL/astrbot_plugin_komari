from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


class StatusRenderer:
    """Render Komari snapshots as compact, readable status cards."""

    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
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
        image.save(self.output_path, format="PNG", optimize=True)
        return str(self.output_path)

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
