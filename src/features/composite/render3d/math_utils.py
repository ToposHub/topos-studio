"""
3D 数学工具库 — 基于 numpy 的轻量矩阵/向量运算。

不引入 pyrr/glm 等外部依赖，numpy 足够覆盖 Scene3D 节点的需求。

所有矩阵均为 4x4（变换）或 3x3（法线矩阵）numpy float32 数组。
约定：矩阵以 **行主序** 存储（numpy 默认），在提交给 ModernGL uniform 前通过
`matrix_to_tuple()` 转换为 GLSL 期望的 **列主序 16 元组**。

参考标准：OpenGL 右手坐标系，相机看向 -Z（gluPerspective / gluLookAt 语义）。
"""

from typing import Tuple

import numpy as np

# 4x4 单位矩阵
IDENTITY4 = np.eye(4, dtype=np.float32)


def perspective(fov_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    """透视投影矩阵（右手系，OpenGL 风格，看向 -Z）。

    Args:
        fov_deg: 垂直视场角（度）。
        aspect: 宽高比 = width / height。
        near: 近裁剪面距离（>0）。
        far: 远裁剪面距离（>near）。

    Returns:
        4x4 float32 投影矩阵（行主序）。
    """
    fov_rad = np.radians(fov_deg)
    # f = cot(fov/2) = 1 / tan(fov/2)
    f = 1.0 / np.tan(fov_rad * 0.5)
    # 防御性钳制，避免除零与负宽高比
    aspect = aspect if abs(aspect) > 1e-6 else 1e-6
    near = near if near > 1e-6 else 1e-6
    far = far if far > near else near + 1.0

    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2.0 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def ortho(scale: float, aspect: float, near: float, far: float) -> np.ndarray:
    """正交投影矩阵（右手系，OpenGL 风格，看向 -Z）。

    用于 top/right/front 正交视图（无透视收缩），C4D 四视图与 Camera3D
    的 ``orthographic`` 模式共用。

    Args:
        scale: 视口半高（世界单位）。半宽 = scale * aspect。
        aspect: 宽高比 = width / height。
        near: 近裁剪面距离。
        far: 远裁剪面距离（>near）。

    Returns:
        4x4 float32 正交投影矩阵（行主序）。
    """
    scale = scale if scale > 1e-6 else 1e-6
    aspect = aspect if abs(aspect) > 1e-6 else 1e-6
    near = float(near)
    far = far if far > near else near + 1.0

    t = scale            # top
    b = -scale           # bottom
    r = scale * aspect   # right
    l = -r               # left
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = 2.0 / (r - l)
    m[1, 1] = 2.0 / (t - b)
    m[2, 2] = -2.0 / (far - near)
    m[0, 3] = -(r + l) / (r - l)
    m[1, 3] = -(t + b) / (t - b)
    m[2, 3] = -(far + near) / (far - near)
    m[3, 3] = 1.0
    return m


def ortho_scale_for_perspective_match(cam_dist: float, fov_deg: float) -> float:
    """计算与透视投影在 target 处视野匹配的正交 scale（视口半高，世界单位）。

    透视视图在距离 cam_dist 处的可见半高 ≈ cam_dist * tan(fov/2)。
    正交视图用此值做 ortho() 的 scale 参数，可使四视图中 target 处的物体
    大小与透视一致（C4D/Blender 四视图标准做法）。

    Args:
        cam_dist: 相机到 target 的距离（世界单位，>0）。
        fov_deg: 透视垂直视场角（度）。

    Returns:
        正交视口半高（世界单位，>=0.1）。
    """
    cam_dist = cam_dist if cam_dist > 1e-6 else 1e-6
    fov_rad = np.radians(fov_deg)
    return max(0.1, cam_dist * float(np.tan(fov_rad * 0.5)))


def pitch_from_eye_target(eye: np.ndarray, target: np.ndarray) -> float:
    """从相机位置与注视点反算俯仰角（度）。

    用于让外接 Camera3D 的真实视角驱动 Scene3D grid 地平线渐隐，
    而非用 Scene3D 自身 fallback 的 ``cam_orbit_pitch``（与外接相机不一致时
    grid fade 会视觉错位）。

    pitch = 眼-目标 向量在 Y 轴的反正弦（-90 低头 / +90 仰头）。

    Returns:
        俯仰角（度），范围 [-89, 89]（钳制避免万向锁）。
    """
    eye = np.asarray(eye, dtype=np.float32).reshape(3)
    target = np.asarray(target, dtype=np.float32).reshape(3)
    # 用 eye - target（相机相对注视点的高度向量），与 orbit_to_eye 的 pitch 同向：
    # 相机高于 target → 正值（抬高），低于 → 负值。这样 grid fade 拿到的 pitch 与
    # orbit 模式的 cam_orbit_pitch 语义一致（正=相机抬高/俯视）。
    d = eye - target
    horiz = float(np.sqrt(d[0] * d[0] + d[2] * d[2]))
    if horiz < 1e-6:
        return 89.0 if d[1] >= 0.0 else -89.0
    pitch = float(np.degrees(np.arctan2(d[1], horiz)))
    return max(-89.0, min(89.0, pitch))


def look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    """视图矩阵（相机位于 eye，看向 target，up 为世界上方）。

    Args:
        eye: 相机位置 (3,)。
        target: 注视点 (3,)。
        up: 上方向参考 (3,)，通常 (0,1,0)。

    Returns:
        4x4 float32 视图矩阵（行主序）。
    """
    eye = np.asarray(eye, dtype=np.float32).reshape(3)
    target = np.asarray(target, dtype=np.float32).reshape(3)
    up = np.asarray(up, dtype=np.float32).reshape(3)

    # 相机坐标轴（右手系）
    f = target - eye
    f_norm = np.linalg.norm(f)
    if f_norm < 1e-6:
        f = np.array([0.0, 0.0, -1.0], dtype=np.float32)
    else:
        f = f / f_norm  # forward（看向方向）

    s = np.cross(f, up)  # right = forward × up
    s_norm = np.linalg.norm(s)
    if s_norm < 1e-6:
        # up 与 forward 平行，选一个备选 up
        up = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        s = np.cross(f, up)
        s_norm = np.linalg.norm(s)
        if s_norm < 1e-6:
            s = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        else:
            s = s / s_norm
    else:
        s = s / s_norm
    u = np.cross(s, f)  # recomputed up = right × forward

    m = np.eye(4, dtype=np.float32)
    # 行 0..2 列 0..2 = 旋转（s, u, -f）
    m[0, 0:3] = s
    m[1, 0:3] = u
    m[2, 0:3] = -f
    # 平移：-R·eye
    m[0, 3] = -np.dot(s, eye)
    m[1, 3] = -np.dot(u, eye)
    m[2, 3] = np.dot(f, eye)
    return m


def translate(x: float, y: float, z: float) -> np.ndarray:
    """平移矩阵。"""
    m = np.eye(4, dtype=np.float32)
    m[0, 3] = float(x)
    m[1, 3] = float(y)
    m[2, 3] = float(z)
    return m


def scale(x: float, y: float, z: float) -> np.ndarray:
    """缩放矩阵。"""
    m = np.eye(4, dtype=np.float32)
    m[0, 0] = float(x)
    m[1, 1] = float(y)
    m[2, 2] = float(z)
    return m


def rotate_x(deg: float) -> np.ndarray:
    """绕 X 轴旋转矩阵（右手系）。"""
    rad = np.radians(deg)
    c, s = np.cos(rad), np.sin(rad)
    m = np.eye(4, dtype=np.float32)
    m[1, 1] = c
    m[1, 2] = -s
    m[2, 1] = s
    m[2, 2] = c
    return m


def rotate_y(deg: float) -> np.ndarray:
    """绕 Y 轴旋转矩阵（右手系）。"""
    rad = np.radians(deg)
    c, s = np.cos(rad), np.sin(rad)
    m = np.eye(4, dtype=np.float32)
    m[0, 0] = c
    m[0, 2] = s
    m[2, 0] = -s
    m[2, 2] = c
    return m


def rotate_z(deg: float) -> np.ndarray:
    """绕 Z 轴旋转矩阵（右手系）。"""
    rad = np.radians(deg)
    c, s = np.cos(rad), np.sin(rad)
    m = np.eye(4, dtype=np.float32)
    m[0, 0] = c
    m[0, 1] = -s
    m[1, 0] = s
    m[1, 1] = c
    return m


def compose(t: np.ndarray, r: np.ndarray, s: np.ndarray) -> np.ndarray:
    """组合变换矩阵 = T · R · S（先缩放、再旋转、再平移）。

    Args:
        t: 平移矩阵。
        r: 旋转矩阵（任意单轴或乘积）。
        s: 缩放矩阵。

    Returns:
        4x4 float32 组合矩阵。
    """
    return (t @ r @ s).astype(np.float32)


def rotate_xyz(rx_deg: float, ry_deg: float, rz_deg: float) -> np.ndarray:
    """欧拉角组合旋转（按 X → Y → Z 顺序）。"""
    return (rotate_x(rx_deg) @ rotate_y(ry_deg) @ rotate_z(rz_deg)).astype(np.float32)


def matrix_to_tuple(m: np.ndarray) -> Tuple[float, ...]:
    """将 4x4 行主序 numpy 矩阵转换为 GLSL mat4 uniform 期望的列主序 16 元组。

    ModernGL/OpenGL 的 mat4 uniform 按列主序接受 16 个 float。
    numpy 默认行主序，因此需先转置。

    Args:
        m: 4x4 行主序矩阵。

    Returns:
        16 元组（列主序），可直接赋给 program['u_xxx'].value。
    """
    m = np.asarray(m, dtype=np.float32).reshape(4, 4)
    # 转置后按 C 序展平 = 列主序
    return tuple(m.T.flatten())


def orbit_to_eye(dist: float, yaw_deg: float, pitch_deg: float,
                 target: np.ndarray = None) -> np.ndarray:
    """轨道相机参数 → 相机世界位置。

    相机绕 target 旋转：yaw（水平角）、pitch（俯仰角），距离 dist。
    yaw=0, pitch=0 时相机位于 +Z 方向看向原点。

    Args:
        dist: 到目标点的距离（>0）。
        yaw_deg: 水平方位角（度），逆时针绕 Y 轴。
        pitch_deg: 俯仰角（度），正值抬高相机。范围 [-89, 89]。
        target: 注视中心，默认原点。

    Returns:
        相机世界位置 (3,) float32。
    """
    yaw = np.radians(yaw_deg)
    pitch = np.radians(pitch_deg)
    # 钳制 pitch 避免万向锁/翻转
    pitch = np.clip(pitch, np.radians(-89.0), np.radians(89.0))

    cp = np.cos(pitch)
    x = dist * cp * np.sin(yaw)
    y = dist * np.sin(pitch)
    z = dist * cp * np.cos(yaw)

    eye = np.array([x, y, z], dtype=np.float32)
    if target is not None:
        eye = eye + np.asarray(target, dtype=np.float32).reshape(3)
    return eye


def vec3(x: float, y: float, z: float) -> np.ndarray:
    """构造 float32 三维向量。"""
    return np.array([float(x), float(y), float(z)], dtype=np.float32)


def rotate_around_axis(v: np.ndarray, axis: np.ndarray, angle_deg: float) -> np.ndarray:
    """绕任意轴旋转向量（罗德里格斯公式）。

    用于相机横滚（roll）：绕前向轴旋转 up 向量。

    Args:
        v: 待旋转向量 (3,)。
        axis: 旋转轴（不需归一化，内部归一化；近平行退化时返回原向量）。
        angle_deg: 旋转角度（度）。

    Returns:
        旋转后向量 (3,) float32。
    """
    v = np.asarray(v, dtype=np.float32).reshape(3)
    axis = np.asarray(axis, dtype=np.float32).reshape(3)
    n = float(np.linalg.norm(axis))
    if n < 1e-6:
        return v.astype(np.float32, copy=True)
    axis = axis / n
    a = np.radians(angle_deg)
    c = float(np.cos(a))
    s = float(np.sin(a))
    # 罗德里格斯：v' = v cosθ + (axis × v) sinθ + axis (axis·v)(1 - cosθ)
    return (v * c
            + np.cross(axis, v) * s
            + axis * np.dot(axis, v) * (1.0 - c)).astype(np.float32)


def normalize(v: np.ndarray) -> np.ndarray:
    """归一化向量（防御零向量）。"""
    v = np.asarray(v, dtype=np.float32).reshape(3)
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else v


# ── P2 灯光辅助函数（CPU 侧预览/图标用；shader 内有独立实现）──


def spot_intensity(cos_theta: float, inner_cos: float, outer_cos: float) -> float:
    """聚光灯锥角强度（与 shader spot_cone_intensity 数值一致）。

    cos_theta = dot(L, spot_axis)；inner_cos/outer_cos 为锥角 cos 值。
    返回 0..1：锥内全亮，外锥外全黑，内外之间为**线性斜坡**（clamp 线性
    插值）。AUD-8911：此处与 shaders.py 的 spot_cone_intensity 实现
    （clamp((cos - outer) / max(inner - outer, 1e-4))）同为线性过渡——
    shader 端注释自称 smoothstep 属同样的文档偏差（shaders.py 不在本批
    修复范围），以实现为准，勿按 smoothstep 语义依赖本函数。
    """
    denom = inner_cos - outer_cos
    if abs(denom) < 1e-4:
        return 1.0 if cos_theta >= inner_cos else 0.0
    return max(0.0, min(1.0, (cos_theta - outer_cos) / denom))


# ── P3 专业摄影机辅助函数 ──


def perspective_with_shift(fov_deg: float, aspect: float, near: float, far: float,
                           shift_x: float = 0.0, shift_y: float = 0.0) -> np.ndarray:
    """带镜头偏移（lens shift）的透视投影矩阵。

    lens shift 平移投影的视锥中心（不产生透视变形），用于摄影机平移构图、
    architectural 渲染避免透视校正。shift 单位为视锥半宽/半高的比例（[-1,1]）。

    在 perspective() 基础上修改 m[0][2] / m[1][2]（NDC 偏移项）。
    """
    m = perspective(fov_deg, aspect, near, far).copy()
    # OpenGL 投影矩阵的 [0][2] / [1][2] 控制 NDC x/y 的平移
    m[0][2] = shift_x
    m[1][2] = shift_y
    return m
