# 常见问题 / FAQ

> getstarted · 计划 04 P3（2026-09-28）。本文内容整理自 `README.md` / `README_EN.md`（迁移瘦身），未新增事实。
> 安装与环境见 [installation.md](installation.md)；全部文档导航见 [docs/INDEX.md](../INDEX.md)。
> 本文为双语文档：简体中文在前，English follows below。

## 中文

### 1. macOS Apple Silicon (M1/M2/M3/M4) 注意事项

| 问题 | 症状 | 解决方案 |
|------|------|----------|
| **Python 架构不匹配** | pip 报 `not a supported wheel` | 确保使用 **arm64 原生 Python**（python.org 3.10+ 安装器），不要使用 Rosetta 转译的 x86_64 版本。可通过 `python -c "import struct; print(struct.calcsize('P')*8)"` 确认，arm64 应输出 `8`，文件 `file $(which python)` 应显示 `arm64` 而非 `x86_64` |
| **PyOpenGL 缺失** | 剪辑(Edit)和合成(Composite)页面不显示 | `pip install PyOpenGL`。缺少此依赖时 `EditModule.is_available()` 返回 `False`，导致 Edit 和 Composite 功能被完全禁用 |
| **pyobjc-framework-CoreVideo 不存在** | `pip install -r requirements.txt` 中途失败 | `pyobjc-framework-CoreVideo` 在 PyPI 上**不存在独立包**，已从 requirements.txt 移除。CoreVideo 的功能已包含在 `pyobjc-framework-CoreMedia` 中。如遇安装失败，检查是否意外添加了此包 |
| **PyGObject / pycairo 安装失败** | `Symbol not found: _cairo_*` 或编译错误 | 需要先安装 Homebrew 系统依赖：`brew install cairo gobject-introspection pkg-config`，然后再 `pip install pycairo PyGObject`。确保使用 arm64 原生 Homebrew（路径 `/opt/homebrew`），不要使用 `/usr/local` 下的 x86 版本 |
| **glcontext 创建 OpenGL 上下文失败** | Composite 页面 GPU 渲染异常 | glcontext 使用 macOS 的 NSOpenGL/CGL API。如果遇到上下文创建失败，可尝试设置环境变量 `export PYOPENGL_PLATFORM=osmesa` 作为临时方案 |

### 2. macOS 26 (Tahoe) 注意事项

| 问题 | 症状 | 解决方案 |
|------|------|----------|
| **Python 版本过旧** | 启动崩溃或段错误 | 支持版本口径（P3-DOC-02）：`pyproject.toml` 声明 **Python ≥ 3.10**，CI 在 **3.10 / 3.12** 双版本验证；macOS 26 平台**建议** 3.13+（该平台已知兼容问题的下限仍以 CI 矩阵为准）。Python 3.9 及以下不受支持。检查版本：`python3 --version` |
| **PyTorch MPS 不可用** | `torch.backends.mps.is_available()` 返回 `False` | 这是 macOS 26 上的 [已知 bug](https://github.com/pytorch/pytorch/issues/177819)。AI 推理主要使用 ONNX Runtime（不受影响），torch 的 MPS 问题只影响 SwinIR/NAFNet 高级 AI 模型。临时方案：回退到 CPU 模式 `device = "cpu"` |
| **VLC 播放器不可用** | `import vlc` 失败 | 需安装 [arm64 版 VLC](https://www.videolan.org/vlc/)。python-vlc 只是绑定库，依赖系统安装的 VLC |

### 3. Windows 注意事项

| 问题 | 症状 | 解决方案 |
|------|------|----------|
| **PyQt6 WebEngine 冲突** | 启动时 OpenGL 上下文失败 (wglShareLists error 0x502) | QtWebEngineWidgets 的 Chromium GPU 进程与 ModernGL 的 OpenGL 上下文冲突。`src/main.py` 已内置检测，会自动为 WebEngine 设置软件渲染 |
| **硬件解码仅 NVIDIA 默认启用** | AMD 显卡硬解不生效或性能不佳 | NVDEC/D3D11VA 硬解下载默认仅在 NVIDIA 上启用（AMD 实测硬解劣于 CPU 多线程）。可用 `TOPOS_DISABLE_WIN_HW_DECODE=1` 关闭硬件解码回退 CPU |
| **PyOpenGL_accelerate** | 可选加速库 | 仅 Windows 平台需要：`pip install PyOpenGL_accelerate`。macOS/Linux 不需要 |
| **长路径问题** | pip 安装失败 | 某些包路径超过 Windows 260 字符限制。启用长路径：`reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f` |
| **MSVC 编译工具** | C 扩展编译失败 | 部分包需要 C 编译器。安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) 或运行 `pip install --only-binary :all: <package>` 跳过编译 |

Windows 下的硬件视频路径按“真实输出格式”裁决，不会把 FFmpeg 编译选项误报成硬件能力：

```powershell
# RTX/NVIDIA：验证 D3D11VA、NVDEC 在项目素材上的真实解码格式
.venv\Scripts\python.exe scripts\probe_windows_capability_matrix.py --backends d3d11va,nvdec

# 用应用生产解码源测吞吐（含 CPU planar 与硬解下载对照）
.venv\Scripts\python.exe scripts\bench_hw_decode_windows.py --hw-backend nvdec --tag nvidia

# 验证 NVENC 编码、回解码和 CPU 基线
.venv\Scripts\python.exe scripts\probe_nvenc_encode_windows.py
```

预览中的 `preserve_planes=True` 会优先保留 NV12/P010 planar 数据，让渲染器直接走 GPU YUV 上传；不支持的 codec/profile 会在首帧验证时明确回退 CPU。`TOPOS_DISABLE_WIN_HW_DECODE=1` 可关闭硬解，`TOPOS_FORCE_WIN_HW_DECODE=1` 可在非 NVIDIA Windows 机器上显式启用 D3D11VA/QSV 试验路径。性能调优应以 `tests/test_videos` 的真实素材基准为准，不能只看 FFmpeg 是否列出某个 encoder/hwaccel 名称。

### 4. Linux 注意事项

| 问题 | 症状 | 解决方案 |
|------|------|----------|
| **PyGObject 系统依赖** | `import gi` 失败 | Ubuntu/Debian: `sudo apt install libgirepository1.0-dev gcc libcairo2-dev pkg-config python3-dev`。Fedora: `sudo dnf install gobject-introspection-devel cairo-devel pkg-config python3-devel` |
| **OpenGL 驱动** | ModernGL 上下文创建失败 | 确保安装了 OpenGL 驱动：`sudo apt install libgl1-mesa-glx libegl1-mesa`（Debian/Ubuntu）|
| **VLC** | `import vlc` 失败 | `sudo apt install vlc libvlc-dev`（Debian/Ubuntu）或 `sudo dnf install vlc`（Fedora）|

### 5. 其他常见问题

*   **虚拟环境不可跨架构复制**：从 Intel Mac 迁移到 Apple Silicon（或反向）时必须删除旧 `.venv` 重建——详见 [installation.md](installation.md) “架构迁移须知”。

---

## English

### 1. macOS Apple Silicon (M1/M2/M3/M4)

| Issue | Symptom | Resolution |
|---|---|---|
| Python architecture mismatch | pip reports `not a supported wheel` | Use native arm64 Python (python.org 3.10+), not an x86_64 Rosetta build. `python -c "import struct; print(struct.calcsize('P')*8)"` should print `8`; `file $(which python)` should report `arm64`. |
| Missing PyOpenGL | Edit and Composite pages do not appear | Install `PyOpenGL`. Without it, `EditModule.is_available()` returns `False`, disabling Edit and Composite. |
| `pyobjc-framework-CoreVideo` unavailable | `pip install -r requirements.txt` fails partway through | There is no separate CoreVideo package on PyPI. CoreVideo functionality is included in `pyobjc-framework-CoreMedia`; do not add the unavailable package. |
| PyGObject/pycairo installation fails | `_cairo_*` symbol errors or build errors | Install system packages first: `brew install cairo gobject-introspection pkg-config`; then install `pycairo PyGObject`. Use native arm64 Homebrew under `/opt/homebrew`, not the x86 `/usr/local` tree. |
| glcontext cannot create OpenGL context | Composite GPU rendering fails | As a temporary workaround, try `export PYOPENGL_PLATFORM=osmesa`. |

### 2. macOS 26 (Tahoe)

| Issue | Symptom | Resolution |
|---|---|---|
| Python version too old | Startup crash or segmentation fault | `pyproject.toml` requires Python ≥ 3.10; CI validates 3.10 and 3.12. Python 3.13+ is recommended on macOS 26, while the supported floor remains the CI matrix. Python 3.9 and earlier are unsupported. |
| PyTorch MPS unavailable | `torch.backends.mps.is_available()` returns `False` | This is a [known PyTorch issue](https://github.com/pytorch/pytorch/issues/177819). Most AI inference uses ONNX Runtime and is unaffected; it impacts advanced SwinIR/NAFNet models. Use CPU mode (`device = "cpu"`) as a temporary workaround. |
| VLC unavailable | `import vlc` fails | Install the arm64 [VLC](https://www.videolan.org/vlc/) application. `python-vlc` is a binding and still requires VLC on the system. |

### 3. Windows

| Issue | Symptom | Resolution |
|---|---|---|
| PyQt6 WebEngine conflict | OpenGL context startup fails (`wglShareLists` error 0x502) | Chromium's QtWebEngine GPU process conflicts with ModernGL. `src/main.py` detects this and enables software rendering for WebEngine. |
| Hardware decode defaults | AMD hardware decode is ineffective or slower | NVDEC/D3D11VA download is enabled by default only on NVIDIA (AMD hardware decode tested slower than multithreaded CPU). Set `TOPOS_DISABLE_WIN_HW_DECODE=1` to fall back to CPU. |
| `PyOpenGL_accelerate` | Optional acceleration package | Install on Windows only: `pip install PyOpenGL_accelerate`. It is not needed on macOS/Linux. |
| Long paths | pip installation fails | Enable paths over 260 characters: `reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f`. |
| MSVC build tools | C extension build fails | Install [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) or try `pip install --only-binary :all: <package>`. |

Windows hardware-video capability is judged by real output formats, not by FFmpeg build flags alone:

```powershell
# Check actual decode formats for D3D11VA and NVDEC with project media
.venv\Scripts\python.exe scripts\probe_windows_capability_matrix.py --backends d3d11va,nvdec

# Benchmark app-produced decode sources, including CPU planar and hardware download
.venv\Scripts\python.exe scripts\bench_hw_decode_windows.py --hw-backend nvdec --tag nvidia

# Verify NVENC encode, decode-back, and CPU baseline
.venv\Scripts\python.exe scripts\probe_nvenc_encode_windows.py
```

Preview's `preserve_planes=True` prefers NV12/P010 planar data so the renderer can upload GPU YUV directly. Unsupported codec/profile combinations fall back to CPU after first-frame validation. `TOPOS_DISABLE_WIN_HW_DECODE=1` disables hardware decode; `TOPOS_FORCE_WIN_HW_DECODE=1` enables the experimental D3D11VA/QSV path on non-NVIDIA Windows machines. Use real media from `tests/test_videos` for performance tuning; an encoder or hwaccel name appearing in FFmpeg does not prove the path works.

### 4. Linux

| Issue | Symptom | Resolution |
|---|---|---|
| PyGObject system dependencies | `import gi` fails | Ubuntu/Debian: `sudo apt install libgirepository1.0-dev gcc libcairo2-dev pkg-config python3-dev`. Fedora: `sudo dnf install gobject-introspection-devel cairo-devel pkg-config python3-devel`. |
| OpenGL driver | ModernGL cannot create a context | Install a driver, for example `sudo apt install libgl1-mesa-glx libegl1-mesa` on Debian/Ubuntu. |
| VLC | `import vlc` fails | Debian/Ubuntu: `sudo apt install vlc libvlc-dev`; Fedora: `sudo dnf install vlc`. |

### 5. Other Common Issues

- **Virtual environments cannot be copied across CPU architectures:** when moving between an Intel Mac and Apple Silicon, remove and recreate `.venv` — see “Moving Between CPU Architectures” in [installation.md](installation.md).
