"""Immutable presentation contract for Files extensions, independent of a host."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FilesPluginInfo:
    plugin_id: str
    name: str
    version: str
    author: str
    description: str
    api_version: int | None
    category: str
    capabilities: tuple[str, ...]
    runtime_loaded: bool
    desired_enabled: bool
    self_disabled: bool
    needs_restart: bool
    error: str
    launch_path: str
    launch_payload_kind: str
    launch_unavailable_reason: str
    path: Path
    diagnostics: tuple[str, ...] = ()
    # [F1] 单一 Inventory 投影字段：来源政策（builtin/installed/local_dev）、
    # 安装事实（installed/discovered/missing）与运行时实际加载版本（空 =
    # 宿主未上报）。默认值保持旧投影调用方不变。
    source: str = "builtin"
    install_state: str = "installed"
    runtime_version: str = ""
    # [F2] 命令键：合并行携带命名空间 plugin_id（偏好/固定/快捷键建档），
    # 命令层（legacy 目录/命令服务）仍以短 id 寻址——空 = 无命令入口
    # （inventory-only 占位行，不可启动）
    command_key: str = ""

    @property
    def can_launch(self) -> bool:
        return bool(self.launch_path and not self.launch_unavailable_reason)
