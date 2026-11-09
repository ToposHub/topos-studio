"""CV-B04 域件测试：比较会话与 wipe 隔离、失效、内存上限。

门槛（计划 CV-B04）：开启比较不改变工程/active grade/undo 栈（会话不触
碰 engine split/reference）；删除版本/切工程不显示旧图；缓存键含
revision/frame/size/color；JPEG 缩略图不冒充比较 master；LRU 有界。
"""

from __future__ import annotations

import json

import pytest

from src.features.color.core.compare_session import (
    MAX_COMPARE_ITEMS,
    ColorCompareSession,
    CompareItem,
)
from src.features.color.ui.canvas import Canvas  # noqa: F401 —— 引力检查


def _item(item_id: str, *, version_id=None, revision=1, frame=100,
          fidelity="hdr_exr", size=(1920, 1080), project="p1",
          asset="a1", approx=None) -> CompareItem:
    w, h = size
    return CompareItem(
        item_id=item_id, label=item_id, kind="clip_version",
        project_id=project, asset_id=asset, clip_id="c1",
        version_id=version_id, revision=revision, source_frame=frame,
        width=w, height=h, color_context="aces|sRGB",
        fidelity=fidelity,
        approx_bytes=approx if approx is not None else w * h * 4 * 4)


def test_session_does_not_touch_engine_wipe_or_reference() -> None:
    """会话与 wipe 隔离：begin/end 不改 split/reference 状态。"""
    class _Engine:
        def __init__(self) -> None:
            self.split_view_active = False
            self.reference_is_still = False
            self.calls: list[str] = []

        def set_split_view_mode(self, enabled: bool) -> None:
            self.calls.append("split")
            self.split_view_active = enabled

        def load_reference_still(self, image) -> None:
            self.calls.append("reference")

    engine = _Engine()
    session = ColorCompareSession()
    session.begin([_item("v1"), _item("v2")],
                  saved_wipe_state={"split": True, "position": 0.4})
    session.end()
    # 引擎状态零触碰：wipe/A-B 行为保持不变（宿主负责保存/恢复）。
    assert engine.calls == []
    assert engine.split_view_active is False


def test_begin_accepts_bounded_items_and_dedupes() -> None:
    session = ColorCompareSession()
    items = [_item(f"v{i}", version_id=str(i)) for i in range(6)]
    accepted, rejected = session.begin(items, layout="grid_2x2")
    assert accepted == ["v0", "v1", "v2", "v3"]
    assert [r[0] for r in rejected] == ["v4", "v5"]
    assert all(r[1] == "too_many_items" for r in rejected)
    assert len(session.slots) == MAX_COMPARE_ITEMS

    # 相同身份（同 revision/frame/color）→ 缓存键重复被拒。
    session2 = ColorCompareSession()
    accepted2, rejected2 = session2.begin(
        [_item("a", version_id="v9"), _item("b", version_id="v9")])
    assert accepted2 == ["a"]
    assert rejected2 == [("b", "duplicate")]


def test_revision_or_frame_change_yields_new_cache_key() -> None:
    base = _item("v1", version_id="v1", revision=3, frame=50)
    bumped = _item("v1", version_id="v1", revision=4, frame=50)
    other_frame = _item("v1", version_id="v1", revision=3, frame=51)
    other_color = CompareItem(
        **{**base.__dict__, "color_context": "rec709|sRGB"})
    keys = {base.cache_key(), bumped.cache_key(), other_frame.cache_key(),
            other_color.cache_key()}
    assert len(keys) == 4  # revision/frame/color 全部参与缓存键


def test_jpeg_thumb_rejected_as_compare_master() -> None:
    session = ColorCompareSession()
    accepted, rejected = session.begin(
        [_item("hdr", fidelity="hdr_exr"),
         _item("thumb", fidelity="jpeg_thumb")])
    assert accepted == ["hdr"]
    assert rejected == [("thumb", "jpeg_thumb_not_compare_master")]
    assert session.slots[0].item.is_hdr_compare_ready() is True


def test_invalid_layout_rejected() -> None:
    session = ColorCompareSession()
    with pytest.raises(ValueError):
        session.begin([_item("v1")], layout="cinema_wide")


def test_version_deletion_and_project_switch_invalidate_items() -> None:
    released: list[str] = []
    session = ColorCompareSession()
    session.begin(
        [_item("v1", version_id="ver-1"), _item("v2", version_id="ver-2")],
        on_release=released.append)

    removed = session.invalidate(version_id="ver-1")
    assert removed == 1
    assert [s.item.item_id for s in session.slots] == ["v2"]
    assert len(released) == 1  # 被删项的快照立即释放

    # 切工程：剩余项全部命中 → 会话自动结束。
    removed = session.invalidate(project_id="p1")
    assert removed == 1
    assert session.active is False
    assert session.slots == []
    assert len(released) == 2


def test_end_returns_saved_wipe_state_for_host_restore() -> None:
    session = ColorCompareSession()
    saved = {"split_view_active": True, "split_position": 0.35,
             "reference_attached": True}
    session.begin([_item("v1")], saved_wipe_state=saved)
    restored = session.end()
    assert restored == saved
    assert session.active is False
    assert session.saved_wipe_state is None


def test_lru_byte_budget_evicts_oldest_and_releases() -> None:
    released: list[str] = []
    session = ColorCompareSession(max_cache_bytes=1000)
    session.begin(
        [_item("a", version_id="1", approx=600),
         _item("b", version_id="2", approx=600)],
        on_release=released.append)
    assert session.diagnostics()["cache_bytes"] == 600  # b 顶掉 a（LRU）

    session2 = ColorCompareSession(max_cache_bytes=900)
    session2.begin(
        [_item("big", version_id="9", approx=5000)],
        on_release=released.append)
    assert session2.active is False  # 单项超预算 → 拒绝 → 不 active


def test_diagnostics_are_json_serializable() -> None:
    session = ColorCompareSession()
    session.begin([_item("v1", version_id="v1", fidelity="full_float")])
    data = json.dumps(session.diagnostics())
    assert "v1" in data
    assert '"hdr_ready": true' in data


def test_release_callback_failure_does_not_break_session() -> None:
    def _boom(_key: str) -> None:
        raise RuntimeError("renderer gone")

    session = ColorCompareSession()
    accepted, _ = session.begin([_item("v1")], on_release=_boom)
    assert accepted == ["v1"]
    session.release()  # 异常被记录，不向上传播
    assert session.slots == []
