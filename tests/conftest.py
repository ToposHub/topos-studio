import sys
import os

# AUD-R2-005：本机 OpenBLAS 多线程 dgetrf 在 QThread 工作线程中并行崩溃
# （exit 132 / SIGILL，见 tests/color/test_tracking_worker_lifecycle.py）。
# OpenBLAS 线程池在共享库加载（即下方 numpy 导入）时按环境变量初始化，
# 必须在此之前设为单线程；测试套件对 BLAS 并行度无依赖。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import pytest
import numpy as np
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Disable update checker network access during tests
os.environ["TOPOS_TEST_MODE"] = "1"

# N20（2026-10-25）：PyAV 日志回调 GIL 死锁防护。tests/edit 全组单进程
# 长跑实测挂起（sample 栈：libavcodec 帧线程 av_log → PyAV Cython 回调
# → PyGILState_Ensure 永等 GIL，主线程持 GIL 等线程收尾）。PyAV 默认
# 把 av_log 路由进 Python logging——编码/解码工作线程因此依赖 GIL。
# 恢复 FFmpeg 默认 C 回调（stderr 直写），线程不再进 Python；诊断信息
# 仍在 stderr 可见。
try:
    import av.logging
    av.logging.restore_default_callback()
except Exception:  # noqa: BLE001 — av 未装的环境按原样
    pass

# Import fixtures from the module-isolation conftest so they are available
# to all test files under tests/.  pytest only auto-discovers fixtures
# from files named exactly "conftest.py", so we re-register them here.
sys.path.insert(0, os.path.dirname(__file__))
from conftest_module_isolation import (  # noqa: E402
    hide_modules,
    color_only_env,
    color_only_physical,
)

# Import QWebEngineWidgets before any QApplication is created.
# MainWindow imports files_panel which imports QWebEngineView at module level.
# If this import happens after QApplication exists, PyQt6 aborts.
if sys.platform != 'win32':
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except ImportError:
        pass


@pytest.fixture(scope="session")
def qapp():
    """Create a headless QApplication for the entire test session."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app
    # The embedded file explorer starts a daemon thumbnail worker at import
    # time. Stop it before pytest-qt tears down QApplication/native Qt state.
    file_explorer = sys.modules.get("file_explorer.main")
    shutdown = getattr(file_explorer, "shutdown_thumbnail_worker", None)
    if callable(shutdown):
        shutdown()


@pytest.fixture
def image_engine(qapp):
    """
    Create an ImageEngine with all GPU/rendering calls neutralized.

    Strategy:
    - QApplication exists (required by QThread/QObject in __init__)
    - GLContextManager is mocked to prevent OpenGL calls
    - render_pipeline is mocked to a no-op
    - renderer is left as None (the __init__ default)
    """
    with patch('src.features.color.core.image_engine.GLContextManager') as MockGL:
        mock_gl_instance = MagicMock()
        mock_gl_instance.initialized = False
        mock_gl_instance.ctx = None
        mock_gl_instance.mutex = MagicMock()
        MockGL.instance.return_value = mock_gl_instance

        from src.features.color.core.image_engine import ImageEngine
        engine = ImageEngine()

    # Neutralize render_pipeline so no GPU work happens
    engine.render_pipeline = MagicMock()

    # Ensure renderer is None (skip all async renderer paths)
    assert engine.renderer is None

    yield engine

    # Cleanup: stop threads（quit 无法中断执行中的槽；超时强杀兜底，
    # 防止未结束线程随 fixture 对象 GC 触发 QThread destroyed Abort）
    engine.shutdown_worker_threads(timeout_ms=2000)


@pytest.fixture
def sample_tile():
    from src.features.color.core.image_engine import Tile
    return Tile(0, 0, 512, 512, 0)


@pytest.fixture
def sample_layer():
    from src.features.color.core.layer import Layer
    return Layer("Test Layer")


@pytest.fixture
def sample_mask_shape():
    from src.features.color.core.layer import MaskShape
    return MaskShape(MaskShape.CIRCLE)


@pytest.fixture
def sample_mask_group():
    from src.features.color.core.layer import MaskGroup
    return MaskGroup("Test Group")


@pytest.fixture
def history_manager():
    from src.features.color.core.history import HistoryManager
    return HistoryManager(max_steps=10)


@pytest.fixture
def sample_image_bgr():
    """100x100 BGR uint8 test image with a known pattern."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[:50, :50] = [255, 0, 0]
    img[:50, 50:] = [0, 255, 0]
    img[50:, :50] = [0, 0, 255]
    img[50:, 50:] = [255, 255, 0]
    return img


# --- Auto marker assignment based on file paths ---
from pathlib import Path as _Path

_COLOR_CORE_FILES = {
    "tests/test_color_session.py",
    "tests/test_alpha_system.py",
    "tests/test_cache_fix.py",
    "tests/test_image_engine_state.py",
    "tests/test_image_engine_layers.py",
    "tests/test_image_engine_params.py",
    "tests/test_history.py",
    "tests/test_layer.py",
    "tests/test_mask_shape.py",
    "tests/test_mask_group.py",
    "tests/test_tile.py",
    "tests/test_tiling.py",
    "tests/test_image_engine_history.py",
    "tests/test_image_engine_transforms.py",
    "tests/test_color_tools_lut_exporter.py",
    "tests/test_color_tools_params.py",
    "tests/test_color_tools_scripting_api.py",
}

_COMPOSITE_EXTRA_FILES = {
    "tests/test_expression_parser.py",
    "tests/test_keyframe.py",
    "tests/test_merge_node.py",
    "tests/test_reformat_crop_transform.py",
    "tests/test_dirty_propagation.py",
    "tests/test_deep_compositing.py",
    "tests/test_group_node.py",
    "tests/test_node_capabilities.py",
    "tests/test_node_graph_commands.py",
    "tests/test_backdrop.py",
    "tests/test_sticky_note.py",
    "tests/test_parallel_evaluation.py",
    "tests/test_render_scheduler.py",
}

# Keep the composite validation layers explicit. These files create a
# standalone ModernGL context or exercise the checked-in media matrix; they
# should not be mixed into the fast, no-GPU gate. The lists live here rather
# than in individual test modules so the same markers work from pytest, CI,
# and scripts/run_composite_tests.sh.
_COMPOSITE_GPU_FILES = {
    "tests/composite/test_color_input_color_space.py",
    "tests/composite/test_nodes/test_output_nodes.py",
    "tests/composite/test_nodes/test_upgraded_nodes.py",
    "tests/composite/test_scene3d_motion_aov.py",
    "tests/composite/test_shader_uniform_contract.py",
    "tests/composite/test_video_frame_playback.py",
}

_COMPOSITE_REAL_MEDIA_FILES = {
    "tests/composite/test_video_frame_playback.py",
}

_COMPOSITE_SMOKE_FILES = {
    "tests/composite/test_composite_gpu_lifecycle.py",
    "tests/composite/test_render_lifecycle_contract.py",
    "tests/composite/test_render_pass_contract.py",
    "tests/composite/test_scene_payload.py",
    "tests/composite/test_runtime_diagnostics.py",
}

_MODULAR_FILES = {
    "tests/test_src_directory_boundaries.py",
    "tests/test_modular_import_boundaries.py",
    "tests/test_page_context.py",
    "tests/test_feature_flags.py",
    "tests/test_feature_flags_registry.py",
    "tests/test_product_physical_missing.py",
}

_EDIT_EXTRA_FILES = {
    "tests/test_offline_asset_manager.py",
    "tests/test_offline_relink_proxy.py",
    "tests/media/test_video_frame_provider_service.py",
}

_DELIVER_FILES = {
    "tests/test_save_router.py",
    "tests/test_export_frame_provider.py",
}


def pytest_collection_modifyitems(config, items):
    """Auto-assign markers based on file paths so CI gates work."""
    root = _Path(config.rootpath)
    for item in items:
        try:
            rel = _Path(str(item.fspath)).resolve().relative_to(root).as_posix()
        except ValueError:
            continue

        if rel in _COLOR_CORE_FILES or rel.startswith("tests/test_color_tools_"):
            item.add_marker(pytest.mark.color_core)

        if rel.startswith("tests/test_composite_") or rel in _COMPOSITE_EXTRA_FILES:
            item.add_marker(pytest.mark.composite_core)

        if rel in _COMPOSITE_GPU_FILES:
            item.add_marker(pytest.mark.gpu_renderer)

        if rel in _COMPOSITE_REAL_MEDIA_FILES:
            item.add_marker(pytest.mark.real_media)

        if rel in _COMPOSITE_SMOKE_FILES:
            item.add_marker(pytest.mark.smoke)

        if rel.startswith("tests/media/"):
            item.add_marker(pytest.mark.media_core)

        if rel.startswith("tests/visual/"):
            item.add_marker(pytest.mark.visual)

        if rel in _MODULAR_FILES:
            item.add_marker(pytest.mark.modular)

        # Edit core: tests in tests/edit/ or extra edit-dependent tests
        if rel.startswith("tests/edit/") or rel in _EDIT_EXTRA_FILES:
            item.add_marker(pytest.mark.edit_core)

        # Edit core: visual edit page tests
        if rel.startswith("tests/visual/test_edit_"):
            item.add_marker(pytest.mark.edit_core)

        # Deliver core: tests in tests/deliver/ or extra deliver-dependent tests
        if rel.startswith("tests/deliver/") or rel in _DELIVER_FILES:
            item.add_marker(pytest.mark.deliver_core)

        # Physical missing: tests in test_product_physical_missing.py
        if rel == "tests/test_product_physical_missing.py":
            item.add_marker(pytest.mark.physical_missing)
