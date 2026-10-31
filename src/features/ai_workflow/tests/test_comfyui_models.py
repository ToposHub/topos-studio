"""P6.1 测试：ComfyUI 兼容层数据模型（plan §14.1 step 1 / §1.4.2）。

验证 ComfyUI 兼容层的核心数据契约（plan §14.1："为 ``ComfyConnectionProfile``、
``ComfyWorkflowSnapshot``、``ComfyInputBinding``、``ComfyJobRef`` 写纯 Python
dataclass 和 JSON schema 测试"）：

- ``ComfyConnectionProfile``：本机/云 Comfy server 连接配置（C0/C1 资产级 + Provider 级）。
- ``ComfyInputBinding``：把 Topos artifact 绑定到 ComfyUI workflow 的输入槽位
  （只允许预先声明的槽位；不暴露任意 class_type 注入）。
- ``ComfyWorkflowSnapshot``：ComfyUI API-format JSON 的只读快照（节点/模型/自定义节点
  依赖），可执行 content_hash 用于等价性校验。
- ``ComfyJobRef``：外部 Comfy job 的引用（异步 job；不持有任何 Topos 内部对象）。

红线（plan §1.4.2 / §0 / §14.1 / §9.4）：

- 不存 API key 原文 / 绝对路径 / 未验证 endpoint（§0 第四条）。
- ``ComfyWorkflowSnapshot`` 是**只读**快照：导入时不执行 custom node 代码（§14.1 step 3）。
- ``ComfyInputBinding`` 只绑定 workflow 预先声明的槽位；不暴露任意 class_type 改写。
- ``ComfyConnectionProfile.kind`` 受控枚举（local / cloud），不允许可执行后端。
- 所有对象 frozen + 可序列化为 JSON；schema_version 化（可迁移）。

不依赖真实 GPU / 网络 / Comfy server（与 §14.1 step 1 "纯 Python dataclass" 一致）。
"""
from __future__ import annotations

import json

import pytest

from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyConnectionKind,
    ComfyConnectionProfile,
    ComfyCustomNodeRequirement,
    ComfyInputBinding,
    ComfyInputSlot,
    ComfyJobState,
    ComfyJobRef,
    ComfyModelRequirement,
    ComfyNodeSummary,
    ComfyOutputArtifact,
    ComfyWorkflowSnapshot,
)


# ---------------------------------------------------------------------- #
# ComfyConnectionKind / ComfyConnectionProfile
# ---------------------------------------------------------------------- #


class TestComfyConnectionProfile:
    def test_kind_is_controlled_no_executable_backend(self) -> None:
        # 红线：不允许可执行后端（custom-node 代码不会通过 profile 自动跑）
        assert ComfyConnectionKind.LOCAL == "local"
        assert ComfyConnectionKind.CLOUD == "cloud"
        assert ComfyConnectionKind.ALL == ("local", "cloud")
        # 没有任何 exec / shell / fs 后端
        for k in ComfyConnectionKind.ALL:
            assert "exec" not in k
            assert "shell" not in k

    def test_local_profile_minimal_construction(self) -> None:
        p = ComfyConnectionProfile(
            profile_id="local-default",
            kind=ComfyConnectionKind.LOCAL,
            base_url="http://127.0.0.1:8188",
        )
        assert p.profile_id == "local-default"
        assert p.kind == ComfyConnectionKind.LOCAL
        assert p.base_url == "http://127.0.0.1:8188"
        assert p.is_cloud is False
        assert p.credential_ref == ""  # 本机默认不需要凭证

    def test_cloud_profile_requires_credential_ref(self) -> None:
        # 红线：云 provider 必须有凭证引用（§0 第四条："凭证经 CredentialStore"）
        with pytest.raises(ValueError, match="credential_ref"):
            ComfyConnectionProfile(
                profile_id="comfy-cloud",
                kind=ComfyConnectionKind.CLOUD,
                base_url="https://api.comfy.cloud",
                credential_ref="",  # 空 → 拒绝
            )

    def test_cloud_profile_with_credential_ok(self) -> None:
        p = ComfyConnectionProfile(
            profile_id="comfy-cloud",
            kind=ComfyConnectionKind.CLOUD,
            base_url="https://api.comfy.cloud",
            credential_ref="cred/comfy-cloud/0",
        )
        assert p.is_cloud is True
        assert p.credential_ref == "cred/comfy-cloud/0"

    def test_base_url_must_be_http_or_https(self) -> None:
        # 红线：不允许可执行 / file:// 端点（§9.4："禁止注入未验证 endpoint"）
        with pytest.raises(ValueError, match="base_url"):
            ComfyConnectionProfile(
                profile_id="x", kind=ComfyConnectionKind.LOCAL,
                base_url="file:///etc/passwd",
            )
        with pytest.raises(ValueError, match="base_url"):
            ComfyConnectionProfile(
                profile_id="x", kind=ComfyConnectionKind.LOCAL,
                base_url="javascript:alert(1)",
            )

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            ComfyConnectionProfile(
                profile_id="x",
                kind="executable",  # type: ignore[arg-type]
                base_url="http://127.0.0.1:8188",
            )

    def test_to_dict_does_not_leak_credential_secret(self) -> None:
        # 红线：序列化只输出 credential_ref（引用），绝不输出 key 原文
        p = ComfyConnectionProfile(
            profile_id="comfy-cloud",
            kind=ComfyConnectionKind.CLOUD,
            base_url="https://api.comfy.cloud",
            credential_ref="cred/comfy-cloud/0",
        )
        d = p.to_dict()
        assert d["credential_ref"] == "cred/comfy-cloud/0"
        # 不应包含任何疑似密钥的字段
        for k in d:
            assert "secret" not in k.lower()
            assert "password" not in k.lower()
            assert "token" not in k.lower()

    def test_to_dict_roundtrip(self) -> None:
        p = ComfyConnectionProfile(
            profile_id="local-default",
            kind=ComfyConnectionKind.LOCAL,
            base_url="http://127.0.0.1:8188",
            display_name="My Local Comfy",
        )
        d = p.to_dict()
        # from_dict 恢复
        p2 = ComfyConnectionProfile.from_dict(d)
        assert p2 == p

    def test_json_serializable(self) -> None:
        p = ComfyConnectionProfile(
            profile_id="local-default",
            kind=ComfyConnectionKind.LOCAL,
            base_url="http://127.0.0.1:8188",
        )
        # json.dumps 不应抛异常（schema 可序列化）
        s = json.dumps(p.to_dict())
        assert "local-default" in s


# ---------------------------------------------------------------------- #
# ComfyInputSlot / ComfyInputBinding
# ---------------------------------------------------------------------- #


class TestComfyInputBinding:
    def test_input_slot_construction(self) -> None:
        slot = ComfyInputSlot(
            slot_id="prompt",
            node_id="6",
            input_name="text",
            artifact_type="text_document",
            description="positive prompt",
        )
        assert slot.slot_id == "prompt"
        assert slot.node_id == "6"
        assert slot.input_name == "text"
        assert slot.artifact_type == "text_document"
        assert slot.required is True

    def test_input_binding_binds_artifact_to_slot(self) -> None:
        slot = ComfyInputSlot(
            slot_id="prompt", node_id="6", input_name="text",
            artifact_type="text_document",
        )
        b = ComfyInputBinding(slot=slot, source_artifact_id="text/doc-001")
        assert b.slot.slot_id == "prompt"
        assert b.source_artifact_id == "text/doc-001"
        assert b.resolved_value == ""  # 未解析

    def test_input_binding_value_type_must_match_slot(self) -> None:
        # 红线：绑定值类型必须与 slot 声明的 artifact_type 兼容
        slot = ComfyInputSlot(
            slot_id="seed", node_id="3", input_name="seed",
            artifact_type="integer",
        )
        # 兼容：integer
        b = ComfyInputBinding(slot=slot, source_artifact_id="", resolved_value="42")
        assert b.resolved_value == "42"
        # artifact_type=text_document 但 source_artifact_id 指向 audio → 拒绝
        slot2 = ComfyInputSlot(
            slot_id="ref", node_id="9", input_name="image",
            artifact_type="asset_ref",
        )
        with pytest.raises(ValueError, match="artifact_type"):
            ComfyInputBinding(
                slot=slot2, source_artifact_id="text/doc-001",  # 文档不能给图像槽
            )

    def test_binding_is_readonly_after_creation(self) -> None:
        slot = ComfyInputSlot(
            slot_id="prompt", node_id="6", input_name="text",
            artifact_type="text_document",
        )
        b = ComfyInputBinding(slot=slot, source_artifact_id="text/doc-001")
        with pytest.raises(Exception):
            b.source_artifact_id = "tampered"  # type: ignore[misc]

    def test_to_dict_roundtrip(self) -> None:
        slot = ComfyInputSlot(
            slot_id="prompt", node_id="6", input_name="text",
            artifact_type="text_document", description="positive",
        )
        b = ComfyInputBinding(slot=slot, source_artifact_id="text/doc-001")
        d = b.to_dict()
        b2 = ComfyInputBinding.from_dict(d)
        assert b2 == b


# ---------------------------------------------------------------------- #
# ComfyModelRequirement / ComfyCustomNodeRequirement / ComfyNodeSummary
# ---------------------------------------------------------------------- #


class TestComfyRequirements:
    def test_model_requirement(self) -> None:
        m = ComfyModelRequirement(
            model_type="checkpoint", model_name="sd_xl_base_1.0.safetensors",
        )
        assert m.model_type == "checkpoint"
        assert m.model_name == "sd_xl_base_1.0.safetensors"
        assert m.directory == ""  # 默认未知（不假设目录结构）

    def test_custom_node_requirement_includes_repo_url(self) -> None:
        cn = ComfyCustomNodeRequirement(
            repo_url="https://github.com/comfyanonymous/ComfyUI",
            commit_hash="",  # 可空（未 pin 版本 = 风险）
            display_name="ComfyUI core",
        )
        assert cn.repo_url == "https://github.com/comfyanonymous/ComfyUI"
        assert cn.has_pinned_commit is False

    def test_custom_node_pinned(self) -> None:
        cn = ComfyCustomNodeRequirement(
            repo_url="https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",
            commit_hash="abc123def",
            display_name="VideoHelperSuite",
        )
        assert cn.has_pinned_commit is True

    def test_node_summary_basic(self) -> None:
        n = ComfyNodeSummary(
            node_id="6", class_type="CLIPTextEncode",
            title="Positive Prompt",
            inputs_summary={"text": "STRING", "clip": "CLIP"},
            category="conditioning",
        )
        assert n.node_id == "6"
        assert n.class_type == "CLIPTextEncode"
        assert n.category == "conditioning"
        assert n.is_output is False


# ---------------------------------------------------------------------- #
# ComfyWorkflowSnapshot
# ---------------------------------------------------------------------- #


class TestComfyWorkflowSnapshot:
    def _minimal_api_format(self) -> dict:
        """ComfyUI API-format JSON 最小 fixture（plan §1.4.2）。

        结构：``{node_id: {"class_type": ..., "inputs": {...}}}``。
        """
        return {
            "3": {"class_type": "KSampler", "inputs": {
                "seed": 12345, "cfg": 7.0,
                "model": ["4", 0], "positive": ["6", 0],
                "negative": ["7", 0], "latent_image": ["5", 0],
            }},
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {
                "ckpt_name": "sd_xl_base_1.0.safetensors",
            }},
            "6": {"class_type": "CLIPTextEncode", "inputs": {
                "text": "a cute cat", "clip": ["4", 1],
            }},
            "7": {"class_type": "CLIPTextEncode", "inputs": {
                "text": "", "clip": ["4", 1],
            }},
            "5": {"class_type": "EmptyLatentImage", "inputs": {
                "width": 1024, "height": 1024, "batch_size": 1,
            }},
            "8": {"class_type": "VAEDecode", "inputs": {
                "samples": ["3", 0], "vae": ["4", 2],
            }},
            "9": {"class_type": "SaveImage", "inputs": {
                "images": ["8", 0], "filename_prefix": "topos",
            }},
        }

    def test_snapshot_from_api_format_dict(self) -> None:
        raw = self._minimal_api_format()
        snap = ComfyWorkflowSnapshot.from_api_format(
            workflow_id="wf-t2i-001",
            raw=raw,
            display_name="Text-to-Image (SDXL)",
        )
        assert snap.workflow_id == "wf-t2i-001"
        assert snap.display_name == "Text-to-Image (SDXL)"
        assert snap.schema_version == "1"
        assert snap.node_count == 7

    def test_snapshot_extracts_node_summaries(self) -> None:
        raw = self._minimal_api_format()
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        # 至少能找到 KSampler / CLIPTextEncode / SaveImage
        classes = {n.class_type for n in snap.nodes}
        assert "KSampler" in classes
        assert "CLIPTextEncode" in classes
        assert "SaveImage" in classes
        # SaveImage 是输出节点
        outputs = [n for n in snap.nodes if n.is_output]
        assert any(n.class_type == "SaveImage" for n in outputs)

    def test_snapshot_extracts_model_requirements(self) -> None:
        raw = self._minimal_api_format()
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        # CheckpointLoaderSimple → checkpoint 模型依赖
        ckpts = [m for m in snap.required_models if m.model_type == "checkpoint"]
        assert len(ckpts) == 1
        assert ckpts[0].model_name == "sd_xl_base_1.0.safetensors"

    def test_snapshot_identifies_output_nodes(self) -> None:
        raw = self._minimal_api_format()
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        # SaveImage 被识别为输出节点
        out_classes = {n.class_type for n in snap.nodes if n.is_output}
        assert "SaveImage" in out_classes

    def test_snapshot_content_hash_stable(self) -> None:
        # 红线：相同 workflow 的 content_hash 必须稳定（等价性校验）
        raw = self._minimal_api_format()
        s1 = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        s2 = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        assert s1.content_hash == s2.content_hash
        assert len(s1.content_hash) > 0

    def test_snapshot_content_hash_changes_with_workflow(self) -> None:
        raw = self._minimal_api_format()
        s1 = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        # 改 seed
        raw2 = json.loads(json.dumps(raw))
        raw2["3"]["inputs"]["seed"] = 99999
        s2 = ComfyWorkflowSnapshot.from_api_format("wf", raw2)
        assert s1.content_hash != s2.content_hash

    def test_snapshot_does_not_execute_custom_nodes(
        self, monkeypatch,
    ) -> None:
        """红线（§14.1 step 3）：导入时不执行任何 custom node 代码（AUD-23552）。

        旧版只断言恶意节点被解析为元数据——dict 字面量本无执行通道，红线不可
        证伪。现安装哨兵：``eval`` / ``exec`` / ``os.system`` 触达即熔断抛错，
        ``__import__`` 记录新加载模块（只覆盖 ``from_api_format`` 调用窗口）。
        """
        raw = self._minimal_api_format()
        # 注入一个 "evil" custom node；导入应只解析元数据，不执行任何东西
        raw["666"] = {"class_type": "EvilNode", "inputs": {"__evil__": "rm -rf /"}}

        import builtins
        import inspect as _inspect  # noqa: F401  预热：MonkeyPatch.setattr 内部懒 import
        import os
        import sys

        fresh_imports: list[str] = []
        real_import = builtins.__import__
        loaded_at_install = frozenset(sys.modules)

        def _refuse(name: str):
            def _boom(*args, **kwargs):
                raise AssertionError(
                    f"红线被破坏：snapshot 解析执行了 {name}（只读元数据解析）"
                )
            return _boom

        def _spy_import(name: str, *args, **kwargs):
            if name not in loaded_at_install and name not in sys.modules:
                fresh_imports.append(f"__import__({name})")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "eval", _refuse("eval"))
        monkeypatch.setattr(builtins, "exec", _refuse("exec"))
        monkeypatch.setattr(builtins, "__import__", _spy_import)
        monkeypatch.setattr(os, "system", _refuse("os.system"))
        try:
            snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        finally:
            monkeypatch.undo()

        # 解析成功，但只是元数据；不执行任何东西
        evil = [n for n in snap.nodes if n.class_type == "EvilNode"]
        assert len(evil) == 1
        # EvilNode 不在已知 custom node 表中 → 标记为 unknown（缺失依赖）
        unknowns = [n for n in snap.nodes if n.is_unknown_custom_node]
        assert any(n.class_type == "EvilNode" for n in unknowns)
        # 红线可证伪：解析路径没有动态加载任何新模块
        assert not fresh_imports, (
            f"红线被破坏：snapshot 解析触发了 import：{fresh_imports}"
        )

    def test_snapshot_rejects_invalid_api_format(self) -> None:
        # 缺 class_type → 无效 API-format
        with pytest.raises(ValueError, match="class_type"):
            ComfyWorkflowSnapshot.from_api_format("wf", {
                "3": {"inputs": {"seed": 1}},  # 无 class_type
            })
        # 非 dict 根 → 拒绝
        with pytest.raises(ValueError, match="API-format"):
            ComfyWorkflowSnapshot.from_api_format("wf", "not a dict")
        with pytest.raises(ValueError, match="API-format"):
            ComfyWorkflowSnapshot.from_api_format("wf", [])

    def test_snapshot_empty_workflow_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            ComfyWorkflowSnapshot.from_api_format("wf", {})

    def test_snapshot_input_slots_extracted_from_unconnected_inputs(self) -> None:
        # 红线（§9.4 #2）：只允许填充预先声明的绑定槽位
        # CLIPTextEncode 的 text 未连接（来自 ["4",1] 是连接，纯 STRING 是字面量）；
        # 我们把"字面量值输入"识别为可绑定 slot（如 prompt）
        raw = self._minimal_api_format()
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        # 至少有 prompt slot（CLIPTextEncode 的 text 是字面量）
        # 应把 "6"/text（positive）绑定 slot
        assert any(s.node_id == "6" and s.input_name == "text" for s in snap.input_slots)
        # seed 也是字面量 → 可绑定 slot
        seed_slots = [s for s in snap.input_slots if s.slot_id == "seed"]
        assert len(seed_slots) >= 1

    def test_snapshot_to_dict_roundtrip(self) -> None:
        raw = self._minimal_api_format()
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw, display_name="T2I")
        d = snap.to_dict()
        s2 = ComfyWorkflowSnapshot.from_dict(d)
        # raw_payload 不进 to_dict（红线：避免 prompt 原文泄漏）；其余字段必须等价
        assert s2.workflow_id == snap.workflow_id
        assert s2.display_name == snap.display_name
        assert s2.node_count == snap.node_count
        assert s2.content_hash == snap.content_hash
        assert len(s2.nodes) == len(snap.nodes)
        assert len(s2.required_models) == len(snap.required_models)
        # raw_payload 在 round-trip 后为空（需重新 from_api_format 才能恢复）
        assert s2.raw_payload == {}

    def test_snapshot_raw_payload_kept_for_submit_only(self) -> None:
        # 红线：原始 API payload 保留（提交时用），但 to_dict 只输出摘要
        raw = self._minimal_api_format()
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        assert snap.raw_payload == raw  # 内部保留
        d = snap.to_dict()
        # 公开 dict 不应包含完整 raw_payload（避免泄漏 prompt 原文到审计）
        # 但保留 content_hash + node_count + 摘要
        assert "raw_payload" not in d
        assert "content_hash" in d
        assert "node_count" in d

    def test_missing_models_listed(self) -> None:
        raw = self._minimal_api_format()
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        assert len(snap.required_models) > 0
        names = {m.model_name for m in snap.required_models}
        assert "sd_xl_base_1.0.safetensors" in names


# ---------------------------------------------------------------------- #
# ComfyJobRef / ComfyJobState
# ---------------------------------------------------------------------- #


class TestComfyJobRef:
    def test_job_state_is_controlled(self) -> None:
        assert ComfyJobState.QUEUED == "queued"
        assert ComfyJobState.RUNNING == "running"
        assert ComfyJobState.SUCCESS == "success"
        assert ComfyJobState.FAILED == "failed"
        assert ComfyJobState.CANCELLED == "cancelled"

    def test_job_ref_construction(self) -> None:
        ref = ComfyJobRef(
            external_job_id="comfy-abc123",
            profile_id="local-default",
            workflow_snapshot_hash="sha256:deadbeef",
        )
        assert ref.external_job_id == "comfy-abc123"
        assert ref.profile_id == "local-default"
        assert ref.state == ComfyJobState.QUEUED  # 初始
        assert ref.is_terminal is False

    def test_job_state_terminal(self) -> None:
        for s in (ComfyJobState.SUCCESS, ComfyJobState.FAILED, ComfyJobState.CANCELLED):
            ref = ComfyJobRef(
                external_job_id="x", profile_id="p",
                workflow_snapshot_hash="h", state=s,
            )
            assert ref.is_terminal is True
        for s in (ComfyJobState.QUEUED, ComfyJobState.RUNNING):
            ref = ComfyJobRef(
                external_job_id="x", profile_id="p",
                workflow_snapshot_hash="h", state=s,
            )
            assert ref.is_terminal is False

    def test_job_ref_does_not_hold_topos_objects(self) -> None:
        # 红线：ComfyJobRef 是外部 job 引用，不持有 RunRecord / MediaAsset / OpenGL 对象
        ref = ComfyJobRef(
            external_job_id="x", profile_id="p", workflow_snapshot_hash="h",
        )
        # 不应有任何 run_id / asset_id 字段
        d = ref.to_dict()
        for k in d:
            assert "run_id" not in k
            assert "asset_id" not in k

    def test_job_ref_to_dict_roundtrip(self) -> None:
        ref = ComfyJobRef(
            external_job_id="comfy-abc",
            profile_id="local-default",
            workflow_snapshot_hash="sha256:xyz",
            state=ComfyJobState.RUNNING,
            progress_fraction=0.5,
        )
        d = ref.to_dict()
        ref2 = ComfyJobRef.from_dict(d)
        assert ref2 == ref


# ---------------------------------------------------------------------- #
# ComfyOutputArtifact
# ---------------------------------------------------------------------- #


class TestComfyOutputArtifact:
    def test_output_artifact_basic(self) -> None:
        a = ComfyOutputArtifact(
            node_id="9", output_index=0,
            artifact_kind="image",
            filename="topos_00001_.png",
            content_hash="sha256:abcd",
            bytes_size=1024 * 1024,
        )
        assert a.artifact_kind == "image"
        assert a.content_hash == "sha256:abcd"
        assert a.bytes_size == 1024 * 1024

    def test_output_artifact_hash_required(self) -> None:
        # 红线（§9.4 #5）：下载结果必须 hash 校验；无 hash 拒绝
        with pytest.raises(ValueError, match="content_hash"):
            ComfyOutputArtifact(
                node_id="9", output_index=0, artifact_kind="image",
                filename="x.png", content_hash="",  # 空 hash
                bytes_size=100,
            )

    def test_artifact_kind_controlled(self) -> None:
        # 受控 kind（不允许可执行）
        for k in ("image", "video", "audio", "latent", "mesh"):
            a = ComfyOutputArtifact(
                node_id="9", output_index=0, artifact_kind=k,
                filename="x", content_hash="h", bytes_size=1,
            )
            assert a.artifact_kind == k
        with pytest.raises(ValueError, match="artifact_kind"):
            ComfyOutputArtifact(
                node_id="9", output_index=0, artifact_kind="executable",
                filename="x.exe", content_hash="h", bytes_size=1,
            )
