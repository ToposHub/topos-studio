"""R01：静帧覆盖保存的多文件事务（staging／备份／恢复／失败零残留）。

域件（``StillCaptureTransaction``）与控制器接线（``on_capture_still``）
两层验证，对应计划 R01 验收矩阵：新建与覆盖 × 成功／SaveRouter False／
抛异常／JPEG 写失败／取消／提交中断；失败后旧文件集合逐字节不变、零
新孤儿文件；成功时全部文件来自同一帧与 grade；播放恢复只发生在
“抓帧前确实在播”且 generation 未变／未关闭时。
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.features.color.core.still_capture_transaction import (
    STATE_INSTALLING,
    TXN_DIR_PREFIX,
    StillCaptureTransaction,
    StillCaptureTransactionError,
    recover_still_transactions,
)

QT_CANCEL = pytest.importorskip("PyQt6.QtWidgets")


# ── 公共工具 ─────────────────────────────────────────────────────────

def _family(folder: str, stem: str = "Still_fix") -> dict[str, str]:
    from src.features.color.core.preset_meta import (
        meta_path_for, still_exr_path_for, still_path_for,
        thumb_path_for,
    )
    topos = os.path.join(folder, f"{stem}.topos")
    return {
        "topos": topos,
        "thumb": thumb_path_for(topos),
        "still": still_path_for(topos),
        "exr": still_exr_path_for(topos),
        "exr_decl": still_exr_path_for(topos) + ".json",
        "meta": meta_path_for(topos),
    }


def _write_old_set(paths: dict[str, str], tag: str = "old") -> dict[str, bytes]:
    """预置旧文件集合，返回 name→bytes 供逐字节比对。"""
    old: dict[str, bytes] = {}
    for key, path in paths.items():
        data = f"{tag}-{key}".encode()
        with open(path, "wb") as fh:
            fh.write(data)
        old[key] = data
    return old


def _assert_set_bytes(paths: dict[str, str], expected: dict[str, bytes]) -> None:
    for key, data in expected.items():
        with open(paths[key], "rb") as fh:
            assert fh.read() == data, key


def _no_staging_left(folder: str) -> None:
    assert not [n for n in os.listdir(folder) if n.startswith(TXN_DIR_PREFIX)]


def _stage_new_set(txn: StillCaptureTransaction, paths: dict[str, str],
                   keys=("topos", "thumb", "still", "meta")) -> None:
    for key in keys:
        with open(txn.staged_path(paths[key]), "wb") as fh:
            fh.write(f"new-{key}".encode())
        txn.add(paths[key])


def _read_state(txn: StillCaptureTransaction) -> str:
    with open(os.path.join(txn.txn_dir, "manifest.json")) as fh:
        return json.load(fh)["state"]


# ── 域件：提交与回滚 ─────────────────────────────────────────────────

class TestTransactionCommitRollback:
    def test_commit_new_set_installs_all_and_cleans_staging(self, tmp_path):
        folder = str(tmp_path)
        paths = _family(folder)
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn, paths)
        assert txn.state == "prepared"
        txn.commit()
        for key in ("topos", "thumb", "still", "meta"):
            with open(paths[key], "rb") as fh:
                assert fh.read() == f"new-{key}".encode()
        assert txn.state == "committed"
        _no_staging_left(folder)

    def test_commit_overwrite_replaces_old_set(self, tmp_path):
        folder = str(tmp_path)
        paths = _family(folder)
        _write_old_set(paths)
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn, paths)
        txn.commit()
        for key in ("topos", "thumb", "still", "meta"):
            with open(paths[key], "rb") as fh:
                assert fh.read() == f"new-{key}".encode()
        _no_staging_left(folder)  # 备份随 staging 一起清理

    def test_rollback_restores_old_set_byte_identical(self, tmp_path):
        folder = str(tmp_path)
        paths = _family(folder)
        old = _write_old_set(paths)
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn, paths)
        txn.rollback()
        _assert_set_bytes(paths, old)
        _no_staging_left(folder)

    def test_commit_interruption_mid_install_restores_old_set(
            self, tmp_path, monkeypatch):
        """提交中断（installing 中途抛错）：旧集合逐字节恢复，零残留。"""
        import src.features.color.core.still_capture_transaction as mod
        folder = str(tmp_path)
        paths = _family(folder)
        old = _write_old_set(paths)
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn, paths)

        real_replace = os.replace
        fired = {"n": 0}

        def _flaky_replace(src, dst, *a, **kw):
            # 安装顺序 topos→thumb→still→meta：在安装 still 前崩，
            # 此刻新 topos/thumb 已就位（真正的事务中段）。
            if (os.path.abspath(dst) == os.path.abspath(paths["still"])
                    and fired["n"] == 0):
                fired["n"] += 1
                raise OSError("simulated commit interruption")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(mod.os, "replace", _flaky_replace)
        with pytest.raises(OSError):
            txn.commit()
        monkeypatch.setattr(mod.os, "replace", real_replace)

        _assert_set_bytes(paths, old)
        _no_staging_left(folder)

    def test_backing_phase_interruption_restores_old_set(
            self, tmp_path, monkeypatch):
        import src.features.color.core.still_capture_transaction as mod
        folder = str(tmp_path)
        paths = _family(folder)
        old = _write_old_set(paths)
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn, paths)

        real_replace = os.replace
        fired = {"n": 0}

        def _flaky_replace(src, dst, *a, **kw):
            # backing 阶段：把第二个旧文件挪进备份时崩（第一个已挪走）。
            if "/backup/" in os.path.abspath(dst).replace(os.sep, "/") \
                    and fired["n"] == 1:
                fired["n"] += 1
                raise OSError("simulated crash while backing up")
            if "/backup/" in os.path.abspath(dst).replace(os.sep, "/"):
                fired["n"] += 1
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(mod.os, "replace", _flaky_replace)
        with pytest.raises(OSError):
            txn.commit()
        monkeypatch.setattr(mod.os, "replace", real_replace)

        _assert_set_bytes(paths, old)
        _no_staging_left(folder)

    def test_drop_entry_drops_stale_sidecar_on_commit_restores_on_rollback(
            self, tmp_path):
        folder = str(tmp_path)
        paths = _family(folder)
        old = _write_old_set(paths)

        # 提交：本次集合没有 EXR → 过期 EXR/声明移除，其余新内容。
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn, paths, keys=("topos", "thumb", "still", "meta"))
        txn.drop(paths["exr"])
        txn.drop(paths["exr_decl"])
        txn.commit()
        assert not os.path.exists(paths["exr"])
        assert not os.path.exists(paths["exr_decl"])
        with open(paths["topos"], "rb") as fh:
            assert fh.read() == b"new-topos"

        # 回滚场景：同一配置在提交前回滚 → 旧 EXR 原样回来。
        _write_old_set(paths)
        txn2 = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn2, paths, keys=("topos", "thumb", "still", "meta"))
        txn2.drop(paths["exr"])
        txn2.drop(paths["exr_decl"])
        txn2.rollback()
        assert os.path.exists(paths["exr"])
        with open(paths["exr"], "rb") as fh:
            assert fh.read() == old["exr"]

    def test_staging_lives_inside_target_dir_same_volume(self, tmp_path):
        txn = StillCaptureTransaction(str(tmp_path), recover_leftovers=False)
        assert os.path.dirname(txn.txn_dir) == str(
            os.path.abspath(tmp_path))
        txn.rollback()

    def test_add_requires_staged_file_and_unique_entries(self, tmp_path):
        folder = str(tmp_path)
        paths = _family(folder)
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        with pytest.raises(StillCaptureTransactionError):
            txn.add(paths["topos"])  # staged 文件不存在
        with open(txn.staged_path(paths["topos"]), "wb") as fh:
            fh.write(b"x")
        txn.add(paths["topos"])
        with pytest.raises(StillCaptureTransactionError):
            txn.add(paths["topos"])  # 重复条目
        with pytest.raises(StillCaptureTransactionError):
            txn.drop(paths["topos"])
        txn.rollback()

    def test_context_manager_rolls_back_on_exception(self, tmp_path):
        folder = str(tmp_path)
        paths = _family(folder)
        old = _write_old_set(paths)
        with pytest.raises(RuntimeError):
            with StillCaptureTransaction(folder, recover_leftovers=False) as txn:
                _stage_new_set(txn, paths)
                raise RuntimeError("boom")
        _assert_set_bytes(paths, old)
        _no_staging_left(folder)


# ── 域件：进程退出恢复 ───────────────────────────────────────────────

class _CrashTxn(StillCaptureTransaction):
    """模拟进程退出：commit 失败后没有机会执行回滚／清理。"""

    def rollback(self) -> None:
        if self.state in ("committed", "rolled_back"):
            return
        # 不恢复也不清理：manifest 停在中断状态，现场留给 recovery。


class TestCrashRecovery:
    def _crash_mid_install(self, folder, paths, monkeypatch):
        import src.features.color.core.still_capture_transaction as mod
        txn = _CrashTxn(folder, recover_leftovers=False)
        _stage_new_set(txn, paths)
        real_replace = os.replace
        fired = {"n": 0}

        def _flaky(src, dst, *a, **kw):
            if (os.path.abspath(dst) == os.path.abspath(paths["topos"])
                    and fired["n"] == 0):
                fired["n"] += 1
                raise OSError("process exit mid-install")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(mod.os, "replace", _flaky)
        with pytest.raises(OSError):
            txn.commit()
        monkeypatch.setattr(mod.os, "replace", real_replace)
        assert os.path.isdir(txn.txn_dir)  # 遗留事务目录在场

    def test_recover_mid_installing_restores_old_set(self, tmp_path,
                                                     monkeypatch):
        folder = str(tmp_path)
        paths = _family(folder)
        old = _write_old_set(paths)
        self._crash_mid_install(folder, paths, monkeypatch)

        recovered = recover_still_transactions(folder)
        assert recovered  # 诊断返回恢复过的 txn id
        _assert_set_bytes(paths, old)
        _no_staging_left(folder)

    def test_recover_prepared_leftover_only_removes_staging(self, tmp_path):
        folder = str(tmp_path)
        paths = _family(folder)
        old = _write_old_set(paths)
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn, paths)  # prepared 状态即“进程退出”
        assert txn.state == "prepared"

        recovered = recover_still_transactions(folder)
        assert recovered
        _assert_set_bytes(paths, old)  # 目标从未被触碰
        _no_staging_left(folder)

    def test_recover_committed_leftover_keeps_new_set(self, tmp_path):
        folder = str(tmp_path)
        paths = _family(folder)
        _write_old_set(paths)
        txn = StillCaptureTransaction(folder, recover_leftovers=False)
        _stage_new_set(txn, paths)

        real_cleanup = StillCaptureTransaction.cleanup

        def _no_cleanup(self):
            pass  # 模拟 committed 后、清理前进程退出

        StillCaptureTransaction.cleanup = _no_cleanup
        try:
            txn.commit()
        finally:
            StillCaptureTransaction.cleanup = real_cleanup
        assert os.path.isdir(txn.txn_dir)

        recover_still_transactions(folder)
        with open(paths["topos"], "rb") as fh:
            assert fh.read() == b"new-topos"  # 新集合保持
        _no_staging_left(folder)

    def test_new_transaction_recovers_leftovers_first(self, tmp_path,
                                                      monkeypatch):
        folder = str(tmp_path)
        paths = _family(folder)
        old = _write_old_set(paths)
        self._crash_mid_install(folder, paths, monkeypatch)

        txn2 = StillCaptureTransaction(folder)  # 构造时先恢复遗留事务
        txn2.rollback()  # 测试不再使用新事务，收掉自己的 staging
        _assert_set_bytes(paths, old)
        _no_staging_left(folder)

    def test_recover_ignores_non_transaction_entries(self, tmp_path):
        folder = str(tmp_path)
        keeper = os.path.join(folder, "keep.txt")
        with open(keeper, "w") as fh:
            fh.write("keep")
        recover_still_transactions(folder)
        assert os.path.isfile(keeper)


# ── 控制器接线：on_capture_still 全链路 ──────────────────────────────

def _make_ctrl(tmp_path, monkeypatch, *, playing=False, video=False):
    """最小 LayerOpsMixin 宿主（复用 gallery 套件的形态，名字固定）。"""
    import numpy as np
    from src.app.controllers.layer_controller import LayerOpsMixin
    from src.features.color.core.layer import Layer

    monkeypatch.chdir(tmp_path)
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)

    img = np.full((8, 8, 3), 96, dtype=np.uint8)

    class _Engine:
        img_high = img
        layers = [Layer(name="s")]
        active_layer_index = 0

        def grab_viewport_image(self):
            return img

        def render_full_image_for_scope(self, max_dim):
            return None  # 无 float 帧 → 降级纯 JPEG 路径

    ctrl.engine = _Engine()
    ctrl.status_bar = MagicMock()
    for attr in ("grading_panel", "layer_panel", "plugin_panel",
                 "curves_widget", "hsl_curves_panel", "mixer_panel",
                 "effects_panel", "qualifier_panel", "lut_panel",
                 "warper_panel", "mask_panel", "info_panel"):
        setattr(ctrl, attr, MagicMock())
    ctrl._get_composite_graph_data = lambda: None
    ctrl._get_composite_state_data = lambda: None
    ctrl.current_file_is_video = video
    ctrl.current_file_path = None
    ctrl.save_router = MagicMock()

    def _fake_save(path, *_a, **_k):
        with open(path, "wb") as fh:
            fh.write(b"PK-new-topos")
        return True

    ctrl.save_router._save_single_asset_project.side_effect = _fake_save
    ctrl.presets_panel = MagicMock()
    presets_user = os.path.join(os.getcwd(), "Presets", "User")
    os.makedirs(presets_user, exist_ok=True)
    ctrl.presets_panel.get_current_folder.return_value = presets_user
    ctrl._page_context_manager = SimpleNamespace(
        get_current_context=lambda: SimpleNamespace(clip_id="clip-1"))
    ctrl._current_edit_project = None
    ctrl._new_still_name = lambda: "Still_fix"

    class _VideoController:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.is_playing = playing

        def is_video_loaded(self) -> bool:
            return True

        def stop_playback(self) -> None:
            self.calls.append("stop")
            self.is_playing = False

        def start_playback(self) -> None:
            self.calls.append("start")

    ctrl.video_controller = _VideoController()
    ctrl._render_current_video_frame_for_grading = lambda: (True, 42)
    # mock 的 StandardButton 必须透传真枚举：调用参数里的
    # Yes|No 与返回值比较才不会因“自动 child mock ≠ 真枚举”误判取消。
    box = MagicMock()
    box.StandardButton = QT_CANCEL.QMessageBox.StandardButton
    box.question.return_value = QT_CANCEL.QMessageBox.StandardButton.Yes
    monkeypatch.setattr(
        "src.app.controllers.layer_controller.QMessageBox", box)
    return ctrl


class TestCaptureStillTransactionIntegration:
    def test_overwrite_success_replaces_whole_family(self, tmp_path,
                                                     monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        paths = _family(folder)
        _write_old_set(paths)

        ctrl.on_capture_still()

        with open(paths["topos"], "rb") as fh:
            assert fh.read() == b"PK-new-topos"
        # 旧字节确实被替换（thumb/still 来自同一张新图，非旧占位内容）
        for key in ("thumb", "still"):
            assert os.path.getsize(paths[key]) > 0
            with open(paths[key], "rb") as fh:
                assert fh.read() != f"old-{key}".encode()
        from src.features.color.core.preset_meta import load_preset_meta
        assert load_preset_meta(paths["topos"]) is not None
        assert not os.path.exists(paths["exr"])  # 无 float 帧 → 无 EXR
        assert not os.path.exists(paths["exr_decl"])
        _no_staging_left(folder)
        ctrl.presets_panel.scan_presets.assert_called()

    def test_overwrite_save_refused_old_family_byte_identical(
            self, tmp_path, monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        paths = _family(folder)
        old = _write_old_set(paths)

        ctrl.save_router._save_single_asset_project.side_effect = None
        ctrl.save_router._save_single_asset_project.return_value = False
        ctrl.on_capture_still()

        _assert_set_bytes(paths, old)
        _no_staging_left(folder)
        ctrl.status_bar.showMessage.assert_called()

    def test_overwrite_save_raises_old_family_byte_identical(
            self, tmp_path, monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        paths = _family(folder)
        old = _write_old_set(paths)

        def _boom(*_a, **_k):
            raise RuntimeError("disk full")

        ctrl.save_router._save_single_asset_project.side_effect = _boom
        ctrl.on_capture_still()

        _assert_set_bytes(paths, old)
        _no_staging_left(folder)

    def test_jpeg_write_failure_keeps_old_family(self, tmp_path,
                                                 monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        paths = _family(folder)
        old = _write_old_set(paths)

        monkeypatch.setattr(
            "src.features.color.core.preset_meta.write_still_images",
            lambda *_a, **_k: (None, None))
        ctrl.on_capture_still()

        _assert_set_bytes(paths, old)
        _no_staging_left(folder)
        ctrl.save_router._save_single_asset_project.assert_not_called()

    def test_cancel_writes_nothing(self, tmp_path, monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        paths = _family(folder)
        old = _write_old_set(paths)

        box = MagicMock()
        box.StandardButton = QT_CANCEL.QMessageBox.StandardButton
        box.question.return_value = QT_CANCEL.QMessageBox.StandardButton.No
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", box)
        ctrl.on_capture_still()

        _assert_set_bytes(paths, old)
        _no_staging_left(folder)
        ctrl.save_router._save_single_asset_project.assert_not_called()

    def test_commit_interruption_during_capture_restores_old_family(
            self, tmp_path, monkeypatch):
        import src.features.color.core.still_capture_transaction as mod
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        paths = _family(folder)
        old = _write_old_set(paths)

        real_replace = os.replace
        topos_dst = os.path.abspath(paths["topos"])
        fired = {"n": 0}

        def _flaky(src, dst, *a, **kw):
            if os.path.abspath(dst) == topos_dst and fired["n"] == 0:
                fired["n"] += 1
                raise OSError("simulated interruption")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(mod.os, "replace", _flaky)
        ctrl.on_capture_still()
        monkeypatch.setattr(mod.os, "replace", real_replace)

        _assert_set_bytes(paths, old)
        _no_staging_left(folder)
        ctrl.status_bar.showMessage.assert_called()

    def test_degraded_exr_drops_stale_exr_on_success(self, tmp_path,
                                                     monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        paths = _family(folder)
        _write_old_set(paths)  # 旧集合带 EXR

        # EXR 编码失败（write_still_exr → None）→ 降级纯 JPEG 成功
        monkeypatch.setattr(
            "src.features.color.core.preset_meta.write_still_exr",
            lambda *_a, **_k: None)
        import numpy as np
        ctrl.engine.render_full_image_for_scope = (
            lambda max_dim: np.zeros((4, 4, 3), dtype=np.float32))
        ctrl.on_capture_still()

        assert not os.path.exists(paths["exr"])  # 过期 EXR 不保留
        assert not os.path.exists(paths["exr_decl"])
        with open(paths["topos"], "rb") as fh:
            assert fh.read() == b"PK-new-topos"
        _no_staging_left(folder)

    def test_meta_write_failure_drops_stale_meta(self, tmp_path,
                                                 monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        paths = _family(folder)
        _write_old_set(paths)

        monkeypatch.setattr(
            "src.features.color.core.preset_meta.save_preset_meta",
            lambda *_a, **_k: False)
        ctrl.on_capture_still()

        assert not os.path.exists(paths["meta"])  # 不留描述旧帧的 meta
        with open(paths["topos"], "rb") as fh:
            assert fh.read() == b"PK-new-topos"
        _no_staging_left(folder)


class TestCapturePlaybackRestorePolicy:
    def _wait(self):
        from PyQt6.QtTest import QTest
        QTest.qWait(160)

    def test_restore_skipped_when_generation_changed(self, qapp, tmp_path,
                                                     monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch, playing=True, video=True)
        old_vc = ctrl.video_controller
        ctrl.on_capture_still()
        # 抓帧后换 clip（video_controller 换实例）→ 旧源不得重新起播
        new_vc = MagicMock()
        ctrl.video_controller = new_vc
        self._wait()
        assert old_vc.calls == ["stop"]  # 旧的没有 start
        new_vc.start_playback.assert_not_called()  # 新的也不被波及

    def test_restore_skipped_after_pipeline_shutdown_flag(self, qapp,
                                                          tmp_path,
                                                          monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch, playing=True, video=True)
        ctrl.on_capture_still()
        ctrl._pipeline_shutdown_done = True
        self._wait()
        assert ctrl.video_controller.calls == ["stop"]

    def test_restore_skipped_when_arbiter_shutdown(self, qapp, tmp_path,
                                                   monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch, playing=True, video=True)
        arbiter = MagicMock()
        arbiter.is_shutdown.return_value = True
        ctrl._viewport_playback_coordinator = arbiter
        ctrl.on_capture_still()
        self._wait()
        assert ctrl.video_controller.calls == ["stop"]

    def test_restore_runs_when_generation_unchanged(self, qapp, tmp_path,
                                                    monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch, playing=True, video=True)
        ctrl.on_capture_still()
        self._wait()
        assert ctrl.video_controller.calls == ["stop", "start"]

    def test_paused_stays_paused_in_video_mode(self, qapp, tmp_path,
                                                    monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch, playing=False, video=True)
        ctrl.on_capture_still()
        self._wait()
        assert ctrl.video_controller.calls == []
