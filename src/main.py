import sys
import os
import logging
import argparse
from pathlib import Path


def _isolate_macos_native_extension_symbols() -> None:
    """Keep bundled native media libraries from interposing on each other.

    OpenCV, PyAV and Qt Multimedia each ship their own FFmpeg build. On
    macOS, loading their Python extension modules with the default ``RTLD_NOW``
    flags can expose common ``av_*`` symbols process-wide. A later FFmpeg
    build may then resolve against an earlier, ABI-incompatible build, which
    presents as native ``malloc`` corruption rather than a Python exception.

    ``RTLD_LOCAL`` keeps symbols from each extension and its bundled libraries
    out of the global lookup namespace while preserving normal dependency
    resolution. The setting remains active for lazy imports such as Qt
    Multimedia when audio playback starts.
    """
    if sys.platform != "darwin":
        return

    try:
        rtld_local = getattr(os, "RTLD_LOCAL")
        rtld_global = getattr(os, "RTLD_GLOBAL", 0)
        current_flags = sys.getdlopenflags()
        isolated_flags = (current_flags & ~rtld_global) | rtld_local
        if isolated_flags != current_flags:
            sys.setdlopenflags(isolated_flags)
    except (AttributeError, OSError, RuntimeError) as exc:
        # This is a hardening measure. Startup must remain usable on Python
        # builds that do not expose POSIX dlopen flags.
        print(
            f"WARNING: native extension symbol isolation unavailable: {exc}",
            file=sys.stderr,
        )


# Must run before importing Qt, OpenCV, PyAV, or any application
# module that may import them transitively.
_isolate_macos_native_extension_symbols()

# ============================================================================
# ⚠️ OpenEXR Support - Must be set BEFORE importing cv2
# ============================================================================
# Modern OpenCV (4.x) disables OpenEXR by default due to past security CVEs.
# Projects using .exr format (HDR images, AI masks, high-precision textures)
# need to explicitly enable this.
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"

# Silence Qt macOS keymapper debug spam (harmless per-key 16-line dumps,
# e.g. "Mismatch between Cocoa ... and Carbon ..."). Only this category is
# disabled; other Qt warnings (OpenGL context errors, etc.) still show.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.keymapper=false")

# Ensure the project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

# Source checkouts must not share user preferences or page layouts through the
# macOS-wide Topos config directory.  Each copied project gets a local hidden
# config folder instead.  Packaged builds keep the platform default unless an
# explicit TOPOS_CONFIG_PATH is supplied.
if not getattr(sys, "frozen", False) and not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(PROJECT_ROOT / ".topos" / "config.json")

# Configure logging for startup diagnostics
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# ============================================================================
# ⚠️ CRITICAL Windows OpenGL + QWebEngine Compatibility Fix
# ============================================================================
# QtWebEngineWidgets on Windows uses Chromium's GPU process which conflicts
# with ModernGL's shared OpenGL context (wglShareLists fails with error 0x502).
# ============================================================================

if sys.platform == 'win32':
    # Windows: 只针对 QtWebEngine 设置软件渲染
    # ⚠️ 不要设置 QT_OPENGL 或 QT_QUICK_BACKEND，否则会破坏 PyQt6 的 UI 渲染
    # 注：启动时 gpu_channel_manager 的 "Failed to create GLES3 context,
    # fallback to GLES2" 数行是软渲染下的正常回退，功能不受影响。Chromium
    # 直写 stderr，--disable-logging/--log-level/--enable-logging=file
    # --log-file=NUL 均无法静音（已逐一实测），属上游固有噪音。
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        "--disable-gpu --disable-gpu-compositing --use-angle=swiftshader"
    )
    logger.info("Windows: Configured Chromium for software rendering")

# ⚠️ CRITICAL: QtWebEngineWidgets MUST be imported BEFORE creating QApplication
# This must happen before any QCoreApplication instance is created.
# [R0-6] 此前 Windows 走"临时禁用导入"的诊断分支并被固化，真实导入推迟到
# main_window.create_panels() → files_panel 顶层（QApplication 之后），恰好
# 违反本约束，QtWebEngine 晚初始化与 ModernGL 的冲突风险并未消除——现在
# 全平台统一在此提前导入（Chromium GPU 冲突由上方 QTWEBENGINE_CHROMIUM_FLAGS
# 软渲染参数处理，与导入时序无关）。
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except ImportError:
    logger.warning("PyQt6-WebEngine not available, some features may be disabled")

# QTimer is used by the module-failure status-bar notice after window.show()
# (AUD-31200): it must be imported unconditionally or that path raises
# NameError and takes the whole startup down via the top-level except.
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtGui import QSurfaceFormat


def _orderly_shutdown(app: QCoreApplication | None, exit_code: int) -> None:
    """有序关闭：停掉原生 worker 后直接退出进程，跳过解释器终结化。

    进程在 interpreter shutdown 阶段存在长期的崩溃问题（Qt6Core fail-fast
    0xc0000409）：CPython 以不可预测的 GC 顺序销毁 Qt 原生对象，与仍在
    派发的事件/运行中的线程竞争。事件循环已经返回，业务状态在此之前
    已保存，因此这里停完线程池后用 os._exit 直接退出，保证退出路径
    确定性、不再随机段错误。

    ``app`` is accepted for call-site symmetry only (the startup fatal path
    may not have constructed one yet); the body never touches it.
    """
    try:
        from PyQt6.QtCore import QCoreApplication, QThreadPool

        pool = QThreadPool.globalInstance()
        if pool is not None:
            pool.clear()
            pool.waitForDone(3000)
        del QCoreApplication  # 下方异常路径仍以 QApplication 语义传递
    except Exception as exc:  # pragma: no cover - 尽力而为的清理
        logger.debug(f"thread pool shutdown: {exc}")
    try:
        # file_explorer 的缩略图线程持有 ffmpeg 子进程，显式停止。
        from file_explorer.main import shutdown_thumbnail_worker

        shutdown_thumbnail_worker()
    except Exception as exc:  # pragma: no cover
        logger.debug(f"thumbnail worker shutdown: {exc}")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(exit_code)


def main() -> int:
    app: QApplication | None = None
    try:
        # Parse CLI arguments
        parser = argparse.ArgumentParser(description="Topos Color")
        parser.add_argument(
            "--product",
            choices=["color", "studio", "edit", "composite"],
            default=None,
            help="Product mode: 'color', 'studio', 'edit', or 'composite'",
        )
        parser.add_argument(
            "--test-startup",
            action="store_true",
            help="Exit after startup verification (no GUI)",
        )
        # [R2-10] 自动化/进阶启动参数：跳过启动弹窗、直接打开指定项目
        parser.add_argument(
            "--no-startup-dialog",
            action="store_true",
            help="Skip the startup Project Manager / Welcome dialog",
        )
        parser.add_argument(
            "--project",
            default=None,
            metavar="PATH",
            help="Open the given .topos project after startup",
        )
        args, remaining = parser.parse_known_args()

        # AUD-31202: 校验 --project，非法路径不再静默无效。仅补诊断日志，
        # 传参与下游行为保持不变（是否升级为拒绝启动属产品决策，未改动）。
        if args.project and not (
            os.path.isfile(args.project)
            and args.project.lower().endswith(".topos")
        ):
            logger.error(
                "--project path is not an existing .topos file: %s "
                "(project will not be opened)",
                args.project,
            )

        # Apply product mode from CLI arg if specified
        if args.product and not os.environ.get("TOPOS_PRODUCT_MODE"):
            os.environ["TOPOS_PRODUCT_MODE"] = args.product
            logger.info(f"Product mode set to: {args.product}")

        # 强制 Qt Quick 使用 OpenGL 后端
        os.environ["QSG_RHI_BACKEND"] = "opengl"

        # Set default OpenGL format before creating QApplication
        fmt = QSurfaceFormat()

        # RTX 4090 / 现代 GPU 支持 OpenGL 4.5+
        # QOpenGLFunctions_4_1_Core 需要至少 4.1 版本才能初始化
        fmt.setVersion(4, 5)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)

        fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)

        # Standard 8-bit RGBA buffers
        fmt.setRedBufferSize(8)
        fmt.setGreenBufferSize(8)
        fmt.setBlueBufferSize(8)
        fmt.setAlphaBufferSize(8)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)

        # Windows: 不在默认格式中启用 MSAA
        if sys.platform != 'win32':
            fmt.setSamples(4)

        fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)

        QSurfaceFormat.setDefaultFormat(fmt)

        # 必须开启全局 OpenGL 上下文共享，否则 QOpenGLWidget 无法读取渲染线程的纹理
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

        app = QApplication(sys.argv)

        # [R2-9] 移除 Windows 每次启动创建一次性 QOpenGLContext 仅打版本日志
        # 的诊断残留；GL 环境问题由渲染管线自身的错误路径呈现

        from src.app.main_window import MainWindow
        from src.shared.i18n.i18n import I18N
        from src.shared.config.config_manager import ConfigManager
        from src.shared.feature_flags import load_feature_flags
        from src.app.feature_registry import FeatureRegistry
        from src.app.app_context import AppContext
        
        config_manager = ConfigManager()
        feature_flags = load_feature_flags(config_manager)
        
        saved_locale = config_manager.get("locale")
        if saved_locale:
            I18N.load_locale(saved_locale)
        else:
            system_locale = I18N.get_system_locale()
            I18N.load_locale(system_locale)
        
        # Initialize Feature Registry (Phase 3-4)
        registry = FeatureRegistry()
        app_context = AppContext(
            config_manager=config_manager,
            feature_flags=feature_flags,
            i18n=I18N,
            logger=logger,
        )
        registry.initialize(app_context)
        
        # [R2-3/R0-5] 特性模块注册收敛到 module_loader 的单一数据驱动实现
        # （此前 main.py 内联 / module_loader / products register() 三份平行
        # 实现已漂移：ai_workflow 分支在生产路径丢失）。Color 恒为核心，
        # 导入失败快速中止启动；可选模块按 flag 注册且逐个隔离。
        from src.app.module_loader import (
            register_color_module,
            register_builtin_modules,
        )
        register_color_module(registry)

        # [R2-8] 收集"flag 启用但加载失败"的模块，启动后给用户可见提示
        module_failures: dict[str, str] = {}
        module_results = register_builtin_modules(
            registry, flags=feature_flags, failures=module_failures
        )
        logger.info(
            "Optional feature modules registered: %s",
            sorted(fid for fid, ok in module_results.items() if ok) or "none",
        )
        
        # Create MainWindow with registry that has all pages registered
        # [R2-10] 传递启动行为参数（跳过弹窗 / 直接打开指定项目）
        window = MainWindow(
            feature_flags=feature_flags,
            feature_registry=registry,
            startup_project=args.project,
            startup_no_dialog=args.no_startup_dialog,
        )
        
        # Update context with main_window and save_router
        app_context.main_window = window
        app_context.save_router = window.save_router
        teaching_module = registry.get_module("teaching_animation")
        if teaching_module is not None and hasattr(teaching_module, "attach_main_window"):
            teaching_module.attach_main_window(window)
        
        # Register save handlers now that save_router is available
        registry.register_all_save_handlers()

        # --test-startup mode: verify startup succeeds without showing GUI
        if args.test_startup:
            logger.info("Startup verification successful — exiting")
            # AUD-31203: _orderly_shutdown 内 os._exit 永不返回，其后的
            # `return 0` 为不可达死代码，已删除；main() 标注 -> int 以匹配
            # 退出码语义（实际所有路径均经 _orderly_shutdown 终止）。
            _orderly_shutdown(app, 0)

        window.show()

        # [R2-8] 有模块加载失败时给出用户可见提示（此前仅 warning 日志，
        # 用户不知道功能被禁用及原因）
        if module_failures:
            _detail = ", ".join(sorted(module_failures))
            logger.warning("Feature modules unavailable: %s", _detail)
            from src.shared.i18n.i18n import I18N as _I18N
            QTimer.singleShot(1500, lambda: window.status_bar.showMessage(
                _I18N.tr("startup.modules_unavailable",
                         "Some features are unavailable: {modules}").format(
                    modules=_detail), 8000))

        # 强制触发一次布局和重绘
        window.update()
        QApplication.processEvents()

        rc = app.exec()
        _orderly_shutdown(app, rc if isinstance(rc, int) else 0)
        return 0  # 不可达：_orderly_shutdown 经 os._exit 不返回（类型闭合）
    except Exception:
        import traceback
        logger.error(f"\n{'='*60}")
        logger.error("FATAL ERROR during application startup:")
        logger.error(f"{'='*60}")
        traceback.print_exc()
        logger.error(f"{'='*60}")
        # AUD-31201: sys.exit(1) would run the full interpreter finalization
        # this file documents as crash-prone (Qt6Core fail-fast 0xc0000409 /
        # random segfaults when native objects race dying threads).  Take the
        # same deterministic exit as the normal path: stop the thread pool and
        # thumbnail workers first, then os._exit.  ``app`` is None when the
        # failure predates QApplication construction; the helper tolerates it.
        _orderly_shutdown(app if app is not None else QApplication.instance(), 1)
        return 1  # 不可达：_orderly_shutdown 经 os._exit 不返回（类型闭合）

if __name__ == "__main__":
    main()
