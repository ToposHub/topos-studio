"""Effect 用户预设持久化（ST-01，音频成熟化计划）。

效果器对话框（Dynamics/Delay/Reverb/EQ/Panner 等）的用户预设此前只
append 到窗口内列表——关窗口/重启/换工程后丢失（计划 §11 ST-01 缺陷）。
本模块提供跨会话的用户预设存储：

- 存储位置：~/.topos/effect_presets.json（用户级，跨工程）。
- 结构：{"version": 1, "presets": {effect_type: [{preset_id, name, params}]}}。
- 原子写入（临时文件 + os.replace）；损坏单条跳过不拖垮整库；整体不可
  解析时拒绝覆写（保留人工修复机会）——与 mixer_preset 同一防护语义。
- 外部引用（如侧链 source track id）按字符串符号保存，不存机器路径。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _store_path() -> str:
    home = os.path.expanduser("~")
    return os.path.join(home, ".topos", "effect_presets.json")


def _load_raw() -> dict[str, list[dict]] | None:
    """读取原始库；整体不可解析时返回 None（写路径据此拒绝覆写）。"""
    path = _store_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error(f"effect preset library unreadable, keep file intact: {exc!r}")
        return None
    if not isinstance(data, dict):
        logger.error("effect preset library schema invalid (not an object)")
        return None
    presets = data.get("presets", {})
    if not isinstance(presets, dict):
        logger.error("effect preset library schema invalid ('presets' not an object)")
        return None
    return presets


def load_user_presets(effect_type: str) -> list[dict[str, Any]]:
    """加载某效果器类型的用户预设列表。

    单条损坏的预设被跳过（带 warning），不影响其余条目。
    """
    raw = _load_raw()
    if not raw:
        return []
    entries = raw.get(effect_type) or []
    out: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        params = item.get("params")
        if not name or not isinstance(params, dict):
            logger.warning("effect preset: skip corrupted entry in %s", effect_type)
            continue
        # R01：algorithm_version 原样读回（0=未标注→legacy 兼容桶），
        # 应用方据此选择 DSP 路径；不再只在保存时写、加载时丢。
        try:
            av = max(0, int(item.get("algorithm_version", 0) or 0))
        except (TypeError, ValueError):
            av = 0
        out.append({
            "preset_id": str(item.get("preset_id", "")),
            "name": str(name),
            "params": params,
            "algorithm_version": av,
        })
    return out


def save_user_preset(effect_type: str, name: str, params: dict[str, Any],
                     preset_id: str | None = None) -> bool:
    """保存（新增或按 id 替换）一条用户预设，原子落盘。

    Returns:
        True=已持久化；False=库不可读/写盘失败（内存视图仍可用）。
    """
    raw = _load_raw()
    if raw is None:
        logger.error("save_user_preset: library unreadable, skip persisting")
        return False
    from src.features.edit.core.audio_dsp import ALGORITHM_VERSION
    entry = {
        "preset_id": preset_id or str(uuid.uuid4()),
        "name": str(name),
        "algorithm_version": ALGORITHM_VERSION,
        "params": dict(params),
    }
    entries = [e for e in (raw.get(effect_type) or [])
               if isinstance(e, dict) and e.get("preset_id") != entry["preset_id"]]
    entries.append(entry)
    raw[effect_type] = entries

    path = _store_path()
    tmp_path = f"{path}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"version": SCHEMA_VERSION, "presets": raw},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.error(f"save effect preset failed: {exc!r}")
        try:
            os.remove(tmp_path)
        except OSError:
            logger.debug(f"leave-behind temp file {tmp_path} not removable")
        return False


def remove_user_preset(effect_type: str, preset_id: str) -> bool:
    """删除一条用户预设，原子落盘。"""
    raw = _load_raw()
    if raw is None:
        return False
    entries = [e for e in (raw.get(effect_type) or [])
               if isinstance(e, dict) and e.get("preset_id") != preset_id]
    raw[effect_type] = entries
    path = _store_path()
    tmp_path = f"{path}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"version": SCHEMA_VERSION, "presets": raw},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.error(f"remove effect preset failed: {exc!r}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False
