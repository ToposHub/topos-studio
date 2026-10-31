"""批量导出队列持久化存储。

独立于 ConfigManager 的轻量存储模块，专门用于保存/恢复批量导出队列状态。

设计要点：
- 单独存 ``batch_queue.json``（与 config.json 同目录），避免把"任务态/会话态"
  和"偏好态"混在一起，也避免几十上百个队列项放大 config.json 的 IO。
- 原子写（临时文件 + os.replace），抄自 config_manager.py 的已验证模式，
  保证渲染中途崩溃也不会损坏文件。
- 状态机：pending → rendering → done | failed；可被 cancelled 中断。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 状态常量 ──────────────────────────────────────────────────────────────
STATUS_PENDING = "pending"        # 等待渲染
STATUS_RENDERING = "rendering"    # 渲染中
STATUS_DONE = "done"              # 已完成
STATUS_FAILED = "failed"          # 失败

# 可恢复的状态：启动时这些项会被认为"未完成"，可继续渲染
RESTORABLE_STATUSES = (STATUS_PENDING, STATUS_RENDERING, STATUS_FAILED)

QUEUE_FILENAME = "batch_queue.json"
APP_NAME = "Topos"


@dataclass
class BatchQueueItem:
    """单个批量导出队列项。

    Attributes:
        file_path: 输入素材绝对路径。
        status: STATUS_PENDING / STATUS_RENDERING / STATUS_DONE / STATUS_FAILED.
        output_path: 渲染产物路径（完成时填，否则 None）。
        error: 失败时的异常信息（成功时为 None）。
        started_at: 渲染开始时间戳（time.time()）。
        finished_at: 渲染完成时间戳（未完成时为 None）。
    """
    file_path: str
    status: str = STATUS_PENDING
    output_path: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "status": self.status,
            "output_path": self.output_path,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchQueueItem":
        return cls(
            file_path=str(data.get("file_path", "")),
            status=str(data.get("status", STATUS_PENDING)),
            output_path=data.get("output_path"),
            error=data.get("error"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
        )

    @property
    def is_restorable(self) -> bool:
        """是否属于"未完成、可恢复"的项。"""
        return self.status in RESTORABLE_STATUSES


# ── 存储路径 ──────────────────────────────────────────────────────────────
def _get_queue_path() -> str:
    """返回 batch_queue.json 的完整路径（与 config.json 同目录）。

    支持 TOPOS_CONFIG_PATH 环境变量覆盖（与 ConfigManager 行为一致）。
    """
    override = os.environ.get("TOPOS_CONFIG_PATH")
    if override:
        # 把同目录下的 batch_queue.json 用作队列文件
        return str(Path(os.path.dirname(os.path.abspath(os.path.expanduser(override)))) / QUEUE_FILENAME)

    system_name = platform.system()
    if system_name == "Darwin":
        base_dir = Path.home() / "Library" / "Application Support" / APP_NAME
    elif system_name == "Windows":
        appdata = os.environ.get("APPDATA")
        base_dir = Path(appdata) / APP_NAME if appdata else Path.home() / "AppData" / "Roaming" / APP_NAME
    else:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        base_dir = Path(xdg_config_home) / "topos" if xdg_config_home else Path.home() / ".config" / "topos"
    return str(base_dir / QUEUE_FILENAME)


# 模块级文件锁，保证读写串行（同进程内）
_file_lock = Lock()


# ── 读写接口 ──────────────────────────────────────────────────────────────
def save_batch_queue(items: list[BatchQueueItem], settings: Optional[dict] = None) -> bool:
    """原子写入队列状态到 batch_queue.json。

    Args:
        items: 队列项列表。
        settings: 可选的导出设置（format/options/naming 等），恢复时需要复用。
            传入时会一并持久化；为 None 则不覆盖已存的设置。

    Returns:
        是否成功写入。
    """
    with _file_lock:
        try:
            queue_path = _get_queue_path()
            config_dir = os.path.dirname(queue_path)
            if config_dir:
                os.makedirs(config_dir, exist_ok=True)

            payload: dict[str, Any] = {
                "items": [item.to_dict() for item in items],
            }
            if settings is not None:
                payload["settings"] = settings
            else:
                # AUD-33115: docstring 承诺"为 None 则不覆盖已存的设置"，
                # 但原子替换是整文件覆写——旧 settings 会被直接抹掉。这里
                # 先读回旧文件合并，保住"None 保设置"的文档语义。（不能调
                # load_batch_queue()：同模块非重入锁会自锁。）
                try:
                    if os.path.exists(queue_path):
                        with open(queue_path, "r", encoding="utf-8") as f:
                            old_settings = json.load(f).get("settings")
                        if old_settings is not None:
                            payload["settings"] = old_settings
                except (OSError, ValueError) as exc:
                    # 旧文件缺失/损坏不阻塞本次保存，仅放弃继承旧设置。
                    logger.warning(f"save_batch_queue: 保留旧 settings 失败: {exc}")

            fd, temp_path = tempfile.mkstemp(
                prefix=f".{QUEUE_FILENAME}.",
                suffix=".tmp",
                dir=config_dir or None,
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, queue_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            return True
        except Exception as e:
            logger.error(f"save_batch_queue failed: {e}")
            return False


def load_batch_queue() -> tuple[list[BatchQueueItem], Optional[dict]]:
    """读取队列状态。

    Returns:
        (items, settings) 元组。文件不存在或损坏时返回 ([], None)。
    """
    with _file_lock:
        try:
            queue_path = _get_queue_path()
            if not os.path.exists(queue_path):
                return [], None
            with open(queue_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            items = [BatchQueueItem.from_dict(d) for d in payload.get("items", [])]
            settings = payload.get("settings")
            return items, settings
        except Exception as e:
            logger.error(f"load_batch_queue failed: {e}")
            return [], None


def clear_batch_queue() -> None:
    """删除队列文件（全部完成或用户选择不恢复时调用）。"""
    with _file_lock:
        try:
            queue_path = _get_queue_path()
            if os.path.exists(queue_path):
                os.remove(queue_path)
        except Exception as e:
            logger.error(f"clear_batch_queue failed: {e}")


def has_restorable_items() -> bool:
    """是否存在未完成（可恢复）的队列项。供启动时快速判断是否弹恢复提示。"""
    items, _ = load_batch_queue()
    return any(item.is_restorable for item in items)
