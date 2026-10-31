"""L01 组件 manifest：身份/参数/图像/端口时间/能力/资源/验证七组契约字段。

纯 stdlib、可 headless 测试。这是**暂存区拟定契约**（specs/spec_schema.md），
宿主消费由 Mac 集成者做窄适配（patches/ 提案），不是新造万能插件引擎：
- 身份字段对齐 src/features/shared/effects/plugin_identity.py 既有 8 态状态机；
- 参数 ptype 枚举对齐 Composite ParamDef；.tglsl @param 四类是其 ABI 子集；
- 能力值三态（supported/unsupported/untested），默认 untested，声明须有证据。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

PACK_ID = "topos_library_v1"

PARAM_TYPES = frozenset({"float", "int", "bool", "option", "color", "vec2", "vec3"})
KINDS = frozenset({"filter", "transition", "node", "preset"})
STATUSES = frozenset({
    "planned", "component_ready", "integrated", "validated",
    "released", "deferred", "reused",
})
STAGING_STATUSES = frozenset({"planned", "component_ready"})
CAP_VALUES = frozenset({"supported", "unsupported", "untested"})
CAP_AXES = ("page", "path", "backend", "mode")
PAGES = ("color", "edit", "composite")
PATHS = ("preview", "video_export", "lut_bake")
BACKENDS = ("cpu", "gpu_gl", "metal")
MODES = ("static", "animated")


def capability_key(page: str, path: str, backend: str, mode: str) -> str:
    """能力矩阵键：page/path/backend/mode（四轴，specs/spec_schema.md §2）。"""
    return f"{page}/{path}/{backend}/{mode}"


@dataclass(frozen=True)
class ParamSpec:
    """单个参数契约。neutral=True 表示默认值数学恒等（须配恒等测试）。"""

    name: str
    ptype: str
    default: Any
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    options: Tuple[str, ...] = ()
    unit: str = ""
    neutral: bool = False
    animatable: bool = False
    validation: str = ""

    def validate(self) -> List[str]:
        errs: List[str] = []
        if self.ptype not in PARAM_TYPES:
            errs.append(f"param {self.name}: ptype {self.ptype!r} 不在 {sorted(PARAM_TYPES)}")
        if self.ptype == "option":
            if len(self.options) < 2:
                errs.append(f"param {self.name}: option 至少 2 个选项")
            if self.options and self.default not in self.options:
                errs.append(f"param {self.name}: default 必须是选项之一")
        elif self.ptype in {"float", "int"}:
            if not isinstance(self.default, (int, float)) or isinstance(self.default, bool):
                errs.append(f"param {self.name}: 数值型 default 必须是数字")
            else:
                if self.minimum is not None and self.default < self.minimum:
                    errs.append(f"param {self.name}: default < minimum")
                if self.maximum is not None and self.default > self.maximum:
                    errs.append(f"param {self.name}: default > maximum")
                if (self.minimum is not None and self.maximum is not None
                        and self.minimum > self.maximum):
                    errs.append(f"param {self.name}: minimum > maximum")
        elif self.ptype == "bool":
            if not isinstance(self.default, bool):
                errs.append(f"param {self.name}: bool default 必须是布尔")
        elif self.ptype == "color":
            if (not isinstance(self.default, (list, tuple)) or len(self.default) != 3):
                errs.append(f"param {self.name}: color default 必须是 3 元组")
        elif self.ptype in {"vec2", "vec3"}:
            want = 2 if self.ptype == "vec2" else 3
            if (not isinstance(self.default, (list, tuple)) or len(self.default) != want):
                errs.append(f"param {self.name}: {self.ptype} default 必须是 {want} 元组")
        return errs


@dataclass(frozen=True)
class ImageContract:
    """图像域契约（specs/spec_schema.md §1 图像组）。"""

    input_space: str = "scene-linear"
    output_space: str = "scene-linear"
    precision: str = "float32"
    alpha: str = "straight"
    output_range: str = "unbounded"
    sampling_border: str = "clamp-half-texel"
    spatial_unit: str = "frame-normalized"


@dataclass(frozen=True)
class TimeContract:
    """时间/seed 契约。L01 全库默认 static（宿主无 frame/seed uniform）。"""

    time_domain: str = "static"
    time_origin: str = "effect-time-0"
    fps: Tuple[int, int] = (0, 1)
    frame_sampling: str = "n/a"
    seed_policy: str = "fixed-param-default"
    dynamic_gate: str = ""


@dataclass(frozen=True)
class PortSpec:
    name: str
    direction: str  # input | output
    data_type: str  # image | float | vec2 | ...
    role: str = ""
    required: bool = True
    missing_behavior: str = ""


@dataclass(frozen=True)
class ResourceRef:
    kind: str  # procedural | external
    name: str
    license: str
    sha256: str = ""
    missing: str = ""


@dataclass(frozen=True)
class VerificationRef:
    oracle: str = ""
    hand_vectors: str = ""
    gpu_evidence: str = ""
    commands: Tuple[str, ...] = ()
    known_limits: str = ""


@dataclass(frozen=True)
class ComponentManifest:
    component_id: str
    kind: str
    slug: str
    version: str
    status: str
    host_min_contract: str
    dedup: str
    params: Tuple[ParamSpec, ...] = ()
    image: ImageContract = field(default_factory=ImageContract)
    time: TimeContract = field(default_factory=TimeContract)
    ports: Tuple[PortSpec, ...] = ()
    capabilities: Mapping[str, str] = field(default_factory=dict)
    resources: Tuple[ResourceRef, ...] = ()
    verification: VerificationRef = field(default_factory=VerificationRef)
    unknown_version_policy: str = "preserve-payload-warn"

    # ── 校验 ──────────────────────────────────────────────────────────
    def validate(self, staging: bool = True) -> List[str]:
        errs: List[str] = []
        if self.status not in STATUSES:
            errs.append(f"status {self.status!r} 不合法")
        elif staging and self.status not in STAGING_STATUSES:
            errs.append(f"暂存区 manifest 状态只能是 {sorted(STAGING_STATUSES)}，当前 {self.status!r}")
        if self.kind not in KINDS:
            errs.append(f"kind {self.kind!r} 不合法")
        if "/" in self.slug or not self.slug:
            errs.append("slug 不能为空或含 /")
        names = [p.name for p in self.params]
        if len(names) != len(set(names)):
            errs.append("参数名重复")
        for p in self.params:
            errs.extend(p.validate())
        if self.time.time_domain != "static" and not self.time.dynamic_gate:
            errs.append("time_domain 非 static 必须显式登记 dynamic_gate（正式 frame/seed 子门）")
        for key, value in self.capabilities.items():
            parts = key.split("/")
            if len(parts) != len(CAP_AXES):
                errs.append(f"能力键 {key!r} 必须是四轴 page/path/backend/mode")
            elif (parts[0] not in PAGES or parts[1] not in PATHS
                    or parts[2] not in BACKENDS or parts[3] not in MODES):
                errs.append(f"能力键 {key!r} 轴值不合法")
            if value not in CAP_VALUES:
                errs.append(f"能力值 {value!r} 只能是 {sorted(CAP_VALUES)}")
        for r in self.resources:
            if r.kind == "external" and (not r.license or not r.sha256):
                errs.append(f"外部资源 {r.name} 必须登记 license 与 sha256（M20 未收口前默认暂缓）")
        if any(p.neutral for p in self.params) and not (
                self.verification.oracle or self.verification.hand_vectors):
            errs.append("存在 neutral 参数时必须登记恒等验证（oracle 或手算向量）")
        return errs

    # ── 序列化 ────────────────────────────────────────────────────────
    def to_json(self) -> str:
        data = asdict(self)
        data["pack_id"] = PACK_ID
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ComponentManifest":
        data = json.loads(text)
        data.pop("pack_id", None)

        def _retuple(p: Dict[str, Any]) -> ParamSpec:
            p = dict(p)
            if isinstance(p.get("options"), list):
                p["options"] = tuple(p["options"])
            default = p.get("default")
            if p.get("ptype") in {"color", "vec2", "vec3"} and isinstance(default, list):
                p["default"] = tuple(default)
            return ParamSpec(**p)

        data["params"] = tuple(_retuple(p) for p in data.get("params", []))
        data["image"] = ImageContract(**data.get("image", {}))
        time_data = data.get("time", {})
        if "fps" in time_data and not isinstance(time_data["fps"], list):
            errs = "time.fps 必须是 [num, den] 列表"
            raise ValueError(errs)
        if isinstance(time_data.get("fps"), list):
            time_data["fps"] = tuple(time_data["fps"])
        data["time"] = TimeContract(**time_data)
        data["ports"] = tuple(PortSpec(**p) for p in data.get("ports", []))
        data["capabilities"] = dict(data.get("capabilities", {}))
        data["resources"] = tuple(ResourceRef(**r) for r in data.get("resources", []))
        verif = dict(data.get("verification", {}))
        if isinstance(verif.get("commands"), list):
            verif["commands"] = tuple(verif["commands"])
        data["verification"] = VerificationRef(**verif)
        return cls(**data)

    # ── 哈希 ──────────────────────────────────────────────────────────
    def parameter_schema_hash(self) -> str:
        """参数 schema hash：对齐 plugin_identity 的 parameter_schema_hash 语义。"""
        schema = [
            {"name": p.name, "ptype": p.ptype, "default": list(p.default) if isinstance(p.default, tuple) else p.default,
             "minimum": p.minimum, "maximum": p.maximum, "options": list(p.options)}
            for p in self.params
        ]
        canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def content_hash(path_like: Any) -> str:
        """内容 hash：文件字节或字符串的 SHA-256（对齐 plugin content_hash 语义）。"""
        if isinstance(path_like, (bytes, bytearray)):
            return hashlib.sha256(bytes(path_like)).hexdigest()
        digest = hashlib.sha256()
        if isinstance(path_like, str) and "\n" not in path_like and len(path_like) < 4096:
            # 短单行字符串按文件路径处理
            with open(path_like, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        digest.update(str(path_like).encode("utf-8"))
        return digest.hexdigest()


def default_capabilities() -> Dict[str, str]:
    """全矩阵 untested 起点（声明纪律：默认 untested，证据到位才升级）。"""
    return {
        capability_key(page, path, backend, mode): "untested"
        for page in PAGES for path in PATHS
        for backend in BACKENDS for mode in MODES
    }
