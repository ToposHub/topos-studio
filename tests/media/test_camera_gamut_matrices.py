"""RC2（M6-I 组）：相机色域矩阵预设库测试。

验收口径（计划 M6-RC2"预设矩阵对照参考实现抽查"）：
1. 推导机制锚点——Rec.709 原色推导 == ITU-R BT.709 规范矩阵；
2. 参考实现锚点——V-Gamut 原色推导 == Panasonic 官方手册 §4.2 发布矩阵；
3. 8 家预设金标准冻结（换 numpy/重排原色立即报警）；
4. 白点保持（各行和=1）+ 可逆性 + TRWM id 双向映射 + 未知 id 显式语义。
"""
import numpy as np
import pytest

from src.shared.video.camera_gamut_matrices import (
    CAMERA_GAMUTS,
    GAMUT_TRWM_IDS,
    TRWM_ID_GAMUTS,
    gamut_matrix_to_bt709,
    gamut_matrix_to_bt709_or_identity,
    get_camera_gamut,
    matrix3_column_major,
    resolve_gamut_id,
)

# ITU-R BT.709 规范 RGB→XYZ（Y=1 白点归一）
_BT709_CANONICAL = np.array([
    [0.4123908, 0.3575843, 0.1804808],
    [0.2126390, 0.7151687, 0.0721923],
    [0.0193308, 0.1191948, 0.9505322]])

# Panasonic《V-Log/V-Gamut REFERENCE MANUAL》Rev.1.0 §4.2 官方矩阵
_VGAMUT_OFFICIAL_XYZ = np.array([
    [0.679644, 0.152211, 0.118600],
    [0.260686, 0.774894, -0.035580],
    [-0.009310, -0.004612, 1.102980]])

# 金标准冻结（2026-09-21 推导，来源见模块 docstring）
_GOLDEN_CAM_TO_709 = {
    "arri-awg3": (
        (1.6175234, -0.5372866, -0.0802368),
        (-0.0705727, 1.3346131, -0.2640403),
        (-0.0211017, -0.2269539, 1.2480556)),
    "sony-sgamut3": (
        (1.8779151, -0.7941688, -0.0837464),
        (-0.1768070, 1.3509996, -0.1741926),
        (-0.0262011, -0.1484223, 1.1746234)),
    "panasonic-vgamut": (
        (1.8065759, -0.6956973, -0.1108786),
        (-0.1700903, 1.3059552, -0.1358649),
        (-0.0252058, -0.1544683, 1.1796741)),
    "davinci-wg": (
        (1.8986149, -0.7921762, -0.1064387),
        (-0.1689488, 1.4889758, -0.3200270),
        (-0.1215392, -0.3156759, 1.4372150)),
    "red-rwg": (
        (1.9819760, -0.9004318, -0.0815442),
        (-0.1781432, 1.5004684, -0.3223252),
        (-0.1017960, -0.5352635, 1.6370594)),
    "canon-cg": (
        (1.9238613, -0.7987607, -0.1251006),
        (-0.2043108, 1.4958985, -0.2915877),
        (-0.0236850, -0.4201270, 1.4438120)),
    "fuji-fgamut": (
        (1.6604910, -0.5876411, -0.0728499),
        (-0.1245505, 1.1328999, -0.0083494),
        (-0.0181508, -0.1005789, 1.1187297)),
    "bmd-wg": (
        (1.5685504, -0.5228361, -0.0457143),
        (-0.0863793, 1.3449178, -0.2585385),
        (-0.0520103, -0.2491165, 1.3011268)),
}


class TestDerivationAnchors:
    def test_rec709_primaries_reproduce_itu_matrix(self):
        """锚点 1：推导机制复现 ITU 规范矩阵（< 1e-6）。"""
        from src.shared.video.camera_gamut_matrices import (
            _BT709_PRIMARIES, _D65_XY, _normalised_primary_matrix)
        m = _normalised_primary_matrix(_BT709_PRIMARIES, _D65_XY)
        assert np.abs(m - _BT709_CANONICAL).max() < 1e-6

    def test_vgamut_primaries_reproduce_panasonic_official_matrix(self):
        """锚点 2：V-Gamut 原色推导 == Panasonic 官方 §4.2 矩阵（< 1e-5）。

        官方矩阵是 RGB→XYZ；此处经 709 链转回（白点保持下等价于对官方
        矩阵施加同一定义）——直接比较 npm 形式最严格。
        """
        from src.shared.video.camera_gamut_matrices import (
            _normalised_primary_matrix, get_camera_gamut)
        gamut = get_camera_gamut("panasonic-vgamut")
        m = _normalised_primary_matrix(gamut.primaries, gamut.white)
        assert np.abs(m - _VGAMUT_OFFICIAL_XYZ).max() < 1e-5


class TestPresetLibrary:
    def test_eight_vendor_presets_present(self):
        ids = [g.gamut_id for g in CAMERA_GAMUTS]
        assert ids == [
            "arri-awg3", "sony-sgamut3", "panasonic-vgamut", "davinci-wg",
            "red-rwg", "canon-cg", "fuji-fgamut", "bmd-wg"]

    def test_golden_matrices_frozen(self):
        for gamut in CAMERA_GAMUTS:
            golden = np.array(_GOLDEN_CAM_TO_709[gamut.gamut_id])
            assert np.abs(gamut.matrix_to_bt709 - golden).max() < 5e-7, (
                f"{gamut.gamut_id} 矩阵漂移")

    def test_white_preserved_row_sums(self):
        """相机白（等 RGB）→ 709 白（等 RGB）：行和恒 1。"""
        for gamut in CAMERA_GAMUTS:
            np.testing.assert_allclose(
                gamut.matrix_to_bt709.sum(axis=1), 1.0, atol=1e-9)

    def test_matrices_invertible(self):
        for gamut in CAMERA_GAMUTS:
            det = float(np.linalg.det(gamut.matrix_to_bt709))
            assert 0.0 < det < np.inf


class TestLookupApi:
    def test_unknown_id_returns_none_not_guess(self):
        """计划 RC2 验收：未知相机显式 None，不静默猜。"""
        assert get_camera_gamut("nonexistent") is None
        assert gamut_matrix_to_bt709("nonexistent") is None
        assert get_camera_gamut("") is None
        assert get_camera_gamut(None) is None

    def test_identity_fallback_flagged(self):
        m, explicit = gamut_matrix_to_bt709_or_identity(None)
        assert not explicit
        np.testing.assert_array_equal(m, np.eye(3))
        m, explicit = gamut_matrix_to_bt709_or_identity("arri-awg3")
        assert explicit
        assert m is not None

    def test_resolve_explicit_wins_over_trwm(self):
        assert resolve_gamut_id("red-rwg", 1) == "red-rwg"
        assert resolve_gamut_id("native", 3) == "panasonic-vgamut"
        assert resolve_gamut_id(None, 3) == "panasonic-vgamut"
        assert resolve_gamut_id("", 0) is None
        assert resolve_gamut_id(None, None) is None

    def test_trwm_id_mapping_bidirectional(self):
        assert GAMUT_TRWM_IDS["arri-awg3"] == 1
        assert TRWM_ID_GAMUTS[1] == "arri-awg3"
        assert TRWM_ID_GAMUTS[8] == "bmd-wg"
        assert TRWM_ID_GAMUTS.get(0) is None
        assert TRWM_ID_GAMUTS.get(9) is None
        for gid, tid in GAMUT_TRWM_IDS.items():
            assert TRWM_ID_GAMUTS[tid] == gid

    def test_column_major_layout(self):
        m = np.array([[1.0, 2.0, 3.0],
                      [4.0, 5.0, 6.0],
                      [7.0, 8.0, 9.0]])
        assert matrix3_column_major(m) == (1.0, 4.0, 7.0,
                                           2.0, 5.0, 8.0,
                                           3.0, 6.0, 9.0)
