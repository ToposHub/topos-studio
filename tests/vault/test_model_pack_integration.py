"""P1-4 ModelPackManager vault spec 注册测试。

验证主项目 ModelPackManager 合并了 vault 的模型包 spec，
让主项目模型管理 UI 能看到 vault 模型（SigLIP2/Qwen3-VL 等）。
"""

from __future__ import annotations

import pytest


def test_vault_specs_merged_into_model_pack_manager():
    """主项目 ModelPackManager 合并后包含 vault 模型包。"""
    from src.app.controllers.ai_editing_mixin import _build_model_pack_manager

    mgr = _build_model_pack_manager()
    ids = {s.pack_id for s in mgr.list_specs()}

    # vault 模型包都在
    vault_ids = {
        "qwen3_embedding_0_6b", "siglip2_base", "qwen3_vl_8b_awq",
        "qwen2_5_omni_7b_awq", "insightface_buffalo_l",
    }
    assert vault_ids.issubset(ids), f"缺 vault 包：{vault_ids - ids}"


def test_main_project_specs_still_present():
    """合并后主项目原有 spec（qwen3_asr 等）仍在。"""
    from src.app.controllers.ai_editing_mixin import _build_model_pack_manager

    mgr = _build_model_pack_manager()
    ids = {s.pack_id for s in mgr.list_specs()}
    assert "qwen3_asr" in ids  # 主项目 ASR


def test_no_duplicate_pack_ids():
    """合并去重：同 pack_id 不重复（vault 与主项目可能有重叠）。"""
    from src.app.controllers.ai_editing_mixin import _build_model_pack_manager

    mgr = _build_model_pack_manager()
    ids = [s.pack_id for s in mgr.list_specs()]
    assert len(ids) == len(set(ids)), f"pack_id 重复：{ids}"


def test_vault_spec_status_queryable():
    """vault 模型包的安装状态可查（get_status 不崩）。"""
    from src.app.controllers.ai_editing_mixin import _build_model_pack_manager

    mgr = _build_model_pack_manager()
    status = mgr.get_status("siglip2_base")
    assert status is not None
    # 未装时 installed=False（测试环境没下模型）
    assert isinstance(status.installed, bool)


def test_to_main_project_specs_conversion():
    """vault spec 能转成主项目 ModelPackSpec（字段对齐）。"""
    from apps.topos_vault.model_specs import to_main_project_specs
    from src.features.edit.ai_editing.model_pack_manager import ModelPackSpec

    vault_specs = to_main_project_specs()
    assert len(vault_specs) >= 5
    # 转换后的类型是主项目的 ModelPackSpec
    assert all(isinstance(s, ModelPackSpec) for s in vault_specs)
    # 字段都有值
    for s in vault_specs:
        assert s.pack_id
        assert s.capability
        assert s.license is not None


def test_merge_degrades_gracefully_if_vault_unavailable(monkeypatch):
    """vault 模块不可用时，降级用纯主项目 BUILTIN_SPECS（不崩）。"""
    import builtins
    from src.app.controllers.ai_editing_mixin import _build_model_pack_manager
    from src.features.edit.ai_editing.model_pack_manager import BUILTIN_SPECS

    # mock to_main_project_specs 抛异常
    import apps.topos_vault.model_specs as ms_mod
    monkeypatch.setattr(ms_mod, "to_main_project_specs",
                        lambda: (_ for _ in ()).throw(RuntimeError("mocked")))

    mgr = _build_model_pack_manager()
    ids = {s.pack_id for s in mgr.list_specs()}
    # 主项目 spec 都在
    builtin_ids = {s.pack_id for s in BUILTIN_SPECS}
    assert builtin_ids.issubset(ids)
