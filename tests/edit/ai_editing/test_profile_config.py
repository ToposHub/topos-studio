"""TAEP M1.4 — ProfileRegistry 配置持久化 + edit.ai_editing 页注册测试。

验收（审查 M1.4）：

- ProfileRegistry.load_from_config / save_to_config 在用户设置里 round-trip
  profiles + active id；只含非 secret（credential_ref 是引用）。
- 损坏/缺失配置不抛错（首启/迁移友好）。
- PreferencesDialog._build_page_registry 为 edit.ai_editing 注册可见页（不再落入
  unknown-page warning 分支）。
"""

from __future__ import annotations

# W07：files_panel 在模块导入期 import QtWebEngineWidgets——Qt 要求它
# （或 AA_ShareOpenGLContexts）先于 QCoreApplication 实例。本文件其它
# 用例先建了 qapp，后导入 preferences_dialog 触发 files_panel 链时在
# Windows 上必炸。在模块导入最前显式设属性（幂等；无 app 时为默认
# 行为不变）。
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication as _QApp

_QApp.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    DataPolicy,
    ProfileRegistry,
    ProviderLimits,
)


class _FakeConfig:
    """最小 ConfigManager 风格桩（get/set）。"""

    def __init__(self):
        self._data = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, auto_save=True):
        self._data[key] = value


def _profile(pid: str = "local-1") -> AiProviderProfile:
    return AiProviderProfile(
        profile_id=pid,
        display_name=f"Profile {pid}",
        provider_kind="local_http",
        endpoint="http://localhost:8000",
        model_id="qwen-video",
        credential_ref=f"keychain:topos:{pid}",  # 引用，不是 secret
        modalities=("text", "tool_call"),
        data_policy=DataPolicy(execution="local", allow_media_upload=False),
        limits=ProviderLimits(max_context_tokens=32000, max_parallel_jobs=1),
    )


# ================================================================
# round-trip
# ================================================================


def test_save_then_load_round_trips_profiles_and_active():
    cfg = _FakeConfig()
    reg = ProfileRegistry()
    reg.add(_profile("local-1"))
    reg.add(_profile("cloud-1"))
    reg.set_active("cloud-1")

    reg.save_to_config(cfg)

    reg2 = ProfileRegistry()
    reg2.load_from_config(cfg)
    assert {p.profile_id for p in reg2.list()} == {"local-1", "cloud-1"}
    assert reg2.active_profile_id == "cloud-1"
    assert reg2.active_profile_for_capability("language").profile_id == "cloud-1"
    # 非 secret 字段 round-trip
    cloud = reg2.get("cloud-1")
    assert cloud.endpoint == "http://localhost:8000"
    assert cloud.credential_ref == "keychain:topos:cloud-1"


def test_loaded_profile_has_no_secret_field():
    """配置里只有 credential_ref，绝不含 secret 本身。"""
    cfg = _FakeConfig()
    reg = ProfileRegistry()
    reg.add(_profile("local-1"))
    reg.save_to_config(cfg)
    stored = cfg.get(ProfileRegistry.CONFIG_KEY_PROFILES)
    # 遍历所有值，不应出现疑似 secret 字样
    blob = str(stored)
    assert "api_key" not in blob.lower()
    assert "sk-" not in blob
    # credential_ref 是允许的（引用）
    assert "credential_ref" in blob


def test_missing_config_keeps_empty_registry():
    """首启（无配置）→ 不抛，注册表为空。"""
    cfg = _FakeConfig()
    reg = ProfileRegistry()
    reg.load_from_config(cfg)
    assert reg.list() == []
    assert reg.active_profile_id == ""


def test_corrupt_profile_skipped_not_fatal():
    """损坏的单条 profile 被跳过，其它仍加载（降级，不整体失败）。"""
    cfg = _FakeConfig()
    cfg.set(ProfileRegistry.CONFIG_KEY_PROFILES, [
        _profile("good").to_dict(),
        {"profile_id": "bad"},  # 缺 display_name → from_dict 抛 ValueError
        "not-a-dict",
    ])
    reg = ProfileRegistry()
    reg.load_from_config(cfg)
    assert {p.profile_id for p in reg.list()} == {"good"}


def test_active_id_not_in_profiles_ignored():
    cfg = _FakeConfig()
    cfg.set(ProfileRegistry.CONFIG_KEY_PROFILES, [_profile("local-1").to_dict()])
    cfg.set(ProfileRegistry.CONFIG_KEY_ACTIVE, "ghost")
    reg = ProfileRegistry()
    reg.load_from_config(cfg)
    # ghost 不在注册表 → active 保持空（不抛）
    assert reg.active_profile_id == ""


# ================================================================
# PreferencesDialog 注册 edit.ai_editing（M1.4）
# ================================================================


def test_preferences_dialog_registers_ai_editing_page(qapp):
    """edit.ai_editing 不再落入 unknown-page warning 分支。

    直接调 ``_build_page_registry``，提供 app-level page factory 的桩，避免构造整个
    dialog（需要 config_manager 等）。重点验证 module-page 分支为 edit.ai_editing
    注册了可见页（不再落入 unknown-page warning）。
    """
    from src.app.ui.preferences_dialog import PreferencesDialog
    from src.features.edit.ui.ai_preferences_page import AiEditingPreferencePage

    ai_page = AiEditingPreferencePage()
    # _build_page_registry 引用多个 self._create_*_page 作为 factory；用 __getattr__
    # 兜底返回一个 no-op factory，避免为每个都写桩。
    class _FakeSelf:
        def __init__(self):
            self._module_pages = {}

        def __getattr__(self, name):
            # 任何 _create_*_page / _setup_* 调用都返回 no-op factory
            return lambda *a, **k: None

    registry = PreferencesDialog._build_page_registry(_FakeSelf(), [ai_page])
    pages = registry.get_pages()
    pids = {p.page_id for p in pages}
    assert "edit.ai_editing" in pids
    ai = next(p for p in pages if p.page_id == "edit.ai_editing")
    assert ai.page_object is ai_page
    assert ai.feature_id == "edit"
