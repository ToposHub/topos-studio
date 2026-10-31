"""Cryptomatte Select node — pick object IDs and extract a matte.

Consumer half of the Cryptomatte pair: the generator side renders the
OBJECT_ID AOV (scene3d ``object_id`` output, 24-bit allocator ids encoded
across RGB/255 — ``render3d/aov.py``) and emits the murmurhash manifest
(``render3d/cryptomatte.py``); this node matches a *selection set* of ids
against the ID texture and outputs a matte (or the input isolated by it).

Two id encodings are supported:

* ``rgba24``  — this project's allocator encode (id = r·1 + g·256 + b·65536
  from 8-bit channels).  Integer comparison, tolerance 0.5.
* ``float_r`` — float32 bit-preserving Cryptomatte id carried in the red
  channel (the standard direction for EXR round-trips).  Comparison,
  tolerance 1e-6.

Durable state is stored as BOTH ids and names plus a manifest name
snapshot: when a new manifest arrives the selection is re-resolved by name
(``float_r`` mode via ``cryptomatte_float_id``); names that cannot be
re-resolved keep their ids and are reported by ``offline_names``.
"""

import json
import math
import struct
from typing import Any, Dict, List, Optional

from src.features.composite.render3d.cryptomatte import uint32_to_safe_float

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

from ..render3d.cryptomatte import CryptomatteManifest

# Shader uniform array capacity — selections beyond this are ignored.
MAX_SELECTED = 32

ID_ENCODINGS = ["rgba24", "float_r"]
OUTPUT_MODES = ["matte", "isolate"]


def decode_rgba24_id(rgba: tuple) -> int:
    """Decode this project's 24-bit allocator id from an RGBA tuple."""
    r, g, b = rgba[0], rgba[1], rgba[2]
    return (int(round(float(r) * 255.0))
            + int(round(float(g) * 255.0)) * 256
            + int(round(float(b) * 255.0)) * 65536)


class CryptomatteSelectNode(Node):
    """Extract a matte for a picked set of Cryptomatte object ids."""

    node_type = "cryptomatte_select"
    node_title = "Cryptomatte Select"
    i18n_title_key = "nodes.cryptomatte_select.title"
    header_color = "#5a6a8a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "cryptomatte", "crypto", "id matte", "id select", "pick",
        "Cryptomatte 抠选", "ID 抠像", "物体抠像", "ID 拾取",
    ]
    DESCRIPTION = "按物体 ID 从 Cryptomatte/OBJECT_ID AOV 抠出 matte"
    TAGS = ["keyer", "cryptomatte", "matte", "3d", "aov"]

    PARAM_DEFS = [
        ParamDef("id_encoding", "option", "rgba24", labels=ID_ENCODINGS,
                 group="Cryptomatte"),
        ParamDef("output_mode", "option", "matte", labels=OUTPUT_MODES,
                 group="Cryptomatte"),
        # Hidden durable state (JSON strings — UI edits via the pick API).
        ParamDef("selected_ids", "str", "[]", group="Cryptomatte",
                 hidden=True),
        ParamDef("selected_names", "str", "[]", group="Cryptomatte",
                 hidden=True),
        ParamDef("manifest", "str", "{}", group="Cryptomatte", hidden=True),
        ParamDef("cache_to_disk", "bool", False, group="Cache"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "id_encoding": "rgba24",
            "output_mode": "matte",
            "selected_ids": "[]",
            "selected_names": "[]",
            "manifest": "{}",
            "cache_to_disk": False,
        }

    # ───────────────────────────────────────────────────────────────────
    # Selection API (used by the property panel / viewer pick path)
    # ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _decode_json_list(raw: Any) -> list:
        """N-11：JSON 只验证语法不验证结构。

        持久化字段是外部可改数据——``null``/数字/对象都能通过
        ``json.loads``，随后在迭代/取属性时抛 TypeError/AttributeError
        逃出 getter。统一解码：只接受 list 结构，其余返回空并让调用方
        得到空选区（工程原 payload 保留，可修复后重试），不吞其他异常。
        """
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else None
        except (TypeError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def get_selected_ids(self) -> List[float]:
        ids = self._decode_json_list(self.parameters.get("selected_ids", "[]"))
        # N-10/N-11：非有限 id（遗留 NaN 工程）与非法类型过滤掉，
        # 不进入位模式键（NaN 位模式无法稳定匹配）。
        return [float(v) for v in ids
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(float(v))]

    def get_selected_names(self) -> List[str]:
        names = self._decode_json_list(
            self.parameters.get("selected_names", "[]"))
        return [str(v) for v in names]

    def get_manifest(self) -> Dict[str, str]:
        try:
            data = json.loads(self.parameters.get("manifest", "{}"))
        except (TypeError, ValueError):
            data = {}
        # N-11：manifest 必须是 dict（"[]" 等合法 JSON 错误结构不再在
        # .items() 上抛 AttributeError）。
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()
                if isinstance(v, str)}

    @staticmethod
    def _id_key(object_id: float) -> int:
        """N-09：选区身份 = float 位模式（uint32），不是数值距离。

        Cryptomatte ID 是 32 位 hash 的位模式，浮点距离比较会误伤：
        绝对 epsilon（1e-9）把大量不同的 tiny-hash ID 连同背景 0 一起
        误判为相同（object_0 与 object_3 实测无法同时选中）。
        """
        return struct.unpack("<I", struct.pack("<f", float(object_id)))[0]

    def _write_selection(self, ids: List[float], names: List[str]) -> None:
        # N-10：allow_nan=False——安全转换后不应出现非有限值，出现即
        # 显式失败而不是写非标准 JSON。
        self.set_parameter("selected_ids",
                           json.dumps(ids[:MAX_SELECTED], allow_nan=False))
        self.set_parameter("selected_names",
                           json.dumps(names[:MAX_SELECTED],
                                      ensure_ascii=False))

    def add_selection(self, object_id: float, name: str = "") -> None:
        """Add an id to the selection set (idempotent on bit pattern)."""
        key = self._id_key(object_id)
        ids = self.get_selected_ids()
        if any(self._id_key(i) == key for i in ids):
            return
        if len(ids) >= MAX_SELECTED:
            return
        ids.append(float(object_id))
        names = self.get_selected_names()
        while len(names) < len(ids):
            names.append("")
        names[len(ids) - 1] = str(name or "")
        self._write_selection(ids, names)

    def remove_selection(self, object_id: float) -> None:
        key = self._id_key(object_id)
        ids = self.get_selected_ids()
        names = self.get_selected_names()
        # N-11：按 zip 位置配对——ids.index() 在近似值上会命中第一个
        # 同值项，名称错位；不等长时旧索引式还会越界。
        keep_ids: List[float] = []
        keep_names: List[str] = []
        for oid, name in zip(ids, names):
            if self._id_key(oid) != key:
                keep_ids.append(oid)
                keep_names.append(name or "")
        self._write_selection(keep_ids, keep_names)

    def toggle_selection(self, object_id: float, name: str = "") -> bool:
        """Add/remove an id; returns True when the id is now selected."""
        key = self._id_key(object_id)
        ids = self.get_selected_ids()
        if any(self._id_key(i) == key for i in ids):
            self.remove_selection(object_id)
            return False
        self.add_selection(object_id, name)
        return True

    def clear_selection(self) -> None:
        self._write_selection([], [])

    def set_manifest(self, manifest: Any) -> None:
        """Install a manifest ({hex_id: name} or CryptomatteManifest) and
        re-resolve the selection by name."""
        if isinstance(manifest, CryptomatteManifest):
            manifest = manifest.manifest
        mapping = {str(k): str(v) for k, v in dict(manifest or {}).items()
                   if isinstance(v, str)}
        self.set_parameter("manifest", json.dumps(
            mapping, ensure_ascii=False, sort_keys=True))
        self.remap_selection()

    def remap_selection(self) -> List[str]:
        """Re-resolve selected names against the current manifest.

        Returns the names that could NOT be re-resolved (kept by id).
        ``float_r`` selections are remapped through the manifest hash;
        ``rgba24`` allocator ids have no manifest representation and are
        kept as-is.
        """
        manifest = self.get_manifest()
        name_to_hex = {name: hex_id for hex_id, name in manifest.items()}
        ids = self.get_selected_ids()
        names = self.get_selected_names()
        while len(names) < len(ids):
            names.append("")
        offline: List[str] = []
        for index, name in enumerate(names):
            if not name or name not in name_to_hex:
                if name:
                    offline.append(name)
                continue
            if self.parameters.get("id_encoding", "rgba24") == "float_r":
                hex_id = name_to_hex[name]
                try:
                    hash_value = int(hex_id, 16)
                except ValueError:
                    offline.append(name)
                    continue
                # N-10：与 manifest/AOV 同一安全 float 转换——旧位
                # reinterpret 会让 remap 后的 id 与拾取到的 id 符号不同。
                float_id = uint32_to_safe_float(hash_value)
                if index < len(ids):
                    ids[index] = float_id
        self._write_selection(ids, names)
        return offline

    def name_for_id(self, object_id: float) -> str:
        """Reverse-lookup a display name for a float id via the manifest.

        N-09：按位模式（uint32）比较——manifest hash 经安全 float 转换，
        数值距离/相等比较在 tiny-hash 区间会误命中。
        """
        manifest = self.get_manifest()
        target_key = self._id_key(object_id)
        for hex_id, name in manifest.items():
            try:
                hash_value = int(hex_id, 16)
            except ValueError:
                continue
            if self._id_key(uint32_to_safe_float(hash_value)) == target_key:
                return name
        return ""

    def pick_pixel(self, rgba: tuple) -> float:
        """Decode an id from a viewer pixel readback (pick path entry)."""
        encoding = self.parameters.get("id_encoding", "rgba24")
        if encoding == "float_r":
            return float(rgba[0])
        return float(decode_rgba24_id(rgba))

    # ───────────────────────────────────────────────────────────────────
    # Geometry contract (per-pixel)
    # ───────────────────────────────────────────────────────────────────

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        return {"image": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    # ───────────────────────────────────────────────────────────────────
    # Evaluation
    # ───────────────────────────────────────────────────────────────────

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Emit the renderer-neutral Cryptomatte Select effect marker."""
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}

        ids = self.get_selected_ids()
        encoding = self.parameters.get("id_encoding", "rgba24")
        if encoding not in ID_ENCODINGS:
            encoding = "rgba24"
        output_mode = self.parameters.get("output_mode", "matte")
        if output_mode not in OUTPUT_MODES:
            output_mode = "matte"

        # With nothing selected the matte is empty — still emit a valid
        # result so downstream connections stay live (black matte).
        clamped = ids[:MAX_SELECTED]

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=True,
                color_space=image.color_space,
            ),
            "_effect_type": "cryptomatte_select",
            "_effect_input": image,
            "_effect_params": {
                "id_encoding": encoding,
                "output_mode": output_mode,
                "selected_ids": clamped,
            },
        }
