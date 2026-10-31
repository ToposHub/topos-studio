"""RuntimeCapabilitySnapshot——单一能力状态模型（总计划 T-P0-00）。

目标：让 UI、AI（ToolFacade/MCP）、CLI（toposctl）和 Deliver preflight 使用
**同一份**运行时能力事实，而不是各自探测、各自猜测 native 库/GPU/模型是否
可用。

核心契约：

- 四态状态机：``AVAILABLE`` / ``UNAVAILABLE`` / ``INCOMPATIBLE`` /
  ``FALLBACK``——禁止用 bool 掩盖原因；每个条目必须带稳定 ``reason_code``
  与人可读 ``detail``。
- 同一输入 → 同一 ``snapshot_id``（检测时间不参与 ID 计算），跨入口可对齐。
- 纯 Python、无 Qt/GL 依赖：离线 CLI（NoQtGlImport）路径可直接构建；
  GL/硬件编码的"真实可用"由运行时 renderer/export trail 证明，快照只记录
  诚实的 ``FALLBACK``（探测推迟）或框架存在性事实。
- codec 域真相源自 ``topos_binding``/``topos_profiles``（不第四份手抄，
  与 tests/media/test_capability_manifest.py 钉死的 native manifest 对齐）。

消费方注入方式（全部可选参数，None 时保持旧行为）：

- ``capability_manifest.build_manifest(runtime_snapshot=...)``
- ``toposctl`` 离线路径
- ``AiEditHostBridgeImpl.get_capability_manifest()``（进程级缓存 + 失效钩子）
- ``PreflightContext.runtime_snapshot``（Deliver preflight 的 codec 域门禁）
"""

from __future__ import annotations

import ctypes.util
import hashlib
import json
import logging
import platform as _platform
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from src.shared.gpu.host_gpu_info import detect_host_gpu_info, metal_info_from_gpu

__all__ = [
    "RuntimeCapabilityState",
    "RuntimeCapabilityEntry",
    "RuntimeCapabilitySnapshot",
    "CapabilitySnapshotStore",
    "build_runtime_capability_snapshot",
    "reset_runtime_probe_cache",
]

#: TOPOS codec 库解析失败的稳定 reason_code（与总计划 T-P0-10 对齐）。
TOPOS_CODEC_LIB_NOT_FOUND = "TOPOS_CODEC_LIB_NOT_FOUND"
TOPOS_CODEC_INCOMPATIBLE = "TOPOS_CODEC_INCOMPATIBLE"


class RuntimeCapabilityState(str, Enum):
    """运行时能力四态（总计划 §5 T-P0-00 步骤 2）。"""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    FALLBACK = "FALLBACK"


@dataclass(frozen=True)
class RuntimeCapabilityEntry:
    """单个能力组件的四态事实。``info`` 必须是 JSON-safe 的映射。"""

    component_id: str
    state: RuntimeCapabilityState
    reason_code: Optional[str] = None
    detail: Optional[str] = None
    info: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_id": self.component_id,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "info": dict(self.info),
        }


@dataclass(frozen=True)
class RuntimeCapabilitySnapshot:
    """一次检测得到的完整能力事实（不可变）。

    正在运行的任务应冻结启动时的 snapshot 引用；``snapshot_id`` 变化即
    表示能力事实发生了变化（ CapabilitySnapshotStore 负责发事件）。
    """

    entries: Tuple[RuntimeCapabilityEntry, ...]
    platform: Mapping[str, Any]
    git_revision: str
    detected_at: str = ""

    def entry(self, component_id: str) -> Optional[RuntimeCapabilityEntry]:
        for e in self.entries:
            if e.component_id == component_id:
                return e
        return None

    def state_of(self, component_id: str) -> Optional[RuntimeCapabilityState]:
        e = self.entry(component_id)
        return e.state if e is not None else None

    @property
    def snapshot_id(self) -> str:
        """稳定 ID：检测时间不参与；同一能力事实 → 同一 ID。"""
        payload = json.dumps(
            {
                "entries": [e.to_dict() for e in self.entries],
                "platform": dict(self.platform),
                "git_revision": self.git_revision,
            },
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "detected_at": self.detected_at,
            "platform": dict(self.platform),
            "git_revision": self.git_revision,
            "entries": [e.to_dict() for e in self.entries],
        }


class CapabilitySnapshotStore:
    """进程级 snapshot 持有者 + 变化事件（总计划 T-P0-00 步骤 5）。

    ``set()`` 仅在 snapshot_id 变化时通知监听者；任务启动时应调用 ``get()``
    冻结当次运行的 snapshot 版本。

    AUD-14314：``_snapshot`` 换写与 ``_listeners`` 增删/遍历持同一把
    ``threading.Lock``——set/subscribe/退订并发时不再有竞态窗口（丢通知、
    "list.remove(x): x not in list"、迭代中变更）。监听者回调在锁外执行，
    避免回调内再调 ``get()``/``subscribe()`` 造成死锁；监听者故障记录日志
    但不阻断状态更新。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Optional[RuntimeCapabilitySnapshot] = None
        self._listeners: list[Callable[[RuntimeCapabilitySnapshot], None]] = []

    def get(self) -> Optional[RuntimeCapabilitySnapshot]:
        with self._lock:
            return self._snapshot

    def set(self, snapshot: RuntimeCapabilitySnapshot) -> bool:
        """写入 snapshot；返回是否发生了可见变化（ID 不同）。"""
        with self._lock:
            previous = self._snapshot
            self._snapshot = snapshot
            if previous is not None and previous.snapshot_id == snapshot.snapshot_id:
                return False
            listeners = tuple(self._listeners)
        for cb in listeners:
            try:
                cb(snapshot)
            except Exception as exc:  # noqa: BLE001 — 监听者故障不得阻断状态更新
                logging.getLogger(__name__).warning(
                    "capability snapshot listener %r failed: %s", cb, exc,
                )
        return True

    def subscribe(
        self, callback: Callable[[RuntimeCapabilitySnapshot], None]
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def refresh(self, **kwargs: Any) -> RuntimeCapabilitySnapshot:
        """重新探测并写入；返回新 snapshot。"""
        snapshot = build_runtime_capability_snapshot(**kwargs)
        self.set(snapshot)
        return snapshot


# ---------------------------------------------------------------------------
# 组件探测（每个函数只依赖既有探测面，不另建缓存）
# ---------------------------------------------------------------------------

def _probe_pyav() -> RuntimeCapabilityEntry:
    try:
        import av  # noqa: PLC0415

        return RuntimeCapabilityEntry(
            component_id="media.ffmpeg.pyav",
            state=RuntimeCapabilityState.AVAILABLE,
            info={"pyav_version": av.__version__,
                  "ffmpeg_version": getattr(av, "ffmpeg_version_info", None)},
        )
    except Exception as exc:  # noqa: BLE001 — ImportError/库加载失败都不可用
        return RuntimeCapabilityEntry(
            component_id="media.ffmpeg.pyav",
            state=RuntimeCapabilityState.UNAVAILABLE,
            reason_code="PYAV_IMPORT_FAILED",
            detail=str(exc),
        )


def _probe_ff_cli(kind: str, finder: Callable[[], Optional[str]]) -> RuntimeCapabilityEntry:
    exe = None
    try:
        exe = finder()
    except Exception as exc:  # noqa: BLE001 — locator 自身故障也可见
        return RuntimeCapabilityEntry(
            component_id=f"media.{kind}.cli",
            state=RuntimeCapabilityState.UNAVAILABLE,
            reason_code=f"{kind.upper()}_LOCATOR_FAILED",
            detail=str(exc),
        )
    if exe:
        return RuntimeCapabilityEntry(
            component_id=f"media.{kind}.cli",
            state=RuntimeCapabilityState.AVAILABLE,
            info={"path": str(exe)},
        )
    return RuntimeCapabilityEntry(
        component_id=f"media.{kind}.cli",
        state=RuntimeCapabilityState.UNAVAILABLE,
        reason_code=f"{kind.upper()}_CLI_NOT_FOUND",
        detail="TOPOS_FFMPEG_HOME/打包 bin/PATH 均未发现；探测仅走 PyAV/native",
    )


def _probe_topos_codec(lib_path: Optional[str]) -> RuntimeCapabilityEntry:
    """TOPOS dylib 四态探测（复用 topos_binding 的三态库解析——T-P0-10
    之后这是唯一解析真相源，media 层与快照层同源）。

    状态语义：
    - 显式路径/环境变量指向不存在的文件 → ``UNAVAILABLE/TOPOS_CODEC_LIB_NOT_FOUND``
    - 文件存在但加载失败或 ABI 不匹配 → ``INCOMPATIBLE/TOPOS_CODEC_INCOMPATIBLE``
    """
    from src.shared.codec.topos_binding import (
        LIB_RESOLUTION_INCOMPATIBLE,
        LIB_RESOLUTION_NOT_FOUND,
        LIB_RESOLUTION_OK,
        resolve_topos_codec_library,
    )

    resolution = resolve_topos_codec_library(lib_path)
    if resolution.status == LIB_RESOLUTION_NOT_FOUND:
        return RuntimeCapabilityEntry(
            component_id="codec.topos",
            state=RuntimeCapabilityState.UNAVAILABLE,
            reason_code=TOPOS_CODEC_LIB_NOT_FOUND,
            detail=resolution.detail,
        )
    if resolution.status == LIB_RESOLUTION_INCOMPATIBLE:
        return RuntimeCapabilityEntry(
            component_id="codec.topos",
            state=RuntimeCapabilityState.INCOMPATIBLE,
            reason_code=TOPOS_CODEC_INCOMPATIBLE,
            detail=resolution.detail,
            info={"library_path": resolution.library_path},
        )
    assert resolution.status == LIB_RESOLUTION_OK
    assert resolution.library_path is not None
    from src.shared.codec import topos_binding
    from src.shared.codec.topos_binding import load_library

    codec = topos_binding.ToposCodec(load_library(resolution.library_path))
    ver = codec.version()
    info: Dict[str, Any] = {
        "library_path": resolution.library_path,
        "abi_version": ver.abi_version,
        "version": {
            "major": ver.major, "minor": ver.minor, "patch": ver.patch,
            "git_commit": ver.git_commit, "build_target": ver.build_target,
        },
    }
    try:
        cap = codec.capabilities()
        info["capabilities"] = {
            "max_width": cap.max_width, "max_height": cap.max_height,
            "full_decode": cap.full_decode,
            "cpu_surface_output": cap.cpu_surface_output,
            "host_visible_gpu_surface": cap.host_visible_gpu_surface,
        }
    except topos_binding.ToposCodecError:
        pass
    return RuntimeCapabilityEntry(
        component_id="codec.topos",
        state=RuntimeCapabilityState.AVAILABLE,
        info=info,
    )


def _probe_topos_profiles(
    codec_entry: RuntimeCapabilityEntry,
) -> RuntimeCapabilityEntry:
    """TOPOS 档位可用性——真相源 topos_profiles（O5：不第四份手抄）。"""
    component = "codec.topos.profiles"
    if codec_entry.state is not RuntimeCapabilityState.AVAILABLE:
        return RuntimeCapabilityEntry(
            component_id=component,
            state=codec_entry.state,
            reason_code=codec_entry.reason_code,
            detail="跟随 codec.topos 状态",
        )
    try:
        from src.shared.codec.topos_profiles import available_topos_tiers

        tiers: Dict[str, Any] = {}
        for tier in available_topos_tiers():
            tiers[str(getattr(tier, "tier", getattr(tier, "name", "?")))] = {
                "available": bool(getattr(tier, "available", True)),
                "unavailable_reason": getattr(tier, "unavailable_reason", None),
            }
        return RuntimeCapabilityEntry(
            component_id=component,
            state=RuntimeCapabilityState.AVAILABLE,
            info={"tiers": tiers},
        )
    except Exception as exc:  # noqa: BLE001 — 档位表读取失败必须可见
        return RuntimeCapabilityEntry(
            component_id=component,
            state=RuntimeCapabilityState.UNAVAILABLE,
            reason_code="TOPOS_PROFILES_READ_FAILED",
            detail=str(exc),
        )


def _probe_videotoolbox() -> RuntimeCapabilityEntry:
    """VideoToolbox 框架存在性探测（macOS）。

    只证明框架可加载；真实编码会话在 ``video_encoder.open()`` 探针处验证，
    失败回退 CPU 并记 ``hw_fallback_reason``——快照不预支编码成功。
    """
    component = "encode.videotoolbox"
    if sys.platform != "darwin":
        return RuntimeCapabilityEntry(
            component_id=component,
            state=RuntimeCapabilityState.UNAVAILABLE,
            reason_code="PLATFORM_NOT_MACOS",
            detail=f"platform={sys.platform}",
        )
    path = ctypes.util.find_library("VideoToolbox")
    loaded = False
    if path:
        try:
            ctypes.CDLL(path)
            loaded = True
        except OSError:
            loaded = False
    if not loaded:
        framework = ("/System/Library/Frameworks/VideoToolbox.framework/"
                     "VideoToolbox")
        try:
            ctypes.CDLL(framework)
            loaded = True
        except OSError:
            loaded = False
    if loaded:
        return RuntimeCapabilityEntry(
            component_id=component,
            state=RuntimeCapabilityState.AVAILABLE,
            detail="框架存在性探测；编码会话可用性由导出时 encoder open 探针证明",
        )
    return RuntimeCapabilityEntry(
        component_id=component,
        state=RuntimeCapabilityState.UNAVAILABLE,
        reason_code="VIDEOTOOLBOX_FRAMEWORK_NOT_FOUND",
    )


def _probe_model_packs(
    model_pack_manager: Any,
) -> Tuple[RuntimeCapabilityEntry, ...]:
    """本地模型包四态（复用 capability_status 的 LOCAL pack 清单与状态语义）。"""
    from src.features.edit.ai_editing.capability_status import _LOCAL_PACK_IDS

    entries: list[RuntimeCapabilityEntry] = []
    # AUD-14312：_LOCAL_PACK_IDS 是 frozenset，str 哈希随机化使迭代顺序随
    # 进程变化 → entries 顺序变化 → snapshot_id 变化，破坏"同一输入 →
    # 同一 snapshot_id、跨入口（CLI/GUI）可对齐"的核心契约。必须排序。
    for pack_id in sorted(_LOCAL_PACK_IDS):
        if model_pack_manager is None:
            entries.append(RuntimeCapabilityEntry(
                component_id=f"models.{pack_id}",
                state=RuntimeCapabilityState.UNAVAILABLE,
                reason_code="MODEL_MANAGER_NOT_PROVIDED",
                detail="当前运行时未接入 ModelPackManager",
            ))
            continue
        try:
            status = model_pack_manager.get_status(pack_id)
        except Exception as exc:  # noqa: BLE001 — manager 故障不得伪装可用
            entries.append(RuntimeCapabilityEntry(
                component_id=f"models.{pack_id}",
                state=RuntimeCapabilityState.UNAVAILABLE,
                reason_code="MODEL_STATUS_QUERY_FAILED",
                detail=str(exc),
            ))
            continue
        usable = bool(getattr(status, "usable", False))
        is_demo = bool(getattr(status, "is_demo", False))
        if usable and not is_demo:
            entries.append(RuntimeCapabilityEntry(
                component_id=f"models.{pack_id}",
                state=RuntimeCapabilityState.AVAILABLE,
            ))
        elif is_demo:
            entries.append(RuntimeCapabilityEntry(
                component_id=f"models.{pack_id}",
                state=RuntimeCapabilityState.FALLBACK,
                reason_code="MODEL_PACK_DEMO",
                detail="demo 档可用，正式档未安装",
            ))
        else:
            entries.append(RuntimeCapabilityEntry(
                component_id=f"models.{pack_id}",
                state=RuntimeCapabilityState.UNAVAILABLE,
                reason_code="MODEL_PACK_NOT_USABLE",
                detail=str(getattr(status, "detail", "") or "模型包不可用"),
            ))
    return tuple(entries)


def _platform_info() -> Dict[str, Any]:
    mac_ver = ""
    if sys.platform == "darwin":
        mac_ver = _platform.mac_ver()[0]
    return {
        "os": _platform.system(),
        "os_version": mac_ver or _platform.release(),
        "arch": _platform.machine(),
        "python": _platform.python_version(),
    }


def _git_revision_uncached() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True,
            timeout=10, check=True,
        )
        return out.stdout.decode().strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


class _ProbeCache:
    """AUD-14313：秒级 subprocess 探测（GPU system_profiler / git rev-parse）
    的模块级缓存——每进程只跑一次，UI 侧首次之后的 snapshot 构建/refresh
    不再重复支付探测成本。

    事实在进程生命周期内视为常量（git HEAD / GPU 拓扑运行中不变）；
    ``reset_runtime_probe_cache()`` 供测试与显式刷新使用。注入的
    ``gpu_prober`` 不经过缓存（测试可重复模拟不同 GPU 状态）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gpu: Optional[Dict[str, Any]] = None
        self._git: Optional[str] = None

    def gpu_info(self) -> Dict[str, Any]:
        with self._lock:
            if self._gpu is None:
                self._gpu = detect_host_gpu_info()
            return self._gpu

    def git_revision(self) -> str:
        with self._lock:
            if self._git is None:
                self._git = _git_revision_uncached()
            return self._git

    def clear(self) -> None:
        with self._lock:
            self._gpu = None
            self._git = None


_PROBE_CACHE = _ProbeCache()


def reset_runtime_probe_cache() -> None:
    """清除模块级 GPU/git 探测缓存（测试或显式刷新用）。"""

    _PROBE_CACHE.clear()


def build_runtime_capability_snapshot(
    *,
    model_pack_manager: Any = None,
    gpu_prober: Optional[Callable[[], Dict[str, Any]]] = None,
    gl_prober: Optional[Callable[[], Dict[str, Any]]] = None,
    topos_lib_path: Optional[str] = None,
    now: Optional[str] = None,
) -> RuntimeCapabilitySnapshot:
    """构建一次运行时能力快照。

    Args:
        model_pack_manager: 可选 ``ModelPackManager``；None 时模型包条目为
            ``UNAVAILABLE/MODEL_MANAGER_NOT_PROVIDED``（fail-closed）。
        gpu_prober: 可注入的 GPU 探测（默认 system_profiler）；测试用它模拟
            GPU 不可用。
        gl_prober: 可注入的 GL 探测；默认记录 ``FALLBACK/GL_PROBE_DEFERRED``
            ——快照不创建 GL context，真实 GL 后端由渲染/导出 trail 证明。
        topos_lib_path: 显式 TOPOS dylib 路径（测试注入旧库/坏库）。
        now: 检测时间（ISO 字符串）；默认 UTC now。不参与 snapshot_id。

    AUD-14313：三个独立的慢速探测（TOPOS dylib 解析、默认 GPU 探测、git
    rev-parse）并行执行（串行时 UI 线程首访冻结时间为三者之和）；默认 GPU
    与 git 探测结果 additionally 走模块级缓存（每进程一次）。注入的
    ``gpu_prober`` 直接调用、不缓存，保持测试可模拟性。
    """
    from src.shared.ffmpeg_locator import find_ffmpeg, find_ffprobe

    def _run_gpu_probe() -> Dict[str, Any]:
        if gpu_prober is not None:
            return dict(gpu_prober())
        return dict(_PROBE_CACHE.gpu_info())

    with ThreadPoolExecutor(max_workers=3) as pool:
        codec_future = pool.submit(_probe_topos_codec, topos_lib_path)
        gpu_future = pool.submit(_run_gpu_probe)
        git_future = pool.submit(_PROBE_CACHE.git_revision)
        codec_entry = codec_future.result()
        gpu = gpu_future.result()
        git_revision = git_future.result()
    gpu_entry = RuntimeCapabilityEntry(
        component_id="gpu.display",
        state=(RuntimeCapabilityState.AVAILABLE if gpu.get("status") == "AVAILABLE"
               else RuntimeCapabilityState.UNAVAILABLE),
        reason_code=gpu.get("reason_code"),
        detail=gpu.get("detail"),
        info={"devices": gpu.get("devices", [])},
    )
    metal = metal_info_from_gpu(gpu)
    metal_entry = RuntimeCapabilityEntry(
        component_id="gpu.metal",
        state=(RuntimeCapabilityState.AVAILABLE if metal.get("status") == "AVAILABLE"
               else RuntimeCapabilityState.UNAVAILABLE),
        reason_code=metal.get("reason_code"),
        detail=metal.get("detail"),
    )
    if gl_prober is not None:
        gl = gl_prober()
        gl_entry = RuntimeCapabilityEntry(
            component_id="render.gl",
            state=(RuntimeCapabilityState.AVAILABLE
                   if gl.get("status") == "AVAILABLE"
                   else RuntimeCapabilityState.UNAVAILABLE),
            reason_code=gl.get("reason_code"),
            detail=gl.get("detail"),
            info={k: v for k, v in gl.items()
                  if k not in ("status", "reason_code", "detail")},
        )
    else:
        gl_entry = RuntimeCapabilityEntry(
            component_id="render.gl",
            state=RuntimeCapabilityState.FALLBACK,
            reason_code="GL_PROBE_DEFERRED_TO_RUNTIME",
            detail="快照不创建 GL context；GL 渲染后端由渲染/导出 trail 证明",
        )

    entries: Tuple[RuntimeCapabilityEntry, ...] = (
        _probe_pyav(),
        _probe_ff_cli("ffmpeg", find_ffmpeg),
        _probe_ff_cli("ffprobe", find_ffprobe),
        codec_entry,
        _probe_topos_profiles(codec_entry),
        gpu_entry,
        metal_entry,
        gl_entry,
        _probe_videotoolbox(),
        *_probe_model_packs(model_pack_manager),
    )
    return RuntimeCapabilitySnapshot(
        entries=entries,
        platform=_platform_info(),
        git_revision=git_revision,
        detected_at=now or time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime()),
    )
