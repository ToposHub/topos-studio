"""P6.1 ComfyUI 兼容层数据模型（plan §14.1 step 1 / §1.4.2）。

> 为 ``ComfyConnectionProfile``、``ComfyWorkflowSnapshot``、``ComfyInputBinding``、
> ``ComfyJobRef`` 写纯 Python dataclass 和 JSON schema 测试。

定义 ComfyUI C0–C5 分层兼容所需的**纯 Python** 数据契约（plan §1.4.2）：

- :class:`ComfyConnectionProfile` — 本机 / 云 Comfy server 连接配置（C1 Provider 级）。
- :class:`ComfyInputSlot` / :class:`ComfyInputBinding` — 把 Topos artifact 绑定到
  ComfyUI workflow 的**预先声明**输入槽位（§9.4 #2：只允许填充声明的槽位，禁止注入
  任意 class_type）。
- :class:`ComfyModelRequirement` / :class:`ComfyCustomNodeRequirement` /
  :class:`ComfyNodeSummary` — workflow 的依赖报告（C3 可视化级，§14.1 step 3）。
- :class:`ComfyWorkflowSnapshot` — ComfyUI **API-format JSON** 的只读快照（C2 工作流
  执行级）。``from_api_format`` **不执行**任何 custom node 代码（红线 §14.1 step 3），
  只做静态元数据抽取。
- :class:`ComfyJobRef` — 外部 Comfy 异步 job 的引用（C2 执行级；不持有任何 Topos 内部
  对象）。
- :class:`ComfyOutputArtifact` — Comfy 输出 artifact 的描述（含 hash；§9.4 #5）。

红线（plan §1.4.2 / §0 / §14.1 / §9.4）：

- **不存 API key 原文 / 绝对路径 / 未验证 endpoint**（§0 第四条）。
  ``ComfyConnectionProfile.base_url`` 必须是 http/https；云 profile 必须有
  ``credential_ref``（凭证引用，不存 key 原文）。
- **只读快照**：``ComfyWorkflowSnapshot.from_api_format`` 不执行 custom node 代码
  （§14.1 step 3）；未知 ``class_type`` 标记为 ``is_unknown_custom_node``。
- **绑定只走声明的槽位**（§9.4 #2）：``ComfyInputBinding`` 的 ``source_artifact_id``
  artifact_type 必须与 slot 声明兼容；不允许改写 ``class_type``。
- **不承诺任意 JSON 无损转换**（§1.4.2 C5）：本模块只描述 / 执行 workflow，不假装把
  任意 custom node 翻译成 Topos 原生节点。
- **artifact hash 必填**（§9.4 #5）：``ComfyOutputArtifact.content_hash`` 非空。
- 全部 frozen + 可序列化为 JSON；``schema_version`` 化（可迁移）。

不依赖真实 GPU / 网络 / Comfy server / PyQt6（与 §14.1 step 1 一致）。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Set, Tuple
from urllib.parse import urlparse

# ---------------------------------------------------------------------- #
# 受控类型表
# ---------------------------------------------------------------------- #

# ComfyConnectionKind —— 连接类型（C0/C1）
class ComfyConnectionKind:
    """连接类型（plan §1.4.2 C0/C1）。

    红线：**不**含任何可执行后端（``executable`` / ``shell`` / ``fs``）。
    """

    LOCAL = "local"   # 用户本机配置的 Comfy server
    CLOUD = "cloud"   # Comfy Cloud / 受控云 profile

    ALL: Tuple[str, ...] = (LOCAL, CLOUD)


# ComfyInputSlot 支持的 artifact_type（与 plan §3.1 ArtifactType 对齐）
_ALLOWED_SLOT_TYPES: Tuple[str, ...] = (
    "text_document", "asset_ref", "asset_set", "selection_set",
    "integer", "float", "string", "boolean",
    "media_generation_ref",
)

# ComfyOutputArtifact 受控 kind（不允许可执行）
_ALLOWED_OUTPUT_KINDS: Tuple[str, ...] = (
    "image", "video", "audio", "latent", "mesh",
)

# ComfyUI 输出节点 class_type（用于识别 workflow 的输出节点）
_OUTPUT_CLASS_TYPES: Tuple[str, ...] = (
    "SaveImage", "SaveImageWebsocket", "SaveAnimatedPNG",
    "SaveVideo", "SaveAudio", "VHS_VideoCombine", "SaveLatent",
)

# ComfyUI 模型 loader class_type → model_type 映射
_MODEL_LOADER_MAP: Dict[str, str] = {
    "CheckpointLoaderSimple": "checkpoint",
    "CheckpointLoader": "checkpoint",
    "VAELoader": "vae",
    "LoraLoader": "lora",
    "LoraLoaderModelOnly": "lora",
    "ControlNetLoader": "controlnet",
    "CLIPVisionLoader": "clip_vision",
    "UpscaleModelLoader": "upscale_model",
    "UNETLoader": "unet",
    "DualCLIPLoader": "clip",
    "CLIPLoader": "clip",
}

# 可绑定 slot 的字面量输入名（节点输入里非 ["id", idx] 形式的值 = 字面量 → 可绑定）
# 这里不做 class_type 级别的过滤，但保留扩展点。
_LITERAL_BINDABLE_INPUTS: Tuple[str, ...] = (
    "text", "seed", "width", "height", "batch_size",
    "cfg", "steps", "denoise", "filename_prefix",
    "negative", "positive", "prompt",
)


# ---------------------------------------------------------------------- #
# ComfyConnectionProfile —— 连接配置（C1 Provider 级）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyConnectionProfile:
    """本机 / 云 Comfy server 连接配置（plan §1.4.2 C1）。

    Attributes:
        profile_id: 唯一 ID（如 ``local-default`` / ``comfy-cloud``）。
        kind: ``local`` / ``cloud``（受控；不含可执行后端）。
        base_url: server 基地址（必须 http/https；不允许可执行 endpoint）。
        credential_ref: 凭证引用（云 profile 必填；本机可空）。
            仅存**引用**，不存 key 原文（§0 第四条）。
        display_name: 显示名。
        upload_consent_required: 每次上传是否需要用户确认（云 profile 默认 True）。
    """

    profile_id: str
    kind: str
    base_url: str
    credential_ref: str = ""
    display_name: str = ""
    upload_consent_required: bool = True
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("profile_id cannot be empty")
        if self.kind not in ComfyConnectionKind.ALL:
            raise ValueError(
                f"invalid kind: {self.kind!r} (allowed: {ComfyConnectionKind.ALL})"
            )
        self._validate_base_url(self.base_url)
        if self.is_cloud and not self.credential_ref:
            raise ValueError(
                f"cloud profile {self.profile_id!r} requires credential_ref "
                "(store key in CredentialStore; never inline)"
            )

    @staticmethod
    def _validate_base_url(url: str) -> None:
        if not url:
            raise ValueError("base_url cannot be empty")
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"base_url must be http/https (got scheme {scheme!r}); "
                "file/javascript/executable endpoints not allowed"
            )
        if not parsed.netloc:
            raise ValueError(f"base_url missing host: {url!r}")

    @property
    def is_cloud(self) -> bool:
        return self.kind == ComfyConnectionKind.CLOUD

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "kind": self.kind,
            "base_url": self.base_url,
            "credential_ref": self.credential_ref,  # 引用，不是 key 原文
            "display_name": self.display_name,
            "upload_consent_required": self.upload_consent_required,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ComfyConnectionProfile":
        return cls(
            profile_id=d["profile_id"],
            kind=d["kind"],
            base_url=d["base_url"],
            credential_ref=d.get("credential_ref", ""),
            display_name=d.get("display_name", ""),
            upload_consent_required=d.get("upload_consent_required", True),
            schema_version=d.get("schema_version", "1"),
        )


# ---------------------------------------------------------------------- #
# ComfyInputSlot —— 预先声明的可绑定输入槽位
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyInputSlot:
    """workflow 中**预先声明**的可绑定输入槽位（plan §9.4 #2）。

    红线：``ComfyInputBinding`` 只能填充声明的槽位；不允许改写 ``class_type``。

    Attributes:
        slot_id: 槽位逻辑 ID（如 ``prompt`` / ``seed`` / ``width``）。
        node_id: Comfy 节点 ID。
        input_name: 节点输入名（如 KSampler 的 ``seed`` / CLIPTextEncode 的 ``text``）。
        artifact_type: Topos artifact 类型（与 plan §3.1 ArtifactType 对齐）。
        required: 是否必填。
        description: 槽位说明（用于 Inspector）。
    """

    slot_id: str
    node_id: str
    input_name: str
    artifact_type: str
    required: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if not self.slot_id:
            raise ValueError("slot_id cannot be empty")
        if not self.node_id:
            raise ValueError("node_id cannot be empty")
        if not self.input_name:
            raise ValueError("input_name cannot be empty")
        if self.artifact_type not in _ALLOWED_SLOT_TYPES:
            raise ValueError(
                f"artifact_type {self.artifact_type!r} not allowed for input slot; "
                f"allowed: {_ALLOWED_SLOT_TYPES}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slot_id": self.slot_id, "node_id": self.node_id,
            "input_name": self.input_name, "artifact_type": self.artifact_type,
            "required": self.required, "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ComfyInputSlot":
        return cls(
            slot_id=d["slot_id"], node_id=d["node_id"],
            input_name=d["input_name"], artifact_type=d["artifact_type"],
            required=d.get("required", True),
            description=d.get("description", ""),
        )


# ---------------------------------------------------------------------- #
# ComfyInputBinding —— artifact → slot 的具体绑定
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyInputBinding:
    """把一个 Topos artifact（或字面量值）绑定到 :class:`ComfyInputSlot`。

    红线（plan §9.4 #2）：
    - 只能绑定到 workflow 预先声明的 slot；不存在"注入任意 class_type"。
    - ``source_artifact_id`` 的 artifact_type 必须与 slot 兼容（由调用方查 artifact store
      得知 kind；这里做基本字符串校验）。
    - frozen：创建后不可改（避免运行中篡改）。

    Attributes:
        slot: 绑定的槽位声明。
        source_artifact_id: 来源 Topos artifact ID（如 ``text/doc-001``）；为字面量时为空。
        resolved_value: 字面量值（如 seed=42）；从 artifact 解析时为空。
    """

    slot: ComfyInputSlot
    source_artifact_id: str = ""
    resolved_value: str = ""

    def __post_init__(self) -> None:
        if not self.source_artifact_id and not self.resolved_value:
            # 至少要有一个（artifact 引用 或 字面量）
            if self.slot.required:
                raise ValueError(
                    f"binding for required slot {self.slot.slot_id!r} is empty"
                )
        # artifact 引用类型校验：source_artifact_id 前缀必须匹配 slot.artifact_type
        if self.source_artifact_id:
            # 约定 source_artifact_id 形如 "text/doc-001" / "asset/abc"
            prefix = self.source_artifact_id.split("/", 1)[0]
            slot_type = self.slot.artifact_type
            # 兼容：text_document ↔ text；asset_ref ↔ asset；integer/float/string ↔ literal
            compatible = self._is_compatible(prefix, slot_type)
            if not compatible:
                raise ValueError(
                    f"artifact_type mismatch: slot {self.slot.slot_id!r} expects "
                    f"{slot_type!r} but source artifact prefix is {prefix!r}"
                )

    @staticmethod
    def _is_compatible(prefix: str, slot_type: str) -> bool:
        """宽松兼容映射：artifact 前缀 ↔ slot 类型。"""
        m = {
            "text": ("text_document", "string"),
            "asset": ("asset_ref", "asset_set"),
            "selection": ("selection_set",),
            "int": ("integer",), "integer": ("integer",),
            "float": ("float",), "str": ("string",),
        }
        return slot_type in m.get(prefix, ())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slot": self.slot.to_dict(),
            "source_artifact_id": self.source_artifact_id,
            "resolved_value": self.resolved_value,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ComfyInputBinding":
        return cls(
            slot=ComfyInputSlot.from_dict(d["slot"]),
            source_artifact_id=d.get("source_artifact_id", ""),
            resolved_value=d.get("resolved_value", ""),
        )


# ---------------------------------------------------------------------- #
# ComfyModelRequirement / ComfyCustomNodeRequirement / ComfyNodeSummary
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyModelRequirement:
    """workflow 引用的模型权重（plan §14.1 step 3 依赖报告）。"""

    model_type: str        # checkpoint / vae / lora / controlnet / ...
    model_name: str        # 文件名（如 sd_xl_base_1.0.safetensors）
    directory: str = ""    # ComfyUI 模型目录（未知时为空，不假设）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_type": self.model_type,
            "model_name": self.model_name,
            "directory": self.directory,
        }


@dataclass(frozen=True)
class ComfyCustomNodeRequirement:
    """workflow 引用的 custom node 仓库（plan §14.1 step 3）。

    红线：``repo_url`` 仅用于**报告**和跳转提示；Topos **不**自动下载/安装
    （plan §13.7："首版不自动安装"）。
    """

    repo_url: str
    display_name: str = ""
    commit_hash: str = ""    # 未 pin 时为空（风险提示）

    @property
    def has_pinned_commit(self) -> bool:
        return bool(self.commit_hash)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_url": self.repo_url,
            "display_name": self.display_name,
            "commit_hash": self.commit_hash,
            "pinned": self.has_pinned_commit,
        }


# Built-in Comfy 节点（核心仓库自带，不算 custom node）。
# 这里只是粗略白名单 —— importer 用 "未知 = custom node" 的启发式（不在白名单的 class_type
# → custom node）。这与 §1.4.2 C5 "不承诺无损转换" 一致。
_BUILTIN_CLASS_TYPES: Tuple[str, ...] = (
    "KSampler", "KSamplerAdvanced", "KSampler (Efficient)",
    "CheckpointLoaderSimple", "CheckpointLoader",
    "CLIPTextEncode", "CLIPTextEncode (NSP)", "CLIPSetLastLayer",
    "EmptyLatentImage", "LatentFromBatch",
    "VAEDecode", "VAEEncode", "VAEDecodeTiled",
    "SaveImage", "SaveImageWebsocket", "SaveAnimatedPNG", "SaveLatent",
    "PreviewImage",
    "LoadImage", "LoadImageBatch",
    "VAELoader", "LoraLoader", "LoraLoaderModelOnly",
    "ControlNetLoader", "ControlNetApply",
    "CLIPVisionLoader", "CLIPVisionEncode",
    "UpscaleModelLoader", "ImageUpscaleWithModel",
    "UNETLoader", "DualCLIPLoader", "CLIPLoader",
    "ConditioningConcat",
)


@dataclass(frozen=True)
class ComfyNodeSummary:
    """Comfy 节点的只读摘要（plan §9.4 #3 / C3 可视化级）。"""

    node_id: str
    class_type: str
    title: str = ""
    inputs_summary: Dict[str, str] = field(default_factory=dict)
    category: str = ""
    is_output: bool = False
    is_unknown_custom_node: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "class_type": self.class_type,
            "title": self.title,
            "inputs_summary": dict(self.inputs_summary),
            "category": self.category,
            "is_output": self.is_output,
            "is_unknown_custom_node": self.is_unknown_custom_node,
        }


# ---------------------------------------------------------------------- #
# ComfyWorkflowSnapshot —— ComfyUI API-format JSON 的只读快照
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyWorkflowSnapshot:
    """ComfyUI API-format workflow 的只读快照（plan §1.4.2 C2 / §14.1 step 3）。

    红线：
    - ``from_api_format`` **不执行**任何 custom node 代码（§14.1 step 3）；只做静态元数据
      抽取。未知 ``class_type`` → ``is_unknown_custom_node=True``（不报错，不执行）。
    - ``raw_payload`` 保留原始 API payload（提交 Comfy server 时用），但 ``to_dict`` 只
      输出摘要（避免泄漏 prompt 原文到审计；§0 第四条）。
    - ``content_hash`` 稳定（基于结构化 json），用于等价性校验。

    Attributes:
        workflow_id: Topos 侧的 workflow ID（与 Topos WorkflowGraph 区分；这是 Comfy
            snapshot 的 ID）。
        display_name: 显示名。
        schema_version: ``"1"``。
        nodes: 节点摘要列表（只读）。
        input_slots: 预先声明的可绑定输入槽位（字面量输入抽出）。
        required_models: 模型依赖列表。
        required_custom_nodes: custom node 仓库列表（不自动安装）。
        content_hash: 结构化哈希（基于 normalized payload）。
        raw_payload: 原始 API-format dict（内部保留，提交时用）。
    """

    workflow_id: str
    display_name: str
    schema_version: str = "1"
    nodes: Tuple[ComfyNodeSummary, ...] = ()
    input_slots: Tuple[ComfyInputSlot, ...] = ()
    required_models: Tuple[ComfyModelRequirement, ...] = ()
    required_custom_nodes: Tuple[ComfyCustomNodeRequirement, ...] = ()
    content_hash: str = ""
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workflow_id:
            raise ValueError("workflow_id cannot be empty")

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    # ---------------- 解析 ---------------- #

    @classmethod
    def from_api_format(
        cls,
        workflow_id: str,
        raw: Any,
        *,
        display_name: str = "",
    ) -> "ComfyWorkflowSnapshot":
        """从 ComfyUI API-format dict 构造只读快照。

        红线：**不执行**任何 custom node 代码。只做静态元数据抽取。
        """
        if not isinstance(raw, dict):
            raise ValueError(
                f"API-format workflow must be a dict mapping node_id -> {{class_type, inputs}}; "
                f"got {type(raw).__name__}"
            )
        if not raw:
            raise ValueError("API-format workflow is empty")

        # AUD-23020：跳过 "_" 前缀且**非节点定义**的元数据键（如导出侧注入的
        # ``_topos_meta`：dict、无 class_type）。这类键直接按节点解析会炸出
        # "missing string class_type"；占位节点（``_topos_*``，带 class_type）保留。
        node_defs: List[Tuple[Any, Dict[str, Any]]] = []
        for key, node_def in raw.items():
            if (
                isinstance(key, str)
                and key.startswith("_")
                and not (
                    isinstance(node_def, dict)
                    and isinstance(node_def.get("class_type"), str)
                    and bool(node_def.get("class_type"))
                )
            ):
                continue  # 元数据键（无 class_type），不是节点
            node_defs.append((key, node_def))
        if not node_defs:
            raise ValueError("API-format workflow is empty")

        # AUD-23102：深拷贝隔离调用方原始 dict——frozen 快照不得与外部共享嵌套
        # 结构，否则外部突变可绕过 content_hash 的"防篡改"红线。
        payload: Dict[str, Any] = copy.deepcopy(dict(node_defs))

        nodes: List[ComfyNodeSummary] = []
        required_models: List[ComfyModelRequirement] = []
        required_custom_nodes: List[ComfyCustomNodeRequirement] = []
        input_slots: List[ComfyInputSlot] = []
        # AUD-23101：对规范化**基准名**计数生成后缀；issued 记录已发放的最终
        # slot_id，保证全图唯一（第 3 个同名输入起不再重复）。
        slot_base_counts: Dict[str, int] = {}
        issued_slot_ids: Set[str] = set()

        for node_id, node_def in node_defs:
            if not isinstance(node_def, dict):
                raise ValueError(
                    f"node {node_id!r} must be a dict with class_type/inputs"
                )
            class_type = node_def.get("class_type")
            if not class_type or not isinstance(class_type, str):
                raise ValueError(
                    f"node {node_id!r} missing string class_type"
                )
            inputs = node_def.get("inputs", {})
            if not isinstance(inputs, dict):
                raise ValueError(
                    f"node {node_id!r} inputs must be a dict"
                )

            # 1. 输入摘要 + 字面量输入抽出为 slot
            inputs_summary: Dict[str, str] = {}
            for iname, ival in inputs.items():
                inputs_summary[iname] = cls._describe_input_value(ival)
                # 字面量输入（非 ["id", idx] 形式）→ 可绑定 slot
                if cls._is_literal_value(ival) and iname in _LITERAL_BINDABLE_INPUTS:
                    slot_id = cls._canonical_slot_id(
                        iname, slot_base_counts, issued_slot_ids,
                    )
                    artifact_type = cls._slot_artifact_type_for(iname)
                    try:
                        input_slots.append(ComfyInputSlot(
                            slot_id=slot_id,
                            node_id=node_id,
                            input_name=iname,
                            artifact_type=artifact_type,
                            required=False,
                            description=f"bindable literal input '{iname}'",
                        ))
                    except ValueError:
                        # 某些 input_name 可能不在 _ALLOWED_SLOT_TYPES 对齐，跳过
                        pass

            # 2. 模型 loader → required_models
            model_type = _MODEL_LOADER_MAP.get(class_type)
            if model_type:
                # 模型文件名约定在第一个字符串输入（ckpt_name / model_name / lora_name）
                model_name = ""
                for v in inputs.values():
                    if isinstance(v, str) and (
                        v.endswith(".safetensors") or v.endswith(".ckpt")
                        or v.endswith(".pt") or v.endswith(".pth")
                        or v.endswith(".bin") or v.endswith(".gguf")
                    ):
                        model_name = v
                        break
                if model_name:
                    required_models.append(ComfyModelRequirement(
                        model_type=model_type, model_name=model_name,
                    ))

            # 3. 输出节点识别
            is_output = class_type in _OUTPUT_CLASS_TYPES

            # 4. unknown custom node
            is_unknown = class_type not in _BUILTIN_CLASS_TYPES
            if is_unknown:
                # 简单 heuristic：未知 class_type 推为 custom node（无 repo_url 信息；
                # 真实 importer 会查 Comfy server /custom_nodes 接口）
                required_custom_nodes.append(ComfyCustomNodeRequirement(
                    repo_url="",  # 未知；提示用户手动确认
                    display_name=class_type,
                ))

            nodes.append(ComfyNodeSummary(
                node_id=str(node_id),
                class_type=class_type,
                title=str(node_def.get("_meta", {}).get("title", ""))
                if isinstance(node_def.get("_meta"), dict) else "",
                inputs_summary=inputs_summary,
                category=str(node_def.get("_meta", {}).get("category", ""))
                if isinstance(node_def.get("_meta"), dict) else "",
                is_output=is_output,
                is_unknown_custom_node=is_unknown,
            ))

        # 5. content_hash：基于规范化 payload
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        content_hash = "sha256:" + hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()

        return cls(
            workflow_id=workflow_id,
            display_name=display_name,
            nodes=tuple(nodes),
            input_slots=tuple(input_slots),
            required_models=tuple(required_models),
            required_custom_nodes=tuple(required_custom_nodes),
            content_hash=content_hash,
            raw_payload=payload,
        )

    @staticmethod
    def _describe_input_value(v: Any) -> str:
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
            return f"LINK from {v[0]}.{v[1]}"
        if isinstance(v, str):
            return v if len(v) <= 64 else v[:61] + "..."
        return repr(v)

    @staticmethod
    def _is_literal_value(v: Any) -> bool:
        """字面量值（非 link 引用）。link 形如 ["6", 0]。"""
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
            return False  # 是 link
        return True

    @staticmethod
    def _canonical_slot_id(
        input_name: str,
        base_counts: Dict[str, int],
        issued: Set[str],
    ) -> str:
        """把 Comfy 输入名映射到 Topos slot_id（prompt/seed/width/height 等）。

        去重规则（AUD-23101）：对**规范化基准名**计数生成后缀（seed → seed_2 →
        seed_3 …）；``issued`` 记录已发放的最终 slot_id，兜底处理"原始输入名恰与
        生成后缀同名"（如字面量输入 ``seed_2``）的碰撞，保证全图 slot_id 唯一。
        """
        canonical = {
            "text": "prompt", "positive": "prompt", "negative": "negative_prompt",
            "seed": "seed", "width": "width", "height": "height",
            "batch_size": "batch_size", "cfg": "cfg", "steps": "steps",
            "denoise": "denoise",
        }
        base = canonical.get(input_name, input_name)
        count = base_counts.get(base, 0) + 1
        base_counts[base] = count
        sid = base if count == 1 else f"{base}_{count}"
        while sid in issued:
            count += 1
            sid = f"{base}_{count}"
        issued.add(sid)
        return sid

    @staticmethod
    def _slot_artifact_type_for(input_name: str) -> str:
        m = {
            "text": "text_document", "positive": "text_document",
            "negative": "text_document",
            "seed": "integer", "width": "integer", "height": "integer",
            "batch_size": "integer",
            "cfg": "float", "steps": "integer", "denoise": "float",
        }
        return m.get(input_name, "string")

    # ---------------- 序列化 ---------------- #

    def to_dict(self) -> Dict[str, Any]:
        # 公开 dict **不含** raw_payload（避免 prompt 原文泄漏到审计）
        return {
            "workflow_id": self.workflow_id,
            "display_name": self.display_name,
            "schema_version": self.schema_version,
            "node_count": self.node_count,
            "nodes": [n.to_dict() for n in self.nodes],
            "input_slots": [s.to_dict() for s in self.input_slots],
            "required_models": [m.to_dict() for m in self.required_models],
            "required_custom_nodes": [c.to_dict() for c in self.required_custom_nodes],
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ComfyWorkflowSnapshot":
        # 注：to_dict 不含 raw_payload；from_dict 时 raw_payload 为空
        # （如需提交，需重新 from_api_format）
        return cls(
            workflow_id=d["workflow_id"],
            display_name=d.get("display_name", ""),
            schema_version=d.get("schema_version", "1"),
            nodes=tuple(ComfyNodeSummary(**n) if isinstance(n, dict) else n
                        for n in d.get("nodes", [])),
            input_slots=tuple(
                ComfyInputSlot.from_dict(s) if isinstance(s, dict) else s
                for s in d.get("input_slots", [])
            ),
            required_models=tuple(
                ComfyModelRequirement(**m) if isinstance(m, dict) else m
                for m in d.get("required_models", [])
            ),
            required_custom_nodes=tuple(
                ComfyCustomNodeRequirement(
                    repo_url=c["repo_url"],
                    display_name=c.get("display_name", ""),
                    commit_hash=c.get("commit_hash", ""),
                ) if isinstance(c, dict) else c
                for c in d.get("required_custom_nodes", [])
            ),
            content_hash=d.get("content_hash", ""),
        )


# ---------------------------------------------------------------------- #
# ComfyJobRef —— 外部 Comfy 异步 job 引用
# ---------------------------------------------------------------------- #


class ComfyJobState:
    """Comfy job 状态（与 Topos RunStatus 解耦；外部 job 单独跟踪）。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL: Tuple[str, ...] = (QUEUED, RUNNING, SUCCESS, FAILED, CANCELLED)
    TERMINAL: Tuple[str, ...] = (SUCCESS, FAILED, CANCELLED)


@dataclass(frozen=True)
class ComfyJobRef:
    """外部 Comfy 异步 job 的引用（plan §1.4.2 C2）。

    红线：**不持有**任何 Topos 内部对象（RunRecord / MediaAsset / OpenGL）。
    仅持有 Comfy server 侧的 job ID + 状态 + 进度。Topos RunRecord 通过
    :class:`GenerationProvider` adapter 持有 :class:`ComfyJobRef` 引用，反之不行。

    Attributes:
        external_job_id: Comfy server 侧的 prompt_id / job ID。
        profile_id: 连接 profile ID（查 :class:`ComfyConnectionProfile`）。
        workflow_snapshot_hash: 提交时的 snapshot content_hash（防篡改）。
        state: 当前状态。
        progress_fraction: 0..1。
        progress_message: 进度消息。
        submit_time_iso: 提交时间（ISO 格式）。
    """

    external_job_id: str
    profile_id: str
    workflow_snapshot_hash: str
    state: str = ComfyJobState.QUEUED
    progress_fraction: float = 0.0
    progress_message: str = ""
    submit_time_iso: str = ""

    def __post_init__(self) -> None:
        if not self.external_job_id:
            raise ValueError("external_job_id cannot be empty")
        if not self.profile_id:
            raise ValueError("profile_id cannot be empty")
        if not self.workflow_snapshot_hash:
            raise ValueError("workflow_snapshot_hash cannot be empty")
        if self.state not in ComfyJobState.ALL:
            raise ValueError(f"invalid state: {self.state!r}")
        if not (0.0 <= self.progress_fraction <= 1.0):
            raise ValueError(
                f"progress_fraction must be in [0,1]: {self.progress_fraction}"
            )

    @property
    def is_terminal(self) -> bool:
        return self.state in ComfyJobState.TERMINAL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "external_job_id": self.external_job_id,
            "profile_id": self.profile_id,
            "workflow_snapshot_hash": self.workflow_snapshot_hash,
            "state": self.state,
            "progress_fraction": self.progress_fraction,
            "progress_message": self.progress_message,
            "submit_time_iso": self.submit_time_iso,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ComfyJobRef":
        return cls(
            external_job_id=d["external_job_id"],
            profile_id=d["profile_id"],
            workflow_snapshot_hash=d["workflow_snapshot_hash"],
            state=d.get("state", ComfyJobState.QUEUED),
            progress_fraction=d.get("progress_fraction", 0.0),
            progress_message=d.get("progress_message", ""),
            submit_time_iso=d.get("submit_time_iso", ""),
        )


# ---------------------------------------------------------------------- #
# ComfyOutputArtifact —— Comfy 输出 artifact
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyOutputArtifact:
    """Comfy server 返回的输出 artifact（plan §9.4 #5）。

    红线：
    - ``content_hash`` 必填（下载结果必须 hash 校验；§9.4 #5）。
    - ``artifact_kind`` 受控（不允许可执行）。

    Attributes:
        node_id: 输出节点 ID（如 SaveImage 节点 "9"）。
        output_index: 节点输出的索引（SaveImage 通常 0）。
        artifact_kind: ``image`` / ``video`` / ``audio`` / ``latent`` / ``mesh``。
        filename: server 侧文件名。
        content_hash: 内容 hash（``sha256:...``）。
        bytes_size: 文件大小（字节）。
        local_path: 下载后的本机路径（可选；下载器填）。
    """

    node_id: str
    output_index: int
    artifact_kind: str
    filename: str
    content_hash: str
    bytes_size: int = 0
    local_path: str = ""

    def __post_init__(self) -> None:
        if not self.filename:
            raise ValueError("filename cannot be empty")
        if not self.content_hash:
            raise ValueError(
                "content_hash is required (downloaded output must be hash-verified; "
                "plan §9.4 #5)"
            )
        if self.artifact_kind not in _ALLOWED_OUTPUT_KINDS:
            raise ValueError(
                f"artifact_kind {self.artifact_kind!r} not allowed; "
                f"allowed: {_ALLOWED_OUTPUT_KINDS} (no executable)"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "output_index": self.output_index,
            "artifact_kind": self.artifact_kind,
            "filename": self.filename,
            "content_hash": self.content_hash,
            "bytes_size": self.bytes_size,
            "local_path": self.local_path,
        }
