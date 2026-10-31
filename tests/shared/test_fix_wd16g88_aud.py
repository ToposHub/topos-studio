"""wd16-g88 修复回归测试（批 D：io / layout / logging 域）。

覆盖 REMEDIATION_PLAN 条目：
- AUD-33902  save_image 吞掉 _save_toos_image 设计性 ValueError（无日志）
- AUD-33903  RAW 单例缓存错误路径留下悬挂状态（已 close 对象 + 旧路径）
- AUD-33904  _save_toos_image 异常路径不保证 encoder close/abort
- AUD-33905  save_image 直接 cv2.imwrite，绕过 imencode+tofile Unicode 策略
- AUD-33906  TIFF 导出对 float 图像无条件 clip(0,1) 且无告警
- AUD-34100  LayoutProfile.from_dict 对外部数据仅部分校验
- AUD-34102  workspace.*/advanced.* 面板被特性前缀错误过滤
- AUD-34103  migrate_legacy_page_layouts 不识别 mode-scoped 结构
- AUD-34201  裸文件名 log_file_path 使 makedirs("") 抛错且无防护
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import numpy as np
import pytest

from src.shared.io.file_io import FileIO
from src.shared.layout.profile import (
    LAYOUT_PROFILE_SCHEMA_VERSION,
    LayoutProfile,
)
from src.shared.layout.store import LayoutStore
from src.shared.logging.log_manager import LogManager


# ---------------------------------------------------------------------------
# 共享 stub
# ---------------------------------------------------------------------------


class _StubConfig:
    """ConfigManager-like stub（get/set，内存态）。"""

    def __init__(self, data: Optional[dict] = None) -> None:
        self._data: dict = dict(data or {})

    def get(self, key: str) -> Any:
        return self._data.get(key)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self._data[key] = value


class _FakeRaw:
    """rawpy 对象替身：记录 close/postprocess 调用。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.closed = False
        self.camera_whitebalance = [2.0, 1.0, 1.5, 1.0]

    def close(self) -> None:
        self.closed = True

    def postprocess(self, **kwargs: Any) -> np.ndarray:
        return np.zeros((4, 4, 3), dtype=np.uint16)


# ---------------------------------------------------------------------------
# AUD-33902：save_image 吞设计性 ValueError
# ---------------------------------------------------------------------------


class TestAud33902SaveImageDesignedErrorsLogged:

    def test_raw_tier_designed_value_error_logged_not_sworn(self,
                                                            tmp_path,
                                                            caplog) -> None:
        """RAW 档 fail-closed ValueError 必须留下 ERROR 日志（不再无声转 False）。"""
        out = tmp_path / "frame.toos"
        img = np.zeros((4, 4, 3), dtype=np.float32)
        with caplog.at_level(logging.ERROR,
                             logger="src.shared.io.file_io"):
            ok = FileIO.save_image(str(out), img,
                                   options={"toos_tier": "raw12-2"})
        assert ok is False
        assert "RAW 档" in caplog.text
        assert "raw12-2" in caplog.text

    def test_generic_exception_logged(self, tmp_path, caplog,
                                      monkeypatch) -> None:
        """任意保存异常都必须有 ERROR + 异常信息（IO 异常边界）。"""
        def _boom(path, image, options):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(FileIO, "_save_toos_image", _boom)
        with caplog.at_level(logging.ERROR,
                             logger="src.shared.io.file_io"):
            ok = FileIO.save_image(str(tmp_path / "x.toos"),
                                   np.zeros((2, 2, 3), np.float32))
        assert ok is False
        assert "disk on fire" in caplog.text


# ---------------------------------------------------------------------------
# AUD-33903：RAW 单例缓存悬挂状态
# ---------------------------------------------------------------------------


class TestAud33903RawSingletonDanglingState:

    @pytest.fixture(autouse=True)
    def _reset_raw_singleton(self, monkeypatch):
        monkeypatch.setattr(FileIO, "_current_raw_obj", None, raising=False)
        monkeypatch.setattr(FileIO, "_current_raw_path", None, raising=False)

    def _make_files(self, tmp_path, *names):
        paths = []
        for n in names:
            p = tmp_path / n
            p.write_bytes(b"not-really-raw")
            paths.append(str(p))
        return paths

    def test_update_raw_decoding_failure_clears_singleton(
            self, tmp_path, monkeypatch) -> None:
        """imread 失败后状态必须清空——旧路径下次请求强制重开。"""
        old_path, bad_path = self._make_files(tmp_path, "old.nef", "bad.nef")
        old_raw = _FakeRaw(old_path)
        monkeypatch.setattr(FileIO, "_current_raw_obj", old_raw)
        monkeypatch.setattr(FileIO, "_current_raw_path", old_path)

        import src.shared.io.file_io as file_io_mod
        calls: list[str] = []

        def _imread(path: str) -> _FakeRaw:
            calls.append(path)
            if path == bad_path:
                raise RuntimeError("corrupt raw")
            return _FakeRaw(path)

        monkeypatch.setattr(file_io_mod.rawpy, "imread", _imread)

        result = FileIO.update_raw_decoding(bad_path, {})
        assert result[0] is None and result[1] is None and result[2] is None
        # AUD-33903 核心：不留“已 close 对象 + 旧路径”的悬挂组合
        assert FileIO._current_raw_obj is None
        assert FileIO._current_raw_path is None
        assert old_raw.closed is True

        # 旧路径请求必须重开（此前路径相等跳过重开 → 永久失败）
        FileIO.update_raw_decoding(old_path, {})
        assert calls[-1] == old_path
        assert FileIO._current_raw_path == old_path

    def test_read_full_image_failure_clears_singleton(
            self, tmp_path, monkeypatch) -> None:
        old_path, bad_path = self._make_files(tmp_path, "old2.nef", "bad2.nef")
        old_raw = _FakeRaw(old_path)
        monkeypatch.setattr(FileIO, "_current_raw_obj", old_raw)
        monkeypatch.setattr(FileIO, "_current_raw_path", old_path)

        import src.shared.io.file_io as file_io_mod

        def _imread(path: str) -> _FakeRaw:
            if path == bad_path:
                raise RuntimeError("corrupt raw")
            return _FakeRaw(path)

        monkeypatch.setattr(file_io_mod.rawpy, "imread", _imread)

        assert FileIO._read_full_image(bad_path) is None
        assert FileIO._current_raw_obj is None
        assert FileIO._current_raw_path is None

        # 旧路径此后仍可解码（重开而非打在已关闭对象上）
        img = FileIO._read_full_image(old_path)
        assert img is not None
        assert FileIO._current_raw_path == old_path


# ---------------------------------------------------------------------------
# AUD-33904：_save_toos_image 异常路径保证 encoder 清理
# ---------------------------------------------------------------------------


class TestAud33904ToosEncoderCleanup:

    def test_encode_failure_aborts_encoder(self, monkeypatch) -> None:
        """encode_frame 抛错时必须走 abort 释放 native 资源。"""
        import src.shared.export.topos_encoder as enc_mod

        events: list[str] = []

        class _FailingEncoder:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                events.append("init")

            def open(self) -> None:
                events.append("open")

            def encode_frame(self, frame: Any) -> None:
                events.append("encode")
                raise RuntimeError("encode exploded")

            def close(self) -> None:
                events.append("close")

            def abort(self) -> None:
                events.append("abort")

        monkeypatch.setattr(
            enc_mod, "ToposImageSequenceEncoder", _FailingEncoder)

        ok = FileIO.save_image(
            "/tmp/aud33904.toos",
            np.zeros((4, 4, 3), dtype=np.float32),
            options={"toos_tier": "float-ultra"},
        )
        assert ok is False
        assert "abort" in events, (
            f"异常路径未调用 abort()（AUD-33904 资源契约），events={events}")
        assert events.index("encode") < events.index("abort")

    def test_abort_failure_does_not_mask_primary_error(
            self, monkeypatch) -> None:
        """abort 自身失败仅告警，不得掩盖原始异常。"""
        import src.shared.export.topos_encoder as enc_mod

        class _BrokenAbortEncoder:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def open(self) -> None:
                pass

            def encode_frame(self, frame: Any) -> None:
                raise RuntimeError("primary failure")

            def close(self) -> None:
                pass

            def abort(self) -> None:
                raise RuntimeError("abort also broken")

        monkeypatch.setattr(
            enc_mod, "ToposImageSequenceEncoder", _BrokenAbortEncoder)

        ok = FileIO.save_image(
            "/tmp/aud33904b.toos",
            np.zeros((4, 4, 3), dtype=np.float32),
            options={"toos_tier": "float-ultra"},
        )
        assert ok is False


# ---------------------------------------------------------------------------
# AUD-33905：save_image 统一 imencode+tofile
# ---------------------------------------------------------------------------


class TestAud33905ImencodeTofileStrategy:

    @pytest.mark.parametrize("filename,img", [
        ("out.png", np.full((4, 4, 3), 128, dtype=np.uint8)),
        ("out.jpg", np.full((4, 4, 3), 128, dtype=np.uint8)),
        ("out.tiff", np.full((4, 4, 3), 1000, dtype=np.uint16)),
    ])
    def test_save_image_never_calls_cv2_imwrite(self, tmp_path, monkeypatch,
                                                filename, img) -> None:
        """cv2.imwrite 必须不再被 save_image 直接调用（Windows Unicode 契约）。"""
        import src.shared.io.file_io as file_io_mod

        def _imwrite_forbidden(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError(
                "save_image must use imencode+tofile, not cv2.imwrite "
                "(AUD-33905)")

        monkeypatch.setattr(file_io_mod.cv2, "imwrite", _imwrite_forbidden)
        out = tmp_path / filename
        assert FileIO.save_image(str(out), img) is True
        assert out.exists()

    def test_roundtrip_via_np_fromfile(self, tmp_path) -> None:
        """落盘文件可经 np.fromfile + imdecode 读回（同一 Unicode 策略）。"""
        import cv2
        img = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
        out = tmp_path / "中文文件名.png"
        assert FileIO.save_image(str(out), img) is True
        back = cv2.imdecode(np.fromfile(str(out), dtype=np.uint8),
                            cv2.IMREAD_UNCHANGED)
        assert back is not None
        assert back.shape == (4, 4, 3)


# ---------------------------------------------------------------------------
# AUD-33906：TIFF float 高光裁剪必须可见
# ---------------------------------------------------------------------------


class TestAud33906TiffFloatClipWarning:

    def test_over_one_highlights_logged(self, tmp_path, caplog) -> None:
        """>1.0 高光被量化裁剪时必须留下 warning（不再静默）。"""
        img = np.array([[[0.25, 1.0, 4.5]]], dtype=np.float32)
        out = tmp_path / "hdr.tif"
        with caplog.at_level(logging.WARNING,
                             logger="src.shared.io.file_io"):
            assert FileIO.save_image(str(out), img) is True
        assert "TIFF export clips float pixels" in caplog.text
        assert "4.5" in caplog.text

        import cv2
        back = cv2.imdecode(np.fromfile(str(out), dtype=np.uint8),
                            cv2.IMREAD_UNCHANGED)
        assert back is not None and back.dtype == np.uint16
        assert int(back.ravel()[2]) == 65535  # 4.5 → clip 到 1.0 → 65535

    def test_in_range_float_no_warning(self, tmp_path, caplog) -> None:
        """值域内的 float TIFF 量化不告警（避免噪音）。"""
        img = np.array([[[0.0, 0.5, 1.0]]], dtype=np.float32)
        out = tmp_path / "ldr.tif"
        with caplog.at_level(logging.WARNING,
                             logger="src.shared.io.file_io"):
            assert FileIO.save_image(str(out), img) is True
        assert "TIFF export clips float pixels" not in caplog.text

    def test_uint16_passthrough_no_warning(self, tmp_path, caplog) -> None:
        """整型输入原样透传，不触发 float 裁剪告警。"""
        img = np.full((2, 2, 3), 60000, dtype=np.uint16)
        out = tmp_path / "int16.tif"
        with caplog.at_level(logging.WARNING,
                             logger="src.shared.io.file_io"):
            assert FileIO.save_image(str(out), img) is True
        assert "TIFF export clips float pixels" not in caplog.text


# ---------------------------------------------------------------------------
# AUD-34100：from_dict 外部数据校验 + store 跳过坏条目
# ---------------------------------------------------------------------------


class TestAud34100FromDictValidation:

    def test_valid_minimal_dict_still_works(self) -> None:
        p = LayoutProfile.from_dict({"product_mode": "color",
                                     "page_id": "Color"})
        assert p.visible_panels == ()
        assert p.qt_state_hex == ""
        assert p.schema_version == LAYOUT_PROFILE_SCHEMA_VERSION

    def test_non_string_product_mode_raises(self) -> None:
        with pytest.raises(ValueError):
            LayoutProfile.from_dict({"product_mode": 5, "page_id": "Color"})

    def test_missing_product_mode_raises(self) -> None:
        with pytest.raises(ValueError):
            LayoutProfile.from_dict({"page_id": "Color"})

    def test_non_string_qt_state_hex_falls_back(self) -> None:
        """qt_state_hex 非 str 时回退 ""——杜绝后续 .encode() AttributeError。"""
        p = LayoutProfile.from_dict({"product_mode": "studio",
                                     "page_id": "Color",
                                     "qt_state_hex": 12345})
        assert isinstance(p.qt_state_hex, str)
        assert p.qt_state_hex == ""

    def test_malformed_visible_panels_dropped(self) -> None:
        p = LayoutProfile.from_dict({
            "product_mode": "studio", "page_id": "Color",
            "visible_panels": ["color.grading", 42, None, "color.curves"],
        })
        assert p.visible_panels == ("color.grading", "color.curves")

    def test_malformed_floating_panels_dropped(self) -> None:
        p = LayoutProfile.from_dict({
            "product_mode": "studio", "page_id": "Color",
            "floating_panels": {"ok": {"x": 1}, "bad": 7},
        })
        assert set(p.floating_panels) == {"ok"}

    def test_non_int_schema_version_falls_back(self) -> None:
        for bad in ("1", 1.5, True, None):
            p = LayoutProfile.from_dict({
                "product_mode": "studio", "page_id": "Color",
                "schema_version": bad,
            })
            assert p.schema_version == LAYOUT_PROFILE_SCHEMA_VERSION

    def test_get_profile_skips_malformed_entry(self, tmp_path) -> None:
        """单条坏数据不得沿 get_profile 上抛（启动路径防崩溃）。"""
        cfg = _StubConfig({
            "layout_profiles": {
                "studio": {
                    "Color": {"page_id": "Color"},  # 缺 product_mode
                    "Edit": {"product_mode": "studio", "page_id": "Edit",
                             "qt_state_hex": "aabb"},
                }
            }
        })
        store = LayoutStore(config=cfg)
        assert store.get_profile("studio", "Color") is None
        edit = store.get_profile("studio", "Edit")
        assert edit is not None and edit.qt_state_hex == "aabb"

    def test_get_profiles_for_mode_skips_malformed_entries(self) -> None:
        cfg = _StubConfig({
            "layout_profiles": {
                "studio": {
                    "Bad": {"qt_state_hex": "zz"},          # 缺必填字段
                    "Good": {"product_mode": "studio",
                             "page_id": "Good", "qt_state_hex": "cc"},
                }
            }
        })
        store = LayoutStore(config=cfg)
        profiles = store.get_profiles_for_mode("studio")
        assert set(profiles) == {"Good"}


# ---------------------------------------------------------------------------
# AUD-34102：workspace.*/advanced.* 面板不得被特性过滤剔除
# ---------------------------------------------------------------------------


class TestAud34102WorkspacePanelsNotFiltered:

    def test_workspace_and_advanced_survive_empty_features(self) -> None:
        store = LayoutStore()
        profile = LayoutProfile(
            product_mode="color", page_id="Color",
            visible_panels=("workspace.viewport", "workspace.library",
                            "workspace.layers", "advanced.script",
                            "color.grading"),
        )
        filtered = store.filter_panels(profile, enabled_features=set())
        assert "workspace.viewport" in filtered.visible_panels
        assert "workspace.library" in filtered.visible_panels
        assert "workspace.layers" in filtered.visible_panels
        assert "advanced.script" in filtered.visible_panels
        assert "color.grading" not in filtered.visible_panels

    def test_default_color_layout_keeps_workspace_panels(self) -> None:
        """get_or_default + enabled_features 不得丢默认布局的工作区面板。"""
        store = LayoutStore()
        result = store.get_or_default("color", "Color",
                                      enabled_features={"color"})
        for panel in ("workspace.viewport", "workspace.library",
                      "workspace.layers"):
            assert panel in result.visible_panels

    def test_feature_panels_still_filtered(self) -> None:
        store = LayoutStore()
        profile = LayoutProfile(
            product_mode="studio", page_id="Edit",
            visible_panels=("edit.media_pool", "composite.node_graph"),
        )
        filtered = store.filter_panels(profile,
                                       enabled_features={"composite"})
        assert "edit.media_pool" not in filtered.visible_panels
        assert "composite.node_graph" in filtered.visible_panels


# ---------------------------------------------------------------------------
# AUD-34103：mode-scoped legacy page_layouts 迁移
# ---------------------------------------------------------------------------


class TestAud34103ModeScopedLegacyMigration:

    def test_mode_scoped_bucket_migrates(self, monkeypatch) -> None:
        """mode-scoped {mode: {page: hex}} 必须迁入当前模式的桶。"""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        cfg = _StubConfig({
            "page_layouts": {
                "color": {"Color": "cafe00", "Edit": "beef01"},
                "studio": {"Color": "deadbeef"},
            },
        })
        store = LayoutStore(config=cfg)
        assert store.migrate_legacy_page_layouts() == 2
        p = store.get_profile("color", "Color")
        assert p is not None and p.qt_state_hex == "cafe00"

    def test_all_bucket_fallback(self, monkeypatch) -> None:
        """当前模式无桶时回退 "all" 桶（与 dock 读取端一致）。"""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        cfg = _StubConfig({
            "page_layouts": {
                "all": {"Color": "a1b2c3"},
                "edit": {"Color": "ffffff"},
            },
        })
        store = LayoutStore(config=cfg)
        assert store.migrate_legacy_page_layouts() == 1
        p = store.get_profile("color", "Color")
        assert p is not None and p.qt_state_hex == "a1b2c3"

    def test_no_matching_bucket_migrates_zero(self, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        cfg = _StubConfig({"page_layouts": {"edit": {"Color": "x"},
                                            "deliver": {"Color": "y"}}})
        store = LayoutStore(config=cfg)
        assert store.migrate_legacy_page_layouts() == 0

    def test_flat_shape_still_migrates_and_skips_non_str(self) -> None:
        """扁平 {page: hex}（含坏叶子）行为保持：仅迁 str 叶子。"""
        cfg = _StubConfig({
            "page_layouts": {"Color": "00aabb", "BadEntry": 12345},
        })
        store = LayoutStore(config=cfg)
        assert store.migrate_legacy_page_layouts() == 1

    def test_mode_scoped_non_str_leaf_skipped(self, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        cfg = _StubConfig({
            "page_layouts": {"color": {"Color": "ok", "Bad": 99}},
        })
        store = LayoutStore(config=cfg)
        assert store.migrate_legacy_page_layouts() == 1
        assert store.get_profile("color", "Bad") is None


# ---------------------------------------------------------------------------
# AUD-34201：log 文件路径防护
# ---------------------------------------------------------------------------


class TestAud34201LogPathGuard:

    @pytest.fixture()
    def root_guard(self):
        """快照/恢复 root logger，避免污染其它测试。"""
        root = logging.getLogger()
        handlers = list(root.handlers)
        level = root.level
        yield root
        root.handlers = handlers
        root.setLevel(level)

    def test_bare_filename_degrades_or_resolves_not_crash(
            self, tmp_path, monkeypatch, root_guard) -> None:
        """裸文件名不再触发 os.makedirs("") FileNotFoundError。"""
        monkeypatch.chdir(tmp_path)
        cfg = _StubConfig({"enable_file_logging": True,
                           "log_file_path": "topos.log"})
        mgr = LogManager(cfg)
        mgr._setup()  # 不得抛异常
        expected = os.path.join(str(tmp_path), "topos.log")
        assert os.path.exists(expected)

    def test_unwritable_path_degrades_to_console_only(
            self, tmp_path, root_guard) -> None:
        """路径不可写 → 降级仅控制台，不得上抛（启动/偏好路径防崩溃）。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        cfg = _StubConfig({"enable_file_logging": True,
                           "log_file_path":
                               str(blocker / "sub" / "topos.log")})
        mgr = LogManager(cfg)
        mgr._setup()  # 不得抛异常
        assert mgr._file_handler is None
        # 控制台 handler 仍然在位
        assert mgr._console_handler is not None
        assert mgr._console_handler in logging.getLogger().handlers

    def test_directory_as_logfile_degrades(self, tmp_path, root_guard) -> None:
        """log_file_path 指向目录（open 必败）→ 降级而非崩溃。"""
        cfg = _StubConfig({"enable_file_logging": True,
                           "log_file_path": str(tmp_path)})
        mgr = LogManager(cfg)
        mgr._setup()  # 不得抛异常
        assert mgr._file_handler is None

    def test_get_log_file_path_matches_written_file(
            self, tmp_path, monkeypatch, root_guard) -> None:
        """UI 显示路径与实际写入路径一致（单一解析源）。"""
        monkeypatch.chdir(tmp_path)
        cfg = _StubConfig({"enable_file_logging": True,
                           "log_file_path": "topos.log"})
        mgr = LogManager(cfg)
        mgr._setup()
        assert os.path.samefile(mgr.get_log_file_path(),
                                os.path.join(str(tmp_path), "topos.log"))
