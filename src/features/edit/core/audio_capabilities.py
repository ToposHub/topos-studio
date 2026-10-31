"""
Audio backend capabilities detection (AM-01 阻断性修复).

单一真相源：聚合音频处理链所依赖的可选后端（PyAV、SciPy）和音频输出设备的可用性探测。

设计动机
--------
剪輯页的 Strip EQ（`audio_dsp.py`）与 LUFS 计量（`loudness_meter.py`）依赖 `scipy.signal`；
缺少 scipy 时二者会**静默旁路**——`is_active` 返回 false、`process` 直接返回原样本，
但 UI 仍把"已开启"的 EQ badge 显示成激活态、LUFS 表显示为底部值，用户无法区分
"信号静音"与"后端不可用"。详见 docs/edit_audio_mixer_audit_and_optimization_plan_2026-07-29.md AM-01。

本模块把分散在三处（`audio_dsp.py`、`loudness_meter.py`、`audio_playback.py`）的
`_SCIPY_AVAILABLE` 定义收口到单一模块，并提供：

- `AudioCapabilities`：探测结果快照（不可变），含每项依赖的 `available`/`reason`
- `probe_audio_capabilities()`：执行一次探测（轻量、幂等、可复用）
- 模块级 `get_audio_capabilities()`：惰性缓存单例，供 DSP 层与 UI 层共用

线程安全：探测本身无共享可变状态；缓存单例用模块级变量 + 锁保护，
探测完成后只读访问，DSP/UI 层可自由读取。

注意：本模块故意保持**零重量级依赖**（仅 import numpy/标准库做能力探测），
因此即便 numpy 缺失也能给出降级结论，避免能力探测器自身成为新的失败点。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CapabilityResult:
    """单项依赖的探测结果。

    Attributes:
        available: 该后端当前是否可用。
        reason: 不可用时的原因（缺失模块名/异常摘要），可用时为 None。
            UI 层可据此向用户解释为何 EQ/LUFS 被旁路。
        unknown: True 表示该项**从未探测**（如 DSP 路径以
            ``check_output_device=False`` 填充缓存时的输出设备）。
            三态语义（AUD-15203）：unknown ≠ available——"未探测"不得被
            当作"可用"，也不计入 ``disabled_components``（那意味着
            "不可用"）。``available`` 在 unknown 时恒为 False。
    """

    available: bool
    reason: Optional[str] = None
    unknown: bool = False


@dataclass(frozen=True)
class AudioCapabilities:
    """音频后端能力快照（不可变）。

    所有字段在 `probe_audio_capabilities()` 调用时一次性填充，之后只读。
    消费方（DSP 层、UI 层、启动诊断）应通过 `get_audio_capabilities()` 获取缓存实例，
    而非各自重复 import-guard，避免定义漂移。

    字段语义：
        scipy: Strip EQ / LUFS / 重采样的核心后端。缺失时 EQ 与 LUFS 静默旁路。
        pyav: 音视频解码/编码后端（实时播放与导出）。缺失时剪辑页音频基本不可用。
        numpy: 数值计算基础。几乎不可能缺失，缺失则音频链整体不可用。
        output_device: 是否至少能找到一个可用音频输出设备（QAudioSink 能否建立）。
            以 ``check_output_device=False`` 探测时为 unknown 三态
            （available=False + unknown=True），见 ``CapabilityResult.unknown``。
    """

    scipy: CapabilityResult = field(default_factory=lambda: CapabilityResult(False))
    pyav: CapabilityResult = field(default_factory=lambda: CapabilityResult(False))
    numpy: CapabilityResult = field(default_factory=lambda: CapabilityResult(False))
    output_device: CapabilityResult = field(default_factory=lambda: CapabilityResult(False))

    @property
    def eq_lufs_available(self) -> bool:
        """Strip EQ 与 LUFS 是否真正生效（而非静默旁路）。

        等价于旧代码中分散的 `_SCIPY_AVAILABLE`，DSP 层用它决定是否短路。
        """
        return self.scipy.available

    @property
    def all_critical_available(self) -> bool:
        """剪辑页音频链的核心依赖是否齐全（numpy + pyav + scipy）。

        启动诊断用它决定是否弹出降级横幅。
        """
        return self.numpy.available and self.pyav.available and self.scipy.available

    def disabled_components(self) -> list[str]:
        """返回被旁路的音频组件名列表（用于 UI/日志展示）。

        顺序固定以保证可测试。unknown（从未探测）≠ 不可用，不计入
        （AUD-15203 三态区分）。
        """
        out: list[str] = []
        if not self.numpy.available:
            out.append("numpy (音频数值计算)")
        if not self.pyav.available:
            out.append("PyAV (音频解码/编码)")
        if not self.scipy.available:
            out.append("SciPy (EQ/LUFS/重采样)")
        if not self.output_device.available and not self.output_device.unknown:
            out.append("Audio Output Device (音频输出设备)")
        return out


# ─────────────────────────────────────────────────────────────────────────────
# 探测实现
# ─────────────────────────────────────────────────────────────────────────────

def _probe_numpy() -> CapabilityResult:
    try:
        import numpy  # noqa: F401
        return CapabilityResult(True)
    except ImportError as e:
        return CapabilityResult(False, f"numpy 不可用: {e}")
    except Exception as e:  # pragma: no cover - 极端环境兜底
        return CapabilityResult(False, f"numpy 探测异常: {e}")


def _probe_pyav() -> CapabilityResult:
    try:
        import av  # noqa: F401
        return CapabilityResult(True)
    except ImportError as e:
        return CapabilityResult(False, f"PyAV (av) 不可用: {e}")
    except Exception as e:  # pragma: no cover
        return CapabilityResult(False, f"PyAV 探测异常: {e}")


def _probe_scipy() -> CapabilityResult:
    """探测 scipy.signal.lfilter（EQ/LUFS 的实际依赖点）。

    与 DSP 层使用同一个导入路径，避免"import scipy 成功但 signal 子模块损坏"的漏检。
    """
    try:
        from scipy.signal import lfilter  # noqa: F401
        return CapabilityResult(True)
    except ImportError as e:
        return CapabilityResult(False, f"scipy.signal 不可用: {e}")
    except Exception as e:  # pragma: no cover
        return CapabilityResult(False, f"scipy 探测异常: {e}")


def _probe_output_device() -> CapabilityResult:
    """探测是否存在可用的音频输出设备。

    通过 QtMultimedia 的 QMediaDevices 枚举输出端点。Qt 不可用时（headless 测试环境）
    返回 unavailable 但不视为致命——离线渲染/分析路径不依赖输出设备。
    """
    try:
        from PyQt6.QtMultimedia import QMediaDevices  # type: ignore
        outputs = QMediaDevices.audioOutputs()
        if outputs:
            return CapabilityResult(True)
        return CapabilityResult(False, "未发现音频输出设备")
    except ImportError:
        # headless / 无 Qt 环境：无法判定，标记为未知（不阻断 DSP 链）
        return CapabilityResult(False, "QtMultimedia 不可用，无法检测输出设备")
    except Exception as e:  # pragma: no cover
        return CapabilityResult(False, f"输出设备探测异常: {e}")


def probe_audio_capabilities(check_output_device: bool = True) -> AudioCapabilities:
    """执行一次音频后端能力探测。

    Args:
        check_output_device: 是否探测输出设备。headless 单测可置 False 跳过 Qt 依赖。

    Returns:
        不可变的 `AudioCapabilities` 快照。
    """
    # AUD-15203：跳过的设备探测不得记为 available=True（未知 ≠ 可用）。
    # 用 unknown 三态占位；需要真实结果时以 check_output_device=True 重探测
    # （get_audio_capabilities 会把 unknown 缓存升级为探测结果）。
    output_device = (
        _probe_output_device() if check_output_device
        else CapabilityResult(False, "跳过探测（unknown）", unknown=True)
    )
    return AudioCapabilities(
        numpy=_probe_numpy(),
        pyav=_probe_pyav(),
        scipy=_probe_scipy(),
        output_device=output_device,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 惰性缓存单例（DSP/UI 共用的真相源）
# ─────────────────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_cached: Optional[AudioCapabilities] = None


def get_audio_capabilities(check_output_device: bool = False) -> AudioCapabilities:
    """返回缓存的音频能力快照，首次调用时惰性探测。

    DSP 热路径（`StripEQFilter.is_active`、`LoudnessMeter.process`）应调用本函数，
    而非各自 import-guard——确保探测逻辑单一、可被启动诊断覆盖。

    Args:
        check_output_device: 默认 False 以避免 DSP 路径触发 Qt 依赖。输出设备
            探测应由启动诊断/健康报告（UI 线程）以 True 请求。
            AUD-15203：True 请求不再被缓存静默忽略——若现有缓存的输出设备
            是 unknown（跳过探测的占位），本调用会重探测并把缓存升级为
            真实结果；缓存已含真实设备结果时直接复用（探测幂等轻量）。

    Note:
        返回结果在进程内缓存。依赖在运行时被安装/卸载属于罕见场景，
        如需重新探测调用 `reset_audio_capabilities_cache()`。
    """
    global _cached
    snapshot = _cached
    if snapshot is None:
        with _cache_lock:
            if _cached is None:
                _cached = probe_audio_capabilities(check_output_device=check_output_device)
            snapshot = _cached
    if check_output_device and snapshot.output_device.unknown:
        # 缓存不含设备探测结果（unknown 占位）→ 升级缓存为真实探测。
        with _cache_lock:
            current = _cached
            # AUD-15204: get/reset 并发时 `current` 可能为 None（reset 在两次
            # 读取之间清空缓存）——此时必须就地补探测，绝不能把 None 返回给
            # 调用方（DSP 热路径会对返回值直接取属性）。
            if current is None or current.output_device.unknown:
                current = probe_audio_capabilities(check_output_device=True)
                _cached = current
            snapshot = current
    return snapshot


def reset_audio_capabilities_cache() -> None:
    """清除缓存，强制下次 `get_audio_capabilities()` 重新探测。

    主要供测试与显式重检测使用。
    """
    global _cached
    with _cache_lock:
        _cached = None


def is_scipy_available_for_dsp() -> bool:
    """DSP 层便捷访问器：scipy 是否可用于 EQ/LUFS。

    等价语义：`get_audio_capabilities().eq_lufs_available`，但避免在 hot path 上
    反复构造属性访问链。内部仍是缓存的 O(1) 查表。
    """
    return get_audio_capabilities().eq_lufs_available
