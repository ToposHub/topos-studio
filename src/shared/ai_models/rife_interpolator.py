"""RIFE-style frame interpolation executor (C2).

Runs an ONNX RIFE v4 network (registered in ``model_manager`` as
``rife_interpolate``) to synthesize the in-between frame
``out = net(frame_a, frame_b, t)``.  Contract details:

* **Injectable session** — tests pass a stub session so the whole chain
  is mockable; production resolves the ONNX model through
  ``model_manager`` (never downloaded silently — the model manager's
  download UI owns that decision).
* **Deterministic** — fixed ``t`` produces byte-identical output.
* **Graceful downgrade** — without a usable model the executor falls
  back to a linear blend and REPORTS it via ``self.used_fallback``, so
  callers/UI can show the quality tier.  (RIFE beats linear blending on
  real motion; the fallback only keeps the node usable.)
* **hd_mode** tiles large frames through the same session to bound
  memory (the tiles reuse the inference engine's overlap-free split).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

from src.shared.ai_models.onnx_env import (
    get_configured_providers,
    get_session_options,
)

logger = logging.getLogger(__name__)

MODEL_NAME = "rife_interpolate"

# Sessions are expensive (model load + provider init) and nodes evaluate
# per frame — cache one InferenceSession per resolved model path for the
# lifetime of the process.
# AUD-31815：模块级缓存由 ``_SESSION_CACHE_LOCK`` 保护——两个节点线程同
# 模型并发首载时，seq 计算式的 check-then-act 不再各建一份会话（持锁创建：
# 首载罕见且不可中断，正确性优先）。
_SESSION_CACHE: dict = {}
_SESSION_CACHE_LOCK = threading.Lock()


class RifeInterpolator:
    """ONNX RIFE wrapper with stub-injectable session."""

    def __init__(self, session: Any = None,
                 model_manager: Any = None):
        self._session = session
        self._model_manager = model_manager
        self.used_fallback = False

    # ── Session resolution ─────────────────────────────────────────────
    def has_model(self) -> bool:
        """True when a real ONNX session can be resolved.

        Cheap probe (cached session or model path lookup) — lets callers
        take a GPU fallback path BEFORE reading frames back to CPU (P2-9).
        """
        return self._resolve_session() is not None

    def _resolve_session(self) -> Any:
        if self._session is not None:
            return self._session
        try:
            # get_model_path is a manager METHOD (module exposes the
            # singleton through get_model_manager()), not a module attr.
            from src.shared.ai_models.model_manager import get_model_manager
            path = get_model_manager().get_model_path(MODEL_NAME)
            if path:
                key = str(path)
                import onnxruntime as ort
                # AUD-31815：不再硬编码 CPUExecutionProvider——走 onnx_env
                # 的统一 provider 决策（尊重 gpu_acceleration / ai_gpu_device
                # 用户偏好，CPU 恒为保底），会话选项（线程数等）同样走统一
                # 配置。创建在锁内：并发同模型首载只建一份会话。
                with _SESSION_CACHE_LOCK:
                    cached = _SESSION_CACHE.get(key)
                    if cached is not None:
                        self._session = cached
                        return self._session
                    providers = get_configured_providers() or [
                        "CPUExecutionProvider"
                    ]
                    session = ort.InferenceSession(
                        key,
                        sess_options=get_session_options(),
                        providers=providers,
                    )
                    _SESSION_CACHE[key] = session
                    self._session = session
                    return self._session
        except Exception as exc:
            logger.warning("RIFE session unavailable: %s", exc)
        return None

    # ── Core ───────────────────────────────────────────────────────────
    def interpolate(self, frame_a: "np.ndarray", frame_b: "np.ndarray",
                    t: float, hd_mode: bool = False) -> "np.ndarray":
        """Synthesize the frame at position ``t`` (0=A, 1=B).

        Inputs are RGB float32 [0..1]; output is the same shape/dtype.
        """
        t = min(1.0, max(0.0, float(t)))
        a = np.ascontiguousarray(frame_a.astype(np.float32))
        b = np.ascontiguousarray(frame_b.astype(np.float32))
        if t <= 0.0:
            return a
        if t >= 1.0:
            return b

        session = self._resolve_session()
        if session is None:
            self.used_fallback = True
            return (a * (1.0 - t) + b * t).astype(np.float32)

        try:
            if hd_mode and max(a.shape[:2]) > 1280:
                self.used_fallback = False
                return self._tiled(session, a, b, t)
            self.used_fallback = False
            return self._run_session(session, a, b, t)
        except Exception as exc:
            logger.warning("RIFE inference failed (%s) — linear fallback",
                           exc)
            self.used_fallback = True
            return (a * (1.0 - t) + b * t).astype(np.float32)

    def _run_session(self, session: Any, a: "np.ndarray", b: "np.ndarray",
                     t: float) -> "np.ndarray":
        """Feed the standard RIFE v4 input contract (NCHW + timestep)."""
        h, w = a.shape[:2]
        inp_a = np.transpose(a, (2, 0, 1))[None, ...]
        inp_b = np.transpose(b, (2, 0, 1))[None, ...]
        timestep = np.asarray([t], dtype=np.float32)
        outputs = session.run(None, {
            session.get_inputs()[0].name: inp_a,
            session.get_inputs()[1].name: inp_b,
            session.get_inputs()[2].name: timestep,
        })
        out = np.clip(outputs[0][0], 0.0, 1.0)
        return np.transpose(out, (1, 2, 0)).astype(np.float32)

    def _tiled(self, session: Any, a: "np.ndarray", b: "np.ndarray",
               t: float, tile: int = 640) -> "np.ndarray":
        """Overlap-free 2D tiling for large frames (hd_mode)."""
        height, width = a.shape[:2]
        out = np.empty_like(a)
        for y0 in range(0, height, tile):
            for x0 in range(0, width, tile):
                y1, x1 = min(y0 + tile, height), min(x0 + tile, width)
                out[y0:y1, x0:x1] = self._run_session(
                    session, a[y0:y1, x0:x1], b[y0:y1, x0:x1], t)
        return out


def cache_signature(frame_a_rev: int, frame_b_rev: int, t: float,
                    hd_mode: bool) -> str:
    """AI-cache signature for one interpolated frame."""
    return (f"rife|{int(frame_a_rev)}|{int(frame_b_rev)}"
            f"|{float(t):.6f}|{int(bool(hd_mode))}")
