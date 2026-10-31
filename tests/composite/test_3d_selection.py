"""F3 对象与组件选择单测（master_plan §6 / §21.6）。

覆盖：
1. SelectionState：object/vertex/edge/face 选择、set/add/subtract/toggle。
2. 框选、反选、清空。
3. get_vertex_mask / get_face_mask（Deformer SelectionField 用）。
4. signature 稳定性。
5. SelectionSet + SelectionSetLibrary：保存/加载命名选择集。

纯 Python / numpy，无 GL/PyQt 依赖。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.render3d.selection import (
    SelectionState, SelectionSet, SelectionSetLibrary,
    SELECT_SET, SELECT_ADD, SELECT_SUBTRACT, SELECT_TOGGLE,
)

pytestmark = pytest.mark.composite_core


# ── 对象选择 ─────────────────────────────────────────────────────────

class TestObjectSelection:
    def test_select_set_replaces(self):
        s = SelectionState()
        s.select_object("a")
        s.select_object("b", mode=SELECT_SET)
        assert s.object_ids == {"b"}
        assert s.active_object == "b"

    def test_select_add(self):
        s = SelectionState()
        s.select_object("a")
        s.select_object("b", mode=SELECT_ADD)
        assert s.object_ids == {"a", "b"}

    def test_select_subtract(self):
        s = SelectionState()
        s.select_object("a")
        s.select_object("b", mode=SELECT_ADD)
        s.select_object("a", mode=SELECT_SUBTRACT)
        assert s.object_ids == {"b"}
        assert s.active_object == "b"

    def test_select_toggle(self):
        s = SelectionState()
        s.select_object("a")
        s.select_object("a", mode=SELECT_TOGGLE)  # 再点取消
        assert "a" not in s.object_ids
        s.select_object("a", mode=SELECT_TOGGLE)  # 再点选中
        assert "a" in s.object_ids

    def test_box_select_set(self):
        s = SelectionState()
        s.select_objects_box(["a", "b", "c"], mode=SELECT_SET)
        assert s.object_ids == {"a", "b", "c"}

    def test_box_select_add(self):
        s = SelectionState()
        s.select_object("a")
        s.select_objects_box(["b", "c"], mode=SELECT_ADD)
        assert s.object_ids == {"a", "b", "c"}

    def test_box_select_subtract_resets_active(self):
        """框选减选移除 active_object 时应重置（回归 MODERATE）。"""
        s = SelectionState()
        s.select_objects_box(["a", "b", "c"], mode=SELECT_SET)
        assert s.active_object is not None
        active = s.active_object
        # 减选包含 active 的集合
        s.select_objects_box([active], mode=SELECT_SUBTRACT)
        assert active not in s.object_ids
        # active 应重置为剩余首个，不是悬空指向已移除对象
        assert s.active_object in s.object_ids or s.active_object is None

    def test_invert_objects(self):
        s = SelectionState()
        s.select_object("a")
        s.invert_objects(["a", "b", "c", "d"])
        assert s.object_ids == {"b", "c", "d"}

    def test_clear(self):
        s = SelectionState()
        s.select_object("a")
        s.clear()
        assert s.is_empty()
        assert s.active_object is None


# ── 组件选择 ─────────────────────────────────────────────────────────

class TestComponentSelection:
    def test_select_vertex_set(self):
        """SELECT_SET 替换：连续两次 set 只保留最后一次。"""
        s = SelectionState()
        s.select_vertex("mesh0", 5)
        s.select_vertex("mesh0", 10)
        assert s.selected_vertex_count("mesh0") == 1  # 替换

    def test_select_vertex_add_accumulates(self):
        """SELECT_ADD 在同 mesh 累加。"""
        s = SelectionState()
        s.select_vertex("mesh0", 5)
        s.select_vertex("mesh0", 10, mode=SELECT_ADD)
        assert s.selected_vertex_count("mesh0") == 2

    def test_select_vertex_set_clears_previous_mesh(self):
        """SELECT_SET 清空其他组件选择。"""
        s = SelectionState()
        s.select_vertex("mesh0", 1)
        s.select_vertex("mesh0", 2, mode=SELECT_SET)  # 清空后选 2
        assert s.vertex_ids == {"mesh0": {2}}

    def test_select_vertex_subtract(self):
        s = SelectionState()
        s.select_vertex("m", 1)
        s.select_vertex("m", 2, mode=SELECT_ADD)
        s.select_vertex("m", 1, mode=SELECT_SUBTRACT)
        assert s.vertex_ids["m"] == {2}

    def test_select_face(self):
        s = SelectionState()
        s.select_face("m", 0)
        s.select_face("m", 1, mode=SELECT_ADD)
        assert s.selected_face_count("m") == 2

    def test_select_edge_normalized(self):
        """边规范化为 (min,max)。"""
        s = SelectionState()
        s.select_edge("m", 5, 3)  # 传 (5,3)
        assert s.edge_ids["m"] == {(3, 5)}  # 存 (3,5)

    def test_select_edge_toggle(self):
        s = SelectionState()
        s.select_edge("m", 1, 2)
        s.select_edge("m", 1, 2, mode=SELECT_TOGGLE)  # 取消
        assert s.edge_ids["m"] == set()

    def test_clear_components_keeps_objects(self):
        """clear_components 保留对象选择。"""
        s = SelectionState()
        s.select_object("obj0")
        s.select_vertex("obj0", 1)
        s.clear_components()
        assert "obj0" in s.object_ids
        assert s.vertex_ids == {}


# ── mask（Deformer 用）──────────────────────────────────────────────

class TestSelectionMasks:
    def test_vertex_mask(self):
        s = SelectionState()
        s.select_vertex("m", 2)
        s.select_vertex("m", 5, mode=SELECT_ADD)
        mask = s.get_vertex_mask("m", 10)
        assert mask.dtype == bool
        assert bool(mask[2]) is True
        assert bool(mask[5]) is True
        assert bool(mask[0]) is False
        assert mask.sum() == 2

    def test_vertex_mask_out_of_range_safe(self):
        """超出范围的顶点索引不崩（被忽略）。"""
        s = SelectionState()
        s.select_vertex("m", 100)  # 超出 vertex_count=10
        mask = s.get_vertex_mask("m", 10)
        assert mask.sum() == 0

    def test_face_mask(self):
        s = SelectionState()
        s.select_face("m", 0)
        s.select_face("m", 3, mode=SELECT_ADD)
        mask = s.get_face_mask("m", 6)
        assert bool(mask[0]) is True
        assert bool(mask[3]) is True
        assert bool(mask[1]) is False

    def test_vertex_mask_empty(self):
        s = SelectionState()
        mask = s.get_vertex_mask("m", 5)
        assert mask.sum() == 0


# ── signature ────────────────────────────────────────────────────────

class TestSelectionSignature:
    def test_same_selection_same_sig(self):
        s1 = SelectionState()
        s1.select_object("a")
        s2 = SelectionState()
        s2.select_object("a")
        assert s1.signature() == s2.signature()

    def test_different_selection_diff_sig(self):
        s1 = SelectionState()
        s1.select_object("a")
        s2 = SelectionState()
        s2.select_object("b")
        assert s1.signature() != s2.signature()


# ── SelectionSet / Library ───────────────────────────────────────────

class TestSelectionSet:
    def test_from_selection_state(self):
        state = SelectionState()
        state.select_vertex("m", 1)
        state.select_vertex("m", 2, mode=SELECT_ADD)
        state.select_face("m", 0, mode=SELECT_ADD)
        ss = SelectionSet(name="set1")
        ss.from_selection_state(state, "set1", "m")
        assert ss.vertex_ids == {1, 2}
        assert ss.face_ids == {0}

    def test_vertex_mask(self):
        ss = SelectionSet(name="s", vertex_ids={1, 3})
        mask = ss.vertex_mask(5)
        assert bool(mask[1]) is True
        assert bool(mask[3]) is True
        assert bool(mask[0]) is False

    def test_is_empty(self):
        ss = SelectionSet(name="s")
        assert ss.is_empty()
        ss.vertex_ids.add(1)
        assert not ss.is_empty()


class TestSelectionSetLibrary:
    def test_save_and_get(self):
        lib = SelectionSetLibrary()
        state = SelectionState()
        state.select_vertex("m", 1)
        lib.save("myset", state, "m")
        assert lib.get("myset") is not None
        assert lib.get("myset").vertex_ids == {1}
        assert lib.count() == 1

    def test_names(self):
        lib = SelectionSetLibrary()
        lib.save("a", SelectionState(), "m")
        lib.save("b", SelectionState(), "m")
        assert set(lib.names()) == {"a", "b"}

    def test_remove(self):
        lib = SelectionSetLibrary()
        lib.save("a", SelectionState(), "m")
        lib.remove("a")
        assert lib.get("a") is None
        assert lib.count() == 0

    def test_overwrite_on_same_name(self):
        lib = SelectionSetLibrary()
        s1 = SelectionState()
        s1.select_vertex("m", 1)
        lib.save("set", s1, "m")
        s2 = SelectionState()
        s2.select_vertex("m", 2)
        lib.save("set", s2, "m")  # 覆盖
        assert lib.get("set").vertex_ids == {2}
        assert lib.count() == 1
