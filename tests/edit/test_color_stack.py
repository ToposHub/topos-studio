"""
Color Stack Tests
调色栈数据模型和功能测试（P9.1）。

测试覆盖：
- ColorStackData: 创建、序列化、更新方法
- ClipInstance: color_stack_id 和 color_shared 字段
- EditProject: color_stacks 管理
- Edit → Color 页面联动

验收标准：
- 所有边界情况覆盖
- 纯函数无 PyQt 依赖
"""

import pytest
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.edit.core.project import ColorStackData, EditProject


# ================================================================
# Test Fixtures (测试数据)
# ================================================================


@pytest.fixture
def empty_color_stack() -> ColorStackData:
    """创建空的调色栈。"""
    return ColorStackData.create()


@pytest.fixture
def color_stack_with_params() -> ColorStackData:
    """创建带参数的调色栈。"""
    stack = ColorStackData.create(asset_id="asset1", name="Test Stack")
    params = {
        "lift": (10, 20, 30),
        "gamma": (5, 10, 15),
        "gain": (100, 200, 150),
        "exposure": 50,
        "temp": -20,
        "tint": 10,
        "contrast": 30,
        "shadow": -10,
        "highlights": 15,
        "saturation": 40,
        "vibrance": 25,
        "mid_detail": 10,
        "sharpness": 5,
        "hue_shift": 15,
    }
    return stack.with_grading_params(params)


@pytest.fixture
def clip_with_color_stack() -> ClipInstance:
    """创建带调色栈引用的剪辑。"""
    return ClipInstance(
        clip_id="clip1",
        asset_id="asset1",
        track_id="track1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        color_stack_id="stack1",
        color_shared=False,
    )


@pytest.fixture
def clip_shared_color() -> ClipInstance:
    """创建共享素材调色的剪辑。"""
    return ClipInstance(
        clip_id="clip2",
        asset_id="asset1",
        track_id="track1",
        timeline_start=100,
        timeline_duration=50,
        source_start=0,
        source_duration=50,
        color_stack_id="asset_stack1",
        color_shared=True,
    )


@pytest.fixture
def project_with_color_stacks() -> EditProject:
    """创建带调色栈的项目。"""
    project = EditProject.create("Test Project")
    # 创建两个调色栈
    project, stack_id1 = project.create_color_stack(asset_id="asset1", name="Asset Default")
    project, stack_id2 = project.create_color_stack(name="Independent Stack")
    return project


# ================================================================
# ColorStackData Tests
# ================================================================


class TestColorStackDataCreation:
    """ColorStackData 创建测试。"""

    def test_create_empty_stack(self, empty_color_stack: ColorStackData):
        """测试创建空调色栈。"""
        assert empty_color_stack.stack_id is not None
        assert empty_color_stack.asset_id is None
        assert empty_color_stack.name == "Default"
        assert empty_color_stack.grading_params is not None
        assert empty_color_stack.lut_path is None
        assert empty_color_stack.lut_intensity == 1.0

    def test_create_stack_with_asset(self):
        """测试创建带素材关联的调色栈。"""
        stack = ColorStackData.create(asset_id="asset1", name="Asset Color")
        assert stack.asset_id == "asset1"
        assert stack.name == "Asset Color"

    def test_create_stack_generates_unique_id(self):
        """测试每次创建生成唯一 ID。"""
        stack1 = ColorStackData.create()
        stack2 = ColorStackData.create()
        assert stack1.stack_id != stack2.stack_id

    def test_invalid_lut_intensity_raises(self):
        """测试无效 LUT 强度抛出异常。"""
        with pytest.raises(ValueError):
            ColorStackData(stack_id="test", lut_intensity=1.5)

        with pytest.raises(ValueError):
            ColorStackData(stack_id="test", lut_intensity=-0.1)

    def test_empty_stack_id_raises(self):
        """测试空 stack_id 抛出异常。"""
        with pytest.raises(ValueError):
            ColorStackData(stack_id="")


class TestColorStackDataSerialization:
    """ColorStackData 序列化测试。"""

    def test_to_dict(self, color_stack_with_params: ColorStackData):
        """测试序列化到字典。"""
        data = color_stack_with_params.to_dict()
        assert "stack_id" in data
        assert "asset_id" in data
        assert "name" in data
        assert "grading_params" in data
        assert "curves_data" in data
        assert "lut_path" in data
        assert "lut_intensity" in data

    def test_from_dict(self, color_stack_with_params: ColorStackData):
        """测试从字典反序列化。"""
        data = color_stack_with_params.to_dict()
        restored = ColorStackData.from_dict(data)

        assert restored.stack_id == color_stack_with_params.stack_id
        assert restored.asset_id == color_stack_with_params.asset_id
        assert restored.name == color_stack_with_params.name
        assert restored.grading_params == color_stack_with_params.grading_params

    def test_roundtrip_preserves_all_data(self):
        """测试 roundtrip 保留所有数据。"""
        stack = ColorStackData.create(asset_id="asset1", name="Test")
        stack = stack.with_grading_params({
            "lift": (10, 20, 30),
            "exposure": 50,
        })
        stack = stack.with_lut("/path/to/lut.cube", 0.8)

        data = stack.to_dict()
        restored = ColorStackData.from_dict(data)

        assert restored.stack_id == stack.stack_id
        assert restored.asset_id == stack.asset_id
        assert restored.name == stack.name
        assert restored.grading_params["lift"] == (10, 20, 30)
        assert restored.grading_params["exposure"] == 50
        assert restored.lut_path == "/path/to/lut.cube"
        assert restored.lut_intensity == 0.8

    def test_from_dict_missing_fields_uses_defaults(self):
        """测试缺少字段时使用默认值。"""
        data = {"stack_id": "test_stack"}
        stack = ColorStackData.from_dict(data)
        assert stack.stack_id == "test_stack"
        assert stack.asset_id is None
        assert stack.name == "Default"
        assert stack.lut_intensity == 1.0

    def test_tracking_data_roundtrip_and_legacy_default(self):
        """编辑页实际使用的 ColorStackData 也必须承载跟踪数据。"""
        trackings = {"schema_version": 1, "bindings": []}
        stack = ColorStackData(stack_id="tracking-stack", trackings=trackings)

        restored = ColorStackData.from_dict(stack.to_dict())
        assert restored.trackings == trackings
        assert stack.with_grading_params({}).trackings == trackings
        assert stack.with_trackings({}).trackings == {}

        # 旧工程没有 trackings 键时，仍然得到可用的空字典。
        legacy = ColorStackData.from_dict({"stack_id": "legacy-stack"})
        assert legacy.trackings == {}


class TestColorStackDataUpdateMethods:
    """ColorStackData 更新方法测试。"""

    def test_with_grading_params(self, empty_color_stack: ColorStackData):
        """测试更新调色参数。"""
        params = {"lift": (10, 0, 0), "exposure": 20}
        updated = empty_color_stack.with_grading_params(params)

        assert updated.grading_params["lift"] == (10, 0, 0)
        assert updated.grading_params["exposure"] == 20
        assert updated.stack_id == empty_color_stack.stack_id  # ID 不变

    def test_with_curves(self, empty_color_stack: ColorStackData):
        """测试更新曲线数据。"""
        curves = {"master": [(0, 0), (128, 128), (255, 255)]}
        updated = empty_color_stack.with_curves(curves)

        assert updated.curves_data == curves
        assert updated.stack_id == empty_color_stack.stack_id

    def test_with_lut(self, empty_color_stack: ColorStackData):
        """测试更新 LUT。"""
        updated = empty_color_stack.with_lut("/path/to/lut.cube", 0.5)

        assert updated.lut_path == "/path/to/lut.cube"
        assert updated.lut_intensity == 0.5

    def test_with_name(self, empty_color_stack: ColorStackData):
        """测试更新名称。"""
        updated = empty_color_stack.with_name("New Name")
        assert updated.name == "New Name"


# ================================================================
# ClipInstance Color Stack Tests
# ================================================================


class TestClipInstanceColorStack:
    """ClipInstance 调色栈字段测试。"""

    def test_clip_has_color_stack_id(self, clip_with_color_stack: ClipInstance):
        """测试剪辑有 color_stack_id 字段。"""
        assert clip_with_color_stack.color_stack_id == "stack1"
        assert clip_with_color_stack.color_shared == False

    def test_clip_shared_color(self, clip_shared_color: ClipInstance):
        """测试共享调色的剪辑。"""
        assert clip_shared_color.color_stack_id == "asset_stack1"
        assert clip_shared_color.color_shared == True

    def test_clip_default_color_stack_none(self):
        """测试默认 color_stack_id 为 None。"""
        clip = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )
        assert clip.color_stack_id is None
        assert clip.color_shared == False

    def test_with_color_stack(self, clip_with_color_stack: ClipInstance):
        """测试 with_color_stack 方法。"""
        updated = clip_with_color_stack.with_color_stack("new_stack", color_shared=True)

        assert updated.color_stack_id == "new_stack"
        assert updated.color_shared == True
        assert updated.clip_id == clip_with_color_stack.clip_id  # 其他字段不变

    def test_clip_serialization_includes_color_stack(self, clip_with_color_stack: ClipInstance):
        """测试序列化包含调色栈字段。"""
        data = clip_with_color_stack.to_dict()
        assert "color_stack_id" in data
        assert data["color_stack_id"] == "stack1"
        assert "color_shared" in data
        assert data["color_shared"] == False

    def test_clip_deserialization_preserves_color_stack(self, clip_with_color_stack: ClipInstance):
        """测试反序列化保留调色栈字段。"""
        data = clip_with_color_stack.to_dict()
        restored = ClipInstance.from_dict(data)

        assert restored.color_stack_id == clip_with_color_stack.color_stack_id
        assert restored.color_shared == clip_with_color_stack.color_shared


# ================================================================
# EditProject Color Stack Management Tests
# ================================================================


class TestEditProjectColorStackManagement:
    """EditProject 调色栈管理测试。"""

    def test_create_project_has_empty_color_stacks(self):
        """测试新建项目有空的调色栈字典。"""
        project = EditProject.create("Test Project")
        assert project.color_stacks == {}
        assert project.color_stack_count == 0

    def test_create_color_stack(self):
        """测试创建调色栈。"""
        project = EditProject.create("Test Project")
        new_project, stack_id = project.create_color_stack(asset_id="asset1")

        assert stack_id is not None
        assert new_project.color_stack_count == 1
        assert stack_id in new_project.color_stacks

        stack = new_project.color_stacks[stack_id]
        assert stack.asset_id == "asset1"

    def test_get_color_stack_by_id(self, project_with_color_stacks: EditProject):
        """测试按 ID 获取调色栈。"""
        stacks = list(project_with_color_stacks.color_stacks.keys())
        stack_id = stacks[0]

        stack = project_with_color_stacks.get_color_stack_by_id(stack_id)
        assert stack is not None
        assert stack.stack_id == stack_id

    def test_get_color_stack_for_asset(self):
        """测试获取素材默认调色栈。"""
        project = EditProject.create("Test Project")
        project, stack_id = project.create_color_stack(asset_id="asset1")

        stack = project.get_color_stack_for_asset("asset1")
        assert stack is not None
        assert stack.asset_id == "asset1"

    def test_update_color_stack(self):
        """测试更新调色栈。"""
        project = EditProject.create("Test Project")
        project, stack_id = project.create_color_stack()

        params = {"lift": (10, 0, 0), "exposure": 20}
        updated_project = project.update_color_stack(stack_id, grading_params=params)

        stack = updated_project.color_stacks[stack_id]
        assert stack.grading_params["lift"] == (10, 0, 0)
        assert stack.grading_params["exposure"] == 20

    def test_delete_color_stack(self):
        """测试删除调色栈。"""
        project = EditProject.create("Test Project")
        project, stack_id = project.create_color_stack()

        assert project.color_stack_count == 1

        updated = project.delete_color_stack(stack_id)
        assert updated.color_stack_count == 0
        assert stack_id not in updated.color_stacks

    def test_delete_nonexistent_stack_returns_same_project(self):
        """测试删除不存在的调色栈返回原项目。"""
        project = EditProject.create("Test Project")
        updated = project.delete_color_stack("nonexistent")
        assert updated == project

    def test_multiple_color_stacks_for_same_asset(self):
        """测试同一素材可以有多个调色栈（独立调色）。"""
        project = EditProject.create("Test Project")
        # 创建素材默认调色栈
        project, stack_id1 = project.create_color_stack(asset_id="asset1", name="Default")

        # 创建独立调色栈
        project, stack_id2 = project.create_color_stack(asset_id="asset1", name="Custom")

        # 创建另一个独立调色栈
        project, stack_id3 = project.create_color_stack(asset_id="asset1", name="Alternative")

        assert project.color_stack_count == 3

        # 所有栈都关联到同一素材
        for stack_id in [stack_id1, stack_id2, stack_id3]:
            stack = project.color_stacks[stack_id]
            assert stack.asset_id == "asset1"


# ================================================================
# Edit → Color Page Linkage Tests
# ================================================================


class TestEditToColorLinkage:
    """Edit → Color 页面联动测试。"""

    def test_clip_with_stack_can_link_to_color(self, clip_with_color_stack: ClipInstance):
        """测试带调色栈的剪辑可以链接到 Color 页面。"""
        # 剪辑有 color_stack_id，可用于页面切换
        assert clip_with_color_stack.color_stack_id is not None

        # 可以根据 color_stack_id 获取调色参数
        stack_id = clip_with_color_stack.color_stack_id
        # 实际页面切换需要 project 中的 color_stacks

    def test_shared_color_vs_independent_color(self):
        """测试共享调色和独立调色的区别。"""
        # 独立调色剪辑
        clip_independent = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            color_stack_id="unique_stack",
            color_shared=False,
        )

        # 共享调色剪辑
        clip_shared = ClipInstance(
            clip_id="clip2",
            asset_id="asset1",
            track_id="track1",
            timeline_start=100,
            timeline_duration=50,
            source_start=0,
            source_duration=50,
            color_stack_id="asset_stack",
            color_shared=True,
        )

        assert clip_independent.color_shared == False
        assert clip_shared.color_shared == True

    def test_color_stack_update_flow(self):
        """测试调色栈更新流程（修改 → 保存）。"""
        # 1. 创建项目和调色栈
        project = EditProject.create("Test Project")
        project, stack_id = project.create_color_stack(asset_id="asset1")

        # 2. 更新调色参数
        params = {"lift": (10, 0, 0), "exposure": 20}
        project = project.update_color_stack(stack_id, grading_params=params)

        # 3. 添加曲线
        curves = {"master": [(0, 0), (255, 255)]}
        project = project.update_color_stack(stack_id, curves_data=curves)

        # 4. 验证更新结果
        stack = project.color_stacks[stack_id]
        assert stack.grading_params["lift"] == (10, 0, 0)
        assert stack.curves_data == curves


# ================================================================
# GradeMode Enum Tests (P9.1)
# ================================================================


class TestGradeModeEnum:
    """GradeMode 枚举测试。"""

    def test_grade_mode_values(self):
        """测试 GradeMode 枚举值。"""
        from src.features.edit.core.timeline_v2 import GradeMode

        assert GradeMode.SHARED.value == "shared"
        assert GradeMode.LOCAL.value == "local"

    def test_grade_mode_properties(self):
        """测试 GradeMode 属性方法。"""
        from src.features.edit.core.timeline_v2 import GradeMode

        assert GradeMode.SHARED.is_shared == True
        assert GradeMode.SHARED.is_local == False
        assert GradeMode.LOCAL.is_shared == False
        assert GradeMode.LOCAL.is_local == True

    def test_grade_mode_equality(self):
        """测试 GradeMode 相等比较。"""
        from src.features.edit.core.timeline_v2 import GradeMode

        assert GradeMode.SHARED == GradeMode.SHARED
        assert GradeMode.LOCAL == GradeMode.LOCAL
        assert GradeMode.SHARED != GradeMode.LOCAL


# ================================================================
# Grade Commands Tests (P9.1)
# ================================================================


class TestGradeCommands:
    """Grade 切换命令测试。"""

    def test_use_shared_grade_command_create(self):
        """测试 UseSharedGradeCommand 工厂方法。"""
        from src.features.edit.core.commands import UseSharedGradeCommand

        command = UseSharedGradeCommand.create(
            track_id="track1",
            clip_id="clip1",
            asset_id="asset1",
        )

        assert command.track_id == "track1"
        assert command.clip_id == "clip1"
        assert command.asset_id == "asset1"
        assert command.command_id is not None

    def test_make_local_grade_copy_command_create(self):
        """测试 MakeLocalGradeCopyCommand 工厂方法。"""
        from src.features.edit.core.commands import MakeLocalGradeCopyCommand

        command = MakeLocalGradeCopyCommand.create(
            track_id="track1",
            clip_id="clip1",
            source_stack_id="source_stack",
            new_stack_name="My Local Grade",
        )

        assert command.track_id == "track1"
        assert command.clip_id == "clip1"
        assert command.source_stack_id == "source_stack"
        assert command.new_stack_name == "My Local Grade"

    def test_use_shared_grade_command_serialization(self):
        """测试 UseSharedGradeCommand 序列化。"""
        from src.features.edit.core.commands import UseSharedGradeCommand

        command = UseSharedGradeCommand.create(
            track_id="track1",
            clip_id="clip1",
            asset_id="asset1",
        )

        data = command.to_dict()
        assert data["command_type"] == "UseSharedGradeCommand"
        assert data["track_id"] == "track1"
        assert data["clip_id"] == "clip1"
        assert data["asset_id"] == "asset1"

        restored = UseSharedGradeCommand.from_dict(data)
        assert restored.track_id == command.track_id
        assert restored.clip_id == command.clip_id
        assert restored.asset_id == command.asset_id

    def test_make_local_grade_copy_command_serialization(self):
        """测试 MakeLocalGradeCopyCommand 序列化。"""
        from src.features.edit.core.commands import MakeLocalGradeCopyCommand

        command = MakeLocalGradeCopyCommand.create(
            track_id="track1",
            clip_id="clip1",
            source_stack_id="source_stack",
        )

        data = command.to_dict()
        assert data["command_type"] == "MakeLocalGradeCopyCommand"
        assert data["track_id"] == "track1"
        assert data["source_stack_id"] == "source_stack"

        restored = MakeLocalGradeCopyCommand.from_dict(data)
        assert restored.track_id == command.track_id
        assert restored.source_stack_id == command.source_stack_id

    def test_grade_mode_from_clip_shared(self):
        """测试从 ClipInstance 判断 Grade 模式（共享）。"""
        clip = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            color_stack_id="asset_stack",
            color_shared=True,
        )

        # 验证共享调色模式
        assert clip.color_shared == True
        # 使用 GradeMode 枚举
        from src.features.edit.core.timeline_v2 import GradeMode
        mode = GradeMode.SHARED if clip.color_shared else GradeMode.LOCAL
        assert mode == GradeMode.SHARED
        assert mode.is_shared == True

    def test_grade_mode_from_clip_local(self):
        """测试从 ClipInstance 判断 Grade 模式（独立）。"""
        clip = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            color_stack_id="local_stack",
            color_shared=False,
        )

        # 验证独立调色模式
        assert clip.color_shared == False
        from src.features.edit.core.timeline_v2 import GradeMode
        mode = GradeMode.SHARED if clip.color_shared else GradeMode.LOCAL
        assert mode == GradeMode.LOCAL
        assert mode.is_local == True


# ================================================================
# Run Tests
# ================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
