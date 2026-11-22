import os
import sys
import io
import time
import ctypes
import struct
import zlib
import threading
import tempfile
import subprocess
import pickle
import importlib.util
from types import SimpleNamespace

# 依赖自检：避免用户用错 Python 时只看到一长串 ModuleNotFoundError。
_REQUIRED_MODULES = ("numpy", "trimesh", "PIL", "PyQt6", "OpenGL")
_MISSING_MODULES = [name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None]
if _MISSING_MODULES:
    print("缺少运行依赖：" + "、".join(_MISSING_MODULES))
    print(f"当前 Python：{sys.executable}")
    print("请使用安装了本应用依赖的 Python 运行：")
    print(f'  /path/to/python "{os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")}"')
    print("请使用已安装 requirements.txt 中依赖的 Python 环境。")
    raise SystemExit(1)

import numpy as np
import trimesh
from PIL import Image

try:
    import assimp_py  # 可选依赖：缺失时仅 .fbx 不可用，其余格式不受影响
except ImportError:
    assimp_py = None

from PyQt6.QtCore import (
    Qt, QPoint, QPointF, QRectF, QTimer, QThread, QFileSystemWatcher, pyqtSignal
)
from PyQt6.QtGui import (
    QSurfaceFormat, QPainter, QColor, QPen, QBrush, QRadialGradient,
    QLinearGradient, QFont, QAction, QActionGroup, QPainterPath
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QFileDialog, QWidget,
    QHBoxLayout, QVBoxLayout, QPushButton, QFrame,
    QListWidget, QListWidgetItem
)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from OpenGL.GL import (
    glClearColor, glClear, glEnable, glDisable, glViewport,
    glPolygonMode, glPolygonOffset, glDepthMask, glBlendFunc,
    glGenVertexArrays, glBindVertexArray, glDeleteVertexArrays,
    glGenBuffers, glBindBuffer, glBufferData, glDeleteBuffers,
    glVertexAttribPointer, glEnableVertexAttribArray, glVertexAttribIPointer,
    glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
    glGenerateMipmap, glActiveTexture, glDeleteTextures,
    glCreateShader, glShaderSource, glCompileShader, glGetShaderiv, glGetShaderInfoLog,
    glCreateProgram, glAttachShader, glLinkProgram, glGetProgramiv, glGetProgramInfoLog,
    glUseProgram, glGetUniformLocation, glUniformMatrix4fv, glUniformMatrix3fv, glUniform3f, glUniform1i,
    glUniform1f,
    glUniform3fv, glUniform2fv, glUniform1iv,
    glDrawElements, glDrawArrays,
    glReadPixels, glGetIntegerv,
    glGenFramebuffers, glDeleteFramebuffers, glBindFramebuffer,
    glGenRenderbuffers, glDeleteRenderbuffers, glBindRenderbuffer,
    glRenderbufferStorage, glFramebufferRenderbuffer, glCheckFramebufferStatus,
    glBlitFramebuffer, glDrawBuffer, glReadBuffer,
    GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT, GL_DEPTH_TEST, GL_MULTISAMPLE,
    GL_BLEND, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_POLYGON_OFFSET_LINE,
    GL_ARRAY_BUFFER, GL_ELEMENT_ARRAY_BUFFER, GL_STATIC_DRAW, GL_DYNAMIC_DRAW,
    GL_FLOAT, GL_FALSE, GL_TRUE, GL_UNSIGNED_INT, GL_UNSIGNED_BYTE, GL_INT,
    GL_TRIANGLES, GL_LINES, GL_TEXTURE_2D, GL_TEXTURE0, GL_TEXTURE1, GL_TEXTURE2,
    GL_TEXTURE3, GL_RGBA,
    GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER, GL_LINEAR_MIPMAP_LINEAR, GL_LINEAR,
    GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_REPEAT, GL_CLAMP_TO_EDGE,
    GL_VERTEX_SHADER, GL_FRAGMENT_SHADER, GL_COMPILE_STATUS, GL_LINK_STATUS,
    GL_FRONT_AND_BACK, GL_LINE, GL_FILL,
    GL_DEPTH_COMPONENT, GL_VIEWPORT,
    GL_FRAMEBUFFER, GL_READ_FRAMEBUFFER, GL_DRAW_FRAMEBUFFER,
    GL_RENDERBUFFER, GL_DEPTH_STENCIL_ATTACHMENT, GL_DEPTH24_STENCIL8,
    GL_NONE, GL_NEAREST, GL_FRAMEBUFFER_COMPLETE, GL_FRAMEBUFFER_BINDING
)

SUPPORTED_EXTS = {".obj", ".stl", ".ply", ".glb", ".gltf", ".off", ".3mf", ".fbx"}
COPY_KEY_HINT = "⌘C" if sys.platform == "darwin" else "Ctrl+C"


class ModelLoadCancelled(Exception):
    """用户快速翻页时，旧的后台解析任务主动退出。"""


class ModelLoadWorker(QThread):
    """把磁盘 IO / Assimp / Trimesh / NumPy 整理放到后台线程。

    OpenGL 资源仍然只在主线程上传；这里只产生纯 Python/NumPy/PIL 数据。
    FBX 会走一个隔离的子进程：assimp-py 在原生导入阶段会长时间持有 GIL，
    单纯 QThread 仍会把主线程卡住；子进程解析完成后用 pickle 回传。
    """

    resultReady = pyqtSignal(int, str, object, str)  # req_id, filepath, result, error

    def __init__(self, req_id: int, filepath: str, parse_fn, parent=None,
                 use_process: bool = False, project_dir: str = ""):
        super().__init__(parent)
        self.req_id = int(req_id)
        self.filepath = filepath
        self._parse_fn = parse_fn
        self._use_process = bool(use_process)
        self._project_dir = project_dir or os.path.dirname(os.path.abspath(__file__))
        self._cancel_event = threading.Event()
        self._proc = None
        self.result_emitted = False

    def request_cancel(self):
        self._cancel_event.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def _run_subprocess(self):
        """启动隔离解析子进程；等待期间只轮询，绝不在主线程解析 Assimp。"""
        fd, out_path = tempfile.mkstemp(prefix="q3d_parse_", suffix=".pkl")
        os.close(fd)
        stderr_text = ""
        try:
            env = os.environ.copy()
            env["QT_QPA_PLATFORM"] = "offscreen"
            old_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                self._project_dir + (os.pathsep + old_pythonpath if old_pythonpath else ""))
            cmd = [
                sys.executable,
                os.path.join(self._project_dir, "quick_3d_parse_worker.py"),
                self.filepath,
                out_path,
            ]
            self._proc = subprocess.Popen(
                cmd,
                cwd=self._project_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=False,
            )
            while True:
                if self.is_cancelled():
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                    return
                if self._proc.poll() is not None:
                    break
                time.sleep(0.12)

            try:
                stderr_text = (self._proc.stderr.read() or b"").decode("utf-8", "replace")
            except Exception:
                stderr_text = ""
            if self._proc.returncode != 0:
                raise RuntimeError(
                    f"后台解析进程退出码 {self._proc.returncode}"
                    + (f"：{stderr_text.strip()[-240:]}" if stderr_text.strip() else ""))
            if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
                raise RuntimeError(
                    "后台解析进程没有产生结果"
                    + (f"：{stderr_text.strip()[-240:]}" if stderr_text.strip() else ""))
            with open(out_path, "rb") as fh:
                payload = pickle.load(fh)
            if not isinstance(payload, (tuple, list)) or len(payload) != 2:
                raise RuntimeError("后台解析结果格式无效")
            status, value = payload
            if status != "ok":
                raise RuntimeError(str(value))
            if self.is_cancelled():
                return
            self.result_emitted = True
            self.resultReady.emit(self.req_id, self.filepath, value, "")
        except Exception as exc:
            if not self.is_cancelled():
                self.result_emitted = True
                self.resultReady.emit(self.req_id, self.filepath, None, str(exc))
        finally:
            try:
                if os.path.exists(out_path):
                    os.unlink(out_path)
            except Exception:
                pass
            self._proc = None

    def run(self):
        if self._use_process:
            self._run_subprocess()
            return
        try:
            result = self._parse_fn(self.filepath, self.is_cancelled)
            if self.is_cancelled():
                return
            self.result_emitted = True
            self.resultReady.emit(self.req_id, self.filepath, result, "")
        except ModelLoadCancelled:
            return
        except Exception as exc:  # 后台异常必须回到主线程显示，不能静默吞掉
            if self.is_cancelled():
                return
            self.result_emitted = True
            self.resultReady.emit(self.req_id, self.filepath, None, str(exc))

# ==============================================================================
# 1. 模型着色器（支持贴图、顶点颜色、雕塑白模、法线视图、线框覆盖）
# ==============================================================================
MESH_VS = """
#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aUV;
layout (location = 3) in vec4 aColor;
layout (location = 4) in ivec4 aBoneIDs;      // 蒙皮网格才启用，静态网格走通用属性 (0,0,0,1)
layout (location = 5) in vec4 aBoneWeights;

uniform mat4 uModel;            // 仅 Z-Up 轴向切换
uniform mat4 uNodeTransform;    // 归一化矩阵 × FBX 节点世界矩阵（蒙皮网格只传归一化矩阵）
uniform mat3 uNormalMatrix;     // 由 uModel × uNodeTransform 在 CPU 端预计算，避免逐顶点求逆
uniform mat4 uView;
uniform mat4 uProj;
uniform int uHasSkinning;
uniform mat4 uBoneMatrices[100];

out vec3 vNormal;
out vec3 vFragPos;
out vec2 vUV;
out vec4 vColor;

void main() {
    vec4 localPos = vec4(aPos, 1.0);
    vec3 localNorm = aNormal;

    if (uHasSkinning == 1) {
        mat4 skinMat = mat4(0.0);
        float totalW = 0.0;
        for (int i = 0; i < 4; ++i) {
            int bid = aBoneIDs[i];
            float w = aBoneWeights[i];
            if (bid >= 0 && bid < 100 && w > 0.0) {
                skinMat += uBoneMatrices[bid] * w;
                totalW += w;
            }
        }
        if (totalW > 0.001) {
            localPos = skinMat * localPos;
            localNorm = mat3(skinMat) * localNorm;
        }
    }

    mat4 worldMat = uModel * uNodeTransform;
    vec4 worldPos = worldMat * localPos;
    vFragPos = worldPos.xyz;
    vNormal = uNormalMatrix * localNorm;
    vUV = aUV;
    vColor = aColor;
    gl_Position = uProj * uView * worldPos;
}
"""

MESH_FS = """
#version 330 core
in vec3 vNormal;
in vec3 vFragPos;
in vec2 vUV;
in vec4 vColor;

uniform vec3 uViewPos;
uniform vec3 uTargetPos;
uniform vec3 uBaseColor;
uniform sampler2D uAlbedoMap;
uniform sampler2D uCheckerMap;
uniform int uHasTexture;
uniform int uUVChecker;     // 1: UV 棋盘格诊断覆盖
uniform int uRenderMode; // 0: 材质/贴图/顶点色, 1: 雕塑白模, 2: 法线视图, 3: 线框纯色, 4: 反照率
uniform int uCheckBackface; // 0: 正常, 1: 面朝向检查（正面蓝 / 背面红）
uniform float uLightYaw;    // 影棚三点布光绕相机上方向的旋转角（弧度）

// FBX 场景灯光（assimp-py 不暴露 Scene.lights，参数由内置二进制解析器读出）
uniform int uUseSceneLights;
uniform int uNumLights;
uniform vec3 uLightPos[8];
uniform vec3 uLightDir[8];
uniform vec3 uLightColor[8];
uniform int uLightType[8];      // 1: 平行光, 2: 点光源, 3: 聚光灯
uniform vec2 uLightCone[8];     // x: cos(innerAngle), y: cos(outerAngle)

// 屏幕空间法线贴图：不需要 CPU 预计算切线，直接用 dFdx/dFdy 构建 TBN
uniform sampler2D uNormalMap;
uniform int uHasNormalMap;

// MatCap: 视空间法线采样；uView 与顶点阶段同名，使用同一份矩阵
uniform mat4 uView;
uniform sampler2D uMatcapMap;
uniform float uCavityStrength; // 0: 关闭，>0: 屏幕空间凹槽/凸边增强

out vec4 FragColor;

vec3 perturbNormal(vec3 N, vec3 p, vec2 uv) {
    vec3 mapN = texture(uNormalMap, uv).xyz * 2.0 - 1.0;
    vec3 dp1 = dFdx(p);
    vec3 dp2 = dFdy(p);
    vec2 duv1 = dFdx(uv);
    vec2 duv2 = dFdy(uv);
    vec3 dp2perp = cross(dp2, N);
    vec3 dp1perp = cross(N, dp1);
    vec3 T = dp2perp * duv1.x + dp1perp * duv2.x;
    vec3 B = dp2perp * duv1.y + dp1perp * duv2.y;
    float invmax = inversesqrt(max(dot(T, T), dot(B, B)) + 1e-8);
    return normalize(mat3(T * invmax, B * invmax, N) * mapN);
}

void main() {
    if (uRenderMode == 3) {
        FragColor = vec4(0.08, 0.09, 0.11, 0.65);
        return;
    }

    vec3 N = normalize(vNormal);

    // Face Orientation 诊断：正面柔蓝，法线翻转的背面刺眼红
    if (uCheckBackface == 1) {
        vec3 orientColor = gl_FrontFacing ? vec3(0.16, 0.46, 0.90) : vec3(0.92, 0.16, 0.16);
        vec3 viewDir = normalize(uViewPos - vFragPos);
        float shade = max(dot(gl_FrontFacing ? N : -N, viewDir), 0.25);
        FragColor = vec4(orientColor * shade, 1.0);
        return;
    }

    if (!gl_FrontFacing) N = -N;

    // 法线贴图在法线视图 / 白模里也生效，便于检查雕刻与机械细节
    if (uHasNormalMap == 1 && uUVChecker == 0) {
        N = perturbNormal(N, vFragPos, vUV);
    }

    // 模式 5：MatCap 材质捕捉（视空间法线采样，自动继承上面的法线贴图细节）
    if (uRenderMode == 5) {
        vec3 viewN = normalize(mat3(uView) * N);
        vec2 matcapUV = viewN.xy * 0.49 + 0.5;
        FragColor = vec4(texture(uMatcapMap, matcapUV).rgb, 1.0);
        return;
    }

    // 模式 2：法线方向可视化（RGB 映射）
    if (uRenderMode == 2) {
        vec3 normColor = N * 0.5 + 0.5;
        FragColor = vec4(normColor, 1.0);
        return;
    }

    // 计算表面基础反照率
    vec3 albedo = uBaseColor;
    if (uRenderMode == 0 || uRenderMode == 4) {
        if (uUVChecker == 1) {
            // UV 棋盘格：直接覆盖材质色，保留光照以观察拉伸/接缝
            albedo = pow(texture(uCheckerMap, vUV).rgb, vec3(2.2));
        } else {
            albedo *= vColor.rgb;
            if (uHasTexture == 1) {
                vec4 texSample = texture(uAlbedoMap, vUV);
                // 将 sRGB 贴图转至线性空间计算光照
                albedo *= pow(texSample.rgb, vec3(2.2));
            }
        }
    } else {
        // 雕塑白模统一使用中性暖灰
        albedo = vec3(0.56, 0.58, 0.61);
    }

    // 模式 4：反照率视图（Albedo，去除光影与高光的纯固有色）
    if (uRenderMode == 4) {
        FragColor = vec4(pow(max(albedo, vec3(0.0)), vec3(1.0 / 2.2)), 1.0);
        return;
    }

    // 屏幕空间曲率：凹槽压暗 + 凸边提亮，不依赖网格分辨率
    if (uCavityStrength > 0.001 && (uRenderMode == 0 || uRenderMode == 1)) {
        vec3 dNdx = dFdx(N);
        vec3 dNdy = dFdy(N);
        vec3 dPdx = dFdx(vFragPos);
        vec3 dPdy = dFdy(vFragPos);
        float curvature = dot(dNdx, dPdx) / max(dot(dPdx, dPdx), 1e-6)
                        + dot(dNdy, dPdy) / max(dot(dPdy, dPdy), 1e-6);
        float cavity = clamp(curvature * 0.032, -0.45, 0.45) * uCavityStrength;
        albedo *= (1.0 + cavity);
    }

    vec3 V = normalize(uViewPos - vFragPos);
    vec3 lighting;
    vec3 specular = vec3(0.0);

    if (uUseSceneLights == 1 && uNumLights > 0) {
        // FBX 场景灯光：微弱环境底光 + 逐灯漫反射/高光
        lighting = vec3(0.10, 0.105, 0.12);
        for (int i = 0; i < 8; ++i) {
            if (i >= uNumLights) break;
            vec3 L;
            float atten = 1.0;
            if (uLightType[i] == 1) {
                L = normalize(-uLightDir[i]);
            } else {
                vec3 toLight = uLightPos[i] - vFragPos;
                float dist = length(toLight);
                L = toLight / max(dist, 1e-4);
                // 预览用软衰减：归一化到 2.0 尺度后，灯光常在 5~10 单位外，纯平方衰减会全黑
                atten = 1.0 / (1.0 + 0.15 * dist + 0.05 * dist * dist);
                if (uLightType[i] == 3) {
                    float theta = dot(L, normalize(-uLightDir[i]));
                    float eps = max(uLightCone[i].x - uLightCone[i].y, 0.001);
                    atten *= clamp((theta - uLightCone[i].y) / eps, 0.0, 1.0);
                }
            }
            float diff = max(dot(N, L), 0.0);
            vec3 H = normalize(L + V);
            float spec = pow(max(dot(N, H), 0.0), 48.0) * 0.25;
            lighting += uLightColor[i] * diff * atten;
            specular += uLightColor[i] * spec * atten;
        }
    } else {
        // 相机空间相对三点影棚布光（确保任何角度观察均有立体明暗）
        vec3 forward = normalize(uTargetPos - uViewPos);
        vec3 worldUp = abs(forward.y) > 0.99 ? vec3(0.0, 0.0, 1.0) : vec3(0.0, 1.0, 0.0);
        vec3 right = normalize(cross(forward, worldUp));
        vec3 camUp = cross(right, forward);

        // Shift + 左键拖拽：三点布光绕 camUp 独立旋转，不移动相机
        float ly = cos(uLightYaw);
        float ls = sin(uLightYaw);
        vec3 rotForward = forward * ly + right * ls;
        vec3 rotRight   = -forward * ls + right * ly;

        vec3 keyDir  = normalize(-rotForward * 0.70 + rotRight * 0.55 + camUp * 0.75);
        vec3 fillDir = normalize(-rotForward * 0.45 - rotRight * 0.80 - camUp * 0.25);
        vec3 rimDir  = normalize( rotForward * 0.85 + camUp * 0.50);

        float keyDiff  = max(dot(N, keyDir), 0.0);
        float fillDiff = max(dot(N, fillDir), 0.0);
        float rimDiff  = max(dot(N, rimDir), 0.0);
        float fresnel  = pow(1.0 - max(dot(N, V), 0.0), 3.2);

        vec3 ambient   = vec3(0.16, 0.17, 0.19);
        vec3 keyLight  = vec3(1.00, 0.97, 0.93) * keyDiff * 0.85;
        vec3 fillLight = vec3(0.55, 0.66, 0.82) * fillDiff * 0.35;
        vec3 rimLight  = vec3(0.82, 0.87, 0.96) * (rimDiff * 0.28 + fresnel * 0.30);

        vec3 H = normalize(keyDir + V);
        float spec = pow(max(dot(N, H), 0.0), 48.0) * 0.18;

        lighting = ambient + keyLight + fillLight + rimLight;
        specular = vec3(spec);
    }

    vec3 color = albedo * lighting + specular;
    color = pow(max(color, vec3(0.0)), vec3(1.0 / 2.2));
    FragColor = vec4(color, 1.0);
}
"""

# ==============================================================================
# 2. Blender 风格无限地面网格着色器（Infinite Anti-Aliased Ground Grid）
# ==============================================================================
GRID_VS = """
#version 330 core
uniform mat4 uInvViewProj;

out vec3 vNearPoint;
out vec3 vFarPoint;

const vec2 QUAD_POS[6] = vec2[](
    vec2(-1.0, -1.0), vec2( 1.0, -1.0), vec2( 1.0,  1.0),
    vec2(-1.0, -1.0), vec2( 1.0,  1.0), vec2(-1.0,  1.0)
);

vec3 unprojectPoint(float x, float y, float z) {
    vec4 p = uInvViewProj * vec4(x, y, z, 1.0);
    return p.xyz / p.w;
}

void main() {
    vec2 pos = QUAD_POS[gl_VertexID];
    vNearPoint = unprojectPoint(pos.x, pos.y, -1.0);
    vFarPoint  = unprojectPoint(pos.x, pos.y,  1.0);
    gl_Position = vec4(pos, 0.0, 1.0);
}
"""

GRID_FS = """
#version 330 core
in vec3 vNearPoint;
in vec3 vFarPoint;

uniform mat4 uView;
uniform mat4 uProj;

out vec4 FragColor;

vec4 evalGrid(vec3 fragPos3D, float scale) {
    vec2 coord = fragPos3D.xz * scale;
    vec2 derivative = fwidth(coord);
    vec2 grid = abs(fract(coord - 0.5) - 0.5) / max(derivative, vec2(1e-5));
    float line = min(grid.x, grid.y);
    float minX = min(derivative.x, 1.0);
    float minZ = min(derivative.y, 1.0);

    vec4 color = vec4(0.28, 0.30, 0.34, 1.0 - min(line, 1.0));

    // Blender 标志性 Z 轴（蓝色）与 X 轴（红色）高亮
    if (abs(fragPos3D.x) < 0.012 / scale + 0.008 * minX) {
        color.rgb = vec3(0.26, 0.52, 0.92);
        color.a = max(color.a, 0.85);
    }
    if (abs(fragPos3D.z) < 0.012 / scale + 0.008 * minZ) {
        color.rgb = vec3(0.88, 0.30, 0.30);
        color.a = max(color.a, 0.85);
    }
    return color;
}

void main() {
    float denom = vFarPoint.y - vNearPoint.y;
    if (abs(denom) < 1e-6) discard;

    float t = -vNearPoint.y / denom;
    if (t <= 0.0) discard;

    vec3 fragPos3D = vNearPoint + t * (vFarPoint - vNearPoint);

    // 写入精确深度，确保网格与 3D 模型正确相互遮挡
    vec4 clipPos = uProj * uView * vec4(fragPos3D, 1.0);
    float ndcDepth = clipPos.z / clipPos.w;
    gl_FragDepth = (ndcDepth + 1.0) * 0.5;

    // 亚网格（0.2m）+ 主网格（1.0m）双层级叠加
    vec4 subGrid  = evalGrid(fragPos3D, 5.0) * vec4(1.0, 1.0, 1.0, 0.35);
    vec4 mainGrid = evalGrid(fragPos3D, 1.0);
    vec4 gridColor = mix(subGrid, mainGrid, mainGrid.a);

    // 距离平滑渐隐
    float dist = length(fragPos3D.xz);
    float fade = clamp(1.0 - dist / 18.0, 0.0, 1.0);
    fade = fade * fade;

    gridColor.a *= fade * 0.85;
    if (gridColor.a < 0.01) discard;
    FragColor = gridColor;
}
"""

# ==============================================================================
# 3. 法线向量线段着色器（Normal Vectors Overlay）
# ==============================================================================
NORMAL_LINE_VS = """
#version 330 core
layout (location = 0) in vec3 aPos;
uniform mat4 uModel;
uniform mat4 uView;
uniform mat4 uProj;
void main() {
    gl_Position = uProj * uView * uModel * vec4(aPos, 1.0);
}
"""

NORMAL_LINE_FS = """
#version 330 core
out vec4 FragColor;
void main() {
    FragColor = vec4(0.18, 0.88, 0.82, 0.85);
}
"""


# ==============================================================================
# 3b. 灯光 Gizmo 着色器（Blender 风格的灯光十字星 / 照射方向射线）
# ==============================================================================
GIZMO_VS = """
#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aColor;
uniform mat4 uModel;
uniform mat4 uView;
uniform mat4 uProj;
out vec3 vColor;
void main() {
    vColor = aColor;
    gl_Position = uProj * uView * uModel * vec4(aPos, 1.0);
}
"""

GIZMO_FS = """
#version 330 core
in vec3 vColor;
out vec4 FragColor;
void main() {
    FragColor = vec4(vColor, 1.0);
}
"""


# ==============================================================================
# 矩阵工具函数
# ==============================================================================
def mat4_perspective(fovy_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / np.tan(np.radians(fovy_deg) * 0.5)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / max(aspect, 1e-5)
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2.0 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def mat4_ortho(half_w: float, half_h: float, near: float, far: float) -> np.ndarray:
    """正交投影矩阵（与 mat4_perspective 相同的行主序存储约定）"""
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = 1.0 / max(half_w, 1e-5)
    m[1, 1] = 1.0 / max(half_h, 1e-5)
    m[2, 2] = -2.0 / max(far - near, 1e-5)
    m[2, 3] = -(far + near) / max(far - near, 1e-5)
    m[3, 3] = 1.0
    return m


def mat4_look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    f = target - eye
    f = f / max(np.linalg.norm(f), 1e-6)
    s = np.cross(f, up)
    s = s / max(np.linalg.norm(s), 1e-6)
    u = np.cross(s, f)

    m = np.eye(4, dtype=np.float32)
    m[0, 0:3] = s
    m[1, 0:3] = u
    m[2, 0:3] = -f
    m[0, 3] = -np.dot(s, eye)
    m[1, 3] = -np.dot(u, eye)
    m[2, 3] = np.dot(f, eye)
    return m


# ==============================================================================
# 4. FBX 二进制灯光解析
#    assimp_py 1.2 的 Scene 未绑定 lights，这里直接读二进制 FBX 的
#    Objects/Connections，取 Light NodeAttribute 参数并按 Model 名对回 assimp 节点。
#    解析失败（ASCII FBX / 非标准头）时静默返回 []，不影响模型加载。
# ==============================================================================
_FBX_WANT_PROPS = {"Model", "NodeAttribute", "P", "C"}
_FBX_SKIP_CHILDREN = {
    "Geometry", "Deformer", "Material", "Texture", "Video", "Pose", "Cluster",
    "AnimationCurve", "AnimationCurveNode", "AnimationLayer", "AnimCurve",
}


def _fbx_str(value) -> str:
    if isinstance(value, bytes):
        return value.split(b"\x00\x01")[0].decode("utf-8", "replace")
    return str(value).split("\x00\x01")[0]


def _fbx_num(values, default):
    try:
        if values is None:
            return default
        return float(values[0]) if len(values) else default
    except (TypeError, ValueError, IndexError):
        return default


def _fbx_read_prop(data: bytes, pos: int):
    t = data[pos:pos + 1]
    if t == b"Y":
        return struct.unpack_from("<h", data, pos + 1)[0], pos + 3
    if t == b"C":
        return bool(data[pos + 1]), pos + 2
    if t == b"I":
        return struct.unpack_from("<i", data, pos + 1)[0], pos + 5
    if t == b"F":
        return struct.unpack_from("<f", data, pos + 1)[0], pos + 5
    if t == b"D":
        return struct.unpack_from("<d", data, pos + 1)[0], pos + 9
    if t == b"L":
        return struct.unpack_from("<q", data, pos + 1)[0], pos + 9
    if t in (b"S", b"R"):
        n = struct.unpack_from("<I", data, pos + 1)[0]
        return data[pos + 5:pos + 5 + n], pos + 5 + n
    if t in (b"f", b"d", b"l", b"i", b"b"):
        n, enc, clen = struct.unpack_from("<III", data, pos + 1)
        start = pos + 13
        raw = data[start:start + clen]
        if enc == 1:
            raw = zlib.decompress(raw)
        code = {b"f": "f", b"d": "d", b"l": "q", b"i": "i", b"b": "B"}[t]
        size = struct.calcsize(code) * n
        return struct.unpack("<" + code * n, raw[:size]), start + clen
    raise ValueError(f"unknown FBX property type {t!r}")


def _fbx_record_header(data: bytes, pos: int, version: int):
    """解析 FBX 记录头：7.4 为 32 位偏移，7.5 为 64 位；名长兼容 1/4 字节两种写法"""
    if version < 7500:
        end, nprop, plen = struct.unpack_from("<III", data, pos)
        name_len_off = pos + 12
    else:
        end, nprop, plen = struct.unpack_from("<QQQ", data, pos)
        name_len_off = pos + 24

    if end == 0:
        return 0, 0, 0, name_len_off + 1, 0

    nlen = data[name_len_off] if name_len_off < len(data) else 0
    name_off = name_len_off + 1
    # 7.5 少数写入器把 NameLen 写成 4 字节；4 字节变体后紧跟 3 个 0 字节
    if version >= 7500 and data[name_len_off + 1:name_len_off + 4] == b"\x00\x00\x00":
        nlen = struct.unpack_from("<I", data, name_len_off)[0]
        name_off = name_len_off + 4
    return end, nprop, plen, name_off, nlen


def _fbx_read_children(data: bytes, pos: int, end: int, version: int, depth: int = 0,
                       want_props=None, skip_children=None) -> list:
    want = _FBX_WANT_PROPS if want_props is None else want_props
    skip = _FBX_SKIP_CHILDREN if skip_children is None else skip_children
    items = []
    while pos + 12 <= end and pos + 12 <= len(data):
        try:
            end_r, nprop, plen, name_off, nlen = _fbx_record_header(data, pos, version)
        except struct.error:
            break
        if end_r == 0 or end_r <= pos or end_r > len(data):
            break
        name = _fbx_str(data[name_off:name_off + nlen])
        props = []
        if name in want and nprop:
            p = name_off + nlen
            for _ in range(nprop):
                try:
                    v, p = _fbx_read_prop(data, p)
                except Exception:
                    break
                props.append(v)
        children = []
        if name not in skip and depth < 6:
            children = _fbx_read_children(data, name_off + nlen + plen, end_r, version, depth + 1,
                                          want, skip)
        items.append({"name": name, "props": props, "children": children})
        pos = end_r
    return items


def parse_fbx_embedded_texture_records(filepath: str) -> list:
    """读取二进制 FBX 的 Objects -> Video -> Content 内嵌贴图，返回 [{name, image}]"""
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        if not data.startswith(b"Kaydara FBX Binary"):
            return []
        version = struct.unpack_from("<I", data, 23)[0]
        pos = 27
        objects = []
        while pos + 12 <= len(data):
            end, nprop, plen, name_off, nlen = _fbx_record_header(data, pos, version)
            if end == 0 or end <= pos or end > len(data):
                break
            name = _fbx_str(data[name_off:name_off + nlen])
            if name == "Objects":
                objects = _fbx_read_children(
                    data, name_off + nlen + plen, end, version,
                    want_props={"Video", "Content", "RelativeFilename", "Filename"},
                    skip_children={"Geometry", "Deformer", "Material", "Texture", "Pose",
                                   "Cluster", "AnimationCurve", "AnimationCurveNode",
                                   "AnimationLayer", "AnimCurve"},
                )
                break
            pos = end

        records = []
        for rec in objects:
            if rec["name"] != "Video":
                continue
            content = None
            rel_name = ""
            for child in rec["children"]:
                if child["name"] == "Content" and child["props"]:
                    raw = child["props"][0]
                    if isinstance(raw, (bytes, bytearray)) and len(raw) > 0:
                        content = bytes(raw)
                elif child["name"] == "RelativeFilename" and child["props"]:
                    rel_name = _fbx_str(child["props"][0])
                elif child["name"] == "Filename" and child["props"] and not rel_name:
                    rel_name = _fbx_str(child["props"][0])
            if not content:
                continue
            # 先只保留压缩字节，真正用到某张图时再解码：
            # 186MB 资产可能带 4 张 4096×4096 贴图，全部预解码会多占约 240MB
            records.append({"name": rel_name, "data": content, "image": None})
        return records
    except Exception:
        return []


def decode_fbx_embedded_record(rec) -> Image.Image | None:
    """按需解码单条内嵌贴图记录；解码后释放压缩字节"""
    if rec is None:
        return None
    img = rec.get("image")
    if img is not None:
        return img
    raw = rec.get("data")
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        img = None
    rec["image"] = img
    rec["data"] = None
    return img


def parse_fbx_embedded_textures(filepath: str) -> list:
    """按 Assimp *0, *1... 顺序提取内嵌贴图；解码失败的位置保留 None"""
    return [decode_fbx_embedded_record(rec)
            for rec in parse_fbx_embedded_texture_records(filepath)]


def parse_fbx_lights(filepath: str) -> list:
    """返回 [{'name','type','color','inner','outer'}]；type 1=平行光 2=点光 3=聚光。"""
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        if not data.startswith(b"Kaydara FBX Binary"):
            return []
        version = struct.unpack_from("<I", data, 23)[0]
        objects, connections = [], []
        pos = 27
        while pos + 12 <= len(data):
            end, nprop, plen, name_off, nlen = _fbx_record_header(data, pos, version)
            if end == 0 or end <= pos or end > len(data):
                break
            name = _fbx_str(data[name_off:name_off + nlen])
            props_off = name_off + nlen + plen
            if name == "Objects":
                objects = _fbx_read_children(data, props_off, end, version)
            elif name == "Connections":
                connections = _fbx_read_children(data, props_off, end, version)
            pos = end

        models = {}
        attrs = []
        for rec in objects:
            if rec["name"] == "Model" and len(rec["props"]) >= 2:
                models[rec["props"][0]] = _fbx_str(rec["props"][1])
            elif rec["name"] == "NodeAttribute" and len(rec["props"]) >= 3:
                if _fbx_str(rec["props"][2]) == "Light":
                    props = {}
                    for child in rec["children"]:
                        if child["name"] == "Properties70":
                            for pr in child["children"]:
                                if pr["name"] == "P" and len(pr["props"]) >= 5:
                                    props[_fbx_str(pr["props"][0])] = pr["props"][4:]
                    attrs.append((rec["props"][0], props))

        links = {}
        for rec in connections:
            if rec["name"] == "C" and len(rec["props"]) >= 3 and _fbx_str(rec["props"][0]) == "OO":
                links[rec["props"][1]] = rec["props"][2]

        out = []
        for attr_id, props in attrs:
            node_name = models.get(links.get(attr_id))
            if not node_name:
                continue
            if "CastLight" in props and _fbx_num(props["CastLight"], 1.0) <= 0.5:
                continue
            ltype = int(_fbx_num(props.get("LightType"), 0.0))
            if ltype not in (0, 1, 2):      # Area / Volume 不支持
                continue
            color = np.array([1.0, 1.0, 1.0], dtype=np.float32)
            if "Color" in props and len(props["Color"]) >= 3:
                color = np.array([float(props["Color"][0]), float(props["Color"][1]),
                                  float(props["Color"][2])], dtype=np.float32)
            if float(np.max(color)) > 1.5:   # 少数管线把颜色写成 0-255
                color = color / 255.0
            intensity = max(_fbx_num(props.get("Intensity"), 100.0), 0.0)
            inner_deg = _fbx_num(props.get("InnerAngle"), 0.0)
            outer_deg = _fbx_num(props.get("OuterAngle"), 45.0)
            if outer_deg <= inner_deg:
                outer_deg = inner_deg + 1.0
            out.append({
                "name": node_name,
                "type": 2 if ltype == 0 else (1 if ltype == 1 else 3),
                "color": np.clip(color, 0.0, 1.0) * (intensity / 100.0),
                "inner": float(np.cos(np.radians(inner_deg))),
                "outer": float(np.cos(np.radians(outer_deg))),
            })

        # 统一压暗：Maya/Blender 常写成物理瓦数（如 100000），按场景峰值等比缩到 1.0 以内
        peak = max((float(np.max(l["color"])) for l in out), default=0.0)
        if peak > 1.0:
            for l in out:
                l["color"] = (l["color"] / peak).astype(np.float32)

        return out
    except Exception:
        return []


# ==============================================================================
# 5. FBX 场景控制器：节点层级求值 / 骨骼蒙皮 / 动画采样 / 灯光同步
# ==============================================================================
def _quat_slerp(q1: np.ndarray, q2: np.ndarray, t: float) -> np.ndarray:
    """Assimp 四元数格式为 (w, x, y, z) 的球面线性插值"""
    q1 = np.asarray(q1, dtype=np.float64)
    q2 = np.asarray(q2, dtype=np.float64)
    dot = float(np.dot(q1, q2))
    if dot < 0.0:
        q2 = -q2
        dot = -dot
    if dot > 0.9995:
        res = q1 + t * (q2 - q1)
        return (res / max(np.linalg.norm(res), 1e-6)).astype(np.float32)
    theta_0 = np.arccos(np.clip(dot, -1.0, 1.0))
    sin_0 = np.sin(theta_0)
    if abs(sin_0) < 1e-6:
        return q1.astype(np.float32)
    s1 = np.sin((1.0 - t) * theta_0) / sin_0
    s2 = np.sin(t * theta_0) / sin_0
    return (s1 * q1 + s2 * q2).astype(np.float32)


def _trs_to_mat4(pos: np.ndarray, rot_wxyz: np.ndarray, scale: np.ndarray) -> np.ndarray:
    w, x, y, z = (float(v) for v in rot_wxyz)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z

    m = np.eye(4, dtype=np.float32)
    m[0, 0] = (1.0 - 2.0 * (yy + zz)) * scale[0]
    m[0, 1] = (2.0 * (xy - wz)) * scale[1]
    m[0, 2] = (2.0 * (xz + wy)) * scale[2]
    m[1, 0] = (2.0 * (xy + wz)) * scale[0]
    m[1, 1] = (1.0 - 2.0 * (xx + zz)) * scale[1]
    m[1, 2] = (2.0 * (yz - wx)) * scale[2]
    m[2, 0] = (2.0 * (xz - wy)) * scale[0]
    m[2, 1] = (2.0 * (yz + wx)) * scale[1]
    m[2, 2] = (1.0 - 2.0 * (xx + yy)) * scale[2]
    m[0, 3], m[1, 3], m[2, 3] = pos
    return m


def skin_vertex_positions(verts: np.ndarray, bone_ids: np.ndarray, bone_weights: np.ndarray,
                          bone_mats: np.ndarray) -> np.ndarray:
    """CPU 端按 top4 权重蒙皮，返回世界坐标（仅用于加载期包围盒估计）。"""
    n = len(verts)
    h = np.concatenate([verts.astype(np.float32), np.ones((n, 1), dtype=np.float32)], axis=1)
    out = np.zeros((n, 4), dtype=np.float32)
    for k in range(4):
        ids = bone_ids[:, k]
        w = bone_weights[:, k]
        valid = w > 0.0
        if not valid.any():
            continue
        m = bone_mats[ids[valid]]
        out[valid] += np.einsum("mij,mj->mi", m, h[valid], optimize=True) * w[valid][:, None]
    return out[:, :3]


class FBXSceneController:
    """管理 FBX 节点层级、动画采样、骨骼矩阵与场景灯光实时求值"""

    MAX_BONES = 100

    def __init__(self, scene, raw_lights=None):
        self.root_node = getattr(scene, "root_node", None)
        self.global_inv = np.eye(4, dtype=np.float32)
        if self.root_node is not None:
            root_t = self._node_matrix(self.root_node)
            try:
                self.global_inv = np.linalg.inv(root_t).astype(np.float32)
            except np.linalg.LinAlgError:
                pass

        self.mesh_node_names = {}   # mesh_idx -> node name
        self.mesh_node_world = {}   # mesh_idx -> rest pose world matrix
        self.mesh_bones = {}        # mesh_idx -> [(bone_node_name, offset_matrix)]
        self.nodes = {}
        self._register_nodes(self.root_node, np.eye(4, dtype=np.float32))

        self.animations = self._parse_animations(scene)
        self.raw_lights = list(raw_lights or [])
        self.norm_matrix = np.eye(4, dtype=np.float32)
        self.last_worlds = {}

    # ---------- 序列化（后台子进程 -> 主进程） ----------
    @classmethod
    def _serialize_node(cls, node):
        if node is None:
            return None
        return {
            "name": getattr(node, "name", ""),
            "transformation": cls._node_matrix(node),
            "mesh_indices": tuple(getattr(node, "mesh_indices", ()) or ()),
            "children": [cls._serialize_node(c) for c in (getattr(node, "children", ()) or ())],
        }

    @classmethod
    def _deserialize_node(cls, data):
        if data is None:
            return None
        node = SimpleNamespace()
        node.name = data.get("name", "")
        node.transformation = np.asarray(data.get("transformation", np.eye(4)), dtype=np.float32)
        node.mesh_indices = tuple(data.get("mesh_indices", ()))
        node.children = [cls._deserialize_node(c) for c in data.get("children", ())]
        return node

    def __getstate__(self):
        return {
            "global_inv": self.global_inv,
            "mesh_node_names": self.mesh_node_names,
            "mesh_node_world": self.mesh_node_world,
            "mesh_bones": self.mesh_bones,
            "animations": self.animations,
            "raw_lights": self.raw_lights,
            "norm_matrix": self.norm_matrix,
            "root_data": self._serialize_node(self.root_node),
        }

    def __setstate__(self, state):
        self.root_node = self._deserialize_node(state.get("root_data"))
        self.global_inv = np.asarray(state.get("global_inv", np.eye(4)), dtype=np.float32)
        self.mesh_node_names = state.get("mesh_node_names", {})
        self.mesh_node_world = state.get("mesh_node_world", {})
        self.mesh_bones = state.get("mesh_bones", {})
        self.animations = state.get("animations", [])
        self.raw_lights = state.get("raw_lights", [])
        self.norm_matrix = np.asarray(state.get("norm_matrix", np.eye(4)), dtype=np.float32)
        self.nodes = {}
        self.last_worlds = {}

    # ---------- 数据准备 ----------
    @staticmethod
    def _node_matrix(node) -> np.ndarray:
        t = getattr(node, "transformation", None)
        if t is None:
            return np.eye(4, dtype=np.float32)
        try:
            arr = np.asarray(t, dtype=np.float32)
            if arr.shape != (4, 4):
                return np.eye(4, dtype=np.float32)
            return arr.copy()
        except Exception:
            return np.eye(4, dtype=np.float32)

    def _register_nodes(self, node, parent_world: np.ndarray, depth: int = 0):
        if node is None or depth > 32:
            return
        name = getattr(node, "name", "")
        self.nodes.setdefault(name, node)
        world = parent_world @ self._node_matrix(node)
        try:
            mesh_indices = tuple(getattr(node, "mesh_indices", ()) or ())
        except TypeError:
            mesh_indices = ()
        for mid in mesh_indices:
            mid = int(mid)
            if mid not in self.mesh_node_names:
                self.mesh_node_names[mid] = name
                self.mesh_node_world[mid] = world
        for child in getattr(node, "children", ()) or ():
            self._register_nodes(child, world, depth + 1)

    @staticmethod
    def _sorted_keys(times, values, comps: int):
        if times is None or values is None:
            return None
        t = np.asarray(times, dtype=np.float64).reshape(-1)
        v = np.asarray(values, dtype=np.float32).reshape(-1, comps)
        n = min(len(t), len(v))
        if n < 2:
            return None
        order = np.argsort(t[:n])
        return t[:n][order], v[:n][order]

    def _parse_animations(self, scene) -> list:
        animations = []
        for idx, anim in enumerate(getattr(scene, "animations", None) or []):
            channels = {}
            for ch in getattr(anim, "channels", None) or []:
                name = getattr(ch, "node_name", "")
                rec = {}
                for key, tname, vname, comps in (
                    ("p", "position_key_times", "position_key_values", 3),
                    ("r", "rotation_key_times", "rotation_key_values", 4),
                    ("s", "scaling_key_times", "scaling_key_values", 3),
                ):
                    keys = self._sorted_keys(getattr(ch, tname, None), getattr(ch, vname, None), comps)
                    if keys is not None and key == "r":
                        # assimp 的 aiQuaternion 内存顺序为 (x, y, z, w)，统一转成 (w, x, y, z)
                        t, v = keys
                        keys = (t, np.ascontiguousarray(v[:, [3, 0, 1, 2]]))
                    if keys is not None:
                        rec[key] = keys
                if rec:
                    channels[name] = rec
            duration = float(getattr(anim, "duration", 0.0) or 0.0)
            tps = float(getattr(anim, "ticks_per_second", 0.0) or 24.0)
            if tps <= 0:
                tps = 24.0
            animations.append({
                "name": getattr(anim, "name", "") or f"Take_{idx + 1}",
                "duration": duration,
                "tps": tps,
                "channels": channels,
            })
        return animations

    # ---------- 采样 ----------
    @staticmethod
    def _sample_vec(keys, tick: float, default: np.ndarray) -> np.ndarray:
        if keys is None:
            return default
        t, v = keys
        if tick <= t[0]:
            return v[0]
        if tick >= t[-1]:
            return v[-1]
        i = int(np.searchsorted(t, tick) - 1)
        i = max(0, min(i, len(t) - 2))
        span = max(t[i + 1] - t[i], 1e-6)
        alpha = (tick - t[i]) / span
        return (v[i] * (1.0 - alpha) + v[i + 1] * alpha).astype(np.float32)

    @staticmethod
    def _sample_quat(keys, tick: float) -> np.ndarray:
        default = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        if keys is None:
            return default
        t, v = keys
        if tick <= t[0]:
            return v[0]
        if tick >= t[-1]:
            return v[-1]
        i = int(np.searchsorted(t, tick) - 1)
        i = max(0, min(i, len(t) - 2))
        span = max(t[i + 1] - t[i], 1e-6)
        alpha = (tick - t[i]) / span
        return _quat_slerp(v[i], v[i + 1], alpha)

    def duration(self, anim_idx: int) -> float:
        if 0 <= anim_idx < len(self.animations):
            anim = self.animations[anim_idx]
            if anim["tps"] > 0 and anim["duration"] > 0:
                return anim["duration"] / anim["tps"]
        return 0.0

    # ---------- 每帧求值 ----------
    def _world_matrices(self, anim_idx: int, time_sec: float):
        channels = {}
        tick = 0.0
        if 0 <= anim_idx < len(self.animations):
            anim = self.animations[anim_idx]
            if anim["duration"] > 0:
                tick = (time_sec * anim["tps"]) % anim["duration"]
            channels = anim["channels"]

        worlds = {}

        def traverse(node, parent_mat: np.ndarray):
            if node is None:
                return
            ch = channels.get(getattr(node, "name", ""))
            if ch is not None:
                pos = self._sample_vec(ch.get("p"), tick, np.zeros(3, dtype=np.float32))
                rot = self._sample_quat(ch.get("r"), tick)
                scl = self._sample_vec(ch.get("s"), tick, np.ones(3, dtype=np.float32))
                local = _trs_to_mat4(pos, rot, scl)
            else:
                local = self._node_matrix(node)
            world = parent_mat @ local
            worlds[getattr(node, "name", "")] = world
            for child in getattr(node, "children", ()) or ():
                traverse(child, world)

        traverse(self.root_node, np.eye(4, dtype=np.float32))
        return worlds, tick

    def evaluate(self, anim_idx: int, time_sec: float):
        """返回 ({mesh_idx: (uNodeTransform, bone_matrices|None)}, [lights...])"""
        worlds, _ = self._world_matrices(anim_idx, time_sec)
        self.last_worlds = worlds

        mesh_states = {}
        for mid, node_name in self.mesh_node_names.items():
            node_world = worlds.get(node_name, self.mesh_node_world.get(mid, np.eye(4, dtype=np.float32)))
            bones = self.mesh_bones.get(mid)
            if bones:
                mats = []
                for bone_name, offset in bones[:self.MAX_BONES]:
                    bone_world = worlds.get(bone_name, np.eye(4, dtype=np.float32))
                    mats.append(self.global_inv @ bone_world @ offset)
                bone_array = (np.ascontiguousarray(np.stack(mats), dtype=np.float32)
                              if mats else None)
                mesh_states[mid] = (self.norm_matrix.copy(), bone_array)
            else:
                mesh_states[mid] = (self.norm_matrix @ node_world, None)

        lights = []
        rot_norm = self.norm_matrix[:3, :3]
        for lt in self.raw_lights:
            world = worlds.get(lt["name"], np.eye(4, dtype=np.float32))
            pos_h = self.norm_matrix @ (world @ np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32))
            direction = rot_norm @ (world[:3, :3] @ np.array([0.0, -1.0, 0.0], dtype=np.float32))
            norm_d = float(np.linalg.norm(direction))
            direction = direction / norm_d if norm_d > 1e-6 else np.array([0.0, -1.0, 0.0], dtype=np.float32)
            lights.append({
                "name": lt["name"],
                "type": lt["type"],
                "pos": pos_h[:3].astype(np.float32),
                "dir": direction.astype(np.float32),
                "color": lt["color"],
                "inner": lt["inner"],
                "outer": lt["outer"],
            })
        return mesh_states, lights

    def get_skeleton_segments(self, worlds=None) -> np.ndarray:
        """归一化空间下的骨骼父子连线 (N*2, 3)，用于 X-Ray 透视叠加"""
        bone_names = {bname for blist in self.mesh_bones.values() for bname, _ in blist}
        if not bone_names:
            return np.zeros((0, 3), dtype=np.float32)
        if worlds is None:
            worlds = self.last_worlds

        pts = []

        def walk(node, parent_pos=None):
            if node is None:
                return
            name = getattr(node, "name", "")
            cur_pos = None
            world = worlds.get(name)
            if world is not None and name in bone_names:
                p = self.norm_matrix @ (world @ np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32))
                cur_pos = p[:3].astype(np.float32)
                if parent_pos is not None:
                    pts.append(parent_pos)
                    pts.append(cur_pos)
            for child in getattr(node, "children", ()) or ():
                walk(child, cur_pos if cur_pos is not None else parent_pos)

        walk(self.root_node, None)
        return np.asarray(pts, dtype=np.float32) if pts else np.zeros((0, 3), dtype=np.float32)


# ==============================================================================
# OpenGL 渲染画布组件
# ==============================================================================
# ==============================================================================
# 7. 极简动画时间轴（拖拽 Scrubbing）
# ==============================================================================
class AnimTimelineBar(QWidget):
    """悬浮在 GL 视口底部的半透明时间轴；仅当模型含动画时显示"""

    def __init__(self, gl_widget):
        super().__init__(gl_widget)
        self.gl = gl_widget
        self.setFixedHeight(34)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.hide()

    def _duration(self) -> float:
        ctrl = self.gl.fbx_controller
        if ctrl is None or not ctrl.animations:
            return 0.0
        return ctrl.duration(self.gl.anim_idx)

    def _seek_from_x(self, x: float):
        dur = self._duration()
        if dur <= 0:
            return
        pad = 16.0
        ratio = float(np.clip((x - pad) / max(self.width() - pad * 2.0, 1.0), 0.0, 1.0))
        self.gl.anim_playing = False
        self.gl.anim_time = ratio * dur
        self.gl.update()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._seek_from_x(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._seek_from_x(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def paintEvent(self, event):
        dur = self._duration()
        if dur <= 0:
            return
        cur = float(self.gl.anim_time % dur)
        ratio = cur / dur

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(20, 22, 26, 205))
        p.setPen(Qt.PenStyle.NoPen)

        pad = 16.0
        bar_y = 11.0
        bar_w = max(self.width() - pad * 2.0, 10.0)
        p.setBrush(QColor(65, 70, 78))
        p.drawRoundedRect(int(pad), int(bar_y), int(bar_w), 4, 2, 2)
        p.setBrush(QColor(76, 154, 255))
        p.drawRoundedRect(int(pad), int(bar_y), int(bar_w * ratio), 4, 2, 2)

        knob_x = int(pad + bar_w * ratio)
        p.setBrush(QColor(240, 244, 250))
        p.drawEllipse(knob_x - 5, int(bar_y) - 3, 10, 10)

        ctrl = self.gl.fbx_controller
        take = ctrl.animations[self.gl.anim_idx]["name"] if ctrl and ctrl.animations else ""
        p.setPen(QColor(190, 196, 206))
        p.drawText(int(pad), 29, f"{cur:.2f}s / {dur:.2f}s   [, / .] 逐帧步进   {take}")


# ==============================================================================
# 8. 右上角交互式 3D 坐标轴控件
#    跟随相机旋转、点击切换六向视图、拖拽旋转视角
# ==============================================================================
class ViewportAxisGizmo(QWidget):
    viewSelected = pyqtSignal(float, float)  # (yaw, pitch)

    def __init__(self, gl_widget, parent=None):
        super().__init__(parent or gl_widget)
        self.gl = gl_widget
        self.setFixedSize(96, 96)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._hovered_axis = None
        self._hovered_bg = False
        self._dragging = False
        self._press_pos = QPoint()
        self._last_pos = QPoint()
        self._axis_hit_targets = []  # [(screen_pt, radius, key)]

        # 6 个轴向定义：(key, 标签, 3D 方向, 颜色, 目标 yaw, 目标 pitch)
        self._axes = [
            ("+X", "X", np.array([1.0, 0.0, 0.0]), QColor(235, 77, 92), 90.0, 0.0),
            ("-X", "", np.array([-1.0, 0.0, 0.0]), QColor(145, 55, 65), -90.0, 0.0),
            ("+Y", "Y", np.array([0.0, 1.0, 0.0]), QColor(112, 196, 68), 0.0, 89.0),
            ("-Y", "", np.array([0.0, -1.0, 0.0]), QColor(72, 125, 48), 0.0, -89.0),
            ("+Z", "Z", np.array([0.0, 0.0, 1.0]), QColor(68, 146, 245), 0.0, 0.0),
            ("-Z", "", np.array([0.0, 0.0, -1.0]), QColor(48, 95, 155), 180.0, 0.0),
        ]

    def _hit_test(self, pos: QPointF):
        for center, radius, key in reversed(self._axis_hit_targets):
            if np.hypot(pos.x() - center.x(), pos.y() - center.y()) <= radius + 3.0:
                return key
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.gl._cam_animating = False
            self._press_pos = event.position().toPoint()
            self._last_pos = self._press_pos
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position()
        if event.buttons() & Qt.MouseButton.LeftButton:
            cur = pos.toPoint()
            if (cur - self._press_pos).manhattanLength() > 4:
                self._dragging = True
            if self._dragging:
                dx = cur.x() - self._last_pos.x()
                dy = cur.y() - self._last_pos.y()
                self._last_pos = cur
                self.gl.yaw -= dx * 0.55
                self.gl.pitch = float(np.clip(self.gl.pitch + dy * 0.55, -89.0, 89.0))
                self.gl.update()
                self.update()
            event.accept()
            return

        cx, cy = self.width() * 0.5, self.height() * 0.5
        self._hovered_bg = np.hypot(pos.x() - cx, pos.y() - cy) <= 42.0
        self._hovered_axis = self._hit_test(pos)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if self._hovered_axis
            else Qt.CursorShape.ArrowCursor
        )
        self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._dragging:
                hit = self._hit_test(event.position())
                for key, _, _, _, yaw, pitch in self._axes:
                    if key == hit:
                        self.viewSelected.emit(yaw, pitch)
                        break
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self._hovered_bg = False
        self._hovered_axis = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() * 0.5, self.height() * 0.5

        # 圆形半透明底盘
        bg_alpha = 185 if (self._hovered_bg or self._hovered_axis) else 125
        p.setPen(QPen(QColor(255, 255, 255, 24), 1))
        p.setBrush(QColor(28, 30, 35, bg_alpha))
        p.drawEllipse(QPointF(cx, cy), 40.0, 40.0)

        # 复用相机 View 矩阵的旋转分量，确保与画面网格同步
        _, view, _ = self.gl._get_camera_matrices()
        rot = view[:3, :3]
        arm_len = 27.0

        projected = []
        for key, label, vec, color, _, _ in self._axes:
            v_cam = rot @ vec
            sx = cx + float(v_cam[0]) * arm_len
            sy = cy - float(v_cam[1]) * arm_len
            depth = float(v_cam[2])  # 越大离屏幕越近
            projected.append((depth, key, label, color, QPointF(sx, sy)))

        # 按深度从远到近绘制
        projected.sort(key=lambda item: item[0])
        self._axis_hit_targets.clear()

        font = QFont("Helvetica Neue", 8, QFont.Weight.Bold)
        p.setFont(font)

        for depth, key, label, color, pt in projected:
            is_pos = key.startswith("+")
            is_hover = key == self._hovered_axis
            radius = 8.5 if is_pos else 5.2
            if is_hover:
                radius += 1.5

            if is_pos:
                line_col = QColor(color)
                line_col.setAlpha(220 if depth >= -0.2 else 130)
                p.setPen(QPen(line_col, 2.0, Qt.PenStyle.SolidLine,
                             Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(cx, cy), pt)

            fill = QColor(color)
            if not is_pos:
                fill.setAlpha(190 if depth >= 0 else 110)
            p.setPen(QPen(QColor(255, 255, 255, 220 if is_hover else 60),
                          1.4 if is_hover else 1.0))
            p.setBrush(fill)
            p.drawEllipse(pt, radius, radius)

            if label:
                p.setPen(QColor(18, 20, 24) if not is_hover else QColor(255, 255, 255))
                p.drawText(
                    QRectF(pt.x() - radius, pt.y() - radius, radius * 2, radius * 2),
                    Qt.AlignmentFlag.AlignCenter,
                    label,
                )

            self._axis_hit_targets.append((pt, radius, key))


# ==============================================================================
# 9. 底部材质球预览栏（白模 / 线框加白模 / 纹理 / 反照率 / 法线）
# ==============================================================================
class MaterialSphereButton(QPushButton):
    """程序化自绘 3D 质感预览球按钮"""

    def __init__(self, mode_id: str, title: str, parent=None):
        super().__init__(parent)
        self.mode_id = mode_id
        self.setToolTip(title)
        self.setCheckable(True)
        self.setFixedSize(36, 36)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.isChecked():
            p.setPen(QPen(QColor(255, 255, 255, 70), 1.2))
            p.setBrush(QColor(255, 255, 255, 34))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 18))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)

        cx, cy, r = self.width() * 0.5, self.height() * 0.5, 11.0
        sphere_rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        if self.mode_id == "clay":
            grad = QRadialGradient(cx - r * 0.35, cy - r * 0.35, r * 1.45)
            grad.setColorAt(0.0, QColor(238, 240, 244))
            grad.setColorAt(0.55, QColor(156, 160, 168))
            grad.setColorAt(1.0, QColor(62, 65, 72))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawEllipse(sphere_rect)

        elif self.mode_id == "clay_wire":
            grad = QRadialGradient(cx - r * 0.35, cy - r * 0.35, r * 1.45)
            grad.setColorAt(0.0, QColor(218, 222, 228))
            grad.setColorAt(0.65, QColor(135, 140, 148))
            grad.setColorAt(1.0, QColor(56, 60, 68))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawEllipse(sphere_rect)

            p.save()
            clip = QPainterPath()
            clip.addEllipse(sphere_rect)
            p.setClipPath(clip)
            p.setPen(QPen(QColor(25, 28, 34, 165), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for scale_x in (0.38, 0.78):
                p.drawEllipse(QPointF(cx, cy), r * scale_x, r)
            for scale_y in (0.35, 0.75):
                p.drawEllipse(QPointF(cx, cy), r, r * scale_y)
            p.restore()

        elif self.mode_id == "texture":
            grad = QRadialGradient(cx - r * 0.35, cy - r * 0.35, r * 1.45)
            grad.setColorAt(0.0, QColor(246, 214, 152))
            grad.setColorAt(0.45, QColor(186, 148, 96))
            grad.setColorAt(0.85, QColor(86, 98, 118))
            grad.setColorAt(1.0, QColor(42, 48, 60))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawEllipse(sphere_rect)

            p.setBrush(QColor(255, 245, 220, 75))
            p.drawEllipse(QPointF(cx - 3.2, cy - 2.5), 2.6, 1.8)
            p.drawEllipse(QPointF(cx + 3.5, cy + 2.0), 2.2, 1.6)

        elif self.mode_id == "albedo":
            lin = QLinearGradient(cx - r, cy - r, cx + r, cy + r)
            lin.setColorAt(0.0, QColor(244, 198, 92))
            lin.setColorAt(0.5, QColor(232, 134, 74))
            lin.setColorAt(1.0, QColor(94, 168, 228))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(lin)
            p.drawEllipse(sphere_rect)

        elif self.mode_id == "normal":
            grad = QRadialGradient(cx - r * 0.25, cy - r * 0.30, r * 1.35)
            grad.setColorAt(0.0, QColor(150, 235, 255))
            grad.setColorAt(0.45, QColor(135, 132, 255))
            grad.setColorAt(0.80, QColor(228, 110, 215))
            grad.setColorAt(1.0, QColor(85, 195, 165))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawEllipse(sphere_rect)


class BottomShadingDock(QFrame):
    """悬浮于视口底部的胶囊形材质球控制栏"""

    presetTriggered = pyqtSignal(str)
    actionTriggered = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BottomShadingDock")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QFrame#BottomShadingDock {
                background-color: rgba(24, 26, 30, 218);
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 14px;
            }
            QPushButton#DockIconBtn {
                background-color: transparent;
                color: #C8D0DC;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton#DockIconBtn:hover {
                background-color: rgba(255, 255, 255, 22);
                color: #FFFFFF;
            }
            QPushButton#DockIconBtn:checked {
                background-color: rgba(76, 154, 255, 65);
                color: #7CC0FF;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(6)

        self.turntable_btn = QPushButton("↻")
        self.turntable_btn.setObjectName("DockIconBtn")
        self.turntable_btn.setToolTip("自动转台旋转 (T)")
        self.turntable_btn.setCheckable(True)
        self.turntable_btn.setFixedSize(32, 32)
        self.turntable_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.turntable_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.turntable_btn.clicked.connect(
            lambda: self.actionTriggered.emit("turntable"))
        layout.addWidget(self.turntable_btn)

        layout.addWidget(self._make_divider())

        self.sphere_buttons = {}
        presets = [
            ("clay", "白模 [2]"),
            ("clay_wire", "线框加白模"),
            ("texture", "纹理材质 [1]"),
            ("albedo", "反照率 Albedo [4]"),
            ("normal", "法线视图 [3]"),
        ]
        for pid, tip in presets:
            btn = MaterialSphereButton(pid, tip, self)
            btn.clicked.connect(
                lambda checked=False, key=pid: self.presetTriggered.emit(key))
            layout.addWidget(btn)
            self.sphere_buttons[pid] = btn

        layout.addWidget(self._make_divider())

        self.reset_btn = QPushButton("⌖")
        self.reset_btn.setObjectName("DockIconBtn")
        self.reset_btn.setToolTip("重置视角 (R)")
        self.reset_btn.setFixedSize(32, 32)
        self.reset_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_btn.clicked.connect(lambda: self.actionTriggered.emit("reset"))
        layout.addWidget(self.reset_btn)

        self.adjustSize()

    @staticmethod
    def _make_divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedSize(1, 20)
        line.setStyleSheet(
            "background-color: rgba(255, 255, 255, 28); border: none;")
        return line

    def sync_state(self, shading_mode: int, wireframe_mode: int, turntable: bool):
        self.turntable_btn.setChecked(turntable)
        active = None
        if shading_mode == 1 and wireframe_mode == 0:
            active = "clay"
        elif shading_mode == 1 and wireframe_mode == 1:
            active = "clay_wire"
        elif shading_mode == 0 and wireframe_mode == 0:
            active = "texture"
        elif shading_mode == 4 and wireframe_mode == 0:
            active = "albedo"
        elif shading_mode == 2 and wireframe_mode == 0:
            active = "normal"

        for pid, btn in self.sphere_buttons.items():
            btn.setChecked(pid == active)


class ModelGLWidget(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mesh_prog = None
        self.grid_prog = None
        self.norm_prog = None
        self.dummy_vao = None

        # GPU 几何缓存列表：每个元素为 dict(vao, vbo, ebo, index_count, tex_id, base_color)
        self.gpu_meshes = []
        self.norm_vao = None
        self.norm_vbo = None
        self.norm_vertex_count = 0

        self._pending_payload = None

        # 轨道相机参数
        self.yaw = -35.0
        self.pitch = 22.0
        self.distance = 3.2
        self.target = np.array([0.0, 0.5, 0.0], dtype=np.float32)
        self.default_target_y = 0.5
        self._last_pos = QPoint()

        # 相机指数阻尼插值目标（点击坐标轴 / F1-F3 / R 时丝滑过渡）
        self._target_yaw = self.yaw
        self._target_pitch = self.pitch
        self._target_distance = self.distance
        self._target_lookat = self.target.copy()
        self._cam_animating = False
        self._last_frame_ts = time.perf_counter()

        # 3D 外接包围盒线框（X 键切换）
        self.show_bbox = False

        # MatCap 材质捕捉贴图（0 红蜡 / 1 曲面斑马纹 / 2 液态铬金）
        self.matcap_textures = []
        self.matcap_idx = 0

        # 屏幕空间曲率增强 / 物理镜头焦距
        self.cavity_enabled = False
        self.focal_length_mm = 35.0   # 全画幅等效，可切 24 / 35 / 50 / 85mm

        # O(1) 深度拾取与两点测量
        self.raw_model_scale = 1.0    # 归一化后的世界距离 × 它 = 模型原始单位
        self.measure_mode = False
        self.measure_points = []      # 最多两个世界坐标
        self._left_press_pos = QPoint()
        # 深度解析目标：QOpenGLWidget 的默认 FBO 是多采样，不能直接 glReadPixels
        self._pick_fbo = None
        self._pick_depth_rb = None

        # 显示开关与着色模式
        self.shading_mode = 0       # 0: 材质贴图/顶点色, 1: 雕塑白模, 2: 法线视图, 4: 反照率
        self.wireframe_mode = 0     # 0: 关闭, 1: 实体+线框叠加, 2: 纯线框
        self.show_grid = True
        self.show_normals = False

        # 新增：轴向与面朝向诊断状态
        self.z_up_mode = False          # False: Y-Up, True: 视为 Z-Up 并绕 X 轴 -90° 翻转
        self.check_backface = False     # 面朝向蓝红诊断开关
        self.norm_size = np.array([1.0, 1.0, 1.0], dtype=np.float32)

        # 新增：FBX 动画 / 骨骼蒙皮 / 场景灯光
        self.gizmo_prog = None
        self.gizmo_vao = None
        self.gizmo_vbo = None

        self.fbx_controller = None      # FBXSceneController；非 FBX 或纯静态 FBX 为 None
        self.anim_playing = True
        self.anim_idx = 0
        self.anim_time = 0.0
        self.use_scene_lights = False
        self.show_light_gizmos = True
        self._current_states = {}       # mesh_idx -> (uNodeTransform, bone_matrices|None)
        self._current_lights = []

        # 新增：骨骼 X-Ray / 正交投影 / 转台 / UV 棋盘格 / 影棚灯旋转 / 背景
        self.show_skeleton = False
        self._skeleton_lines = np.zeros((0, 3), dtype=np.float32)
        self.is_ortho = False
        self.turntable = False
        self.light_yaw = 0.0
        self.uv_checker = False
        self.checker_tex = None
        self.bg_presets = [
            (0.114, 0.122, 0.137),
            (0.280, 0.290, 0.310),
            (0.820, 0.830, 0.850),
            (0.020, 0.020, 0.024),
        ]
        self.bg_idx = 0

        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(16)  # ~60 FPS
        self.anim_timer.timeout.connect(self._on_anim_tick)

    def set_fbx_scene(self, controller):
        """装载/清空 FBX 动画与灯光状态，并同步 60fps 定时器"""
        self.fbx_controller = controller
        self.anim_idx = 0
        self.anim_time = 0.0
        self.anim_playing = True
        self.use_scene_lights = bool(controller and controller.raw_lights)
        self._current_states = {}
        self._current_lights = []
        if (controller and controller.animations) or self.turntable:
            self.anim_timer.start()
        else:
            self.anim_timer.stop()
        self.update()

    def _on_anim_tick(self):
        changed = False
        if self.fbx_controller and self.fbx_controller.animations and self.anim_playing:
            self.anim_time += 0.016
            changed = True
        if self.turntable:
            self.yaw = (self.yaw + 0.45) % 360.0
            changed = True
        if changed:
            self.update()
            if self.turntable:
                self._sync_axis_gizmo()
            parent = self.parent()
            bar = getattr(parent, "timeline_bar", None)
            if bar is not None and bar.isVisible():
                bar.update()

    def set_scene_data(
        self, submeshes: list, normal_lines: np.ndarray, norm_size: np.ndarray,
        model_scale: float = 1.0, reset_camera: bool = True,
    ):
        self._pending_payload = (submeshes, normal_lines)
        self.norm_size = np.asarray(norm_size, dtype=np.float32)
        self.raw_model_scale = float(model_scale) if model_scale > 0 else 1.0
        # 新模型/热重载后旧测量点已失效
        self.measure_points.clear()
        parent = self.parent()
        tag = getattr(parent, "measure_tag", None)
        if tag is not None:
            tag.hide()
        # 轴向切换后，模型的有效高度取自不同的分量，相机注视点需同步
        active_h = self.norm_size[2] if self.z_up_mode else self.norm_size[1]
        self.default_target_y = float(active_h * 0.5)
        if reset_camera:
            self.reset_camera()
        else:
            # 外部修改热重载：保留当前相机，不要跳回默认角度
            self._cam_animating = False
            self._target_yaw = self.yaw
            self._target_pitch = self.pitch
            self._target_distance = self.distance
            self._target_lookat = self.target.copy()
        self.update()

    def toggle_z_up(self):
        self.z_up_mode = not self.z_up_mode
        active_h = self.norm_size[2] if self.z_up_mode else self.norm_size[1]
        self.default_target_y = float(active_h * 0.5)
        self.target[1] = self.default_target_y
        self.update()

    def _get_model_matrix(self) -> np.ndarray:
        """Z-Up -> Y-Up：绕 X 轴旋转 -90°，并把模型的 Z 厚度重新抬到 Y = 0 贴地"""
        m = np.eye(4, dtype=np.float32)
        if self.z_up_mode:
            m[1, 1] = 0.0
            m[1, 2] = 1.0
            m[2, 1] = -1.0
            m[2, 2] = 0.0
            m[1, 3] = float(self.norm_size[2] * 0.5)
        return m

    def _sync_axis_gizmo(self):
        """把相机角度变化通知右上角坐标轴，避免拖拽时指示器滞后"""
        parent = self.parent()
        gizmo = getattr(parent, "axis_gizmo", None)
        if gizmo is not None:
            gizmo.update()

    def animate_camera_to(self, yaw=None, pitch=None, distance=None, target=None):
        """平滑过渡到目标视角；target 为 None 时保持当前注视点。"""
        self._target_yaw = self.yaw if yaw is None else float(yaw)
        self._target_pitch = (self.pitch if pitch is None
                              else float(np.clip(pitch, -89.0, 89.0)))
        self._target_distance = self.distance if distance is None else float(distance)
        self._target_lookat = (self.target.copy() if target is None
                               else np.asarray(target, dtype=np.float32))
        self._cam_animating = True
        self._last_frame_ts = time.perf_counter()
        self.update()

    def _step_camera_animation(self):
        now = time.perf_counter()
        dt = min(now - self._last_frame_ts, 0.05)
        self._last_frame_ts = now
        if not self._cam_animating:
            return

        alpha = 1.0 - float(np.exp(-dt * 16.0))
        # 偏航角走最短路径，避免从 359° 过渡到 0° 时绕一整圈
        dyaw = (self._target_yaw - self.yaw + 180.0) % 360.0 - 180.0
        dpitch = self._target_pitch - self.pitch
        ddist = self._target_distance - self.distance
        dlook = self._target_lookat - self.target

        if (abs(dyaw) < 0.05 and abs(dpitch) < 0.05
                and abs(ddist) < 0.002 and float(np.linalg.norm(dlook)) < 0.002):
            self.yaw = self._target_yaw
            self.pitch = self._target_pitch
            self.distance = self._target_distance
            self.target = self._target_lookat.copy()
            self._cam_animating = False
        else:
            self.yaw += dyaw * alpha
            self.pitch += dpitch * alpha
            self.distance += ddist * alpha
            self.target += dlook * alpha

        self._sync_axis_gizmo()

    def reset_camera(self, smooth: bool = True):
        target = np.array([0.0, self.default_target_y, 0.0], dtype=np.float32)
        base_distance = 3.8 * (float(self.focal_length_mm) / 35.0)
        if smooth:
            self.animate_camera_to(-35.0, 22.0, base_distance, target)
        else:
            self._cam_animating = False
            self.yaw, self.pitch, self.distance, self.target = -35.0, 22.0, base_distance, target
            self._sync_axis_gizmo()
            self.update()

    def set_focal_length(self, mm: float):
        """切换物理焦距；按焦距比例同步轨道半径，保持主体构图不跳变。"""
        old = max(float(self.focal_length_mm), 1.0)
        new = max(float(mm), 10.0)
        self.focal_length_mm = new
        if not self.is_ortho:
            self.distance = float(np.clip(
                self.distance * (new / old), 0.15, 35.0))
            self._target_distance = self.distance
            self._cam_animating = False
        self.update()

    def apply_shading_preset(self, preset: str):
        """统一切换 5 种核心观察模式：clay / clay_wire / texture / albedo / normal"""
        if preset == "clay":
            self.shading_mode = 1
            self.wireframe_mode = 0
        elif preset == "clay_wire":
            self.shading_mode = 1
            self.wireframe_mode = 1
        elif preset == "texture":
            self.shading_mode = 0
            self.wireframe_mode = 0
        elif preset == "albedo":
            self.shading_mode = 4
            self.wireframe_mode = 0
        elif preset == "normal":
            self.shading_mode = 2
            self.wireframe_mode = 0
        elif preset == "matcap":
            self.shading_mode = 5
            self.wireframe_mode = 0
        self.update()

    def set_preset_view(self, view_name: str, invert: bool = False):
        """标准三视图；invert=True 时看背面 / 左面 / 底面"""
        if view_name == "front":
            self.animate_camera_to(180.0 if invert else 0.0, 0.0)
        elif view_name == "right":
            self.animate_camera_to(-90.0 if invert else 90.0, 0.0)
        elif view_name == "top":
            self.animate_camera_to(0.0, -89.0 if invert else 89.0)

    def toggle_turntable(self):
        self.turntable = not self.turntable
        self._cam_animating = False
        if self.turntable:
            self.anim_timer.start()
        elif not (self.fbx_controller and self.fbx_controller.animations):
            self.anim_timer.stop()
        self.update()

    def _compile_program(self, vs_src: str, fs_src: str) -> int:
        def build_stage(src, stage):
            shader = glCreateShader(stage)
            glShaderSource(shader, src)
            glCompileShader(shader)
            if not glGetShaderiv(shader, GL_COMPILE_STATUS):
                raise RuntimeError(glGetShaderInfoLog(shader).decode())
            return shader

        vs = build_stage(vs_src, GL_VERTEX_SHADER)
        fs = build_stage(fs_src, GL_FRAGMENT_SHADER)
        prog = glCreateProgram()
        glAttachShader(prog, vs)
        glAttachShader(prog, fs)
        glLinkProgram(prog)
        if not glGetProgramiv(prog, GL_LINK_STATUS):
            raise RuntimeError(glGetProgramInfoLog(prog).decode())
        return prog

    def initializeGL(self):
        # Blender 经典深灰视口背景色
        glClearColor(0.114, 0.122, 0.137, 1.0)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_MULTISAMPLE)

        self.mesh_prog = self._compile_program(MESH_VS, MESH_FS)
        self.grid_prog = self._compile_program(GRID_VS, GRID_FS)
        self.norm_prog = self._compile_program(NORMAL_LINE_VS, NORMAL_LINE_FS)
        self.gizmo_prog = self._compile_program(GIZMO_VS, GIZMO_FS)
        self.dummy_vao = glGenVertexArrays(1)
        self.gizmo_vao = glGenVertexArrays(1)
        self.gizmo_vbo = glGenBuffers(1)
        if self.checker_tex is None:
            self.checker_tex = self._create_uv_checker_texture()
        if not self.matcap_textures:
            self.matcap_textures = self._create_procedural_matcaps()

    def _create_uv_checker_texture(self) -> int:
        """程序化生成 512×512 灰白棋盘 + 四象限淡彩，用于 UV 拉伸/接缝诊断"""
        size, cells = 512, 16
        step = size // cells
        yy, xx = np.indices((size, size))
        checker = ((xx // step) + (yy // step)) % 2
        base = np.where(checker[..., None] == 0, 220, 60).astype(np.float32)
        tint = np.ones((size, size, 3), dtype=np.float32)
        tint[:size // 2, :size // 2] = [1.0, 0.82, 0.82]
        tint[:size // 2, size // 2:] = [0.82, 1.0, 0.85]
        tint[size // 2:, :size // 2] = [0.82, 0.88, 1.0]
        img = np.clip(base * tint, 0, 255).astype(np.uint8)
        return self._upload_texture(Image.fromarray(img, mode="RGB"))

    def _create_procedural_matcaps(self) -> list:
        """生成 3 张 256×256 球形 MatCap：红蜡 / 曲面斑马纹 / 液态铬金。"""
        size = 256
        lin = np.linspace(-1.0, 1.0, size, dtype=np.float32)
        xx, yy = np.meshgrid(lin, -lin)  # 上传时还会做一次 Y 翻转，这里先对齐
        r2 = xx * xx + yy * yy
        mask = r2 <= 1.0
        zz = np.zeros_like(xx)
        zz[mask] = np.sqrt(np.maximum(0.0, 1.0 - r2[mask]))

        # 球外区域做边缘外扩，避免双线性采样在圆盘边缘出现黑边
        r = np.sqrt(np.maximum(r2, 1e-6))
        nx = np.where(mask, xx, xx / r)
        ny = np.where(mask, yy, yy / r)
        nz = np.where(mask, zz, 0.0)

        textures = []

        # 1. ZBrush 风格红蜡：暖色漫反射 + 细高光 + 轻微边缘光
        ldir = np.array([-0.45, 0.65, 0.61], dtype=np.float32)
        diff = np.clip(nx * ldir[0] + ny * ldir[1] + nz * ldir[2], 0.0, 1.0)
        hvec = ldir + np.array([0.0, 0.0, 1.0], dtype=np.float32)
        hvec /= np.linalg.norm(hvec)
        spec = np.power(
            np.clip(nx * hvec[0] + ny * hvec[1] + nz * hvec[2], 0.0, 1.0), 36.0)
        rim = np.power(1.0 - np.clip(nz, 0.0, 1.0), 2.5)
        wax = np.stack([
            0.22 + diff * 0.56 + spec * 0.42 + rim * 0.16,
            0.06 + diff * 0.19 + spec * 0.34 + rim * 0.07,
            0.05 + diff * 0.14 + spec * 0.30 + rim * 0.06,
        ], axis=-1)
        textures.append(self._upload_matcap_array(wax))

        # 2. CAD 曲面连续性检测斑马纹
        refl_y = 2.0 * nz * ny - 0.35 * nx
        stripes = 0.5 + 0.5 * np.tanh(np.sin(refl_y * 26.0) * 6.0)
        shade = 0.35 + 0.65 * np.clip(nz * 0.85 + ny * 0.35, 0.0, 1.0)
        zebra_val = (0.14 + 0.82 * stripes) * shade
        zebra = np.stack([zebra_val * 0.94, zebra_val * 0.97, zebra_val], axis=-1)
        textures.append(self._upload_matcap_array(zebra))

        # 3. 硬表面液态铬金 / 影棚反射
        sky_t = np.clip(ny * 0.5 + 0.5, 0.0, 1.0)
        horizon = np.exp(-np.abs(ny + 0.08) * 14.0)
        hotspot = np.power(
            np.clip(nx * -0.35 + ny * 0.72 + nz * 0.60, 0.0, 1.0), 24.0)
        chrome = np.stack([
            0.18 + 0.48 * (sky_t ** 1.5) + 0.30 * horizon + 0.55 * hotspot,
            0.21 + 0.52 * (sky_t ** 1.4) + 0.28 * horizon + 0.56 * hotspot,
            0.26 + 0.58 * (sky_t ** 1.2) + 0.24 * horizon + 0.60 * hotspot,
        ], axis=-1)
        textures.append(self._upload_matcap_array(chrome))

        return textures

    def _upload_matcap_array(self, rgb_float: np.ndarray) -> int:
        arr = (np.clip(rgb_float, 0.0, 1.0) * 255.0).astype(np.uint8)
        tex_id = self._upload_texture(Image.fromarray(arr, mode="RGB"))
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glBindTexture(GL_TEXTURE_2D, 0)
        return tex_id

    def _clear_gpu_resources(self):
        for item in self.gpu_meshes:
            glDeleteVertexArrays(1, [item["vao"]])
            glDeleteBuffers(1, [item["vbo"]])
            glDeleteBuffers(1, [item["ebo"]])
            for key in ("bone_vbo_ids", "bone_vbo_weights"):
                if item.get(key) is not None:
                    glDeleteBuffers(1, [item[key]])
            for key in ("tex_id", "norm_tex_id"):
                if item.get(key) is not None:
                    glDeleteTextures(1, [item[key]])
        self.gpu_meshes.clear()

        if self.norm_vao is not None:
            glDeleteVertexArrays(1, [self.norm_vao])
            glDeleteBuffers(1, [self.norm_vbo])
            self.norm_vao = None
            self.norm_vbo = None
            self.norm_vertex_count = 0

    def _upload_texture(self, pil_img: Image.Image) -> int:
        img = pil_img.convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        img_bytes = img.tobytes()
        w, h = img.size

        tex_id = glGenTextures(1)
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, img_bytes)
        glGenerateMipmap(GL_TEXTURE_2D)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
        glBindTexture(GL_TEXTURE_2D, 0)
        return tex_id

    def _upload_pending_data(self):
        if self._pending_payload is None:
            return
        submeshes, normal_lines = self._pending_payload
        self._pending_payload = None
        self._clear_gpu_resources()

        stride = 12 * 4  # pos(3) + normal(3) + uv(2) + color(4) = 12 floats
        for sm in submeshes:
            interleaved = sm["interleaved"]
            indices = sm["indices"]

            vao = glGenVertexArrays(1)
            vbo = glGenBuffers(1)
            ebo = glGenBuffers(1)

            glBindVertexArray(vao)
            glBindBuffer(GL_ARRAY_BUFFER, vbo)
            glBufferData(GL_ARRAY_BUFFER, interleaved.nbytes, interleaved, GL_STATIC_DRAW)

            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ebo)
            glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL_STATIC_DRAW)

            # 0: Position (3f)
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(0))
            glEnableVertexAttribArray(0)
            # 1: Normal (3f)
            glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(12))
            glEnableVertexAttribArray(1)
            # 2: UV (2f)
            glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(24))
            glEnableVertexAttribArray(2)
            # 3: Vertex Color RGBA (4f)
            glVertexAttribPointer(3, 4, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(32))
            glEnableVertexAttribArray(3)

            # 4/5: 骨骼 ID 与权重（独立 VBO，仅蒙皮网格启用；静态网格保持 48B 紧凑布局）
            bone_ids = sm.get("bone_ids")
            bone_weights = sm.get("bone_weights")
            bone_vbo_ids = None
            bone_vbo_weights = None
            if bone_ids is not None and bone_weights is not None:
                bone_ids = np.ascontiguousarray(bone_ids, dtype=np.int32)
                bone_weights = np.ascontiguousarray(bone_weights, dtype=np.float32)

                bone_vbo_ids = glGenBuffers(1)
                glBindBuffer(GL_ARRAY_BUFFER, bone_vbo_ids)
                glBufferData(GL_ARRAY_BUFFER, bone_ids.nbytes, bone_ids, GL_STATIC_DRAW)
                glVertexAttribIPointer(4, 4, GL_INT, 4 * 4, ctypes.c_void_p(0))
                glEnableVertexAttribArray(4)

                bone_vbo_weights = glGenBuffers(1)
                glBindBuffer(GL_ARRAY_BUFFER, bone_vbo_weights)
                glBufferData(GL_ARRAY_BUFFER, bone_weights.nbytes, bone_weights, GL_STATIC_DRAW)
                glVertexAttribPointer(5, 4, GL_FLOAT, GL_FALSE, 4 * 4, ctypes.c_void_p(0))
                glEnableVertexAttribArray(5)
            glBindVertexArray(0)

            tex_id = self._upload_texture(sm["texture"]) if sm["texture"] is not None else None
            norm_img = sm.get("normal_texture")
            norm_tex_id = self._upload_texture(norm_img) if norm_img is not None else None
            self.gpu_meshes.append({
                "vao": vao,
                "vbo": vbo,
                "ebo": ebo,
                "index_count": len(indices),
                "tex_id": tex_id,
                "norm_tex_id": norm_tex_id,
                "base_color": sm["base_color"],
                "name": sm.get("name", f"Mesh_{len(self.gpu_meshes) + 1}"),
                "tri_count": len(indices) // 3,
                "visible": True,
                "mesh_index": int(sm.get("mesh_index", -1)),
                "bone_vbo_ids": bone_vbo_ids,
                "bone_vbo_weights": bone_vbo_weights,
            })

        # 上传法线向量线段缓冲区
        if normal_lines is not None and len(normal_lines) > 0:
            self.norm_vao = glGenVertexArrays(1)
            self.norm_vbo = glGenBuffers(1)
            self.norm_vertex_count = len(normal_lines)

            glBindVertexArray(self.norm_vao)
            glBindBuffer(GL_ARRAY_BUFFER, self.norm_vbo)
            glBufferData(GL_ARRAY_BUFFER, normal_lines.nbytes, normal_lines, GL_STATIC_DRAW)
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 12, ctypes.c_void_p(0))
            glEnableVertexAttribArray(0)
            glBindVertexArray(0)

        # 新 GPU 网格上传完成后刷新大纲；多部件模型的复杂结构默认展开
        parent = self.parent()
        panel = getattr(parent, "outliner_panel", None)
        if panel is not None:
            panel.rebuild()
            if len(self.gpu_meshes) > 1 and not panel.isVisible():
                panel.setVisible(True)
            if panel.isVisible() and hasattr(parent, "_layout_timeline"):
                parent._layout_timeline()

    def resizeGL(self, w: int, h: int):
        glViewport(0, 0, w, max(h, 1))

    def _get_camera_matrices(self):
        rx = np.radians(self.pitch)
        ry = np.radians(self.yaw)
        offset = np.array([
            self.distance * np.cos(rx) * np.sin(ry),
            self.distance * np.sin(rx),
            self.distance * np.cos(rx) * np.cos(ry)
        ], dtype=np.float32)

        eye = self.target + offset
        up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        view = mat4_look_at(eye, self.target, up)
        aspect = self.width() / max(self.height(), 1)
        if self.is_ortho:
            half_h = self.distance * np.tan(np.radians(22.5))
            proj = mat4_ortho(half_h * aspect, half_h, 0.02, 100.0)
        else:
            # 全画幅 36×24mm 片门，按垂直片门 24mm 换算物理焦距视场角
            fovy = float(np.degrees(
                2.0 * np.arctan(12.0 / max(float(self.focal_length_mm), 10.0))))
            proj = mat4_perspective(fovy, aspect, 0.02, 100.0)
        return eye, view, proj

    def _draw_meshes(self, eye, view, proj, render_mode_override=None, model_mat=None):
        if model_mat is None:
            model_mat = self._get_model_matrix()

        glUseProgram(self.mesh_prog)
        glUniformMatrix4fv(glGetUniformLocation(self.mesh_prog, "uModel"), 1, GL_TRUE, model_mat)
        glUniformMatrix4fv(glGetUniformLocation(self.mesh_prog, "uView"), 1, GL_TRUE, view)
        glUniformMatrix4fv(glGetUniformLocation(self.mesh_prog, "uProj"), 1, GL_TRUE, proj)
        glUniform3f(glGetUniformLocation(self.mesh_prog, "uViewPos"), *eye)
        glUniform3f(glGetUniformLocation(self.mesh_prog, "uTargetPos"), *self.target)

        mode = render_mode_override if render_mode_override is not None else self.shading_mode
        glUniform1i(glGetUniformLocation(self.mesh_prog, "uRenderMode"), mode)
        glUniform1f(
            glGetUniformLocation(self.mesh_prog, "uCavityStrength"),
            0.75 if self.cavity_enabled else 0.0)
        if self.matcap_textures:
            mc_tex = self.matcap_textures[self.matcap_idx % len(self.matcap_textures)]
            glActiveTexture(GL_TEXTURE3)
            glBindTexture(GL_TEXTURE_2D, mc_tex)
            glUniform1i(glGetUniformLocation(self.mesh_prog, "uMatcapMap"), 3)
        check_bf = 1 if (self.check_backface and render_mode_override != 3) else 0
        glUniform1i(glGetUniformLocation(self.mesh_prog, "uCheckBackface"), check_bf)
        glUniform1i(glGetUniformLocation(self.mesh_prog, "uUVChecker"),
                    1 if self.uv_checker else 0)
        glUniform1f(glGetUniformLocation(self.mesh_prog, "uLightYaw"), float(self.light_yaw))

        # UV 棋盘格固定占用纹理单元 1，材质贴图仍用单元 0
        if self.checker_tex is not None:
            glActiveTexture(GL_TEXTURE1)
            glBindTexture(GL_TEXTURE_2D, self.checker_tex)
            glUniform1i(glGetUniformLocation(self.mesh_prog, "uCheckerMap"), 1)

        # FBX 场景灯光参数（最多 8 盏）
        lights = self._current_lights or []
        use_lights = 1 if (self.use_scene_lights and lights) else 0
        glUniform1i(glGetUniformLocation(self.mesh_prog, "uUseSceneLights"), use_lights)
        glUniform1i(glGetUniformLocation(self.mesh_prog, "uNumLights"), min(len(lights), 8) if use_lights else 0)
        if use_lights:
            n = min(len(lights), 8)
            light_pos = np.array([l["pos"] for l in lights[:n]], dtype=np.float32)
            light_dir = np.array([l["dir"] for l in lights[:n]], dtype=np.float32)
            light_col = np.array([l["color"] for l in lights[:n]], dtype=np.float32)
            light_type = np.array([l["type"] for l in lights[:n]], dtype=np.int32)
            light_cone = np.array([[l["inner"], l["outer"]] for l in lights[:n]], dtype=np.float32)
            glUniform3fv(glGetUniformLocation(self.mesh_prog, "uLightPos"), n, light_pos)
            glUniform3fv(glGetUniformLocation(self.mesh_prog, "uLightDir"), n, light_dir)
            glUniform3fv(glGetUniformLocation(self.mesh_prog, "uLightColor"), n, light_col)
            glUniform1iv(glGetUniformLocation(self.mesh_prog, "uLightType"), n, light_type)
            glUniform2fv(glGetUniformLocation(self.mesh_prog, "uLightCone"), n, light_cone)

        u_node_loc = glGetUniformLocation(self.mesh_prog, "uNodeTransform")
        u_normal_loc = glGetUniformLocation(self.mesh_prog, "uNormalMatrix")
        u_skin_loc = glGetUniformLocation(self.mesh_prog, "uHasSkinning")
        u_bone_loc = glGetUniformLocation(self.mesh_prog, "uBoneMatrices")
        u_norm_map_loc = glGetUniformLocation(self.mesh_prog, "uNormalMap")
        u_has_norm_loc = glGetUniformLocation(self.mesh_prog, "uHasNormalMap")
        identity = np.eye(4, dtype=np.float32)

        for item in self.gpu_meshes:
            if not item.get("visible", True):
                continue
            r, g, b = item["base_color"]
            glUniform3f(glGetUniformLocation(self.mesh_prog, "uBaseColor"), r, g, b)

            if item["tex_id"] is not None:
                glActiveTexture(GL_TEXTURE0)
                glBindTexture(GL_TEXTURE_2D, item["tex_id"])
                glUniform1i(glGetUniformLocation(self.mesh_prog, "uAlbedoMap"), 0)
                glUniform1i(glGetUniformLocation(self.mesh_prog, "uHasTexture"), 1)
            else:
                glUniform1i(glGetUniformLocation(self.mesh_prog, "uHasTexture"), 0)

            if item.get("norm_tex_id") is not None:
                glActiveTexture(GL_TEXTURE2)
                glBindTexture(GL_TEXTURE_2D, item["norm_tex_id"])
                glUniform1i(u_norm_map_loc, 2)
                glUniform1i(u_has_norm_loc, 1)
            else:
                glUniform1i(u_has_norm_loc, 0)

            # 节点层级变换 / 骨骼矩阵（静态网格传单位阵，关闭蒙皮分支）
            state = self._current_states.get(item["mesh_index"])
            node_mat = state[0] if state is not None else identity
            bone_mats = state[1] if state is not None else None
            glUniformMatrix4fv(u_node_loc, 1, GL_TRUE, node_mat)
            world_mat = (model_mat @ node_mat).astype(np.float32)
            try:
                # 行向量约定：法线按 n · W⁻¹ 变换；上传时 GLSL 会再做一次转置解读
                normal_mat = np.linalg.inv(world_mat)[:3, :3].astype(np.float32)
            except np.linalg.LinAlgError:
                normal_mat = np.eye(3, dtype=np.float32)
            glUniformMatrix3fv(u_normal_loc, 1, GL_TRUE, normal_mat)
            if (bone_mats is not None and len(bone_mats) > 0
                    and item.get("bone_vbo_ids") is not None):
                glUniform1i(u_skin_loc, 1)
                glUniformMatrix4fv(u_bone_loc, min(len(bone_mats), 100), GL_TRUE,
                                   np.ascontiguousarray(bone_mats[:100], dtype=np.float32))
            else:
                glUniform1i(u_skin_loc, 0)

            glBindVertexArray(item["vao"])
            glDrawElements(GL_TRIANGLES, item["index_count"], GL_UNSIGNED_INT, None)
        glBindVertexArray(0)
        # 解绑专用纹理单元，避免与 QOpenGLWidget 自身的合成纹理状态冲突
        glActiveTexture(GL_TEXTURE3)
        glBindTexture(GL_TEXTURE_2D, 0)
        glActiveTexture(GL_TEXTURE2)
        glBindTexture(GL_TEXTURE_2D, 0)
        glActiveTexture(GL_TEXTURE1)
        glBindTexture(GL_TEXTURE_2D, 0)
        glActiveTexture(GL_TEXTURE0)

    def _draw_skeleton(self, model_mat, view, proj):
        """X-Ray 骨骼：关闭深度测试，叠加橙色父子连线"""
        pts = self._skeleton_lines
        if not self.show_skeleton or len(pts) == 0:
            return
        colors = np.tile(np.array([[1.0, 0.55, 0.12]], dtype=np.float32), (len(pts), 1))
        buf = np.ascontiguousarray(np.hstack([pts, colors]), dtype=np.float32)

        glDisable(GL_DEPTH_TEST)
        glUseProgram(self.gizmo_prog)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uModel"), 1, GL_TRUE, model_mat)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uView"), 1, GL_TRUE, view)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uProj"), 1, GL_TRUE, proj)

        glBindVertexArray(self.gizmo_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.gizmo_vbo)
        glBufferData(GL_ARRAY_BUFFER, buf.nbytes, buf, GL_DYNAMIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 24, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 24, ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)
        glDrawArrays(GL_LINES, 0, len(buf))
        glBindVertexArray(0)
        glEnable(GL_DEPTH_TEST)

    def _draw_light_gizmos(self, model_mat, view, proj):
        """视口中标出灯光位置（十字星）与平行光/聚光灯照射方向（黄色射线）"""
        lights = self._current_lights or []
        if not lights or not self.show_light_gizmos:
            return
        rows = []
        scale = 0.06
        for lt in lights:
            p = np.asarray(lt["pos"], dtype=np.float32)
            c = np.clip(np.asarray(lt["color"], dtype=np.float32), 0.35, 1.0)
            for axis in (np.array([scale, 0.0, 0.0], dtype=np.float32),
                         np.array([0.0, scale, 0.0], dtype=np.float32),
                         np.array([0.0, 0.0, scale], dtype=np.float32)):
                rows.append(np.hstack([p - axis, c]))
                rows.append(np.hstack([p + axis, c]))
            if lt["type"] in (1, 3):
                ray_col = np.array([1.0, 0.85, 0.2], dtype=np.float32)
                ray_end = p + np.asarray(lt["dir"], dtype=np.float32) * 0.45
                rows.append(np.hstack([p, ray_col]))
                rows.append(np.hstack([ray_end, ray_col]))
        if not rows:
            return
        buf = np.ascontiguousarray(np.stack(rows), dtype=np.float32)

        glUseProgram(self.gizmo_prog)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uModel"), 1, GL_TRUE, model_mat)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uView"), 1, GL_TRUE, view)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uProj"), 1, GL_TRUE, proj)

        glBindVertexArray(self.gizmo_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.gizmo_vbo)
        glBufferData(GL_ARRAY_BUFFER, buf.nbytes, buf, GL_DYNAMIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 24, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 24, ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)
        glDrawArrays(GL_LINES, 0, len(buf))
        glBindVertexArray(0)

    def _draw_bounding_box(self, model_mat, view, proj):
        """复用 gizmo_prog 绘制模型归一化后的真实比例外接包围盒。"""
        if not self.show_bbox or not self.gpu_meshes or self.gizmo_prog is None:
            return

        hx = float(self.norm_size[0] * 0.5)
        hy = float(self.norm_size[1])
        hz = float(self.norm_size[2] * 0.5)

        # 8 个角点，y 从 0（贴地）到模型高度；12 条边
        c000, c100 = [-hx, 0.0, -hz], [hx, 0.0, -hz]
        c010, c110 = [-hx, hy, -hz], [hx, hy, -hz]
        c001, c101 = [-hx, 0.0, hz], [hx, 0.0, hz]
        c011, c111 = [-hx, hy, hz], [hx, hy, hz]
        edges = [
            c000, c100, c100, c101, c101, c001, c001, c000,  # 底面
            c010, c110, c110, c111, c111, c011, c011, c010,  # 顶面
            c000, c010, c100, c110, c101, c111, c001, c011,  # 4 条立柱
        ]
        col = [0.35, 0.68, 1.0]
        buf = np.ascontiguousarray(
            [np.asarray(p, dtype=np.float32).tolist() + col for p in edges],
            dtype=np.float32)

        glDisable(GL_DEPTH_TEST)
        glUseProgram(self.gizmo_prog)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uModel"), 1, GL_TRUE, model_mat)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uView"), 1, GL_TRUE, view)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uProj"), 1, GL_TRUE, proj)

        glBindVertexArray(self.gizmo_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.gizmo_vbo)
        glBufferData(GL_ARRAY_BUFFER, buf.nbytes, buf, GL_DYNAMIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 24, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 24, ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)
        glDrawArrays(GL_LINES, 0, len(buf))
        glBindVertexArray(0)
        glEnable(GL_DEPTH_TEST)

    def _ensure_pick_fbo(self) -> bool:
        """1×1 的单采样深度 FBO，把多采样深度中的一个像素解析出来。"""
        if self._pick_fbo is not None:
            return True
        fbo = glGenFramebuffers(1)
        rb = glGenRenderbuffers(1)
        glBindRenderbuffer(GL_RENDERBUFFER, rb)
        glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH24_STENCIL8, 1, 1)
        glBindFramebuffer(GL_FRAMEBUFFER, fbo)
        glFramebufferRenderbuffer(
            GL_FRAMEBUFFER, GL_DEPTH_STENCIL_ATTACHMENT, GL_RENDERBUFFER, rb)
        glDrawBuffer(GL_NONE)
        glReadBuffer(GL_NONE)
        status = glCheckFramebufferStatus(GL_FRAMEBUFFER)
        if status != GL_FRAMEBUFFER_COMPLETE:
            glDeleteFramebuffers(1, [fbo])
            glDeleteRenderbuffers(1, [rb])
            glBindFramebuffer(GL_FRAMEBUFFER, 0)
            return False
        self._pick_fbo = int(fbo)
        self._pick_depth_rb = int(rb)
        return True

    def pick_surface_point(self, widget_pos: QPointF):
        """把多采样深度缓冲中的目标像素解析到 1×1 FBO，再反投影成世界坐标。

        每次只 blit 一个像素，复杂度 O(1)，与 200 万面或 20 万面无关。
        """
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return None
        viewport = glGetIntegerv(GL_VIEWPORT)
        fb_w = int(viewport[2]) if len(viewport) >= 4 else w
        fb_h = int(viewport[3]) if len(viewport) >= 4 else h
        if fb_w <= 0 or fb_h <= 0:
            return None

        sx = fb_w / float(w)
        sy = fb_h / float(h)
        fb_x = int(np.clip(widget_pos.x() * sx, 0, fb_w - 1))
        fb_y = int(np.clip((h - widget_pos.y()) * sy, 0, fb_h - 1))

        self.makeCurrent()
        prev_raw = glGetIntegerv(GL_FRAMEBUFFER_BINDING)
        prev_fbo = int(prev_raw[0] if hasattr(prev_raw, "__len__") else prev_raw)
        try:
            if not self._ensure_pick_fbo():
                return None
            # QOpenGLWidget 默认 FBO 通常是 4x MSAA；单采样 FBO 只接 1 个像素
            glBindFramebuffer(GL_READ_FRAMEBUFFER, self.defaultFramebufferObject())
            glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self._pick_fbo)
            glBlitFramebuffer(
                fb_x, fb_y, fb_x + 1, fb_y + 1,
                0, 0, 1, 1,
                GL_DEPTH_BUFFER_BIT, GL_NEAREST)

            glBindFramebuffer(GL_FRAMEBUFFER, self._pick_fbo)
            raw = np.empty((1, 1), dtype=np.float32)
            glReadPixels(0, 0, 1, 1, GL_DEPTH_COMPONENT, GL_FLOAT, raw)
            depth = float(raw.reshape(-1)[0])
        finally:
            glBindFramebuffer(GL_FRAMEBUFFER, prev_fbo)
            # 不调用 doneCurrent()：QOpenGLWidget 释放上下文后会丢弃默认 FBO 的深度，
            # 连续拾取第二个点时就会读到 1.0。保持上下文直到本次事件返回即可。

        if depth >= 0.99999:
            return None

        ndc = np.array([
            (widget_pos.x() / w) * 2.0 - 1.0,
            1.0 - (widget_pos.y() / h) * 2.0,
            depth * 2.0 - 1.0,
            1.0,
        ], dtype=np.float32)

        _, view, proj = self._get_camera_matrices()
        inv_vp = np.linalg.inv(proj @ view).astype(np.float32)
        world_h = inv_vp @ ndc
        if abs(float(world_h[3])) < 1e-6:
            return None
        return (world_h[:3] / world_h[3]).astype(np.float32)

    def cleanup_pick_resources(self):
        """销毁 1×1 拾取 FBO；父窗口关闭时必须显式调用，QOpenGLWidget.closeEvent 不一定触发。"""
        if not self._pick_fbo and not self._pick_depth_rb:
            return
        try:
            self.makeCurrent()
            if self._pick_fbo:
                glDeleteFramebuffers(1, [self._pick_fbo])
            if self._pick_depth_rb:
                glDeleteRenderbuffers(1, [self._pick_depth_rb])
        except Exception:
            pass
        finally:
            self._pick_fbo = None
            self._pick_depth_rb = None
            self.doneCurrent()

    def closeEvent(self, event):
        self.cleanup_pick_resources()
        super().closeEvent(event)

    def _draw_measurements(self, view, proj):
        """绘制两点 3D 卡尺和端点十字，并更新悬浮距离标签。"""
        parent = self.parent()
        tag = getattr(parent, "measure_tag", None)
        if not self.measure_mode or not self.measure_points:
            if tag is not None:
                tag.hide()
            return
        if self.gizmo_prog is None:
            return

        rows = []
        col_pt = [1.0, 0.82, 0.22]
        col_ln = [0.25, 0.88, 1.0]
        s = 0.025
        for p in self.measure_points:
            p = np.asarray(p, dtype=np.float32)
            for ax in (
                np.array([s, 0.0, 0.0], dtype=np.float32),
                np.array([0.0, s, 0.0], dtype=np.float32),
                np.array([0.0, 0.0, s], dtype=np.float32),
            ):
                rows.append(np.hstack([p - ax, col_pt]))
                rows.append(np.hstack([p + ax, col_pt]))

        if len(self.measure_points) >= 2:
            p1, p2 = [np.asarray(p, dtype=np.float32) for p in self.measure_points[:2]]
            rows.append(np.hstack([p1, col_ln]))
            rows.append(np.hstack([p2, col_ln]))
        if not rows:
            return
        buf = np.ascontiguousarray(np.stack(rows), dtype=np.float32)

        glDisable(GL_DEPTH_TEST)
        glUseProgram(self.gizmo_prog)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uModel"), 1, GL_TRUE,
                           np.eye(4, dtype=np.float32))
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uView"), 1, GL_TRUE, view)
        glUniformMatrix4fv(glGetUniformLocation(self.gizmo_prog, "uProj"), 1, GL_TRUE, proj)

        glBindVertexArray(self.gizmo_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.gizmo_vbo)
        glBufferData(GL_ARRAY_BUFFER, buf.nbytes, buf, GL_DYNAMIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 24, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 24, ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)
        glDrawArrays(GL_LINES, 0, len(buf))
        glBindVertexArray(0)
        glEnable(GL_DEPTH_TEST)

        if tag is None:
            return
        if len(self.measure_points) == 1:
            tag.setText("请在模型表面点击第二个测量点…")
            anchor = np.asarray(self.measure_points[0], dtype=np.float32)
        else:
            p1, p2 = [np.asarray(p, dtype=np.float32) for p in self.measure_points[:2]]
            real_dist = float(np.linalg.norm(p2 - p1)) / max(self.raw_model_scale, 1e-6)
            tag.setText(f"距离：{real_dist:.4f}  原始单位")
            anchor = (p1 + p2) * 0.5

        clip = proj @ view @ np.array([
            anchor[0], anchor[1], anchor[2], 1.0], dtype=np.float32)
        if clip[3] > 1e-5:
            sx = int((clip[0] / clip[3] * 0.5 + 0.5) * self.width())
            sy = int((0.5 - clip[1] / clip[3] * 0.5) * self.height())
            tag.adjustSize()
            tag.move(
                int(sx - tag.width() // 2),
                max(int(sy - tag.height() - 10), 8),
            )
            tag.show()
            tag.raise_()
        else:
            tag.hide()

    def paintGL(self):
        self._step_camera_animation()
        r, g, b = self.bg_presets[self.bg_idx % len(self.bg_presets)]
        glClearColor(r, g, b, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        self._upload_pending_data()
        eye, view, proj = self._get_camera_matrices()
        model_mat = self._get_model_matrix()

        # 每帧求值 FBX 节点层级 / 骨骼矩阵 / 灯光状态
        self._current_states, self._current_lights = {}, []
        if self.fbx_controller is not None:
            self._current_states, self._current_lights = self.fbx_controller.evaluate(
                self.anim_idx, self.anim_time)
            if self.show_skeleton:
                self._skeleton_lines = self.fbx_controller.get_skeleton_segments()
            else:
                self._skeleton_lines = np.zeros((0, 3), dtype=np.float32)
        else:
            self._skeleton_lines = np.zeros((0, 3), dtype=np.float32)

        # 1. 绘制模型实体与线框
        if self.gpu_meshes:
            if self.wireframe_mode == 2:
                # 纯线框模式
                glPolygonMode(GL_FRONT_AND_BACK, GL_LINE)
                self._draw_meshes(eye, view, proj, model_mat=model_mat)
                glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
            else:
                # 实体着色
                glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
                self._draw_meshes(eye, view, proj, model_mat=model_mat)

                # Blender 风格实体+线框叠加
                if self.wireframe_mode == 1:
                    glEnable(GL_BLEND)
                    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
                    glEnable(GL_POLYGON_OFFSET_LINE)
                    glPolygonOffset(-1.0, -1.0)
                    glPolygonMode(GL_FRONT_AND_BACK, GL_LINE)
                    self._draw_meshes(eye, view, proj, render_mode_override=3, model_mat=model_mat)
                    glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
                    glDisable(GL_POLYGON_OFFSET_LINE)
                    glDisable(GL_BLEND)

        # 2. 绘制顶点法线向量（青色指示针，随轴向同步旋转）
        if self.show_normals and self.norm_vao is not None:
            glUseProgram(self.norm_prog)
            glUniformMatrix4fv(glGetUniformLocation(self.norm_prog, "uModel"), 1, GL_TRUE, model_mat)
            glUniformMatrix4fv(glGetUniformLocation(self.norm_prog, "uView"), 1, GL_TRUE, view)
            glUniformMatrix4fv(glGetUniformLocation(self.norm_prog, "uProj"), 1, GL_TRUE, proj)
            glBindVertexArray(self.norm_vao)
            glDrawArrays(GL_LINES, 0, self.norm_vertex_count)
            glBindVertexArray(0)

        # 2b. 骨骼 X-Ray（橙色父子连线，关闭深度测试）
        self._draw_skeleton(model_mat, view, proj)

        # 2c. 灯光 Gizmo（位置十字星 + 照射方向）
        self._draw_light_gizmos(model_mat, view, proj)

        # 3. 绘制 Blender 无限地面网格（开启 Alpha 混合但保持深度写入）
        if self.show_grid:
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glUseProgram(self.grid_prog)

            inv_vp = np.linalg.inv(proj @ view).astype(np.float32)
            glUniformMatrix4fv(glGetUniformLocation(self.grid_prog, "uInvViewProj"), 1, GL_TRUE, inv_vp)
            glUniformMatrix4fv(glGetUniformLocation(self.grid_prog, "uView"), 1, GL_TRUE, view)
            glUniformMatrix4fv(glGetUniformLocation(self.grid_prog, "uProj"), 1, GL_TRUE, proj)

            glBindVertexArray(self.dummy_vao)
            glDrawArrays(GL_TRIANGLES, 0, 6)
            glBindVertexArray(0)
            glDisable(GL_BLEND)

        # 3b. 真实比例 3D 包围盒（关闭深度测试，保证背面线框也可见）
        self._draw_bounding_box(model_mat, view, proj)

        # 3c. 两点测量卡尺
        self._draw_measurements(view, proj)

        if self._cam_animating:
            self.update()

    # --- 鼠标交互：左键轨道旋转 / 右键或中键屏幕空间平移 / 滚轮缩放 ---
    def mousePressEvent(self, event):
        self._cam_animating = False  # 手动操作立即接管相机
        self._last_pos = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton:
            self._left_press_pos = self._last_pos

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        dx = pos.x() - self._last_pos.x()
        dy = pos.y() - self._last_pos.y()
        self._last_pos = pos

        # 测量模式下左键点击只用于取点，不旋转相机
        if self.measure_mode and (event.buttons() & Qt.MouseButton.LeftButton):
            return

        if event.buttons() & Qt.MouseButton.LeftButton:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift+拖拽明确用于影棚布光；若当前是 FBX 场景灯光，先切回影棚模式
                if self.use_scene_lights:
                    self.use_scene_lights = False
                    parent = self.parent()
                    if parent is not None and hasattr(parent, "_flash_hud_note"):
                        parent._flash_hud_note("场景灯光已关闭，Shift+拖拽控制影棚布光（L 恢复）")
                self.light_yaw = (self.light_yaw + dx * 0.015) % (2.0 * np.pi)
            else:
                self.yaw -= dx * 0.45
                self.pitch = float(np.clip(self.pitch + dy * 0.45, -89.0, 89.0))
                self._sync_axis_gizmo()
            self.update()
        elif event.buttons() & (Qt.MouseButton.RightButton | Qt.MouseButton.MiddleButton):
            ry = np.radians(self.yaw)
            rx = np.radians(self.pitch)
            right = np.array([np.cos(ry), 0.0, -np.sin(ry)], dtype=np.float32)
            cam_up = np.array([
                -np.sin(rx) * np.sin(ry),
                np.cos(rx),
                -np.sin(rx) * np.cos(ry)
            ], dtype=np.float32)
            scale = self.distance * 0.0015
            self.target += (-right * dx + cam_up * dy) * scale
            self.update()

    def _add_measure_point(self, position: QPointF):
        hit = self.pick_surface_point(position)
        if hit is None:
            return False
        if len(self.measure_points) >= 2:
            self.measure_points = [hit]
        else:
            self.measure_points.append(hit)
        self.update()
        return True

    def mouseReleaseEvent(self, event):
        if (self.measure_mode
                and event.button() == Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._left_press_pos).manhattanLength() < 4):
            self._add_measure_point(event.position())
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.measure_mode:
            # 快速连点两下会被 Qt 识别为 double click；测量模式也照常取第二个点
            self._add_measure_point(event.position())
            return
        if event.button() == Qt.MouseButton.LeftButton and not self.measure_mode:
            hit = self.pick_surface_point(event.position())
            if hit is not None:
                # 双击表面：把相机注视点移动到该细节，并缩短轨道半径
                self.animate_camera_to(
                    target=hit, distance=max(self.distance * 0.55, 0.35))
            else:
                self.reset_camera(smooth=True)
            return
        event.ignore()

    def wheelEvent(self, event):
        self._cam_animating = False
        delta = event.angleDelta().y()
        factor = 0.88 if delta > 0 else 1.14
        self.distance = float(np.clip(self.distance * factor, 0.15, 35.0))
        self.update()


# ==============================================================================
# 子网格大纲面板（Outliner）
# ==============================================================================
class MeshOutlinerPanel(QFrame):
    """左侧半透明部件列表：勾选显隐，双击独显（Solo）。"""

    visibilityChanged = pyqtSignal()

    def __init__(self, gl_widget, parent=None):
        super().__init__(parent or gl_widget)
        self.gl = gl_widget
        self._solo_idx = None
        self.setFixedWidth(230)
        self.setMinimumHeight(110)
        self.setMaximumHeight(340)
        self.setStyleSheet("""
            QFrame {
                background-color: rgba(24, 26, 30, 215);
                border: 1px solid rgba(255, 255, 255, 28);
                border-radius: 10px;
            }
            QLabel {
                background: transparent; border: none;
                color: #D8DEE9; font-size: 12px; font-weight: bold;
            }
            QListWidget {
                background: transparent; border: none;
                color: #CFD6E2; font-size: 11px; outline: none;
            }
            QListWidget::item {
                padding: 4px 6px; border-radius: 5px;
            }
            QListWidget::item:hover {
                background-color: rgba(255, 255, 255, 18);
            }
            QListWidget::item:selected {
                background-color: rgba(68, 146, 245, 55);
                color: #FFFFFF;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.title_lbl = QLabel("网格部件 (0)")
        header.addWidget(self.title_lbl)

        btn_all = QPushButton("全显")
        btn_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_all.setToolTip("显示全部子网格")
        btn_all.setStyleSheet(
            "QPushButton{background:#32353E;color:#DCE1E8;border:none;"
            "border-radius:4px;padding:2px 8px;font-size:11px;}"
            "QPushButton:hover{background:#434754;}"
        )
        btn_all.clicked.connect(self.show_all)
        header.addWidget(btn_all)
        layout.addLayout(header)

        self.list_widget = QListWidget()
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.setToolTip("勾选显示/隐藏；双击只显示当前部件，再次双击恢复全部")
        self.list_widget.itemChanged.connect(self._on_item_changed)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.list_widget)
        self.hide()

    def rebuild(self):
        self._solo_idx = None
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for idx, m in enumerate(self.gl.gpu_meshes):
            name = m.get("name") or f"Mesh_{idx + 1}"
            # 有些 FBX 导出器给多个部件同名，附上序号避免大纲难以区分
            base_name = name
            for prev in range(idx):
                if (self.gl.gpu_meshes[prev].get("name") or f"Mesh_{prev + 1}") == base_name:
                    name = f"{base_name} #{idx + 1}"
                    break
            tris = int(m.get("tri_count", 0))
            item = QListWidgetItem(f"{name}  ({tris:,} 面)")
            item.setToolTip(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if m.get("visible", True) else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, idx)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        self.title_lbl.setText(f"网格部件 ({len(self.gl.gpu_meshes)})")
        self.title_lbl.setToolTip("勾选显隐；双击条目独显，再次双击恢复全部")
        self.adjustSize()

    def _on_item_changed(self, item: QListWidgetItem):
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is not None and 0 <= idx < len(self.gl.gpu_meshes):
            visible = item.checkState() == Qt.CheckState.Checked
            self.gl.gpu_meshes[idx]["visible"] = visible
            if not visible and self._solo_idx == idx:
                self._solo_idx = None
            self.gl.update()
            self.visibilityChanged.emit()

    def _on_item_double_clicked(self, item: QListWidgetItem):
        target_idx = item.data(Qt.ItemDataRole.UserRole)
        if target_idx is None:
            return
        if self._solo_idx == target_idx:
            self.show_all()
            return

        self._solo_idx = target_idx
        self.list_widget.blockSignals(True)
        for row in range(self.list_widget.count()):
            it = self.list_widget.item(row)
            idx = it.data(Qt.ItemDataRole.UserRole)
            visible = (idx == target_idx)
            it.setCheckState(Qt.CheckState.Checked if visible else Qt.CheckState.Unchecked)
            if idx is not None and 0 <= idx < len(self.gl.gpu_meshes):
                self.gl.gpu_meshes[idx]["visible"] = visible
        self.list_widget.blockSignals(False)
        self.gl.update()
        self.visibilityChanged.emit()

    def show_all(self):
        self._solo_idx = None
        self.list_widget.blockSignals(True)
        for row in range(self.list_widget.count()):
            it = self.list_widget.item(row)
            idx = it.data(Qt.ItemDataRole.UserRole)
            it.setCheckState(Qt.CheckState.Checked)
            if idx is not None and 0 <= idx < len(self.gl.gpu_meshes):
                self.gl.gpu_meshes[idx]["visible"] = True
        self.list_widget.blockSignals(False)
        self.gl.update()
        self.visibilityChanged.emit()


# ==============================================================================
# 主窗口与资产解析器
# ==============================================================================
class QuickModelViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Quick 3D Preview")
        self.resize(1120, 760)
        self.setAcceptDrops(True)

        self.gl_widget = ModelGLWidget(self)
        self.setCentralWidget(self.gl_widget)

        self.model_info_header = (
            "将 OBJ / STL / PLY / GLTF / GLB / 3MF / FBX 模型拖拽至此窗口\n"
            "鼠标：左键轨道旋转  |  右键/中键平移  |  滚轮缩放  |  双击重置视角"
        )

        # FBX 贴图解析缓存：(模型目录, 原始路径) -> PIL.Image 或 None
        self._fbx_tex_cache = {}
        self._fbx_controller = None
        self._fbx_embedded_tex_count = 0
        self._fbx_embedded_tex_decoded = 0
        self._fbx_embedded_records = None

        # 后台加载：请求版本号 + 单工作线程 + 只保留最新待处理请求
        self._load_req_id = 0
        self._active_worker = None
        self._pending_load = None

        # 同目录翻页状态
        self.folder_files = []
        self.current_file_idx = -1
        self.current_file_path = ""

        # HUD 临时提示（截图结果等），到时自动清除
        self._hud_note = ""
        self._hud_note_timer = QTimer(self)
        self._hud_note_timer.setSingleShot(True)
        self._hud_note_timer.timeout.connect(self._clear_hud_note)

        self.hud_label = QLabel(self.gl_widget)
        self.hud_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hud_label.setStyleSheet("""
            QLabel {
                background-color: rgba(20, 22, 26, 195);
                color: #E4E8EE;
                border: 1px solid rgba(255, 255, 255, 32);
                border-radius: 8px;
                padding: 10px 14px;
                font-family: "Helvetica Neue", "PingFang SC", sans-serif;
                font-size: 12px;
                line-height: 1.55;
            }
        """)
        self.hud_label.move(16, 16)

        # 3D 测量读数标签（作为 GL 视口的子控件，随相机投影位置移动）
        self.measure_tag = QLabel(self.gl_widget)
        self.measure_tag.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.measure_tag.setStyleSheet("""
            QLabel {
                background-color: rgba(18, 22, 28, 225);
                color: #5CE1FF;
                border: 1px solid rgba(92, 225, 255, 110);
                border-radius: 6px;
                padding: 3px 9px;
                font-size: 12px;
                font-weight: bold;
            }
        """)
        self.measure_tag.hide()

        # 外部修改热重载：文件/目录双监视 + 350ms 防抖
        self._preserve_camera_req = 0
        self._current_file_mtime = 0.0
        self._file_watcher = QFileSystemWatcher(self)
        self._file_watcher.fileChanged.connect(self._on_watched_file_changed)
        self._file_watcher.directoryChanged.connect(self._on_watched_file_changed)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(350)
        self._reload_timer.timeout.connect(self._trigger_hot_reload)

        # 顶部暗色菜单栏
        self._setup_menubar()

        # 右上角交互式 3D 坐标轴
        self.axis_gizmo = ViewportAxisGizmo(self.gl_widget)
        self.axis_gizmo.viewSelected.connect(self._on_gizmo_view_selected)
        self.axis_gizmo.raise_()

        # 底部悬浮材质球切换栏
        self.shading_dock = BottomShadingDock(self.gl_widget)
        self.shading_dock.presetTriggered.connect(self._on_shading_preset_selected)
        self.shading_dock.actionTriggered.connect(self._on_dock_action)
        self.shading_dock.raise_()

        # 左侧子网格大纲（默认隐藏，O 键 / 显示菜单切换）
        self.outliner_panel = MeshOutlinerPanel(self.gl_widget)
        self.outliner_panel.visibilityChanged.connect(self._refresh_hud)
        self.outliner_panel.raise_()

        # 动画时间轴（贴底悬浮，仅动画模型可见）
        self.timeline_bar = AnimTimelineBar(self.gl_widget)
        self.timeline_bar.raise_()
        self._layout_timeline()
        self._refresh_hud()

    def _layout_timeline(self):
        """布局视口浮层：坐标轴右上、材质球栏贴底居中、时间轴避让在球栏上方、大纲在 HUD 下方"""
        if not hasattr(self, "timeline_bar"):
            return
        w_gl = max(self.gl_widget.width(), 1)
        h_gl = max(self.gl_widget.height(), 1)

        if hasattr(self, "axis_gizmo"):
            self.axis_gizmo.move(max(w_gl - self.axis_gizmo.width() - 18, 0), 16)
            self.axis_gizmo.raise_()

        dock_h = 0
        if hasattr(self, "shading_dock"):
            self.shading_dock.adjustSize()
            dw, dh = self.shading_dock.width(), self.shading_dock.height()
            self.shading_dock.move(max((w_gl - dw) // 2, 0), max(h_gl - dh - 16, 0))
            self.shading_dock.raise_()
            dock_h = dh + 24

        if hasattr(self, "outliner_panel") and self.outliner_panel.isVisible():
            self.hud_label.adjustSize()
            self.outliner_panel.adjustSize()
            top = (self.hud_label.y() + self.hud_label.height() + 12
                   if self.hud_label.isVisible() else 16)
            panel_h = min(self.outliner_panel.height(), max(h_gl - dock_h - top - 24, 110))
            self.outliner_panel.resize(self.outliner_panel.width(), max(panel_h, 110))
            self.outliner_panel.move(16, top)
            self.outliner_panel.raise_()

        self.timeline_bar.setGeometry(
            16, max(h_gl - dock_h - 38, 0), max(w_gl - 32, 100), 34)
        if self.timeline_bar.isVisible():
            self.timeline_bar.raise_()

    def _setup_menubar(self):
        menubar = self.menuBar()
        menubar.setNativeMenuBar(False)
        menubar.setStyleSheet("""
            QMenuBar {
                background-color: #1E1E22;
                color: #DCE1E8;
                border-bottom: 1px solid #2F3138;
                padding: 2px 6px;
                font-family: "Helvetica Neue", "PingFang SC", sans-serif;
                font-size: 12px;
            }
            QMenuBar::item {
                background: transparent;
                padding: 5px 10px;
                border-radius: 4px;
            }
            QMenuBar::item:selected {
                background-color: #32343C;
            }
            QMenu {
                background-color: #27272B;
                color: #E4E8EE;
                border: 1px solid #3C3E47;
                border-radius: 6px;
                padding: 4px;
                font-size: 12px;
            }
            QMenu::item {
                padding: 6px 24px 6px 12px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #3B76D8;
                color: #FFFFFF;
            }
            QMenu::separator {
                height: 1px;
                background: #383A42;
                margin: 4px 8px;
            }
        """)

        def add_act(menu, text, slot, checkable=False, checked_fn=None):
            act = QAction(text, self)
            act.setCheckable(checkable)
            # QAction.triggered 会附带 bool；统一包一层，避免槽函数参数不匹配
            act.triggered.connect(lambda checked=False, fn=slot: fn())
            menu.addAction(act)
            if checkable and checked_fn is not None:
                menu.aboutToShow.connect(
                    lambda a=act, fn=checked_fn: a.setChecked(bool(fn())))
            return act

        # --- 文件 ---
        m_file = menubar.addMenu("文件")
        add_act(m_file, "打开模型...", self._open_file_dialog)
        m_file.addSeparator()
        add_act(m_file, "上一个模型  (←)", lambda: self.step_model(-1))
        add_act(m_file, "下一个模型  (→)", lambda: self.step_model(1))
        m_file.addSeparator()
        add_act(m_file, f"复制视口截图  ({COPY_KEY_HINT})",
                lambda: self.capture_viewport(False))
        add_act(m_file, "保存截图为 PNG...  (S)",
                lambda: self.capture_viewport(True))

        # --- 着色模式 ---
        m_shade = menubar.addMenu("着色")
        add_act(
            m_shade, "纹理材质  [1]",
            lambda: self._on_shading_preset_selected("texture"),
            True,
            lambda: (self.gl_widget.shading_mode == 0
                     and self.gl_widget.wireframe_mode == 0),
        )
        add_act(
            m_shade, "雕塑白模  [2]",
            lambda: self._on_shading_preset_selected("clay"),
            True,
            lambda: (self.gl_widget.shading_mode == 1
                     and self.gl_widget.wireframe_mode == 0),
        )
        add_act(
            m_shade, "线框加白模",
            lambda: self._on_shading_preset_selected("clay_wire"),
            True,
            lambda: (self.gl_widget.shading_mode == 1
                     and self.gl_widget.wireframe_mode == 1),
        )
        add_act(
            m_shade, "反照率 Albedo  [4]",
            lambda: self._on_shading_preset_selected("albedo"),
            True,
            lambda: (self.gl_widget.shading_mode == 4
                     and self.gl_widget.wireframe_mode == 0),
        )
        add_act(
            m_shade, "法线视图  [3]",
            lambda: self._on_shading_preset_selected("normal"),
            True,
            lambda: (self.gl_widget.shading_mode == 2
                     and self.gl_widget.wireframe_mode == 0),
        )
        add_act(
            m_shade, "MatCap 材质捕捉  [5]",
            self._set_matcap_mode,
            True,
            lambda: (self.gl_widget.shading_mode == 5
                     and self.gl_widget.wireframe_mode == 0),
        )
        m_mc = m_shade.addMenu("MatCap 预设")
        mc_group = QActionGroup(self)
        mc_group.setExclusive(True)
        for idx, mc_name in enumerate(("雕塑红蜡", "曲面斑马纹 (Zebra)", "液态铬金 (Chrome)")):
            mc_act = add_act(
                m_mc, mc_name,
                lambda checked=False, i=idx: self._set_matcap_preset(i),
                True,
                lambda i=idx: (self.gl_widget.shading_mode == 5
                               and self.gl_widget.matcap_idx == i),
            )
            mc_act.setActionGroup(mc_group)
        add_act(
            m_shade, "曲率 / 凹槽增强  (C)",
            self._toggle_cavity,
            True,
            lambda: self.gl_widget.cavity_enabled,
        )
        m_shade.addSeparator()
        add_act(
            m_shade, "循环切换线框模式  (W)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "wireframe_mode",
                (self.gl_widget.wireframe_mode + 1) % 3)),
        )
        add_act(
            m_shade, "UV 棋盘格诊断  (U)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "uv_checker", not self.gl_widget.uv_checker)),
            True,
            lambda: self.gl_widget.uv_checker,
        )
        add_act(
            m_shade, "面朝向正反检查  (B)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "check_backface",
                not self.gl_widget.check_backface)),
            True,
            lambda: self.gl_widget.check_backface,
        )

        # --- 视图与相机 ---
        m_view = menubar.addMenu("视图")
        add_act(m_view, "重置默认视角  (R)",
                lambda: self._exec_gl(self.gl_widget.reset_camera))
        lens_menu = m_view.addMenu("镜头焦距")
        lens_group = QActionGroup(self)
        lens_group.setExclusive(True)
        for mm, lens_name in (
            (24, "24mm 广角"),
            (35, "35mm 标准"),
            (50, "50mm 人像"),
            (85, "85mm 长焦无畸变"),
        ):
            lens_act = add_act(
                lens_menu, lens_name,
                lambda checked=False, focal=mm: self._set_focal_length(focal),
                True,
                lambda focal=mm: abs(self.gl_widget.focal_length_mm - focal) < 0.01,
            )
            lens_act.setActionGroup(lens_group)
        m_view.addSeparator()
        add_act(
            m_view, "正视图  (F1)",
            lambda: self._exec_gl(
                lambda: self.gl_widget.set_preset_view("front", False)),
        )
        add_act(
            m_view, "后视图  (Shift+F1)",
            lambda: self._exec_gl(
                lambda: self.gl_widget.set_preset_view("front", True)),
        )
        add_act(
            m_view, "右视图  (F2)",
            lambda: self._exec_gl(
                lambda: self.gl_widget.set_preset_view("right", False)),
        )
        add_act(
            m_view, "左视图  (Shift+F2)",
            lambda: self._exec_gl(
                lambda: self.gl_widget.set_preset_view("right", True)),
        )
        add_act(
            m_view, "顶视图  (F3)",
            lambda: self._exec_gl(
                lambda: self.gl_widget.set_preset_view("top", False)),
        )
        add_act(
            m_view, "底视图  (Shift+F3)",
            lambda: self._exec_gl(
                lambda: self.gl_widget.set_preset_view("top", True)),
        )
        m_view.addSeparator()
        add_act(
            m_view, "正交投影模式  (P)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "is_ortho", not self.gl_widget.is_ortho)),
            True,
            lambda: self.gl_widget.is_ortho,
        )
        add_act(
            m_view, "自动转台旋转  (T)",
            lambda: self._exec_gl(self.gl_widget.toggle_turntable),
            True,
            lambda: self.gl_widget.turntable,
        )
        add_act(
            m_view, "Z-Up 坐标系翻转  (Z)",
            lambda: self._exec_gl(self.gl_widget.toggle_z_up),
            True,
            lambda: self.gl_widget.z_up_mode,
        )

        # --- 显示与叠加层 ---
        m_disp = menubar.addMenu("显示")
        add_act(
            m_disp, "地面网格  (G)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "show_grid", not self.gl_widget.show_grid)),
            True,
            lambda: self.gl_widget.show_grid,
        )
        add_act(
            m_disp, "顶点法线向量  (N)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "show_normals", not self.gl_widget.show_normals)),
            True,
            lambda: self.gl_widget.show_normals,
        )
        add_act(
            m_disp, "骨骼 X-Ray  (K)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "show_skeleton", not self.gl_widget.show_skeleton)),
            True,
            lambda: self.gl_widget.show_skeleton,
        )
        add_act(
            m_disp, "FBX 场景灯光  (L)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "use_scene_lights",
                not self.gl_widget.use_scene_lights)),
            True,
            lambda: self.gl_widget.use_scene_lights,
        )
        add_act(
            m_disp, "左上角信息面板  (H)",
            lambda: self.hud_label.setVisible(self.hud_label.isHidden()),
            True,
            lambda: not self.hud_label.isHidden(),
        )
        add_act(
            m_disp, "子网格大纲  (O)",
            self.toggle_outliner,
            True,
            lambda: self.outliner_panel.isVisible(),
        )
        add_act(
            m_disp, "3D 包围盒  (X)",
            lambda: self._exec_gl(lambda: setattr(
                self.gl_widget, "show_bbox", not self.gl_widget.show_bbox)),
            True,
            lambda: self.gl_widget.show_bbox,
        )
        add_act(
            m_disp, "两点 3D 距离测量  (M)",
            self._toggle_measure_mode,
            True,
            lambda: self.gl_widget.measure_mode,
        )
        m_disp.addSeparator()

        m_bg = m_disp.addMenu("背景颜色")
        for idx, name in enumerate(["深灰 (默认)", "中性灰", "浅灰", "纯黑"]):
            add_act(
                m_bg, name,
                lambda checked=False, i=idx: self._exec_gl(
                    lambda: setattr(self.gl_widget, "bg_idx", i)),
                True,
                lambda i=idx: self.gl_widget.bg_idx == i,
            )

    def _exec_gl(self, fn):
        fn()
        self.gl_widget.update()
        if hasattr(self, "axis_gizmo"):
            self.axis_gizmo.update()
        self._refresh_hud()

    def _open_file_dialog(self):
        exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 3D 模型文件", "", f"3D 模型 ({exts})")
        if path:
            self.load_model(path)

    def _on_gizmo_view_selected(self, yaw: float, pitch: float):
        # 点击右上角坐标轴也走同一套阻尼过渡
        self.gl_widget.animate_camera_to(yaw, pitch)
        self.axis_gizmo.update()

    def toggle_outliner(self):
        self.outliner_panel.setVisible(self.outliner_panel.isHidden())
        self.outliner_panel.raise_()
        if self.outliner_panel.isVisible():
            self.outliner_panel.rebuild()
        self._layout_timeline()
        self._refresh_hud()

    def _on_shading_preset_selected(self, preset: str):
        self.gl_widget.apply_shading_preset(preset)
        self._refresh_hud()

    def _set_matcap_mode(self):
        self.gl_widget.shading_mode = 5
        self.gl_widget.wireframe_mode = 0
        self._exec_gl(lambda: None)

    def _set_matcap_preset(self, idx: int):
        self.gl_widget.matcap_idx = int(idx)
        self.gl_widget.shading_mode = 5
        self.gl_widget.wireframe_mode = 0
        self._exec_gl(lambda: None)
        names = ("红蜡", "曲面斑马纹", "液态铬金")
        self._flash_hud_note(f"MatCap：{names[idx % len(names)]}")

    def _set_focal_length(self, mm: float):
        self.gl_widget.set_focal_length(float(mm))
        self._exec_gl(lambda: None)
        self._flash_hud_note(f"镜头焦距：{int(mm)}mm")

    def _toggle_cavity(self):
        self.gl_widget.cavity_enabled = not self.gl_widget.cavity_enabled
        self._exec_gl(lambda: None)
        self._flash_hud_note(
            "曲率/凹槽增强：开" if self.gl_widget.cavity_enabled
            else "曲率/凹槽增强：关")

    def _toggle_measure_mode(self):
        self.gl_widget.measure_mode = not self.gl_widget.measure_mode
        if not self.gl_widget.measure_mode:
            self.gl_widget.measure_points.clear()
            self.measure_tag.hide()
        self._exec_gl(lambda: None)
        self._flash_hud_note(
            "3D 测量已开启：点击模型表面两点测距" if self.gl_widget.measure_mode
            else "3D 测量已关闭")

    def _on_dock_action(self, action: str):
        if action == "turntable":
            self.gl_widget.toggle_turntable()
        elif action == "reset":
            self.gl_widget.reset_camera()
            self.axis_gizmo.update()
        self._refresh_hud()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_timeline()

    def _refresh_hud(self):
        # HUD 只保留资产统计与临时提示；全部开关已收纳进顶部菜单栏。
        note_line = f"\n{self._hud_note}" if self._hud_note else ""
        self.hud_label.setText(f"{self.model_info_header}{note_line}")
        self.hud_label.adjustSize()

        if hasattr(self, "shading_dock"):
            self.shading_dock.sync_state(
                self.gl_widget.shading_mode,
                self.gl_widget.wireframe_mode,
                self.gl_widget.turntable,
            )

        ctrl = self.gl_widget.fbx_controller
        has_anim = bool(
            ctrl is not None
            and ctrl.animations
            and ctrl.duration(self.gl_widget.anim_idx) > 0
        )
        self.timeline_bar.setVisible(has_anim)
        self._layout_timeline()
        if has_anim:
            self.timeline_bar.raise_()
            self.timeline_bar.update()

    def _flash_hud_note(self, text: str):
        """在 HUD 上短暂显示一条结果提示，1.8 秒后自动消失"""
        self._hud_note = text
        self._refresh_hud()
        self._hud_note_timer.start(1800)

    def _clear_hud_note(self):
        if self._hud_note:
            self._hud_note = ""
            self._refresh_hud()

    def _update_folder_index(self, filepath: str):
        """扫描模型所在目录内所有支持格式，供 ← / → 连续翻页"""
        abs_path = os.path.abspath(filepath)
        folder = os.path.dirname(abs_path)
        try:
            files = sorted(
                os.path.join(folder, f) for f in os.listdir(folder)
                if os.path.splitext(f)[1].lower() in SUPPORTED_EXTS
            )
            self.folder_files = files
            if abs_path in files:
                self.current_file_idx = files.index(abs_path)
            else:
                # 大小写或路径写法不一致时按文件名兜底匹配
                base = os.path.basename(abs_path).casefold()
                match = next((f for f in files if os.path.basename(f).casefold() == base), None)
                self.current_file_idx = files.index(match) if match else -1
        except Exception:
            self.folder_files = [abs_path]
            self.current_file_idx = 0

    def _clear_file_watcher(self):
        """切换模型 / 开始热重载前，先移除旧文件与旧目录，避免事件串台。"""
        if not hasattr(self, "_file_watcher"):
            return
        try:
            files = self._file_watcher.files()
            if files:
                self._file_watcher.removePaths(files)
            dirs = self._file_watcher.directories()
            if dirs:
                self._file_watcher.removePaths(dirs)
        except Exception:
            pass
        self._current_file_mtime = 0.0

    def _watch_current_file(self, filepath: str):
        """监视当前模型文件与其目录；目录事件用于捕获 Blender 的原子替换保存。"""
        self._clear_file_watcher()
        abs_path = os.path.abspath(filepath)
        if not os.path.isfile(abs_path):
            return
        try:
            self._current_file_mtime = float(os.path.getmtime(abs_path))
        except OSError:
            self._current_file_mtime = 0.0
        self._file_watcher.addPath(abs_path)
        folder = os.path.dirname(abs_path)
        if os.path.isdir(folder):
            self._file_watcher.addPath(folder)

    def _on_watched_file_changed(self, _path: str):
        """文件或目录变动时先比对当前文件 mtime，过滤同目录无关文件事件。"""
        path = self.current_file_path
        if not path or not os.path.isfile(path):
            return
        try:
            mtime = float(os.path.getmtime(path))
        except OSError:
            return
        if self._current_file_mtime and abs(mtime - self._current_file_mtime) < 1e-5:
            return
        self._reload_timer.start()

    def _trigger_hot_reload(self):
        path = self.current_file_path
        if not path or not os.path.isfile(path):
            return
        try:
            self._current_file_mtime = float(os.path.getmtime(path))
        except OSError:
            pass
        self.load_model(path, preserve_camera=True)
        self._flash_hud_note("检测到文件已更新，正在后台热重载")

    def step_model(self, delta: int):
        if len(self.folder_files) <= 1 or self.current_file_idx < 0:
            return
        next_idx = (self.current_file_idx + delta) % len(self.folder_files)
        self.load_model(self.folder_files[next_idx])

    def capture_viewport(self, save_file: bool = False):
        """临时隐藏 HUD，抓取高清抗锯齿帧缓冲：⌘C/Ctrl+C 进剪贴板，S 存 PNG"""
        hud_was_visible = not self.hud_label.isHidden()
        self.hud_label.hide()
        try:
            self.gl_widget.repaint()
            img = self.gl_widget.grabFramebuffer()
        finally:
            if hud_was_visible:
                self.hud_label.show()

        if save_file:
            base = os.path.splitext(os.path.basename(self.current_file_path or "preview"))[0]
            default_dir = os.path.dirname(self.current_file_path) if self.current_file_path else ""
            default_name = os.path.join(default_dir, f"{base}_preview.png")
            save_path, _ = QFileDialog.getSaveFileName(
                self, "保存视口截图", default_name, "PNG 图片 (*.png)"
            )
            if save_path:
                try:
                    if not img.save(save_path, "PNG"):
                        raise OSError("无法写入 PNG 图片")
                    self._flash_hud_note(f"截图已保存：{os.path.basename(save_path)}")
                    if hasattr(self, "_notify_export"):
                        self._notify_export(save_path)
                except Exception as e:
                    self._flash_hud_note(f"截图保存失败：{e}")
        else:
            QApplication.clipboard().setImage(img)
            self._flash_hud_note(f"已复制 {img.width()}×{img.height()} 截图到剪贴板")

    def _extract_material_visuals(self, mesh: trimesh.Trimesh, num_verts: int):
        """提取子网格的 UV 坐标、漫反射/PBR贴图、法线贴图、基础色与顶点颜色"""
        uvs = np.zeros((num_verts, 2), dtype=np.float32)
        colors = np.ones((num_verts, 4), dtype=np.float32)
        texture_img = None
        normal_img = None
        base_color = (0.85, 0.86, 0.88)

        visual = getattr(mesh, "visual", None)
        if visual is None:
            return uvs, colors, texture_img, normal_img, base_color

        kind = getattr(visual, "kind", None)
        if kind == "texture":
            if getattr(visual, "uv", None) is not None and len(visual.uv) == num_verts:
                uvs = np.asarray(visual.uv, dtype=np.float32)

            mat = getattr(visual, "material", None)
            if mat is not None:
                # 兼容 PBRMaterial 与 SimpleMaterial
                img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
                if isinstance(img, Image.Image):
                    texture_img = img

                # glTF / GLB / 部分 OBJ 的法线贴图
                n_img = getattr(mat, "normalTexture", None)
                if isinstance(n_img, Image.Image):
                    normal_img = n_img

                factor = getattr(mat, "baseColorFactor", None)
                if factor is None:
                    factor = getattr(mat, "diffuse", None)
                if factor is not None and len(factor) >= 3:
                    arr = np.asarray(factor[:3], dtype=np.float32)
                    if arr.max() > 1.0:
                        arr = arr / 255.0
                    # 将 sRGB 材质因子转换到线性空间
                    base_color = tuple(float(np.clip(c, 0.0, 1.0) ** 2.2) for c in arr)

        elif kind in ("vertex", "face"):
            color_vis = visual.to_color() if kind == "face" else visual
            vcols = getattr(color_vis, "vertex_colors", None)
            if vcols is not None and len(vcols) == num_verts:
                arr = np.asarray(vcols, dtype=np.float32) / 255.0
                if arr.shape[1] == 3:
                    alpha = np.ones((num_verts, 1), dtype=np.float32)
                    arr = np.hstack([arr, alpha])
                # sRGB 顶点色转线性空间
                arr[:, :3] = np.power(np.clip(arr[:, :3], 0.0, 1.0), 2.2)
                colors = arr

        return uvs, colors, texture_img, normal_img, base_color

    def _resolve_fbx_texture(self, fbx_filepath: str, raw_tex_path):
        """解析 FBX 贴图路径（兼容艺术家残留的 Windows 绝对路径、相对子目录与大小写差异）"""
        if not raw_tex_path:
            return None

        raw = str(raw_tex_path).strip().strip('"')
        if not raw or raw.startswith("*"):
            # "*0" 是 Assimp 的内嵌贴图索引，当前绑定不暴露纹理数据，退回基础色渲染
            return None

        fbx_dir = os.path.dirname(os.path.abspath(fbx_filepath))
        cache_key = (fbx_dir, raw)
        if cache_key in self._fbx_tex_cache:
            return self._fbx_tex_cache[cache_key]

        clean_rel = raw.replace("file:///", "").replace("file://", "").replace("\\", "/")
        fname = os.path.basename(clean_rel)

        candidates = [
            clean_rel,
            os.path.join(fbx_dir, clean_rel),
            os.path.join(fbx_dir, fname),
            os.path.join(fbx_dir, "textures", fname),
            os.path.join(fbx_dir, "Textures", fname),
            os.path.join(fbx_dir, "maps", fname),
            os.path.join(fbx_dir, "Maps", fname),
        ]

        img = None
        for path in candidates:
            if os.path.isfile(path):
                try:
                    img = Image.open(path)
                    img.load()  # 立即读入像素，避免文件句柄悬挂
                    break
                except Exception:
                    img = None

        if img is None and fname:
            # 兜底：在模型目录与常见贴图子目录中做一次不区分大小写的文件名匹配
            for folder in (fbx_dir, os.path.join(fbx_dir, "textures"), os.path.join(fbx_dir, "Textures")):
                if not os.path.isdir(folder):
                    continue
                try:
                    names = os.listdir(folder)
                except OSError:
                    continue
                match = next((n for n in names if n.casefold() == fname.casefold()), None)
                if match:
                    try:
                        img = Image.open(os.path.join(folder, match))
                        img.load()
                        break
                    except Exception:
                        img = None

        self._fbx_tex_cache[cache_key] = img
        return img

    @staticmethod
    def _extract_skin_weights(mesh, num_verts: int):
        """把逐骨骼的 (vertex_id, weight) 合并为每顶点 top4 骨骼 ID / 权重（行归一化）"""
        bones = getattr(mesh, "bones", None) or []
        vids_list, bids_list, wts_list = [], [], []
        for bi, b in enumerate(bones[:FBXSceneController.MAX_BONES]):
            vids = getattr(b, "weight_vertex_ids", None)
            ws = getattr(b, "weights", None)
            if vids is None or ws is None:
                continue
            try:
                va = np.asarray(vids, dtype=np.int64).reshape(-1)
                wa = np.asarray(ws, dtype=np.float32).reshape(-1)
            except (TypeError, ValueError):
                continue
            n = min(len(va), len(wa))
            if n == 0:
                continue
            vids_list.append(va[:n])
            bids_list.append(np.full(n, bi, dtype=np.int32))
            wts_list.append(wa[:n])
        if not vids_list:
            return None, None

        v = np.concatenate(vids_list)
        b = np.concatenate(bids_list)
        w = np.concatenate(wts_list)
        valid = (v >= 0) & (v < num_verts) & (w > 0.0)
        v, b, w = v[valid], b[valid], w[valid]
        if len(v) == 0:
            return None, None

        # 每顶点按权重降序取前 4，再按行归一化（Assimp 原始权重和常为 0.88~1.38）
        order = np.lexsort((-w, v))
        v, b, w = v[order], b[order], w[order]
        first = np.zeros(len(v), dtype=bool)
        first[1:] = v[1:] != v[:-1]
        group_start = np.maximum.accumulate(np.where(first, np.arange(len(v)), 0))
        rank = np.arange(len(v)) - group_start
        take = rank < 4

        ids = np.zeros((num_verts, 4), dtype=np.int32)
        weights = np.zeros((num_verts, 4), dtype=np.float32)
        ids[v[take], rank[take]] = b[take]
        weights[v[take], rank[take]] = w[take]

        row = weights.sum(axis=1)
        row[row < 1e-6] = 1.0
        weights /= row[:, None]
        return ids, weights

    def _load_fbx_parts(self, filepath: str, should_cancel=None):
        """使用 Assimp 解析 FBX，并输出与现有 trimesh 管线一致的子网格数据结构。

        返回 (parts, controller, embedded_count, embedded_decoded)。
        该函数只产生 Python/NumPy/PIL 数据，不碰 Qt 控件与 OpenGL，可在后台线程执行。
        """
        if assimp_py is None:
            raise RuntimeError("缺少 assimp-py 依赖，请先执行：pip install assimp-py")
        if should_cancel is not None and should_cancel():
            raise ModelLoadCancelled()

        # assimp_py 1.2 未绑定 Scene.lights，灯光参数另走内置二进制解析器
        raw_lights = parse_fbx_lights(filepath)
        controller = None
        embedded_tex_count = 0
        embedded_tex_decoded = 0

        base_flags = (
            assimp_py.Process_Triangulate
            | assimp_py.Process_GenSmoothNormals
            | assimp_py.Process_JoinIdenticalVertices
        )
        # 先探测：含动画 / 骨骼蒙皮 / 灯光的 FBX 必须保留节点层级
        scene = assimp_py.import_file(filepath, base_flags)
        if should_cancel is not None and should_cancel():
            raise ModelLoadCancelled()
        has_anim = bool(getattr(scene, "animations", None))
        has_bones = any((getattr(m, "bones", None) or []) for m in (getattr(scene, "meshes", None) or []))
        dynamic = bool(has_anim or has_bones or raw_lights)

        if dynamic:
            controller = FBXSceneController(scene, raw_lights)
            for mid, m in enumerate(getattr(scene, "meshes", None) or []):
                mesh_bones = getattr(m, "bones", None) or []
                if mesh_bones:
                    controller.mesh_bones[mid] = [
                        (b.name, np.asarray(b.offset_matrix, dtype=np.float32).reshape(4, 4))
                        for b in mesh_bones[:controller.MAX_BONES]
                    ]
        else:
            # 纯静态 FBX 仍用 PreTransformVertices，换取最快加载与最简渲染路径
            # 先释放探测场景：否则第二次导入时两份百万级场景会同时驻留，内存峰值翻倍
            del scene
            scene = assimp_py.import_file(
                filepath, base_flags | assimp_py.Process_PreTransformVertices)
            if should_cancel is not None and should_cancel():
                raise ModelLoadCancelled()

        materials = getattr(scene, "materials", None) or []
        parts = []
        self._fbx_embedded_records = None

        def embedded_records():
            if self._fbx_embedded_records is None:
                self._fbx_embedded_records = parse_fbx_embedded_texture_records(filepath)
            return self._fbx_embedded_records

        for mid, m in enumerate(getattr(scene, "meshes", None) or []):
            if should_cancel is not None and should_cancel():
                raise ModelLoadCancelled()
            # Assimp 的顶点/法线/UV/顶点色都是扁平内存视图（每顶点 3/3/2/4 个 float），必须 reshape
            verts = np.array(m.vertices, dtype=np.float32, copy=True).reshape(-1, 3)
            num_verts = len(verts)
            if num_verts == 0:
                continue

            raw_idx = np.array(m.indices, dtype=np.uint32, copy=True).reshape(-1)
            if raw_idx.size == 0 or raw_idx.size % 3 != 0:
                continue
            faces = raw_idx.reshape(-1, 3)

            # 法线提取（若缺失则借用 trimesh 自动计算平滑法线）
            mv_normals = getattr(m, "normals", None)
            if mv_normals is not None and len(mv_normals) == num_verts * 3:
                normals = np.array(mv_normals, dtype=np.float32, copy=True).reshape(-1, 3)
            else:
                tmp = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
                normals = np.asarray(tmp.vertex_normals, dtype=np.float32)

            # UV 坐标（保持 Assimp 的 OpenGL 原点，不翻转 V，与现有 _upload_texture 的贴图翻转保持一致）
            uvs = np.zeros((num_verts, 2), dtype=np.float32)
            tex_sets = getattr(m, "texcoords", None)
            if tex_sets is not None:
                if not isinstance(tex_sets, (list, tuple)):
                    tex_sets = [tex_sets]
                for tc in tex_sets:
                    if tc is None:
                        continue
                    arr = np.array(tc, dtype=np.float32, copy=True).reshape(-1)
                    if arr.size == num_verts * 2:
                        uvs = arr.reshape(-1, 2)
                        break
                    if arr.size == num_verts * 3:   # 偶有三通道 UV
                        uvs = arr.reshape(-1, 3)[:, :2]
                        break

            # 顶点颜色（sRGB -> 线性空间）
            vcolors = np.ones((num_verts, 4), dtype=np.float32)
            color_sets = getattr(m, "colors", None)
            if color_sets is not None:
                if not isinstance(color_sets, (list, tuple)):
                    color_sets = [color_sets]
                for vc in color_sets:
                    if vc is None:
                        continue
                    arr = np.array(vc, dtype=np.float32, copy=True).reshape(-1)
                    if arr.size == num_verts * 4:
                        arr = arr.reshape(-1, 4)
                    elif arr.size == num_verts * 3:
                        arr = np.hstack([arr.reshape(-1, 3), np.ones((num_verts, 1), dtype=np.float32)])
                    else:
                        continue
                    if arr.max(initial=0.0) > 1.0:   # 少数管线写 0-255 的整数色
                        arr[:, :3] = arr[:, :3] / 255.0
                    arr[:, :3] = np.power(np.clip(arr[:, :3], 0.0, 1.0), 2.2)
                    vcolors = arr
                    break

            # 材质基础色与漫反射 / PBR 贴图
            base_color = (0.85, 0.86, 0.88)
            tex_img = None
            normal_img = None
            mat_idx = getattr(m, "material_index", -1)
            mat_idx = int(mat_idx) if mat_idx is not None else -1
            if 0 <= mat_idx < len(materials):
                mat = materials[mat_idx] or {}
                diff = mat.get("COLOR_DIFFUSE")
                if diff is None:
                    diff = mat.get("COLOR_BASE")
                if diff is not None and len(diff) >= 3:
                    base_color = tuple(float(np.clip(c, 0.0, 1.0) ** 2.2) for c in diff[:3])

                textures = mat.get("TEXTURES") or {}
                # 1 = TextureType_DIFFUSE, 12 = TextureType_BASE_COLOR（PBR 导出的 FBX 多走 12）
                tex_list = textures.get(1) or textures.get(12)
                if tex_list:
                    tex_ref = str(tex_list[0]).strip()
                    if tex_ref.startswith("*"):
                        try:
                            idx = int(tex_ref[1:])
                            recs = embedded_records()
                            tex_img = (decode_fbx_embedded_record(recs[idx])
                                       if 0 <= idx < len(recs) else None)
                        except ValueError:
                            tex_img = None
                        if tex_img is None:
                            embedded_tex_count += 1
                        else:
                            embedded_tex_decoded += 1
                    else:
                        tex_img = self._resolve_fbx_texture(filepath, tex_ref)
                        if tex_img is None:
                            # 外部路径失效时，用 Video 的 RelativeFilename 兜底匹配内嵌图
                            base = os.path.basename(str(tex_ref).replace("\\", "/")).casefold()
                            for rec in embedded_records():
                                rec_base = os.path.basename((rec.get("name") or "").replace("\\", "/")).casefold()
                                if rec_base and rec_base == base:
                                    rec_img = decode_fbx_embedded_record(rec)
                                    if rec_img is not None:
                                        tex_img = rec_img
                                        embedded_tex_decoded += 1
                                        break

                # 6 = TextureType_NORMALS；少数导出器写在 5
                normal_img = None
                norm_list = textures.get(6) or textures.get(5)
                if norm_list:
                    norm_ref = str(norm_list[0]).strip()
                    if norm_ref.startswith("*"):
                        try:
                            n_idx = int(norm_ref[1:])
                            recs = embedded_records()
                            if 0 <= n_idx < len(recs):
                                normal_img = decode_fbx_embedded_record(recs[n_idx])
                        except ValueError:
                            normal_img = None
                        if normal_img is None:
                            embedded_tex_count += 1
                        else:
                            embedded_tex_decoded += 1
                    else:
                        normal_img = self._resolve_fbx_texture(filepath, norm_ref)
                        if normal_img is None:
                            # 法线贴图外链失效时同样按文件名匹配内嵌记录
                            base = os.path.basename(str(norm_ref).replace("\\", "/")).casefold()
                            for rec in embedded_records():
                                rec_base = os.path.basename((rec.get("name") or "").replace("\\", "/")).casefold()
                                if rec_base and rec_base == base:
                                    rec_img = decode_fbx_embedded_record(rec)
                                    if rec_img is not None:
                                        normal_img = rec_img
                                        embedded_tex_decoded += 1
                                        break

            # 骨骼蒙皮权重（仅动态路径；蒙皮网格不允许再叠加节点变换）
            bone_ids, bone_weights = (None, None)
            if controller is not None:
                bone_ids, bone_weights = self._extract_skin_weights(m, num_verts)

            mesh_name = getattr(m, "name", "") or f"Mesh_{mid + 1}"
            parts.append({
                "vertices": verts,
                "normals": normals,
                "faces": faces,
                "uvs": uvs,
                "vcolors": vcolors,
                "texture": tex_img,
                "normal_texture": normal_img,
                "base_color": base_color,
                "mesh_index": mid,
                "name": mesh_name,
                "bone_ids": bone_ids,
                "bone_weights": bone_weights,
            })

        # 所有材质已处理完，释放未被采用的内嵌贴图压缩字节
        if self._fbx_embedded_records:
            self._fbx_embedded_records = []
        return parts, controller, embedded_tex_count, embedded_tex_decoded

    def _parse_model_data(self, filepath: str, should_cancel=None):
        """纯 CPU 解析：磁盘 IO、Assimp/Trimesh、归一化与 VBO 数组整理。

        不修改 Qt 控件，不创建 OpenGL 资源，因此可以在 QThread 中安全执行。
        """
        def check_cancel():
            if should_cancel is not None and should_cancel():
                raise ModelLoadCancelled()

        check_cancel()
        ext = os.path.splitext(filepath)[1].lower()
        parts = []
        controller = None
        dynamic = False
        embedded_count = 0
        embedded_decoded = 0

        if ext == ".fbx":
            # FBX 走 Assimp：静态模型烘焙节点层级，含动画/骨骼/灯光则保留层级实时求值
            parts, controller, embedded_count, embedded_decoded = self._load_fbx_parts(
                filepath, should_cancel)
            dynamic = controller is not None
        else:
            loaded = trimesh.load(filepath)
            check_cancel()
            if isinstance(loaded, trimesh.Scene):
                raw_meshes = [
                    g for g in loaded.dump(concatenate=False)
                    if isinstance(g, trimesh.Trimesh) and len(g.vertices) > 0
                ]
            elif isinstance(loaded, trimesh.Trimesh):
                raw_meshes = [loaded] if len(loaded.vertices) > 0 else []
            else:
                raw_meshes = []

            for idx, m in enumerate(raw_meshes):
                check_cancel()
                num_v = len(m.vertices)
                uvs, vcolors, tex_img, normal_img, base_color = self._extract_material_visuals(
                    m, num_v)
                parts.append({
                    "vertices": np.asarray(m.vertices, dtype=np.float32),
                    "normals": np.asarray(m.vertex_normals, dtype=np.float32),
                    "faces": np.asarray(m.faces, dtype=np.uint32),
                    "uvs": uvs,
                    "vcolors": vcolors,
                    "texture": tex_img,
                    "normal_texture": normal_img,
                    "base_color": base_color,
                    "mesh_index": idx,
                    "name": getattr(m, "name", "") or f"Mesh_{idx + 1}",
                    "bone_ids": None,
                    "bone_weights": None,
                })

        if not parts:
            raise RuntimeError(f"模型不包含有效几何体 ({os.path.basename(filepath)})")

        check_cancel()
        # 以下归一化、法线生成与 VBO 数组整理对所有格式统一处理
        if dynamic:
            # 动态场景用 rest pose 世界坐标算包围盒：
            # 蒙皮网格取骨骼结果，非蒙皮网格走节点世界矩阵
            states, _ = controller.evaluate(0, 0.0)
            rest_sets = []
            for p in parts:
                check_cancel()
                state = states.get(p.get("mesh_index", -1))
                node_mat, bone_mats = state if state is not None else (np.eye(4, dtype=np.float32), None)
                if bone_mats is not None and p.get("bone_ids") is not None:
                    rest_sets.append(skin_vertex_positions(
                        p["vertices"], p["bone_ids"], p["bone_weights"], bone_mats))
                else:
                    verts = p["vertices"]
                    h = np.concatenate([verts, np.ones((len(verts), 1), dtype=np.float32)], axis=1)
                    rest_sets.append((node_mat @ h.T).T[:, :3])
            all_verts = np.vstack(rest_sets).astype(np.float32)
        else:
            all_verts = np.vstack([p["vertices"] for p in parts])
        b_min = all_verts.min(axis=0)
        b_max = all_verts.max(axis=0)
        size = b_max - b_min
        max_extent = float(np.max(size))
        scale = 2.0 / max_extent if max_extent > 0 else 1.0

        offset = np.array([
            (b_min[0] + b_max[0]) * 0.5,
            b_min[1],
            (b_min[2] + b_max[2]) * 0.5
        ], dtype=np.float32)

        # 归一化矩阵：交给 GPU 与 FBX 节点/骨骼矩阵逐帧相乘
        norm_matrix = np.eye(4, dtype=np.float32)
        if dynamic:
            norm_matrix[0, 0] = norm_matrix[1, 1] = norm_matrix[2, 2] = scale
            norm_matrix[0, 3] = -offset[0] * scale
            norm_matrix[1, 3] = -offset[1] * scale
            norm_matrix[2, 3] = -offset[2] * scale
            controller.norm_matrix = norm_matrix

            # 预览补偿：FBX 常用米制灯光放在很远的位置，归一化到 2 单位后
            # 平方衰减会把画面压黑；按"灯到模型中心距离"给点光/聚光做温和增益
            if controller.raw_lights:
                _, evaluated = controller.evaluate(0, 0.0)
                center = np.array([0.0, float((size[1] * scale) * 0.5), 0.0], dtype=np.float32)
                for raw, lt in zip(controller.raw_lights, evaluated):
                    if lt["type"] == 1:
                        continue
                    dist = float(np.linalg.norm(lt["pos"] - center))
                    boost = min(2.5, max(1.0, (dist / 2.0) ** 2))
                    raw["color"] = (raw["color"] * boost).astype(np.float32)

        submeshes_data = []
        normal_segments = []
        total_verts = 0
        total_faces = 0
        tex_count = 0
        normal_tex_count = 0
        has_vcolor = False

        for p in parts:
            check_cancel()
            if dynamic:
                # 顶点保持局部坐标，节点矩阵 / 骨骼矩阵在 GPU 上逐帧求值
                verts = np.ascontiguousarray(p["vertices"], dtype=np.float32)
            else:
                verts = ((p["vertices"] - offset) * scale).astype(np.float32)
            normals = p["normals"]
            faces_flat = p["faces"].flatten()
            uvs = p["uvs"]
            vcolors = p["vcolors"]
            tex_img = p["texture"]
            normal_img = p.get("normal_texture")
            base_color = p["base_color"]

            if tex_img is not None:
                tex_count += 1
            if normal_img is not None:
                normal_tex_count += 1
            if not np.allclose(vcolors[:, :3], 1.0):
                has_vcolor = True

            interleaved = np.hstack([verts, normals, uvs, vcolors]).astype(np.float32)
            submeshes_data.append({
                "interleaved": interleaved,
                "indices": faces_flat,
                "texture": tex_img,
                "normal_texture": normal_img,
                "base_color": base_color,
                "mesh_index": int(p.get("mesh_index", -1)),
                "name": p.get("name") or f"Mesh_{len(submeshes_data) + 1}",
                "bone_ids": p.get("bone_ids"),
                "bone_weights": p.get("bone_weights"),
            })

            total_verts += len(verts)
            total_faces += len(p["faces"])

            # 生成顶点法线可视化线段（动态蒙皮场景无法静态跟随，跳过）
            if not dynamic:
                step = max(1, len(verts) // 25000)
                v_sub = verts[::step]
                n_sub = normals[::step]
                tips = v_sub + n_sub * 0.045
                segs = np.empty((len(v_sub) * 2, 3), dtype=np.float32)
                segs[0::2] = v_sub
                segs[1::2] = tips
                normal_segments.append(segs)

        all_normal_lines = np.vstack(normal_segments) if normal_segments else np.zeros((0, 3), dtype=np.float32)
        norm_size = (size * scale).astype(np.float32)

        # 组装资产状态标签
        asset_tags = []
        if tex_count > 0:
            asset_tags.append(f"贴图 ({tex_count})")
        if normal_tex_count > 0:
            asset_tags.append(f"法线贴图 ({normal_tex_count})")
        if has_vcolor:
            asset_tags.append("顶点色")
        if ext == ".fbx" and embedded_decoded > 0:
            asset_tags.append(f"内嵌贴图 {embedded_decoded}")
        if ext == ".fbx" and embedded_count > 0:
            asset_tags.append(f"内嵌贴图 {embedded_count} 解码失败")
        if dynamic:
            if controller.animations:
                asset_tags.append(f"动画 {len(controller.animations)} 段")
            if controller.mesh_bones:
                asset_tags.append(f"骨骼 {sum(len(v) for v in controller.mesh_bones.values())}")
            if controller.raw_lights:
                asset_tags.append(f"场景灯光 {len(controller.raw_lights)}")
        if not asset_tags:
            asset_tags.append("无贴图/白模")

        return {
            "submeshes": submeshes_data,
            "normal_lines": all_normal_lines,
            "norm_size": norm_size,
            "fbx_controller": controller if dynamic else None,
            "asset_tags": asset_tags,
            "parts_count": len(parts),
            "total_verts": total_verts,
            "total_faces": total_faces,
            "size": size.astype(np.float32),
            "scale": float(scale),
            "filename": os.path.basename(filepath),
            "embedded_count": embedded_count,
            "embedded_decoded": embedded_decoded,
        }

    def load_model(self, filepath: str, preserve_camera: bool = False):
        """主线程调度入口：只负责 UI 状态与版本号，解析放到后台线程。"""
        if not filepath:
            return
        filepath = os.path.abspath(str(filepath))
        if not os.path.isfile(filepath):
            self.model_info_header = f"加载失败：文件不存在\n{filepath}"
            self._refresh_hud()
            return

        self._load_req_id += 1
        req_id = self._load_req_id
        self._preserve_camera_req = req_id if preserve_camera else 0
        self.current_file_path = filepath
        self._clear_file_watcher()
        # 先刷新目录索引，即便当前模型解析失败，← / → 翻页仍然可用
        self._update_folder_index(filepath)

        self.model_info_header = (
            f"正在后台加载：{os.path.basename(filepath)}\n"
            "网格解析中（界面可继续操作；←/→ 切换会自动丢弃旧任务）"
        )
        self._refresh_hud()

        if self._active_worker is not None:
            # 只保留最新的请求；旧任务在网格循环里检查取消标志后尽快退出
            self._pending_load = (filepath, req_id, preserve_camera)
            self._active_worker.request_cancel()
            return

        self._start_load_worker(filepath, req_id)

    def _start_load_worker(self, filepath: str, req_id: int):
        if self._active_worker is not None:
            return
        # .fbx 的 assimp 原生导入会长时间持有 GIL，放进子进程；其余格式用 QThread
        use_process = os.path.splitext(filepath)[1].lower() == ".fbx"
        worker = ModelLoadWorker(
            req_id, filepath, self._parse_model_data, self,
            use_process=use_process,
            project_dir=os.path.dirname(os.path.abspath(__file__)),
        )
        worker.resultReady.connect(self._on_model_loaded)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.finished.connect(worker.deleteLater)
        self._active_worker = worker
        worker.start()

    def _on_worker_finished(self, worker):
        if self._active_worker is worker:
            # 正常结果会先走 resultReady；这里兜底处理“没有结果就退出”的情况
            if not worker.result_emitted:
                self._active_worker = None
                if worker.req_id == self._load_req_id and not worker.is_cancelled():
                    self.model_info_header = (
                        f"解析出错：后台线程未返回结果 ({os.path.basename(worker.filepath)})"
                    )
                    self._refresh_hud()
                self._start_pending_load()

    def _on_model_loaded(self, req_id: int, filepath: str, result, error: str):
        worker = self._active_worker
        if worker is not None and worker.req_id == req_id:
            self._active_worker = None

        # 快速翻页时旧任务已经过期，直接丢弃
        if req_id != self._load_req_id:
            self._start_pending_load()
            return

        if error or not result:
            self.gl_widget.set_fbx_scene(None)
            self._fbx_controller = None
            self._preserve_camera_req = 0
            self.model_info_header = f"解析出错：{error or '无效几何体'} ({os.path.basename(filepath)})"
            self._refresh_hud()
            self._start_pending_load()
            return

        preserve_camera = (req_id == self._preserve_camera_req)
        self.gl_widget.set_scene_data(
            result["submeshes"], result["normal_lines"], result["norm_size"],
            model_scale=result.get("scale", 1.0),
            reset_camera=not preserve_camera,
        )
        self.gl_widget.set_fbx_scene(result["fbx_controller"])
        self._fbx_controller = result["fbx_controller"]
        self._fbx_embedded_tex_count = result.get("embedded_count", 0)
        self._fbx_embedded_tex_decoded = result.get("embedded_decoded", 0)
        self._preserve_camera_req = 0
        self._watch_current_file(filepath)

        filename = result["filename"]
        idx_tag = (
            f"[{self.current_file_idx + 1}/{len(self.folder_files)}] "
            if self.folder_files and self.current_file_idx >= 0 else ""
        )
        self.setWindowTitle(f"{idx_tag}{filename} - Quick 3D Preview")
        size = result["size"]
        self.model_info_header = (
            f"文件：{filename}  [{' + '.join(result['asset_tags'])}]\n"
            f"网格：{result['parts_count']}   顶点：{result['total_verts']:,}   "
            f"三角面：{result['total_faces']:,}   "
            f"尺寸：{size[0]:.2f} × {size[1]:.2f} × {size[2]:.2f}"
        )
        self._refresh_hud()
        self.gl_widget.update()
        self._start_pending_load()

    def _start_pending_load(self):
        if self._pending_load is None:
            return
        path, req_id, preserve_camera = self._pending_load
        self._pending_load = None
        if req_id != self._load_req_id or self._active_worker is not None:
            return
        self._preserve_camera_req = req_id if preserve_camera else 0
        self._start_load_worker(path, req_id)

    # --- 文件拖拽支持 ---
    def closeEvent(self, event):
        try:
            self.gl_widget.cleanup_pick_resources()
        except Exception:
            pass
        worker = self._active_worker
        if worker is not None and worker.isRunning():
            worker.request_cancel()
            worker.wait(3000)
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if os.path.splitext(url.toLocalFile())[1].lower() in SUPPORTED_EXTS:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.splitext(path)[1].lower() in SUPPORTED_EXTS:
                self.load_model(path)
                break

    # --- 快捷键控制 ---
    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_1:
            self.gl_widget.apply_shading_preset("texture")
        elif key == Qt.Key.Key_2:
            self.gl_widget.apply_shading_preset("clay")
        elif key == Qt.Key.Key_3:
            self.gl_widget.apply_shading_preset("normal")
        elif key == Qt.Key.Key_4:
            self.gl_widget.apply_shading_preset("albedo")
        elif key == Qt.Key.Key_5:
            self.gl_widget.apply_shading_preset("matcap")
        elif key == Qt.Key.Key_W:
            self.gl_widget.wireframe_mode = (self.gl_widget.wireframe_mode + 1) % 3
        elif key == Qt.Key.Key_N:
            self.gl_widget.show_normals = not self.gl_widget.show_normals
        elif key == Qt.Key.Key_G:
            self.gl_widget.show_grid = not self.gl_widget.show_grid
        elif key == Qt.Key.Key_Z:
            self.gl_widget.toggle_z_up()
        elif key == Qt.Key.Key_B:
            self.gl_widget.check_backface = not self.gl_widget.check_backface
        elif key == Qt.Key.Key_Left:
            self.step_model(-1)
            return
        elif key == Qt.Key.Key_Right:
            self.step_model(1)
            return
        elif key == Qt.Key.Key_S:
            self.capture_viewport(save_file=True)
            return
        elif key == Qt.Key.Key_C and (
            event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        ):
            self.capture_viewport(save_file=False)
            return
        elif key == Qt.Key.Key_C and not (
            event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        ):
            self._toggle_cavity()
            return
        elif key == Qt.Key.Key_M:
            self._toggle_measure_mode()
            return
        elif key == Qt.Key.Key_R:
            self.gl_widget.reset_camera()
        elif key == Qt.Key.Key_Space:
            ctrl = self.gl_widget.fbx_controller
            if ctrl is not None and ctrl.animations:
                self.gl_widget.anim_playing = not self.gl_widget.anim_playing
        elif key == Qt.Key.Key_Tab:
            ctrl = self.gl_widget.fbx_controller
            if ctrl is not None and len(ctrl.animations) > 1:
                self.gl_widget.anim_idx = (self.gl_widget.anim_idx + 1) % len(ctrl.animations)
                self.gl_widget.anim_time = 0.0
                self._flash_hud_note(
                    f"动作 {self.gl_widget.anim_idx + 1}/{len(ctrl.animations)}："
                    f"{ctrl.animations[self.gl_widget.anim_idx]['name']}")
        elif key == Qt.Key.Key_L:
            ctrl = self.gl_widget.fbx_controller
            if ctrl is not None and ctrl.raw_lights:
                self.gl_widget.use_scene_lights = not self.gl_widget.use_scene_lights
                self._flash_hud_note(
                    f"场景灯光：{'开' if self.gl_widget.use_scene_lights else '关'}")
        elif key == Qt.Key.Key_K:
            self.gl_widget.show_skeleton = not self.gl_widget.show_skeleton
        elif key == Qt.Key.Key_P:
            self.gl_widget.is_ortho = not self.gl_widget.is_ortho
        elif key == Qt.Key.Key_T:
            self.gl_widget.toggle_turntable()
        elif key == Qt.Key.Key_U:
            self.gl_widget.uv_checker = not self.gl_widget.uv_checker
        elif key == Qt.Key.Key_V:
            self.gl_widget.bg_idx = (self.gl_widget.bg_idx + 1) % len(self.gl_widget.bg_presets)
        elif key == Qt.Key.Key_H:
            self.hud_label.setVisible(self.hud_label.isHidden())
            self._layout_timeline()
            return
        elif key == Qt.Key.Key_O:
            self.toggle_outliner()
        elif key == Qt.Key.Key_X:
            self.gl_widget.show_bbox = not self.gl_widget.show_bbox
        elif key in (Qt.Key.Key_F1, Qt.Key.Key_F2, Qt.Key.Key_F3):
            invert = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            view_name = {Qt.Key.Key_F1: "front", Qt.Key.Key_F2: "right",
                         Qt.Key.Key_F3: "top"}[key]
            self.gl_widget.set_preset_view(view_name, invert)
        elif key in (Qt.Key.Key_Comma, Qt.Key.Key_Period):
            ctrl = self.gl_widget.fbx_controller
            if ctrl is not None and ctrl.animations:
                dur = ctrl.duration(self.gl_widget.anim_idx)
                if dur > 0:
                    self.gl_widget.anim_playing = False
                    step = -1.0 / 24.0 if key == Qt.Key.Key_Comma else 1.0 / 24.0
                    self.gl_widget.anim_time = (self.gl_widget.anim_time + step) % dur
                    self.timeline_bar.update()
        elif key == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)
            return

        self.gl_widget.update()
        self._refresh_hud()


if __name__ == "__main__":
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    viewer = QuickModelViewer()
    viewer.show()

    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        viewer.load_model(sys.argv[1])

    sys.exit(app.exec())
