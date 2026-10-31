"""F8 Camera/Light 导入单测（master_plan §11 / §21.11）。

覆盖：
1. CameraData：字段、fov 从 focal/sensor 推算、transform 类型。
2. LightData：字段、颜色归一化、type 映射。
3. SceneGraphRoot.cameras/lights 默认空列表。
4. loader _extract_assimp_cameras / _extract_assimp_lights（mock scene）。
5. CameraData.fov_y 自动计算。

纯 Python / numpy，无 GL/PyQt 依赖。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import numpy as np
import pytest

from src.features.composite.render3d import scene_graph as sg
from src.features.composite.render3d import model_loader
from src.features.composite.render3d.scene_graph import (
    CameraData, LightData,
    CAMERA_TYPE_PERSPECTIVE, CAMERA_TYPE_ORTHOGRAPHIC,
    LIGHT_TYPE_DIRECTIONAL, LIGHT_TYPE_POINT, LIGHT_TYPE_SPOT,
    LIGHT_TYPE_AMBIENT, LIGHT_TYPE_AREA,
)

pytestmark = pytest.mark.composite_core


# ── CameraData ───────────────────────────────────────────────────────

class TestCameraData:
    def test_defaults(self):
        c = CameraData()
        assert c.camera_type == CAMERA_TYPE_PERSPECTIVE
        assert c.near == 0.1
        assert c.far == 1000.0
        assert c.transform.shape == (4, 4)

    def test_fov_auto_from_focal_sensor(self):
        """fov_y 未设时从 focal_length + sensor_height 推算。"""
        c = CameraData(focal_length=35.0, sensor_height=24.0, fov_y=0.0)
        # fov = 2 * atan(12 / 35) ≈ 37.8°
        assert 37.0 < c.fov_y < 39.0

    def test_explicit_fov_preserved(self):
        """显式 fov_y 不被覆盖。"""
        c = CameraData(focal_length=35.0, fov_y=90.0)
        assert c.fov_y == 90.0

    def test_orthographic_type(self):
        c = CameraData(camera_type=CAMERA_TYPE_ORTHOGRAPHIC, orthographic_height=4.0)
        assert c.camera_type == CAMERA_TYPE_ORTHOGRAPHIC
        assert c.orthographic_height == 4.0

    def test_transform_float32(self):
        c = CameraData()
        assert c.transform.dtype == np.float32


# ── LightData ────────────────────────────────────────────────────────

class TestLightData:
    def test_defaults(self):
        l = LightData()
        assert l.light_type == LIGHT_TYPE_DIRECTIONAL
        assert l.color.shape == (3,)
        assert np.allclose(l.color, [1, 1, 1])

    def test_spot_light(self):
        l = LightData(light_type=LIGHT_TYPE_SPOT, inner_cone=20.0, outer_cone=40.0)
        assert l.light_type == LIGHT_TYPE_SPOT
        assert l.inner_cone == 20.0

    def test_color_normalized(self):
        l = LightData(color=[0.5, 0.3, 0.1])
        assert l.color.dtype == np.float32
        assert l.color.shape == (3,)

    def test_transform_float32(self):
        l = LightData()
        assert l.transform.dtype == np.float32


# ── SceneGraphRoot cameras/lights ────────────────────────────────────

class TestSceneGraphRootCamerasLights:
    def test_default_empty(self):
        root = sg.SceneGraphRoot(root=sg.SceneNode(name="r"))
        assert root.cameras == []
        assert root.lights == []

    def test_attach_cameras_lights(self):
        root = sg.SceneGraphRoot(root=sg.SceneNode(name="r"))
        root.cameras.append(CameraData(name="cam0"))
        root.lights.append(LightData(name="light0"))
        assert len(root.cameras) == 1
        assert len(root.lights) == 1


# ── loader 提取（mock scene）─────────────────────────────────────────

class _MockAssimpCamera:
    """模拟 pyassimp camera。"""
    def __init__(self, name="cam", fov=1.047, near=0.1, far=1000.0):
        self.mName = name
        self.mHorizontalFOV = fov
        self.mClipPlaneNear = near
        self.mClipPlaneFar = far


class _MockAssimpLight:
    """模拟 pyassimp light。"""
    def __init__(self, name="light", mtype=1, color=(1, 0.8, 0.6)):
        self.mName = name
        self.mType = mtype
        self.mColorDiffuse = color
        self.mAngleInnerCone = 0.5
        self.mAngleOuterCone = 0.8


class _MockAssimpScene:
    """模拟 pyassimp scene（含 cameras/lights）。"""
    def __init__(self, cameras=None, lights=None):
        self.cameras = cameras or []
        self.lights = lights or []
        self.rootnode = None
        self.meshes = []
        self.materials = []


class TestLoaderExtractCamerasLights:
    def test_extract_camera_from_scene(self):
        scene = _MockAssimpScene(cameras=[_MockAssimpCamera(name="cam0", fov=1.0)])
        cams = model_loader._extract_assimp_cameras(scene)
        assert len(cams) == 1
        assert cams[0].name == "cam0"
        assert cams[0].camera_type == CAMERA_TYPE_PERSPECTIVE
        assert cams[0].fov_y > 0  # 从 fov 弧度转角度

    def test_extract_no_cameras(self):
        scene = _MockAssimpScene()
        assert model_loader._extract_assimp_cameras(scene) == []

    def test_extract_light_point(self):
        scene = _MockAssimpScene(lights=[_MockAssimpLight(name="l0", mtype=1)])
        lights = model_loader._extract_assimp_lights(scene)
        assert len(lights) == 1
        assert lights[0].light_type == LIGHT_TYPE_POINT

    def test_extract_light_spot(self):
        scene = _MockAssimpScene(lights=[_MockAssimpLight(name="l1", mtype=2)])
        lights = model_loader._extract_assimp_lights(scene)
        assert lights[0].light_type == LIGHT_TYPE_SPOT

    def test_extract_light_directional(self):
        scene = _MockAssimpScene(lights=[_MockAssimpLight(name="l2", mtype=0)])
        lights = model_loader._extract_assimp_lights(scene)
        assert lights[0].light_type == LIGHT_TYPE_DIRECTIONAL

    def test_convert_assimp_scene_populates_cameras_lights(self):
        """_convert_assimp_scene 应把提取的 cameras/lights 放到 SceneGraphRoot。"""
        scene = _MockAssimpScene(
            cameras=[_MockAssimpCamera(name="cam")],
            lights=[_MockAssimpLight(name="light", mtype=1)],
        )
        root = model_loader._convert_assimp_scene(scene)
        assert len(root.cameras) == 1
        assert len(root.lights) == 1
        # report 计数应与提取数量一致
        assert root.import_report.camera_count == 1
        assert root.import_report.light_count == 1

    def test_loader_has_extract_functions(self):
        """model_loader 应有 _extract_assimp_cameras / _extract_assimp_lights。"""
        assert hasattr(model_loader, "_extract_assimp_cameras")
        assert hasattr(model_loader, "_extract_assimp_lights")
