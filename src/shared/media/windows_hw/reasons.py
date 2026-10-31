"""Windows 后端失败与回退的显式 reason 词表（W0-05，计划 §4 的代码级展开）。

硬规则（计划 §4 / .clinerules:11）：每个失败点一个显式 reason 常量，
禁止裸字符串散落；「看似成功实际软解」视为缺陷，不是降级。

reason 形态分两类：

- **原子 reason**：``FallbackReason`` 成员原样使用（如 ``not_windows``）。
- **参数化 reason**：``param(FallbackReason.GPU_PROBE_FAILED, detail)``
  产出 ``"gpu_probe_failed:<detail>"``。消费方用 ``split(":", 1)[0]``
  取回原子 reason 做归类，冒号后的 detail 进诊断链（backend_selector
  trail/reason 语义）。

跨模块约定（供 backend_selector / 诊断 UI 归类）：
前缀即语义，冒号后缀只是上下文 —— 归类永远以前缀为准。
"""

from __future__ import annotations

from enum import Enum


class FallbackReason(str, Enum):
    """计划 §4 回退表的 reason 常量（值即落盘字符串，不得改写）。"""

    # --- 平台 / 环境门（capability.py，已实现的语义收编） ---
    NOT_WINDOWS = "not_windows"
    ENV_ZERO_COPY_OFF = "env_zero_copy_off"
    ENV_FORCE_CPU_UPLOAD = "env_force_cpu_upload"
    NOT_PROBED = "not_probed"

    # --- 探测层 ---
    GPU_PROBE_FAILED = "gpu_probe_failed"  # param: <子原因>
    PROBE_ERROR = "probe_error"            # param: <异常摘要>
    NVIDIA_DRIVER_VERSION_UNAVAILABLE = "nvidia_driver_version_unavailable"
    DRIVER_BELOW_GATE = "driver_below_gate"  # param: <版本>（W1-04 矩阵随附）

    # --- interop 层（W3 前，骨架的唯一诚实 reason） ---
    INTEROP_NOT_YET_IMPLEMENTED = "interop_not_yet_implemented"
    INTEROP_INIT_FAILED = "interop_init_failed"  # param: <阶段>
    WGL_NV_DX_INTEROP_UNAVAILABLE = "wgl_nv_dx_interop_unavailable"
    ADAPTER_MISMATCH = "adapter_mismatch"  # param: <luid 对>（W1-03）

    # --- 解码 / 编码能力层 ---
    CODEC_UNSUPPORTED_BY_HW = "codec_unsupported_by_hw"  # param: <codec/pixfmt>
    ENCODER_HW_UNAVAILABLE = "encoder_hw_unavailable"    # param: <hwaccel>

    # --- compute 层（W4） ---
    CUDA_UNAVAILABLE = "cuda_unavailable"

    # --- 骨架通用（windows_zero_copy_probe / d3d11_decoder） ---
    NOT_YET_IMPLEMENTED = "not_yet_implemented"


def param(reason: FallbackReason, detail: str) -> str:
    """构造参数化 reason：``<原子 reason>:<detail>``。

    detail 内的冒号允许（如 hr 码、异常文本），归类只看第一段。
    """
    return f"{reason.value}:{detail}"


def codec_unsupported_reason(codec: str, pix_fmt: str = "") -> str:
    """``codec_unsupported_by_hw:<codec>`` 或 ``:<codec>/<pixfmt>``。"""
    detail = f"{codec}/{pix_fmt}" if pix_fmt else codec
    return param(FallbackReason.CODEC_UNSUPPORTED_BY_HW, detail)


def reason_prefix(reason: str) -> str:
    """取参数化 reason 的原子前缀（诊断归类用）。"""
    return reason.split(":", 1)[0]
