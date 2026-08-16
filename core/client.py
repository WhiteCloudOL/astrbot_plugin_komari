from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from astrbot.api import logger


class KomariClient:
    """Fetch normalized node snapshots from a Komari JSON-RPC endpoint."""

    def __init__(self, base_url: str, api_token: str, cache_ttl: int):
        self.base_url = base_url.strip().rstrip("/")
        self.api_token = api_token.strip()
        self.cache_ttl = max(0, cache_ttl)
        self._snapshot_cache: tuple[float, list[dict[str, Any]]] | None = None

    async def fetch_snapshot(self, force: bool = False) -> list[dict[str, Any]] | None:
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
        self,
        session: aiohttp.ClientSession,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        """Call one Komari JSON-RPC method."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        async with session.post(f"{self.base_url}/api/rpc2", json=payload) as response:
            response.raise_for_status()
            body = await response.json(content_type=None)
        if body.get("error"):
            raise RuntimeError(str(body["error"]))
        return body.get("result")
