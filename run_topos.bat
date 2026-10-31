@echo off
if "%~1"=="__rl" goto :__main
chcp 65001 > nul 2>&1
cmd /c call "%~f0" __rl
exit /b
:__main
setlocal enabledelayedexpansion

:: ███████╗████████╗███████╗██╗░░░░░██╗░░░░░
:: ██╔════╝╚══██╔══╝██╔════╝██║░░░░░██║░░░░░
:: █████╗░░░░░██║░░░█████╗░░██║░░░░░██║░░░░░
:: ██╔══╝░░░░░██║░░░██╔══╝░░██║░░░░░██║░░░░░
:: ██║░░░░░░░░██║░░░███████╗███████╗███████╗
:: ╚═╝░░░░░░░░╚═╝░░░╚══════╝╚══════╝╚══════╝
:: Topos Color - GPU-Accelerated Video/Image Grading Software
::
:: run_topos.bat — 一键启动脚本 (带依赖自检)
:: Launch script with dependency self-check

:: ── 进入脚本所在目录 ──────────────────────────────────────────────────────
cd /d "%~dp0"

echo.
echo   正在启动 Topos Color... / Starting Topos Color...
echo   ────────────────────────────────────────

:: ════════════════════════════════════════════════════════════════════════════
:: Step 1: 检测系统环境 / Detect System
:: ════════════════════════════════════════════════════════════════════════════
echo.
echo   [Step 1/4] 检测系统环境 / Detecting System...
echo.

:: 检测 CPU 架构
set "ARCH=x64"
if "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=ARM64"
if defined PROCESSOR_ARCHITEW6432 set "ARCH=x64_emulated"

echo     [OK] 操作系统 / OS: Windows (%ARCH%)

:: ════════════════════════════════════════════════════════════════════════════
:: Step 2: 检测 Python / Detect Python
:: ════════════════════════════════════════════════════════════════════════════
echo.
echo   [Step 2/4] 检测 Python 环境 / Detecting Python...
echo.

set "PYTHON_CMD="

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=.venv\Scripts\python.exe"
    echo     [OK] 找到虚拟环境 / Found virtual environment: .venv\Scripts\python.exe
) else (
    echo     [..] 未找到 .venv，尝试系统 Python / .venv not found, trying system Python...
    where py > nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=py"
    ) else (
        where python > nul 2>&1
        if !errorlevel! equ 0 (
            set "PYTHON_CMD=python"
        ) else (
            echo.
            echo     [FAIL] 未找到 Python！请先安装 Python 3.10+
            echo     [FAIL] Python not found! Please install Python 3.10+
            echo.
            pause
            exit /b 1
        )
    )
)

:: 检查 Python 版本
for /f "tokens=*" %%v in ('%PYTHON_CMD% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2^>nul') do set "PY_VERSION=%%v"

for /f "tokens=1 delims=." %%a in ("%PY_VERSION%") do set "PY_MAJOR=%%a"
for /f "tokens=2 delims=." %%a in ("%PY_VERSION%") do set "PY_MINOR=%%a"

if not defined PY_VERSION (
    echo     [FAIL] 无法获取 Python 版本，请检查 Python 安装 / Unable to determine Python version
    echo.
    pause
    exit /b 1
)

if %PY_MAJOR% lss 3 (
    goto :py_too_old
)
if %PY_MAJOR% equ 3 if %PY_MINOR% lss 10 (
    goto :py_too_old
)
echo     [OK] Python 版本 / Python version: %PY_VERSION%
goto :py_ok

:py_too_old
echo.
echo     [FAIL] Python 版本过低: %PY_VERSION%，需要 3.10+
echo     [FAIL] Python version too old: %PY_VERSION%, requires 3.10+
echo.
pause
exit /b 1

:py_ok

:: ════════════════════════════════════════════════════════════════════════════
:: Step 3: 依赖自检 / Dependency Self-Check
:: ════════════════════════════════════════════════════════════════════════════
echo.
echo   [Step 3/4] 依赖自检 / Dependency Check
echo.

set "TOTAL=0"
set "PASSED=0"
set "CRITICAL_FAILED=0"
set "HIGH_FAILED=0"
set "OPTIONAL_FAILED=0"

:: ── 关键依赖 (C) ──────────────────────────────────────────────────────────

call :check_dep "PyQt6"      "PyQt6"    "C" "应用无法启动"                               "App won't start"                             "Core UI"
call :check_dep "NumPy"      "numpy"    "C" "应用无法启动"                               "App won't start"                             "Core Data"
call :check_dep "OpenCV"     "cv2"      "C" "无法处理图像和视频"                           "Cannot process images & videos"               "Image/Video"
call :check_dep "ModernGL"   "moderngl" "C" "GPU 渲染不可用"                              "GPU rendering unavailable"                    "GPU Rendering"

:: ── 重要依赖 (H) ──────────────────────────────────────────────────────────

call :check_dep "PyOpenGL"   "OpenGL"   "H" "剪辑与合成页面不可用"                          "Edit & Composite pages unavailable"           "Edit & Composite"
call :check_dep "Pillow"     "PIL"      "H" "无法加载部分图片格式"                           "Cannot load some image formats"               "Image Loading"
call :check_dep "SciPy"      "scipy"    "H" "部分调色算法降级"                              "Some color algorithms degraded"               "Color Grading"
call :check_dep "ONNX Runtime" "onnxruntime" "H" "AI 降噪等功能不可用"                       "AI features (denoise etc.) unavailable"       "AI Features"
call :check_dep "PyAV"       "av"       "H" "视频解码/编码不可用"                           "Video decode/encode unavailable"              "Video Processing"
call :check_dep "QtAwesome"  "qtawesome" "H" "工具栏图标缺失"                              "Toolbar icons missing"                        "UI Icons"
call :check_dep "MessagePack" "msgpack"  "H" "项目序列化失败"                               "Project serialization fails"                 "Save/Load"

:: ── 可选依赖 (O) ──────────────────────────────────────────────────────────

call :check_dep "psutil"     "psutil"   "O" "性能监控不可用"                               "Performance monitoring unavailable"            "System Monitor"
call :check_dep "Send2Trash" "send2trash" "O" "删除文件不经过回收站"                        "Files deleted permanently (no trash)"         "File Management"
call :check_dep "Flask"      "flask"    "O" "内部预览服务不可用"                            "Internal preview service unavailable"         "Preview"
call :check_dep "rawpy"      "rawpy"    "O" "RAW 格式图片无法打开"                          "Cannot open RAW image files"                 "RAW Support"
call :check_dep "Pillow-HEIF" "pillow_heif" "O" "HEIF/HEIC 格式不可用"                      "HEIF/HEIC format unavailable"                 "HEIF Support"

:: ── 自检结果汇总 / Summary ─────────────────────────────────────────────────

echo.
echo   ────────────────────────────────────────
echo   自检结果 / Check Summary:
echo     通过 / Passed:  %PASSED% / %TOTAL%

if %CRITICAL_FAILED% gtr 0 (
    echo     关键缺失 / Critical:  %CRITICAL_FAILED%
)
if %HIGH_FAILED% gtr 0 (
    echo     重要缺失 / High:      %HIGH_FAILED%
)
if %OPTIONAL_FAILED% gtr 0 (
    echo     可选缺失 / Optional:  %OPTIONAL_FAILED%
)

:: ════════════════════════════════════════════════════════════════════════════
:: Step 4: 处理缺失依赖 / Handle Missing Dependencies
:: ════════════════════════════════════════════════════════════════════════════

set /a "TOTAL_FAILED=CRITICAL_FAILED + HIGH_FAILED + OPTIONAL_FAILED"

if %TOTAL_FAILED% gtr 0 (
    echo.
    echo   ────────────────────────────────────────

    if %CRITICAL_FAILED% gtr 0 (
        echo.
        echo     [FAIL] 存在关键依赖缺失，无法启动！
        echo     [FAIL] Critical dependencies missing, cannot start!
        echo.
        echo     修复方法 / Fix:
        echo       %PYTHON_CMD% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )

    echo.
    echo     [WARN] 部分功能将不可用 / Some features will be unavailable
    echo.
    echo     建议修复 / Recommended fix:
    echo       %PYTHON_CMD% -m pip install -r requirements.txt
    echo.

    set /a "TOTAL_IMPORTANT=HIGH_FAILED + OPTIONAL_FAILED"
    if !TOTAL_IMPORTANT! gtr 0 (
        echo.
        set /p "answer=     是否自动安装缺失依赖？ / Auto-install? [Y/n]: "
        if /i "!answer!"=="n" (
            echo.
            echo     跳过安装，继续启动... / Skipping install, continuing...
        ) else (
            echo.
            echo     正在安装缺失依赖 / Installing missing dependencies...
            echo.
            %PYTHON_CMD% -m pip install -r requirements.txt
            if !errorlevel! neq 0 (
                echo.
                echo     [WARN] 部分依赖安装失败，将继续启动 / Some installs failed, continuing anyway
            )
            echo.
            echo     [OK] 依赖安装完成 / Dependencies installed
        )
    )
)

:: ════════════════════════════════════════════════════════════════════════════
:: Step 5: 启动应用 / Launch Application
:: ════════════════════════════════════════════════════════════════════════════
echo.
echo   ────────────────────────────────────────
echo   [Step 4/4] 启动 Topos Color / Launching Topos Color...
echo   ────────────────────────────────────────
echo.

%PYTHON_CMD% src\main.py

:: ── 退出处理 / Exit Handler ────────────────────────────────────────────────
echo.
echo   程序已退出 / Application exited.
pause
exit /b 0

:: ════════════════════════════════════════════════════════════════════════════
:: 函数: check_dep
:: 用法: call :check_dep "显示名" "import名" "级别" "后果中文" "后果English" "功能区域"
:: ════════════════════════════════════════════════════════════════════════════
:check_dep
set "dep_name=%~1"
set "dep_import=%~2"
set "dep_level=%~3"
set "dep_cn=%~4"
set "dep_en=%~5"
set "dep_area=%~6"

set /a "TOTAL+=1"

%PYTHON_CMD% -c "import %dep_import%" > nul 2>&1
if %errorlevel% equ 0 (
    echo     [OK]  %dep_name%  !dep_area!
    set /a "PASSED+=1"
    goto :eof
)

:: 导入失败
if "%dep_level%"=="C" (
    echo     [FAIL] %dep_name%  [关键/CRITICAL]
    echo             -^> !dep_cn! / !dep_en!
    set /a "CRITICAL_FAILED+=1"
)
if "%dep_level%"=="H" (
    echo     [WARN] %dep_name%  [重要/HIGH]
    echo             -^> !dep_cn! / !dep_en!
    set /a "HIGH_FAILED+=1"
)
if "%dep_level%"=="O" (
    echo     [INFO] %dep_name%  [可选/OPTIONAL]
    echo             -^> !dep_cn! / !dep_en!
    set /a "OPTIONAL_FAILED+=1"
)
goto :eof
