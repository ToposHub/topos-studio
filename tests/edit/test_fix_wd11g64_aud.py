"""wd11-g64 批次修复回归测试（REMEDIATION_PLAN 批 D，edit 模块/偏好/UI a 段域）。

覆盖条目：
- AUD-19302 View/Timeline 菜单可达性（由 AUD-3600 的 MenuBuilder 自动补建修复）
- AUD-19305 preferences.py 外部 config 数值键类型/范围归一
- AUD-19400/19401 ADR 试听解码 container 句柄 + 时长/大小护栏
- AUD-19410 AiBriefEditor 自定义 beat 无损往返
- AUD-19500 "改用内置模板"独立信号
- AUD-19501 消息未变时跳过全量重建
- AUD-19502 MG 方案卡畸形 card_data 不逃逸异常
- AUD-19503 外部文本 HTML 转义
- AUD-19603/19604 测试连接后台化 / 本地 profile 不再标"已通过"
- AUD-19605 切换 profile 时保存输入中的 API key
- AUD-19606 凭据删除延后到 apply()
- AUD-19607 键入路径不整表重建、凭据读盘缓存
- AUD-19619 diff 预检警告渲染与透传
"""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.edit_ui


# ══════════════════════════════════════════════════════════════════════ #
# AUD-19302：View/Timeline 5 个动作经 MenuBuilder 自动补建顶层菜单后可达
# ══════════════════════════════════════════════════════════════════════ #


class TestAud19302MenuReachability:
    _EXPECTED = (
        "edit.timeline.zoom_in",
        "edit.timeline.zoom_out",
        "edit.timeline.fit",
        "edit.playback.hud",
        "edit.playback.export_perf_report",
    )

    def test_view_timeline_actions_buildable(self, qapp):
        """未注册 View 顶级菜单时，MenuBuilder 也应为这 5 个动作建出 QAction。"""
        from PyQt6.QtWidgets import QMenuBar
        from src.app.menu.menu_builder import MenuBuilder
        from src.features.edit.module import EditModule
        from src.shared.actions import ActionRegistry

        registry = ActionRegistry()
        EditModule().register_menu_actions(registry)
        for action_id in self._EXPECTED:
            assert registry.get_action(action_id) is not None, action_id

        bar = QMenuBar()
        builder = MenuBuilder(
            menu_bar=bar,
            action_registry=registry,
            callback_resolver=lambda _name: (lambda *_a: None),
            shortcut_manager=None,
        )
        built = builder.build(enabled_features={"edit"})
        for action_id in self._EXPECTED:
            assert action_id in built, f"{action_id} 未构建（菜单不可达）"


# ══════════════════════════════════════════════════════════════════════ #
# AUD-19305：preferences 数值键归一
# ══════════════════════════════════════════════════════════════════════ #


class TestAud19305ConfigValidation:
    def test_get_scene_cut_settings_bad_types_fall_back(self) -> None:
        from src.features.edit.preferences import get_scene_cut_settings_from_config

        settings = get_scene_cut_settings_from_config(
            {
                "scene_cut_sensitivity": "garbage",
                "scene_cut_use_proxy": "not-a-bool",
                "scene_cut_min_scene_frames": None,
            }
        )
        assert settings["sensitivity"] == 0.55
        assert settings["use_proxy"] is True
        assert settings["min_scene_frames"] == 8

    def test_get_scene_cut_settings_out_of_range_clamped(self) -> None:
        from src.features.edit.preferences import get_scene_cut_settings_from_config

        settings = get_scene_cut_settings_from_config(
            {
                "scene_cut_sensitivity": 0.95,
                "scene_cut_min_scene_frames": -5,
            }
        )
        assert settings["sensitivity"] == 0.75
        assert settings["min_scene_frames"] == 2

    def test_load_with_corrupt_config_does_not_raise(self, qapp) -> None:
        """load 中途抛 TypeError 的旧缺陷会半加载 + Apply 写回默认值覆盖用户设置。"""
        from src.features.edit.preferences import EditPreferencePage

        page = EditPreferencePage()
        widget = page.create_widget(None)  # noqa: F841 - 持有引用防止 C++ 对象被回收
        page.load(
            {
                "scene_cut_sensitivity": "garbage",
                "scene_cut_min_scene_frames": "x",
                "video_preview_max_queue": 999,
                "video_pipeline_allow_v2_fallback": "yes",
                "scene_cut_use_proxy": 1,
            }
        )
        assert page._widgets["scene_cut_sensitivity"].value() == 55  # 默认 0.55
        assert page._widgets["scene_cut_min_scene_frames"].intValue() == 8
        assert page._widgets["video_preview_max_queue"].intValue() == 32  # clamp
        assert page._widgets["video_pipeline_allow_v2_fallback"].isChecked() is True

    def test_restore_defaults_with_corrupt_values_does_not_raise(self, qapp) -> None:
        from src.features.edit.preferences import EditPreferencePage

        page = EditPreferencePage()
        widget = page.create_widget(None)  # noqa: F841 - 持有引用防止 C++ 对象被回收
        page.restore_defaults(
            {"scene_cut_sensitivity": [0.5], "video_preview_max_queue": "bad"}
        )
        assert page._widgets["scene_cut_sensitivity"].value() == 55
        assert page._widgets["video_preview_max_queue"].intValue() == 3


# ══════════════════════════════════════════════════════════════════════ #
# AUD-19400 / AUD-19401：ADR 试听解码护栏
# ══════════════════════════════════════════════════════════════════════ #


class _FakeAvFrame:
    sample_rate = 48000

    def to_ndarray(self) -> np.ndarray:
        return np.zeros((1, 480), dtype=np.float32)


class _FakeAvContainer:
    def __init__(self, duration_us: float = 100_000, n_frames: int = 2,
                 exc: Exception | None = None) -> None:
        self.duration = duration_us
        self._n_frames = n_frames
        self._exc = exc
        self.closed = False
        self.decoded_frames = 0

    def decode(self, audio: int = 0):
        for _ in range(self._n_frames):
            if self._exc is not None:
                raise self._exc
            self.decoded_frames += 1
            yield _FakeAvFrame()

    def close(self) -> None:
        self.closed = True


def _install_fake_av(monkeypatch, container: _FakeAvContainer) -> None:
    module = types.ModuleType("av")
    module.open = lambda _path: container  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "av", module)
    # 预检 getsize 需要一个存在的"小文件"
    monkeypatch.setattr(os.path, "getsize", lambda _p: 1024)


class TestAud19400DecodeContainerLeak:
    def test_decode_exception_closes_container(self, monkeypatch) -> None:
        from src.features.edit.ui.adr_take_sheet_panel import TakeAuditionPlayer

        container = _FakeAvContainer(exc=RuntimeError("corrupt take"))
        _install_fake_av(monkeypatch, container)
        samples, _rate, _ch = TakeAuditionPlayer._decode("/take.wav")
        assert samples is None
        assert container.closed is True, "异常路径必须关闭 container（句柄泄漏）"

    def test_decode_success_closes_container_once(self, monkeypatch) -> None:
        from src.features.edit.ui.adr_take_sheet_panel import TakeAuditionPlayer

        container = _FakeAvContainer(n_frames=2)
        _install_fake_av(monkeypatch, container)
        samples, rate, ch = TakeAuditionPlayer._decode("/take.wav")
        assert samples is not None and samples.shape[1] == 960
        assert (rate, ch) == (48000, 1)
        assert container.closed is True


class TestAud19401DecodeGuards:
    def test_oversize_file_refused_before_decode(self, monkeypatch) -> None:
        from src.features.edit.ui.adr_take_sheet_panel import TakeAuditionPlayer

        container = _FakeAvContainer(n_frames=5)
        _install_fake_av(monkeypatch, container)
        monkeypatch.setattr(
            os.path, "getsize", lambda _p: TakeAuditionPlayer._MAX_AUDITION_FILE_BYTES + 1
        )
        samples, _, _ = TakeAuditionPlayer._decode("/take.wav")
        assert samples is None
        assert container.decoded_frames == 0

    def test_overlong_duration_refused_before_decode(self, monkeypatch) -> None:
        from src.features.edit.ui.adr_take_sheet_panel import TakeAuditionPlayer

        container = _FakeAvContainer(
            duration_us=(TakeAuditionPlayer._MAX_AUDITION_SECONDS + 1) * 1_000_000,
            n_frames=5,
        )
        _install_fake_av(monkeypatch, container)
        samples, _, _ = TakeAuditionPlayer._decode("/take.wav")
        assert samples is None
        assert container.decoded_frames == 0
        assert container.closed is True

    def test_sample_cap_truncates_decode(self, monkeypatch) -> None:
        from src.features.edit.ui.adr_take_sheet_panel import TakeAuditionPlayer

        container = _FakeAvContainer(n_frames=10)
        _install_fake_av(monkeypatch, container)
        monkeypatch.setattr(TakeAuditionPlayer, "_MAX_AUDITION_SAMPLES", 500)
        samples, _, _ = TakeAuditionPlayer._decode("/take.wav")
        assert samples is not None
        assert samples.shape[1] == 480  # 第 2 帧越限即停，仅保留第 1 块
        assert container.decoded_frames == 2  # 第 2 帧被拉取后丢弃，第 3 帧起不再解码
        assert container.closed is True


# ══════════════════════════════════════════════════════════════════════ #
# AUD-19410：自定义 beat 往返
# ══════════════════════════════════════════════════════════════════════ #


class TestAud19410CustomBeats:
    def test_custom_beats_survive_roundtrip(self, qapp) -> None:
        from src.features.edit.ui.ai_brief_editor import AiBriefEditor
        from src.features.edit.ai_editing.editorial_models import EditorialBrief

        editor = AiBriefEditor()
        brief = EditorialBrief(
            brief_id="b1",
            required_beats=("hook", "custom_beat", "product_value"),
        )
        editor.set_brief(brief)

        texts = [
            editor._beats_list.item(i).text()
            for i in range(editor._beats_list.count())
        ]
        assert "custom_beat" in texts, "自定义 beat 必须追加进列表"
        collected = editor._collect_brief()
        assert "custom_beat" in collected.required_beats
        assert "hook" in collected.required_beats
        assert "product_value" in collected.required_beats
        assert "problem" not in collected.required_beats  # 未勾选的固定项被排除

    def test_set_brief_twice_does_not_duplicate(self, qapp) -> None:
        from src.features.edit.ui.ai_brief_editor import AiBriefEditor
        from src.features.edit.ai_editing.editorial_models import EditorialBrief

        editor = AiBriefEditor()
        brief = EditorialBrief(brief_id="b1", required_beats=("hook", "custom_beat"))
        editor.set_brief(brief)
        editor.set_brief(brief)
        texts = [
            editor._beats_list.item(i).text()
            for i in range(editor._beats_list.count())
        ]
        assert texts.count("custom_beat") == 1


# ══════════════════════════════════════════════════════════════════════ #
# AUD-19500 / 19501 / 19502 / 19503：AI 编辑助手面板
# ══════════════════════════════════════════════════════════════════════ #


@pytest.fixture
def assistant_panel(qapp):
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    p = AiEditAssistantPanel()
    yield p
    p.deleteLater()


class TestAud19500TemplateSignal:
    def test_falls_back_to_confirm_without_subscriber(self, assistant_panel) -> None:
        panel = assistant_panel
        confirm, template = [], []
        panel.motion_graphic_confirm_requested.connect(lambda rid: confirm.append(rid))
        panel.motion_graphic_template_requested.connect(lambda rid: template.append(rid))
        # 断开 template 订阅，模拟尚未接线的旧宿主
        panel.motion_graphic_template_requested.disconnect()

        panel._request_motion_graphic_template("r1")
        assert confirm == ["r1"]
        assert template == []

    def test_dedicated_signal_used_when_subscribed(self, assistant_panel) -> None:
        panel = assistant_panel
        confirm, template = [], []
        panel.motion_graphic_confirm_requested.connect(lambda rid: confirm.append(rid))
        panel.motion_graphic_template_requested.connect(lambda rid: template.append(rid))

        panel._request_motion_graphic_template("r1")
        assert template == ["r1"]
        assert confirm == []


class TestAud19501SkipUnchangedRebuild:
    def _snapshot(self, text2: str):
        from src.features.edit.ai_editing.assistant_events import (
            AssistantMessage,
            AssistantStateSnapshot,
        )

        return AssistantStateSnapshot(
            messages=(
                AssistantMessage(message_id="m1", role="user", text="帮我剪口播"),
                AssistantMessage(message_id="m2", role="assistant", text=text2),
            ),
        )

    def test_unchanged_snapshot_keeps_cards(self, assistant_panel) -> None:
        panel = assistant_panel
        panel.refresh_state(self._snapshot("第一版"))
        first = list(panel._message_widgets)
        assert len(first) == 2

        panel.refresh_state(self._snapshot("第一版"))
        assert panel._message_widgets == first, "消息未变时不应销毁重建卡片"

    def test_changed_text_rebuilds(self, assistant_panel) -> None:
        panel = assistant_panel
        panel.refresh_state(self._snapshot("第一版"))
        first = list(panel._message_widgets)

        panel.refresh_state(self._snapshot("第二版"))
        assert panel._message_widgets != first
        assert len(panel._message_widgets) == 2


class TestAud19502MalformedCardData:
    def test_malformed_fields_do_not_raise(self, assistant_panel) -> None:
        panel = assistant_panel
        panel.set_motion_graphic_proposal_card(
            "req1",
            {
                "title": "t",
                "canvas_size": "bad-value",
                "duration_seconds": "not-a-number",
                "quality_warnings": None,
                "quality_errors": None,
                "risks": None,
                "elements": None,
                "actions": None,
                "mode": "plan_review",
                "plan_rows": None,
            },
        )
        assert "req1" in panel._motion_graphic_proposals

    def test_non_dict_payload_caught_by_component_error(self, assistant_panel, monkeypatch) -> None:
        panel = assistant_panel
        errors: list[object] = []
        monkeypatch.setattr(
            panel, "_show_component_error",
            lambda component, exc: errors.append((component, exc)),
        )
        panel.set_motion_graphic_proposal_card("req2", object())  # type: ignore[arg-type]
        assert len(errors) == 1
        # _updating 锁必须复位
        assert panel._updating is False


class TestAud19503HtmlEscape:
    def test_variant_label_escaped(self, assistant_panel) -> None:
        from src.features.edit.ai_editing.assistant_events import VariantCard
        from PyQt6.QtWidgets import QLabel

        card = assistant_panel._build_variant_card(
            VariantCard(variant_id="v1", label="<b>x&y</b>"), True
        )
        title = card.findChildren(QLabel)[0]
        assert "&lt;b&gt;x&amp;y&lt;/b&gt;" in title.text()

    def test_motion_graphic_title_escaped(self, assistant_panel) -> None:
        from PyQt6.QtWidgets import QLabel

        assistant_panel.set_motion_graphic_proposal_card(
            "req3", {"title": "<i>injected</i>", "is_fallback": True,
                     "source_label": "llm&<b>"}
        )
        card = assistant_panel._motion_graphic_proposals["req3"]
        heading = card.findChildren(QLabel)[0].text()
        assert "<i>" not in heading
        assert "&lt;i&gt;injected&lt;/i&gt;" in heading


# ══════════════════════════════════════════════════════════════════════ #
# AUD-19603 / 19604 / 19605 / 19606 / 19607：AI 偏好页
# ══════════════════════════════════════════════════════════════════════ #


class _CountingCredentialStore:
    """内存凭据仓 + 读盘计数（验证 AUD-19607 的缓存）。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.get_calls = 0
        self.set_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []

    def get_secret(self, credential_ref: str) -> str | None:
        self.get_calls += 1
        return self._store.get(credential_ref)

    def set_secret(self, credential_ref: str, secret: str) -> None:
        self.set_calls.append((credential_ref, secret))
        self._store[credential_ref] = secret

    def delete_secret(self, credential_ref: str) -> None:
        self.delete_calls.append(credential_ref)
        self._store.pop(credential_ref, None)


class _FakeConfigManager:
    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def get(self, key: str, default: object = None) -> object:
        return self._data.get(key, default)

    def set(self, key: str, value: object, auto_save: bool = False) -> None:
        self._data[key] = value


def _make_profile(pid: str, *, endpoint: str = "https://api.example.com",
                  source_kind: str = "", capabilities: tuple = ("language",)):
    from src.features.edit.ai_editing.provider_profiles import AiProviderProfile

    return AiProviderProfile(
        profile_id=pid,
        display_name=pid,
        provider_kind="openai",
        endpoint=endpoint,
        model_id="gpt-test",
        credential_ref=f"topos:{pid}",
        source_kind=source_kind,
        capabilities=capabilities,
    )


def _make_page(qapp, profiles, store):
    from src.features.edit.ui.ai_preferences_page import AiEditingPreferencePage
    from src.features.edit.ai_editing.provider_profiles import ProfileRegistry

    registry = ProfileRegistry()
    for p in profiles:
        registry.add(p)
    if not registry.active_profile_id:
        registry.set_active(profiles[0].profile_id)
    page = AiEditingPreferencePage(registry=registry, credential_store=store)
    # 持有引用防止 C++ widget 被 Python GC 回收
    page._owned_widget = page.create_widget(None)  # type: ignore[attr-defined]
    return page


def _isolate_user_files(monkeypatch, page) -> None:
    """apply() 不应触碰真实 ~/.topos 文件。"""
    from src.features.edit.ai_editing import credential_store as cs_module

    monkeypatch.setattr(cs_module, "set_credential_storage_preference", lambda loc: None)
    monkeypatch.setattr(page._registry, "save_to_user_file", lambda *a, **k: None)


class TestAud19603BackgroundTestConnection:
    def _run_worker_to_completion(self, qapp, page, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while page._test_worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()
        assert page._test_worker is None, "后台连接测试未在超时内完成"

    def test_success_via_background_worker(self, qapp, monkeypatch) -> None:
        import src.features.edit.ui.ai_preferences_page as prefs_mod
        from PyQt6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
        monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))

        class _FakeClient:
            def test_connection(self) -> None:
                return None

        monkeypatch.setattr(prefs_mod, "make_client", lambda profile, store: _FakeClient())

        store = _CountingCredentialStore()
        page = _make_page(qapp, [_make_profile("p1")], store)
        page._on_test_connection()
        self._run_worker_to_completion(qapp, page)
        assert page._connection_results.get("p1") is True
        assert page._widgets["test_btn"].isEnabled()

    def test_failure_via_background_worker(self, qapp, monkeypatch) -> None:
        import src.features.edit.ui.ai_preferences_page as prefs_mod
        from PyQt6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
        monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))

        class _FakeClient:
            def test_connection(self) -> None:
                raise RuntimeError("boom")

        monkeypatch.setattr(prefs_mod, "make_client", lambda profile, store: _FakeClient())

        store = _CountingCredentialStore()
        page = _make_page(qapp, [_make_profile("p1")], store)
        page._on_test_connection()
        self._run_worker_to_completion(qapp, page)
        assert page._connection_results.get("p1") == "boom"
        assert page._widgets["test_btn"].isEnabled()


class TestAud19604LocalUnverified:
    def test_local_profile_not_marked_tested(self, qapp, monkeypatch) -> None:
        from src.features.edit.ai_editing.ai_provider_catalog import SOURCE_LOCAL
        from PyQt6.QtWidgets import QMessageBox

        shown: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "information",
            staticmethod(lambda _p, _t, text, *a, **k: shown.append(text)),
        )
        store = _CountingCredentialStore()
        page = _make_page(qapp, [_make_profile("loc", source_kind=SOURCE_LOCAL)], store)
        page._on_test_connection()
        assert "loc" not in page._connection_results, "本地分支不得写入已通过标记"
        assert "不会将其标记为已通过连接测试" in shown[0]


class TestAud19605PendingKeyFlush:
    def test_switch_flushes_pending_key_to_old_profile(self, qapp) -> None:
        store = _CountingCredentialStore()
        page = _make_page(qapp, [_make_profile("pa"), _make_profile("pb")], store)
        lst = page._widgets["profile_list"]
        assert lst.count() == 2
        assert page._current_profile_id == "pa"

        page._widgets["api_key"].setText("secret-A")
        assert page._pending_api_key == "secret-A"
        lst.setCurrentRow(1)  # 切换到 pb
        assert page._current_profile_id == "pb"
        assert store._store.get("topos:pa") == "secret-A", "切换时 key 必须落盘到旧 profile"

    def test_empty_pending_key_keeps_stored_secret(self, qapp) -> None:
        store = _CountingCredentialStore()
        store.set_secret("topos:pa", "old-secret")
        page = _make_page(qapp, [_make_profile("pa"), _make_profile("pb")], store)
        page._widgets["api_key"].setText("")  # 留空 = 保持已存 key
        page._on_profile_selected(page._widgets["profile_list"].item(1), None)
        assert store._store.get("topos:pa") == "old-secret"


class TestAud19606DeferredSecretDeletion:
    def test_remove_defers_delete_until_apply(self, qapp, monkeypatch) -> None:
        store = _CountingCredentialStore()
        store.set_secret("topos:p1", "doomed")
        page = _make_page(qapp, [_make_profile("p1")], store)
        _isolate_user_files(monkeypatch, page)

        page._on_remove_profile()
        assert store.delete_calls == [], "删除 profile 时不得立即删盘"
        assert store._store.get("topos:p1") == "doomed"
        assert page._pending_secret_deletions == ["topos:p1"]

        page.apply(_FakeConfigManager())
        assert store.delete_calls == ["topos:p1"]
        assert store._store.get("topos:p1") is None


class TestAud19607TypingPathPerformance:
    def test_typing_updates_label_in_place_without_rebuild(self, qapp, monkeypatch) -> None:
        store = _CountingCredentialStore()
        page = _make_page(qapp, [_make_profile("p1"), _make_profile("p2")], store)
        lst = page._widgets["profile_list"]
        rebuilds = []
        monkeypatch.setattr(
            page, "_refresh_profile_list",
            lambda *a, **k: rebuilds.append(1),
        )

        reads_before = store.get_calls
        page._widgets["display_name"].setText("Renamed")
        assert rebuilds == [], "键入路径不得整表重建列表"
        assert "Renamed" in lst.item(0).text(), "标签须就地更新"
        assert store.get_calls == reads_before, "凭据读取须命中缓存"

    def test_credential_cache_invalidated_on_write(self, qapp) -> None:
        store = _CountingCredentialStore()
        page = _make_page(qapp, [_make_profile("pa"), _make_profile("pb")], store)
        page._widgets["api_key"].setText("k1")
        page._on_profile_selected(page._widgets["profile_list"].item(1), None)
        # 写入后缓存失效：下一次读取应重新击穿并拿到新值
        assert page._credential_secret_present("topos:pa") is True
        assert store._store.get("topos:pa") == "k1"


# ══════════════════════════════════════════════════════════════════════ #
# AUD-19619：diff 预检警告
# ══════════════════════════════════════════════════════════════════════ #


class TestAud19619DiffWarnings:
    def test_warnings_rendered_and_exposed(self, qapp) -> None:
        from src.features.edit.ui.ai_timeline_diff import AiTimelineDiffPanel

        panel = AiTimelineDiffPanel()
        panel.set_diff(
            {
                "added": [{"change": "add", "clip_id": "c1", "track_id": "v1"}],
                "removed": [],
                "moved": [],
                "trimmed": [],
                "warnings": ["音乐版权：Fast Pop 01 需授权", "降级 effect：blur→none"],
                "total_changes": 1,
                "duration_before": 100,
                "duration_after": 90,
            }
        )
        assert panel.current_warnings == (
            "音乐版权：Fast Pop 01 需授权",
            "降级 effect：blur→none",
        )
        # 2 条警告 + 1 条 added + 尾部 stretch
        assert panel._container_layout.count() == 4
        assert "预检警告 2" in panel._summary_label.text()

    def test_missing_warnings_defaults_empty(self, qapp) -> None:
        from src.features.edit.ui.ai_timeline_diff import AiTimelineDiffPanel

        panel = AiTimelineDiffPanel()
        panel.set_diff({"added": [], "removed": [], "warnings": None})
        assert panel.current_warnings == ()
        assert panel._container_layout.count() == 1  # 仅 stretch
