"""RC2（M6-I 组）：相机色域矩阵预设库（camera-native → BT.709 线性）。

TRAW CFA debayer 输出的是相机原生 RGB（IDT 语义），消费前须经 3x3 矩阵
变换到工作色空间（复合管线 = BT.709 线性）。本模块提供 8 家相机厂商色域
的预设矩阵与 TRWM `sensor_matrix_id` 的双向映射。

数据来源与验证（计划验收"预设矩阵对照参考实现抽查"的落实）：
矩阵不由任何上游数值拷贝而来，而是由各厂商**官方发布的 CIE 1931 xy 原色
+ 白点**经标准色度推导（NPM + Bradford 适配）在本模块内计算。推导机制用
双锚点测试锁定（tests/media/test_camera_gamut_matrices.py）：

1. Rec.709 原色推导 == ITU-R BT.709 规范矩阵（< 1e-6）；
2. V-Gamut 原色推导 == Panasonic《V-Log/V-Gamut REFERENCE MANUAL》
   Rev.1.0 §4.2 官方发布的 V-Gamut→XYZ 矩阵（< 1e-5，实际 ~6e-7）。

各色域原色的来源（全部经公开索引页核验，非模型记忆值）：
- ARRI Wide Gamut 3：ARRI《ALEXA Log C Curve - Usage in VFX》白皮书
  （R 0.6840/0.3130, G 0.2210/0.8480, B 0.0861/-0.1020；D65）。AWG3 与
  初代 AWG 原色相同（"V3" 指 LogC 配对版本）。
- Sony S-Gamut3：Sony《Technical Summary for S-Gamut3.Cine/S-Log3 and
  S-Gamut3/S-Log3》（0.730/0.280, 0.140/0.855, 0.100/-0.050；D65）；
  经 fmtconv 色度表核验一致。
- Panasonic V-Gamut：《V-Log/V-Gamut REFERENCE MANUAL》Rev.1.0
  （2014-11-28；0.730/0.280, 0.165/0.840, 0.100/-0.030；D65）——原色由
  官方 §4.2 矩阵反推一致（锚点 2）。
- DaVinci Wide Gamut：Blackmagic Design《DaVinci Resolve 17 – Wide Gamut
  / Intermediate》技术说明（0.8000/0.3130, 0.1682/0.9877, 0.0790/-0.1155；
  D65）。
- REDWideGamutRGB：RED《REDWideGamutRGB and Log3G10 White Paper》
  （0.780308/0.304253, 0.121595/1.493994, 0.095612/-0.084589；D65）；
  经 ACESCentral IDT 对比帖与 fmtconv "redwide" 双源核验。
- Canon Cinema Gamut：Canon《Color Gamut Conversion for DCI-P3+ and
  Cinema Gamut》（0.740/0.270, 0.170/1.140, 0.080/-0.100；D65）；经
  fmtconv 色度表核验一致。
- Fujifilm F-Gamut：Fujifilm F-Log 技术资料（F-Gamut 原色与白点符合
  Rec.2020，见 PostWork.io "F-Log to ACES the Hard Way"）——原色即
  ITU-R BT.2020 (0.708/0.292, 0.170/0.797, 0.131/0.046；D65)。
- Blackmagic Wide Gamut（BMD Film Gen5 配对色域）：Blackmagic
  《Generation 5 Color Science Technical Reference》（2021-05；
  0.7177/0.3171, 0.2280/0.8616, 0.1006/-0.0820；D65）；经独立实现
  （colour-science 数据集/farg crate/公开 Colab）逐位交叉核验。

TRWM `sensor_matrix_id`（RC1 预留 u8 域）映射：0 = 未指定（identity），
1..8 = 下方预设顺序。未知 id 按 0 处理（上层显式提示"未知相机"，不静默
猜测——计划 RC2 验收条款）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np

__all__ = [
    "CameraGamut",
    "CAMERA_GAMUTS",
    "GAMUT_TRWM_IDS",
    "TRWM_ID_GAMUTS",
    "get_camera_gamut",
    "gamut_matrix_to_bt709",
    "resolve_gamut_id",
    "gamut_matrix_to_bt709_or_identity",
    "matrix3_column_major",
    "resolve_debayer_params",
]

# ---- 色度推导基元（内部） ----

_BT709_PRIMARIES: Tuple[Tuple[float, float], ...] = (
    (0.64, 0.33), (0.30, 0.60), (0.15, 0.06))
_D65_XY: Tuple[float, float] = (0.3127, 0.3290)

# Bradford 色适应矩阵（标准常数）
_BRADFORD = np.array([
    [0.8951, 0.2664, -0.1614],
    [-0.7502, 1.7135, 0.0367],
    [0.0389, -0.0685, 1.0296],
])


def _xy_to_xyz(xy: Tuple[float, float]) -> np.ndarray:
    """xy 色度坐标 → XYZ（Y=1 归一形式，与 ITU 规范矩阵同约定）。"""
    x, y = float(xy[0]), float(xy[1])
    return np.array([x / y, 1.0, (1.0 - x - y) / y])


def _normalised_primary_matrix(
    primaries: Tuple[Tuple[float, float], ...],
    white: Tuple[float, float],
) -> np.ndarray:
    """原色 + 白点 → RGB→XYZ 矩阵（列向量约定 M @ rgb）。

    经典构造：M = P · diag(c)，c = P⁻¹·W；M @ (1,1,1) == white(XYZ)。
    """
    p_mat = np.column_stack([_xy_to_xyz(p) for p in primaries])
    gains = np.linalg.solve(p_mat, _xy_to_xyz(white))
    return p_mat @ np.diag(gains)


def _camera_to_bt709(
    primaries: Tuple[Tuple[float, float], ...],
    white: Tuple[float, float] = _D65_XY,
) -> np.ndarray:
    """相机原色 RGB → BT.709 原色 RGB（线性域，Bradford 白点适配）。"""
    m_cam = _normalised_primary_matrix(primaries, white)
    if tuple(white) != _D65_XY:
        rho = (_BRADFORD @ _xy_to_xyz(_D65_XY)) / (_BRADFORD @ _xy_to_xyz(white))
        m_cam = np.linalg.inv(_BRADFORD) @ np.diag(rho) @ _BRADFORD @ m_cam
    xyz709 = _normalised_primary_matrix(_BT709_PRIMARIES, _D65_XY)
    return np.linalg.inv(xyz709) @ m_cam


# ---- 预设表 ----


@dataclass(frozen=True)
class CameraGamut:
    """单个相机色域预设：原色（权威来源）+ 推导出的 →709 矩阵。"""

    gamut_id: str
    label: str
    primaries: Tuple[Tuple[float, float], Tuple[float, float],
                     Tuple[float, float]]
    source: str
    white: Tuple[float, float] = _D65_XY
    matrix_to_bt709: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "matrix_to_bt709", _camera_to_bt709(self.primaries, self.white))


CAMERA_GAMUTS: Tuple[CameraGamut, ...] = (
    CameraGamut(
        "arri-awg3", "ARRI Wide Gamut 3",
        ((0.6840, 0.3130), (0.2210, 0.8480), (0.0861, -0.1020)),
        "ARRI ALEXA LogC whitepaper"),
    CameraGamut(
        "sony-sgamut3", "Sony S-Gamut3",
        ((0.730, 0.280), (0.140, 0.855), (0.100, -0.050)),
        "Sony S-Gamut3/S-Log3 Technical Summary"),
    CameraGamut(
        "panasonic-vgamut", "Panasonic V-Gamut",
        ((0.730, 0.280), (0.165, 0.840), (0.100, -0.030)),
        "Panasonic V-Log/V-Gamut Reference Manual Rev.1.0"),
    CameraGamut(
        "davinci-wg", "DaVinci Wide Gamut",
        ((0.8000, 0.3130), (0.1682, 0.9877), (0.0790, -0.1155)),
        "BMD DaVinci Resolve 17 Wide Gamut/Intermediate note"),
    CameraGamut(
        "red-rwg", "REDWideGamutRGB",
        ((0.780308, 0.304253), (0.121595, 1.493994), (0.095612, -0.084589)),
        "RED REDWideGamutRGB and Log3G10 White Paper"),
    CameraGamut(
        "canon-cg", "Canon Cinema Gamut",
        ((0.740, 0.270), (0.170, 1.140), (0.080, -0.100)),
        "Canon DCI-P3+/Cinema Gamut whitepaper"),
    CameraGamut(
        "fuji-fgamut", "Fujifilm F-Gamut",
        ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046)),
        "Fujifilm F-Log/F-Gamut (= Rec.2020 primaries)"),
    CameraGamut(
        "bmd-wg", "Blackmagic Wide Gamut",
        ((0.7177, 0.3171), (0.2280, 0.8616), (0.1006, -0.0820)),
        "BMD Generation 5 Color Science Technical Reference"),
)

# TRWM sensor_matrix_id ↔ gamut_id（1..8 预设序；0 = 未指定/identity）
GAMUT_TRWM_IDS: Dict[str, int] = {
    g.gamut_id: i + 1 for i, g in enumerate(CAMERA_GAMUTS)}
TRWM_ID_GAMUTS: Dict[int, str] = {
    i + 1: g.gamut_id for i, g in enumerate(CAMERA_GAMUTS)}

_GAMUT_BY_ID: Dict[str, CameraGamut] = {g.gamut_id: g for g in CAMERA_GAMUTS}


# ---- 查询 API ----


def get_camera_gamut(gamut_id: str) -> Optional[CameraGamut]:
    """gamut_id → 预设；未知返回 None（上层显式提示，不静默猜）。"""
    return _GAMUT_BY_ID.get(str(gamut_id or ""))


def gamut_matrix_to_bt709(gamut_id: str) -> Optional[np.ndarray]:
    """gamut_id → 3x3（行主序，camera-native linear → BT.709 linear）。

    未知 id 返回 None——调用方决定回退语义（identity + 显式提示）。
    """
    gamut = get_camera_gamut(gamut_id)
    return None if gamut is None else gamut.matrix_to_bt709


def resolve_gamut_id(
    color_space: Optional[str],
    trwm_sensor_matrix_id: Optional[int] = None,
) -> Optional[str]:
    """RC3 语义：用户显式选择 > TRWM 携带 id > None（未知相机）。

    - `color_space`：RAW 面板/clip 参数（"native" 或空 = 未指定）；
    - `trwm_sensor_matrix_id`：TRWM 元数据的 u8（0/None = 未指定）。
    """
    cs = str(color_space or "").strip()
    if cs and cs != "native":
        gamut = get_camera_gamut(cs)
        if gamut is not None:
            return gamut.gamut_id
    if trwm_sensor_matrix_id:
        gamut_id = TRWM_ID_GAMUTS.get(int(trwm_sensor_matrix_id))
        if gamut_id is not None:
            return gamut_id
    return None


def gamut_matrix_to_bt709_or_identity(
    gamut_id: Optional[str],
) -> Tuple[np.ndarray, bool]:
    """返回 (矩阵, 是否显式预设)。未知/None → identity + False。"""
    if gamut_id:
        m = gamut_matrix_to_bt709(gamut_id)
        if m is not None:
            return m, True
    return np.eye(3, dtype=np.float64), False


def matrix3_column_major(matrix: np.ndarray) -> Tuple[float, ...]:
    """行主序 3x3 → GLSL mat3 列主序扁平 9 元组（ModernGL 注入用）。"""
    m = np.asarray(matrix, dtype=np.float64)
    return tuple(float(v) for v in m.T.reshape(-1))


def _raw_params_all_neutral(raw: Any) -> bool:
    """AUD-38700：判断 clip RawParams 是否完全未被用户调过。

    RawParams 是 clip 创建时即携带的 dataclass（中性默认 1.0/0.0/
    "native"/"preview-2.2"）。此前 resolve_debayer_params 按整对象取舍：
    只要 raw 非 None，即使全字段都是中性默认，也会用中性值静默覆盖 TRWM
    as-shot WB/黑位。这里把"全中性（等价于未调过 RAW 面板）"的对象按
    TRWM as-shot 基线处理；任一字段被用户改过（非默认）则整个对象视为
    用户 authored，全字段优先（tests/media/test_cfa_gamut_debayer.py
    TestResolveDebayerParams.test_user_over_trwm 锁定的语义）。
    """
    neutral_scalars = {
        'wb_gain_r': 1.0, 'wb_gain_g': 1.0, 'wb_gain_b': 1.0,
        'exposure_stops': 0.0, 'black_level': 0.0,
    }
    for name, default in neutral_scalars.items():
        try:
            if float(getattr(raw, name, default)) != default:
                return False
        except (TypeError, ValueError):
            return False
    if str(getattr(raw, 'color_space', '') or '') not in ('', 'native'):
        return False
    if str(getattr(raw, 'gamma', '') or '') not in ('', 'preview-2.2'):
        return False
    # display_ootf=False 是显式用户选择（RawParams 默认 True）
    if not bool(getattr(raw, 'display_ootf', True)):
        return False
    return True


def resolve_debayer_params(raw: Any = None,
                           trwm: Optional[dict] = None) -> dict:
    """CFA debayer 渲染参数统一解析（2026-09-21 复查收口）。

    优先级：用户 clip RawParams > 帧 TRWM as-shot > 单位默认——
    GPU 预览（edit_gpu_preview_renderer._render_cfa_frame）、CPU 回退
    （timeline_preview_renderer._convert_planar_to_bgr）、交付导出
    （timeline_export._debayer_cfa_to_bgr）三链同源取参，防止
    "预览吃用户参数、导出只吃 TRWM" 的链间漂移。

    - raw：clip RawParams（或任意同名属性对象）；None = 未调过 RAW 面板；
      AUD-38700：全中性默认的 RawParams（等价未调过）不覆盖 TRWM as-shot；
    - trwm：帧 extra['topos_trwm']（无 TRWM 原子 → None/{}）；
    - 返回 dict：wb_gains(3 元组) / exposure_stops / black_level /
      gamma_mode（0=linear 1=gamma2.2 2=sRGB）/ matrix（行主序 3x3，
      camera-native→BT.709 linear；未知相机 identity）/ explicit_gamut。
    """
    trwm = trwm or {}
    raw_is_user_authored = raw is not None and not _raw_params_all_neutral(raw)
    if raw_is_user_authored:
        wb_gains = (float(getattr(raw, 'wb_gain_r', 1.0)),
                    float(getattr(raw, 'wb_gain_g', 1.0)),
                    float(getattr(raw, 'wb_gain_b', 1.0)))
        exposure_stops = float(getattr(raw, 'exposure_stops', 0.0))
        black_level = float(getattr(raw, 'black_level', 0.0))
        color_space = str(getattr(raw, 'color_space', '') or '')
        gamma_name = str(getattr(raw, 'gamma', '') or '')
        display_ootf = bool(getattr(raw, 'display_ootf', True))
    else:
        # RC4 / AUD-38700：clip 无用户 RAW 参数（含全中性 RawParams）
        # → 文件 as-shot 基线（TRWM）
        wb_gains = (float(trwm.get('wb_gain_r', 1.0)),
                    float(trwm.get('wb_gain_g', 1.0)),
                    float(trwm.get('wb_gain_b', 1.0)))
        exposure_stops = float(trwm.get('exposure_stops', 0.0))
        black_level = float(trwm.get('black_level', 0.0))
        color_space = ''
        gamma_name = ''
        display_ootf = True
    sensor_id = int(trwm.get('sensor_matrix_id', 0) or 0)
    gamut_id = resolve_gamut_id(color_space or None, sensor_id)
    matrix, explicit = gamut_matrix_to_bt709_or_identity(gamut_id)
    gamma_mode = {"preview-2.2": 1, "srgb": 2, "linear": 0}.get(
        gamma_name, 1 if display_ootf else 0)
    return {
        'wb_gains': wb_gains,
        'exposure_stops': exposure_stops,
        'black_level': black_level,
        'gamma_mode': gamma_mode,
        'matrix': matrix,
        'explicit_gamut': explicit,
    }
