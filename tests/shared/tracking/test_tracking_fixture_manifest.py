"""阶段 0 验收：固定跟踪测试素材清单与真值一致性。

验证四类合成素材（平移 / TRS / 透视 / 遮挡）可稳定生成、读取，
且 ground truth 与视频内容一致。
"""

import json
import os

import numpy as np
import pytest

from src.shared.tracking.frame_sampler import FrameSampler, AnalysisQuality

try:
    from tools.make_tracking_fixtures import (
        FIXTURE_NAMES,
        generate_all,
        read_barcode,
    )
    TOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover - 工具缺失时给出明确跳过原因
    TOOLS_AVAILABLE = False

pytestmark = pytest.mark.skipif(not TOOLS_AVAILABLE, reason="tracking fixture 工具不可用")


@pytest.fixture(scope="session")
def fixture_dir() -> str:
    out = generate_all(force=False)
    assert os.path.isdir(out)
    return out


def _load_manifest(fixture_dir: str, name: str) -> dict:
    path = os.path.join(fixture_dir, f"{name}.ground_truth.json")
    assert os.path.exists(path), f"缺少真值文件: {path}"
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestFixtureManifest:
    def test_all_fixtures_exist(self, fixture_dir):
        for name in FIXTURE_NAMES:
            assert os.path.exists(os.path.join(fixture_dir, f"{name}.mp4")), name
            assert os.path.exists(os.path.join(fixture_dir, f"{name}.ground_truth.json")), name

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_manifest_matches_video(self, fixture_dir, name):
        manifest = _load_manifest(fixture_dir, name)
        video_path = os.path.join(fixture_dir, manifest["video"])

        assert manifest["width"] > 0 and manifest["height"] > 0
        assert manifest["frame_count"] == len(manifest["frames"])

        with FrameSampler(video_path) as sampler:
            w, h, fps, total = sampler.probe_info()
        assert w == manifest["width"]
        assert h == manifest["height"]
        assert total == manifest["frame_count"]
        assert abs(fps - manifest["fps"]) < 1.0

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_reference_frame_is_identity(self, fixture_dir, name):
        manifest = _load_manifest(fixture_dir, name)
        ref = manifest["frames"][manifest["reference_frame"]]
        H = np.array(ref["homography"], dtype=np.float64).reshape(3, 3)
        assert np.allclose(H, np.eye(3), atol=1e-6), "参考帧单应性必须为单位矩阵"

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_pattern_rect_valid(self, fixture_dir, name):
        manifest = _load_manifest(fixture_dir, name)
        x, y, w, h = manifest["pattern_rect_reference"]
        assert w > 10 and h > 10
        assert 0 <= x and x + w <= manifest["width"]
        assert 0 <= y and y + h <= manifest["height"]

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_corners_shape_and_continuity(self, fixture_dir, name):
        manifest = _load_manifest(fixture_dir, name)
        frames = manifest["frames"]
        margin = max(manifest["width"], manifest["height"])
        prev_corners = None
        for meta in frames:
            corners = np.array(meta["corners"], dtype=np.float64)
            assert corners.shape == (4, 2)
            # 角点允许出画（遮挡素材），但不能出现爆炸值
            assert np.all(np.abs(corners) < margin * 3)
            if prev_corners is not None and meta["visible"]:
                # 相邻帧四角不应跳变（合成轨迹是连续的）
                assert np.max(np.abs(corners - prev_corners)) < manifest["width"] * 0.5
            prev_corners = corners

    def test_occlusion_flags_present(self, fixture_dir):
        manifest = _load_manifest(fixture_dir, "occlusion")
        occluded = [f for f in manifest["frames"] if f["occluded"]]
        invisible = [f for f in manifest["frames"] if not f["visible"]]
        assert len(occluded) > 0, "遮挡素材必须包含遮挡帧"
        assert len(invisible) > 0, "遮挡素材必须包含出画帧"

    def test_translation_has_no_rotation_scale(self, fixture_dir):
        manifest = _load_manifest(fixture_dir, "translation")
        for meta in manifest["frames"]:
            # ground truth 角点为 float32 存储，允许微小数值噪声
            assert abs(meta["rotation_deg"]) < 1e-2
            assert abs(meta["scale"] - 1.0) < 1e-3

    def test_trs_has_rotation_and_scale(self, fixture_dir):
        manifest = _load_manifest(fixture_dir, "trs")
        rotations = [abs(f["rotation_deg"]) for f in manifest["frames"]]
        scales = [f["scale"] for f in manifest["frames"]]
        assert max(rotations) > 3.0, "TRS 素材必须包含明显旋转"
        assert max(scales) - min(scales) > 0.1, "TRS 素材必须包含明显缩放"

    def test_perspective_is_not_affine(self, fixture_dir):
        """透视素材的单应性应包含明显透视分量（底行偏离 [0,0,1]）。"""
        manifest = _load_manifest(fixture_dir, "perspective")
        max_perspective = 0.0
        for meta in manifest["frames"]:
            H = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            max_perspective = max(max_perspective, abs(H[2, 0]) + abs(H[2, 1]))
        assert max_perspective > 1e-5, "透视素材必须包含非仿射分量"


class TestBarcodeFrameIndex:
    """通过帧号条码验证按 source frame 读取的正确性。"""

    @pytest.mark.parametrize("frame_index", [0, 1, 13, 62, 124])
    def test_read_exact_frame(self, fixture_dir, frame_index):
        video_path = os.path.join(fixture_dir, "translation.mp4")
        with FrameSampler(video_path) as sampler:
            sample = sampler.read_gray(frame_index, AnalysisQuality.HIGH)
            assert sample.frame_index == frame_index
            from cv2 import cvtColor, COLOR_GRAY2BGR

            assert read_barcode(cvtColor(sample.gray, COLOR_GRAY2BGR)) == frame_index
