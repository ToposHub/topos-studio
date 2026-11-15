"""H09：Freeze 捕获资产存储（项目侧 sidecar，raw PCM 不进参数 JSON）。

- 捕获内容 = 颗粒历史环形缓冲（有界 8s 立体声 float32）+ 锚点（绝对
  采样位）/seed/调度参数元数据；
- 存储形态：``<project_dir>/freeze_captures/<capture_id>.f32``（原始
  PCM，原子写）+ ``<capture_id>.json``（元数据）；工程 JSON 只保存
  ``freeze_captures`` 的 id→相对路径/元数据映射（无机器绝对路径）；
- ``FreezeCaptureStore``：save/load/list/remove + 装载校验（缺失/
  尺寸不符 → 明确诊断，preflight 失败不静默）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

#: 元数据 schema 版本（捕获格式演进时递增；旧格式保留可读）
FREEZE_CAPTURE_META_VERSION = 1

_STORE_DIRNAME = "freeze_captures"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class FreezeCaptureStore:
    """项目级 Freeze 捕获资产库（目录 sidecar）。"""

    def __init__(self, project_dir: str) -> None:
        self._dir = Path(project_dir) / _STORE_DIRNAME

    # ── 保存 ──
    def save(self, capture: Dict[str, Any]) -> bool:
        """保存捕获（capture 见 ``_GrainModule.capture_freeze`` 的 dict）。"""
        cid = str(capture.get("id", "") or "")
        if not cid:
            return False
        hist = capture.get("history")
        if not isinstance(hist, np.ndarray) or hist.ndim != 2:
            return False
        pcm = np.ascontiguousarray(hist.astype(np.float32))
        meta = {
            "id": cid,
            "meta_version": FREEZE_CAPTURE_META_VERSION,
            "anchor_sample": int(capture.get("anchor", 0)),
            "seed": int(capture.get("seed", 0)),
            "sample_rate": float(capture.get("sample_rate", 48000.0)),
            "channels": int(pcm.shape[0]),
            "length": int(pcm.shape[1]),
            "params": dict(capture.get("params", {}) or {}),
            "created_at": str(capture.get("created_at", "")),
        }
        _atomic_write_bytes(self._dir / f"{cid}.f32", pcm.tobytes())
        _atomic_write_bytes(
            self._dir / f"{cid}.json",
            json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
        return True

    # ── 装载 ──
    def load(self, capture_id: str) -> Optional[Dict[str, Any]]:
        """装载捕获；缺失/损坏返回 None 并记录诊断（不静默伪造）。"""
        cid = str(capture_id or "")
        meta_path = self._dir / f"{cid}.json"
        pcm_path = self._dir / f"{cid}.f32"
        if not meta_path.exists() or not pcm_path.exists():
            logger.warning(
                "freeze capture %r missing (%s%s)；重放将退化为当前历史",
                cid, "" if meta_path.exists() else " no-meta",
                "" if pcm_path.exists() else " no-pcm")
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ch = int(meta.get("channels", 2))
            length = int(meta.get("length", 0))
            raw = np.frombuffer(pcm_path.read_bytes(), dtype=np.float32)
            if raw.size != ch * length:
                logger.warning(
                    "freeze capture %r size mismatch: %d != %d",
                    cid, raw.size, ch * length)
                return None
            hist = raw.reshape(ch, length).astype(np.float64)
        except Exception as exc:
            logger.warning("freeze capture %r unreadable: %r", cid, exc)
            return None
        return {
            "id": cid,
            "anchor": int(meta.get("anchor_sample", 0)),
            "seed": int(meta.get("seed", 0)),
            "sample_rate": float(meta.get("sample_rate", 48000.0)),
            "history": hist,
            "params": dict(meta.get("params", {}) or {}),
        }

    def remove(self, capture_id: str) -> bool:
        ok = False
        for ext in (".f32", ".json"):
            p = self._dir / f"{capture_id}{ext}"
            if p.exists():
                try:
                    p.unlink()
                    ok = True
                except OSError:
                    pass
        return ok

    def list_ids(self) -> list:
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.f32"))


def freeze_registry_from_project(project: Any) -> Dict[str, Dict[str, Any]]:
    """从工程装载全部捕获（渲染期注册表；缺资产逐条诊断，不中断）。"""
    if project is None:
        return {}
    base = getattr(project, "project_dir", None) or getattr(
        project, "path", None) or ""
    if not base:
        return {}
    store = FreezeCaptureStore(str(base))
    out: Dict[str, Dict[str, Any]] = {}
    meta_ids = set(getattr(project, "freeze_capture_ids", ()) or ())
    for cid in store.list_ids():
        cap = store.load(cid)
        if cap is not None:
            out[cid] = cap
            meta_ids.discard(cid)
    for missing in sorted(meta_ids):
        logger.warning("project freeze capture %r asset missing on disk",
                       missing)
    return out


def apply_freeze_registry(dsp_chain: Any, chain_params: Any,
                          registry: Dict[str, Dict[str, Any]]) -> int:
    """把注册表中的捕获装载进 DSP 链（按实例 ``freeze_capture_id`` 匹配）。

    返回成功安装数；缺资产/类型不符逐条诊断（调用方 preflight 汇总）。
    """
    installed = 0
    effects = getattr(chain_params, "effects", ()) or ()
    for e in effects:
        params = getattr(e, "params", None)
        cid = params.get("freeze_capture_id") if isinstance(params, dict) else None
        if not cid:
            continue
        capture = registry.get(cid)
        if capture is None:
            logger.warning(
                "freeze capture %r not in registry; instance %s replays "
                "current history", cid, getattr(e, "instance_id", ""))
            continue
        try:
            if dsp_chain.install_freeze_capture_by_id(
                    getattr(e, "instance_id", ""), capture):
                installed += 1
        except Exception as exc:
            logger.warning("freeze capture %r install failed: %r", cid, exc)
    return installed


__all__ = [
    "FREEZE_CAPTURE_META_VERSION",
    "FreezeCaptureStore",
    "freeze_registry_from_project",
    "apply_freeze_registry",
]
