"""C2 阶段 1: 用户自定义转场（参数化模板实例）测试。

覆盖计划验收：
- JSON 加载：合法 / 坏 JSON / 缺字段 / 未知 base / 重复 id
- resolve/params 合并（实例优先）
- TransitionInstance 接受已注册的用户类型；未知类型仍拒绝
- is_spatial_transition 透传 base 空间属性；渲染 mask 走 base 公式
- 旧工程（无用户转场）不受影响
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core import custom_transitions as ct  # noqa: E402
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    TransitionInstance,
)


@pytest.fixture(autouse=True)
def user_dir(tmp_path: Path, monkeypatch) -> Path:
    """Presets 根目录重定向到临时目录，并在每次用例后清空注册表。"""
    monkeypatch.chdir(tmp_path)
    ct._REGISTRY.clear()
    yield Path(ct.user_transitions_dir())
    ct._REGISTRY.clear()


def _write(name: str, payload) -> Path:
    directory = Path(ct.user_transitions_dir())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(payload, encoding="utf-8")
    return path


class TestUserTransitionLoading:
    def test_valid_declaration_registers(self) -> None:
        _write("my_wipe.json", {
            "id": "my_wipe", "label": "我的斜擦", "base": "wipe",
            "params": {"direction": "left", "softness": 0.2},
        })
        registry = ct.reload_user_transitions()
        assert "user_my_wipe" in registry
        entry = registry["user_my_wipe"]
        assert entry.label == "我的斜擦"
        assert entry.base == "wipe"
        assert entry.params == {"direction": "left", "softness": 0.2}
        assert entry.spatial is True

    def test_bad_json_skipped(self) -> None:
        _write("broken.json", "{not json")
        registry = ct.reload_user_transitions()
        assert registry == {}

    def test_unknown_base_skipped(self) -> None:
        _write("bad_base.json", {
            "id": "x", "label": "X", "base": "plasma_wipe", "params": {},
        })
        assert ct.reload_user_transitions() == {}

    def test_missing_fields_skipped(self) -> None:
        _write("no_label.json", {"id": "a", "base": "wipe"})
        _write("no_id.json", {"label": "B", "base": "wipe"})
        assert ct.reload_user_transitions() == {}

    def test_duplicate_id_skipped(self) -> None:
        payload = {"id": "dup", "label": "First", "base": "wipe", "params": {}}
        _write("one.json", payload)
        _write("two.json", {**payload, "label": "Second"})
        registry = ct.reload_user_transitions()
        assert [e.label for e in registry.values()] == ["First"]

    def test_non_spatial_base(self) -> None:
        _write("my_dip.json", {
            "id": "my_dip", "label": "我的黑场", "base": "dip_to_color",
            "params": {"color": "#000000"},
        })
        entry = ct.reload_user_transitions()["user_my_dip"]
        assert entry.spatial is False


class TestUserTransitionResolution:
    def test_params_merge_instance_priority(self) -> None:
        _write("my_wipe.json", {
            "id": "my_wipe", "label": "W", "base": "wipe",
            "params": {"direction": "left", "softness": 0.2},
        })
        ct.reload_user_transitions()
        resolved = ct.merge_user_transition_params(
            "user_my_wipe", {"direction": "right", "angle": 45})
        assert resolved is not None
        base, params = resolved
        assert base == "wipe"
        # 实例优先
        assert params["direction"] == "right"
        # 定义参数兜底
        assert params["softness"] == 0.2

    def test_builtin_type_not_resolved(self) -> None:
        ct.reload_user_transitions()
        assert ct.merge_user_transition_params("wipe", None) is None


class TestModelAndRenderIntegration:
    def test_instance_accepts_registered_user_type(self) -> None:
        _write("my_wipe.json", {
            "id": "my_wipe", "label": "W", "base": "wipe", "params": {},
        })
        ct.reload_user_transitions()
        instance = TransitionInstance(
            transition_type="user_my_wipe",
            track_id="v1", left_clip_id="a", right_clip_id="b", duration=12,
        )
        assert instance.transition_type == "user_my_wipe"
        # params 独立保存进实例（序列化走既有 to_dict）
        assert instance.params == {}

    def test_instance_rejects_unregistered_type(self) -> None:
        with pytest.raises(ValueError, match="Invalid transition_type"):
            TransitionInstance(
                transition_type="user_never_registered",
                track_id="v1", left_clip_id="a", right_clip_id="b", duration=12,
            )

    def test_spatial_flag_inherits_base(self) -> None:
        from src.features.edit.core.transition_catalog import (
            is_spatial_transition,
        )

        _write("my_wipe.json", {
            "id": "my_wipe", "label": "W", "base": "wipe", "params": {},
        })
        ct.reload_user_transitions()
        assert is_spatial_transition("user_my_wipe") is True
        assert is_spatial_transition("wipe") is True

    def test_mask_uses_base_formula_with_merged_params(self) -> None:
        """用户 wipe 的遮罩与 base 同公式（direction 参数生效）。"""
        from src.features.edit.core.transition_mask import (
            compute_transition_mask,
        )

        _write("my_left.json", {
            "id": "my_left", "label": "L", "base": "wipe",
            "params": {"direction": "left"},
        })
        ct.reload_user_transitions()
        user_mask = compute_transition_mask(64, 36, 0.5, "user_my_left", None)
        base_mask = compute_transition_mask(
            64, 36, 0.5, "wipe", {"direction": "left"})
        assert np.array_equal(user_mask, base_mask)

    def test_legacy_project_untouched(self) -> None:
        """未注册任何用户转场时旧类型行为完全不变。"""
        instance = TransitionInstance(
            transition_type="cross_dissolve",
            track_id="v1", left_clip_id="a", right_clip_id="b", duration=12,
        )
        assert instance.transition_type == "cross_dissolve"
        with pytest.raises(ValueError):
            TransitionInstance(
                transition_type="user_unknown",
                track_id="v1", left_clip_id="a", right_clip_id="b", duration=12,
            )
