# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""vault 只读客户端（主项目侧调用 vault 查询服务）。

主项目（进程 A）通过本客户端连 vault 查询服务（进程 C，Flask），
拿到检索结果。vault 未启动时优雅降级（返回空，不阻塞主项目）。

用法：
    from src.shared.vault.client import VaultClient

    client = VaultClient()  # 默认连 http://127.0.0.1:8288
    if client.is_available():
        files = client.search("微笑", media_type="image", limit=50)
        stats = client.get_stats()

设计：
    - 只读：主项目不写 vault 数据，只查（符合三进程分离架构）
    - HTTP：用标准库 urllib（无新依赖；主项目已有很多 HTTP 客户端可选，
      但 vault 查询简单，urllib 够用且零依赖）
    - 降级：vault 没起 → is_available()=False，所有查询返回空/None，不抛异常
    - 超时短：查询是辅助功能，不能让 vault 卡住主项目（默认 1.5s 超时）

环境变量：
    TOPOS_VAULT_URL=http://127.0.0.1:8288  查询服务地址（默认）
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_VAULT_URL = "http://127.0.0.1:8288"
_DEFAULT_TIMEOUT = 1.5  # 秒（vault 是辅助，不能卡主项目）


@dataclass
class VaultStats:
    """vault 库统计（对应 /api/stats）。"""
    total: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    missing: int = 0
    db_path: str = ""


class VaultClient:
    """vault 查询服务的只读 HTTP 客户端。

    所有方法在 vault 未启动时返回空/False，不抛异常（降级安全）。
    """

    def __init__(self, base_url: str | None = None, *, timeout: float = _DEFAULT_TIMEOUT):
        """构造客户端。

        Args:
            base_url: vault 服务地址。None 则读 TOPOS_VAULT_URL，再默认 127.0.0.1:8288。
            timeout: 请求超时（秒）。vault 没响应就降级，不等。
        """
        if base_url is None:
            base_url = os.environ.get("TOPOS_VAULT_URL", DEFAULT_VAULT_URL)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_available(self) -> bool:
        """vault 服务是否在线（调 /api/health）。"""
        try:
            data = self._get("/api/health")
            return data.get("status") == "ok"
        except Exception:
            return False

    def search(
        self,
        query: str,
        *,
        media_type: str = "all",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """搜索素材（对应 /api/search）。

        Args:
            query: 搜索关键词。
            media_type: all / video / image / audio。
            limit: 最多返回多少条。

        Returns:
            file_explorer 兼容的文件字典列表。vault 不可用时返回 []。
        """
        if not query.strip():
            return []
        try:
            data = self._get("/api/search", params={
                "q": query, "type": media_type, "limit": str(limit),
            })
            return data.get("files", [])
        except Exception as e:
            logger.debug("vault search 失败（降级返回空）：%s", e)
            return []

    def get_stats(self) -> VaultStats | None:
        """库统计（对应 /api/stats）。vault 不可用时返回 None。"""
        try:
            data = self._get("/api/stats")
            return VaultStats(
                total=data.get("total", 0),
                by_type=data.get("by_type", {}),
                missing=data.get("missing", 0),
                db_path=data.get("db_path", ""),
            )
        except Exception as e:
            logger.debug("vault get_stats 失败：%s", e)
            return None

    # ------------------------------------------------------------------ #
    # 内部：HTTP 请求
    # ------------------------------------------------------------------ #

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """发 GET 请求，返回解析后的 JSON dict。失败抛异常（调用方降级）。"""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
