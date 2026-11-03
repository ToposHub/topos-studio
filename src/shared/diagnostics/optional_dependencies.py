"""N17: 可选依赖统一探测（Qt FFmpeg vs 外部工具 vs AI ModelPack）。

此前启动期告警分散（ThumbEngine 外部 ffmpeg 路径、各 AI provider 的
ModelPack 未安装 WARNING），且「Qt multimedia 自带 FFmpeg 可用」与
「外部 ffmpeg 工具缺失」语义混在一起。本模块提供**单一探测源**：

- ``qt_multimedia``：Qt 运行时多媒体后端（qEnvironmentVariable / 运行期
  曾成功初始化的标志——探测只读进程内状态，不重复加载）；
- ``external_ffmpeg``：外部工具路径（``utils.get_tool_path``，供
  ThumbEngine/CLI 类用途；缺失不影响 Qt 播放/解码）；
- ``ai_model_packs``：ModelPack 注册表逐项 installed+verified。

消费方：启动日志**聚合一条**（替代散落 WARNING，不泄露无关内容）；
R01 目录 ``ai_model_missing`` 与 R06 预检以本探测为单一来源（未安装
模型不得作为 AI 通过证据——可用性恒 fail-closed）。全部探测可注入
（测试/无外部工具环境）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)


def _probe_qt_multimedia() -> dict[str, Any]:
    """Qt multimedia 运行时可用性（进程内标志；不重复初始化后端）。"""
    try:
        from PyQt6.QtMultimedia import QMediaPlayer  # noqa: F401
        available = True
        detail = "PyQt6.QtMultimedia importable（运行时后端随 Qt）"
    except Exception as exc:  # noqa: BLE001 — 缺模块=不可用
        available = False
        detail = f"QtMultimedia 不可用: {exc}"
    return {"available": available, "source": "qt_runtime", "detail": detail}


def _probe_external_ffmpeg(tool_path: Callable[[str], str | None]) -> dict[str, Any]:
    path = ""
    try:
        path = tool_path("ffmpeg") or ""
    except Exception:  # noqa: BLE001
        path = ""
    return {"available": bool(path), "source": "external_tool",
            "detail": f"path={path or '(not found)'}（Qt 播放/解码不依赖）"}


def _probe_ai_model_packs() -> dict[str, dict[str, Any]]:
    packs: dict[str, dict[str, Any]] = {}
    try:
        from src.shared.ai_models.generation.model_pack_manager import (
            get_model_pack_manager,
        )
        manager = get_model_pack_manager()
        for spec in manager.list_specs():
            installed = False
            try:
                manager.get_runtime(spec.pack_id)  # 未装/未验证 → 抛
                installed = True
            except Exception:  # noqa: BLE001 — 单包失败不影响其余
                installed = False
            packs[spec.pack_id] = {
                "available": installed,
                "source": "model_pack_registry",
                "detail": "" if installed else "未安装或未验证",
            }
    except Exception as exc:  # noqa: BLE001 — 注册表缺席=空表
        packs["__registry__"] = {"available": False,
                                 "source": "model_pack_registry",
                                 "detail": str(exc)[:80]}
    return packs


def optional_dependency_status(
        *, tool_path: Callable[[str], str | None] | None = None,
        ai_probe: Callable[[], dict[str, dict[str, Any]]] | None = None,
        qt_probe: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """聚合可选依赖状态（单一探测源；全部可注入）。"""
    from external.file_explorer.utils import get_tool_path
    return {
        "qt_multimedia": (qt_probe or _probe_qt_multimedia)(),
        "external_ffmpeg": _probe_external_ffmpeg(
            tool_path or get_tool_path),
        "ai_model_packs": (ai_probe or _probe_ai_model_packs)(),
    }


def summarize(status: "dict[str, Any] | None" = None) -> str:
    """一行人类可读摘要（启动聚合日志用；不含路径以外的环境细节）。"""
    status = status or optional_dependency_status()
    qt = status["qt_multimedia"]["available"]
    ffmpeg = status["external_ffmpeg"]["available"]
    packs = status["ai_model_packs"]
    missing_packs = sorted(k for k, v in packs.items()
                           if not v.get("available"))
    parts = [f"qt_multimedia={'ok' if qt else 'MISSING'}",
             f"external_ffmpeg={'ok' if ffmpeg else 'missing'}"]
    if missing_packs:
        parts.append(f"ai_model_packs_missing={','.join(missing_packs)}")
    return "optional dependencies: " + " ".join(parts)


def ai_model_available(pack_id: str,
                       status: dict[str, Any] | None = None) -> bool:
    """N17 单一来源：未安装模型恒 False（不得作为 AI 通过证据）。"""
    if status is None:
        status = optional_dependency_status()
    entry = (status.get("ai_model_packs") or {}).get(pack_id)
    return bool(entry and entry.get("available"))


def log_startup_summary() -> None:
    """启动聚合一条（替代散落 WARNING；DEBUG 级避免刷屏）。"""
    _LOGGER.info(summarize())
