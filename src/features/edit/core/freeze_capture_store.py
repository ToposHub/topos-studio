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
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

#: 元数据 schema 版本（捕获格式演进时递增；旧格式保留可读）
FREEZE_CAPTURE_META_VERSION = 1

_STORE_DIRNAME = "freeze_captures"
_CAPTURE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}")


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
        if not _CAPTURE_ID.fullmatch(cid):
            return False
        hist = capture.get("history")
        if (not isinstance(hist, np.ndarray) or hist.ndim != 2 or hist.dtype.kind not in "fiu"
                or not np.isfinite(hist).all()):
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
        if (not 1 <= meta["channels"] <= 2 or not 1.0 <= meta["sample_rate"] <= 384000.0
                or not 0 < meta["length"] <= max(1024, int(8 * meta["sample_rate"]))
                or meta["anchor_sample"] <= 0):
            return False
        metadata = json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8")
        _atomic_write_bytes(self._dir / f"{cid}.f32", pcm.tobytes())
        _atomic_write_bytes(self._dir / f"{cid}.json", metadata)
        return True

    # ── 装载 ──
    def load(self, capture_id: str) -> Optional[Dict[str, Any]]:
        """装载捕获；缺失/损坏返回 None 并记录诊断（不静默伪造）。"""
        cid = str(capture_id or "")
        if not _CAPTURE_ID.fullmatch(cid):
            logger.warning("invalid freeze capture ID %r", cid)
            return None
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
            if (not isinstance(meta, dict) or meta.get("id", cid) != cid
                    or meta.get("meta_version", 1) != FREEZE_CAPTURE_META_VERSION):
                logger.warning("freeze capture %r metadata is invalid", cid)
                return None
            ch = int(meta.get("channels", 2))
            length = int(meta.get("length", 0))
            sample_rate = float(meta.get("sample_rate", 48000.0))
            anchor = int(meta.get("anchor_sample", 0))
            seed = int(meta.get("seed", 0))
            params = dict(meta.get("params", {}) or {})
            if not 1 <= ch <= 2 or not 1.0 <= sample_rate <= 384000.0 or anchor <= 0:
                logger.warning("freeze capture %r channel layout, rate or anchor is invalid", cid)
                return None
            expected = ch * length * np.dtype(np.float32).itemsize
            size = pcm_path.stat().st_size
            if not 0 < length <= max(1024, int(8 * sample_rate)) or expected != size:
                logger.warning(
                    "freeze capture %r size mismatch: %d != %d",
                    cid, size, expected)
                return None
            raw = np.frombuffer(pcm_path.read_bytes(), dtype=np.float32)
            hist = raw.reshape(ch, length).astype(np.float64)
            if not np.isfinite(hist).all():
                logger.warning("freeze capture %r contains invalid samples", cid)
                return None
        except (OSError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("freeze capture %r unreadable: %r", cid, exc)
            return None
        return {
            "id": cid,
            "anchor": anchor,
            "seed": seed,
            "sample_rate": sample_rate,
            "history": hist,
            "params": params,
        }

    def remove(self, capture_id: str) -> bool:
        if not _CAPTURE_ID.fullmatch(str(capture_id or "")):
            return False
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
    effects = getattr(chain_params, "effects", ()) or ()
    entries = ((e.effect_type, e.params, e.enabled, e.instance_id) for e in effects)
    return apply_freeze_entries(dsp_chain, entries, registry)


def apply_freeze_entries(dsp_chain: Any, entries: Any,
                         registry: Dict[str, Dict[str, Any]]) -> int:
    """Resolve captures for both track snapshots and the clip chain entry format."""
    installed = 0
    for entry in entries:
        if len(entry) < 4 or entry[0] != "spectral_mangler":
            continue
        _, params, _, instance_id = entry[:4]
        cid = params.get("freeze_capture_id") if isinstance(params, dict) else None
        if not cid:
            continue
        if not isinstance(cid, str) or not _CAPTURE_ID.fullmatch(cid):
            logger.warning("invalid freeze capture reference on instance %s", instance_id)
            continue
        capture = registry.get(cid)
        if capture is None:
            logger.warning(
                "freeze capture %r not in registry; instance %s replays "
                "current history", cid, instance_id)
            continue
        try:
            if dsp_chain.install_freeze_capture_by_id(instance_id, capture):
                installed += 1
            else:
                logger.warning("freeze capture %r rejected by instance %s", cid, instance_id)
        except Exception as exc:
            logger.warning("freeze capture %r install failed: %r", cid, exc)
    return installed


__all__ = [
    "FREEZE_CAPTURE_META_VERSION",
    "FreezeCaptureStore",
    "freeze_registry_from_project",
    "apply_freeze_registry",
    "apply_freeze_entries",
]
