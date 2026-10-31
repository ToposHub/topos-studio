"""ffmpeg/ffprobe 可执行文件定位(打包目录优先,PATH 兜底)。

目标:让打包发行版开箱即用——把 ffmpeg/ffprobe 二进制放进应用自带的
``bin/`` 目录即可,用户无需单独安装 FFmpeg。

查找顺序(找到即返回):

1. 环境变量 ``TOPOS_FFMPEG_HOME``:指向同时含 ffmpeg/ffprobe 的目录,
   便于用户手动指定自定义构建。
2. 打包目录候选下的 ``bin/``:
   - PyInstaller 冻结后的可执行目录 / ``sys._MEIPASS``
   - 应用根目录(仓库运行时 = 本文件所在仓库根)
3. 系统 ``PATH``(``shutil.which``)。

设计约束(遵循 .clinerules):

- 纯标准库、无 Qt / PyAV / numpy 依赖,可在任意线程与轻量模块中导入
  (ai_editing 等纯 Python 模块也引用本模块)。
- 不抛异常:找不到返回 ``None``,由调用方决定降级行为。
- 结果缓存;``clear_cache()`` 供测试与运行时环境变化使用。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_ENV_HOME = "TOPOS_FFMPEG_HOME"

_cached: dict[str, str | None] = {}


def _exe_name(name: str) -> str:
    """按平台补全可执行文件后缀(Windows 需要 .exe)。"""
    return f"{name}.exe" if sys.platform == "win32" else name


def candidate_dirs() -> list[Path]:
    """按优先级返回可能存放 ffmpeg/ffprobe 的目录。"""
    dirs: list[Path] = []

    env_home = os.environ.get(_ENV_HOME, "").strip()
    if env_home:
        dirs.append(Path(env_home))

    # PyInstaller 冻结环境:可执行文件目录 / 一次性解包目录
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            dirs.append(Path(meipass))

    # 源码运行:仓库根 = 本文件所在仓库根
    # (src/shared/ffmpeg_locator.py → parents[2] = 仓库根;此前
    #  parent.parent 解析到 src/,导致仓库根 bin/ 下的 ffmpeg 永远找不到)
    dirs.append(Path(__file__).resolve().parents[2])

    # 候选目录下的 bin/ 子目录也纳入查找
    with_bin: list[Path] = []
    for d in dirs:
        with_bin.append(d)
        with_bin.append(d / "bin")
    return with_bin


def _resolve_in_dir(directory: Path, exe: str) -> str | None:
    """在单个目录内解析二进制（is_file + 可执行权限检查，PATH 无关）。

    [AUD-33703] 不可执行文件（下载残缺、执行位被剥离、macOS Gatekeeper
    隔离属性等）视为无效候选并返回 None——继续尝试后续候选目录 /
    ``shutil.which`` 的 PATH 兜底，而不是让坏文件永久遮蔽 PATH 上的有效
    ffmpeg。Windows 无 POSIX 执行位概念，跳过 ``os.access`` 检查。
    """
    path = directory / exe
    if not path.is_file():
        return None
    if sys.platform != "win32" and not os.access(path, os.X_OK):
        logger.debug("Skipping non-executable candidate: %s", path)
        return None
    return str(path)


def find_binary(name: str) -> str | None:
    """定位指定二进制(``"ffmpeg"`` / ``"ffprobe"``),找不到返回 None。"""
    if name in _cached:
        return _cached[name]

    exe = _exe_name(name)
    result: str | None = None

    for directory in candidate_dirs():
        result = _resolve_in_dir(directory, exe)
        if result is not None:
            break

    if result is None:
        which = shutil.which(exe)
        if which is not None:
            result = which

    if result is None:
        logger.debug("%s binary not found (bundled dirs and PATH)", name)
    else:
        logger.debug("%s resolved to %s", name, result)

    _cached[name] = result
    return result


def find_ffmpeg() -> str | None:
    """定位 ffmpeg 可执行文件;找不到返回 None。"""
    return find_binary("ffmpeg")


def find_ffprobe() -> str | None:
    """定位 ffprobe 可执行文件;找不到返回 None。"""
    return find_binary("ffprobe")


def clear_cache() -> None:
    """清除定位缓存(测试或运行时环境变化后使用)。"""
    _cached.clear()


__all__ = [
    "candidate_dirs",
    "clear_cache",
    "find_binary",
    "find_ffmpeg",
    "find_ffprobe",
]
