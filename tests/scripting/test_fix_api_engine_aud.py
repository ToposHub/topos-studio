"""批 A（AUD-25900 / 25901 / 25909）修复回归测试。

覆盖：
- AUD-25900: `_interpolate_curve_255` 的 Y 轴方向与 CurvesWidget
  （(255 - y) / 255）一致，含真实 CurvesWidget 的脚本→UI 往返对比。
- AUD-25901: `ToposAPI.batch()` 在 output_dir=None 时写入源文件旁的
  graded/ 子目录，绝不原地覆盖源文件；显式 output_dir 行为不变。
- AUD-25909: `_run_script` / `_run_action` 捕获 SystemExit /
  KeyboardInterrupt 等 BaseException：script_finished 必发、异常不逃出
  run() 重写；sys.exit(0) 视为正常完成。
"""

from typing import List

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# AUD-25900: RGB 曲线 Y 轴方向
# ---------------------------------------------------------------------------

class TestCurveYAxisConvention:
    """曲线 y 坐标必须遵循 CurvesWidget 的倒置 Y 轴约定。"""

    def test_ui_identity_curve_is_identity_lut(self) -> None:
        from src.features.scripting.api import _interpolate_curve_255

        # CurvesWidget 默认曲线（恒等线）是 [(0, 255), (255, 0)]
        result = _interpolate_curve_255([(0, 255), (255, 0)])
        identity = np.arange(256, dtype=np.float32) / 255.0

        assert result.shape == (256,)
        assert np.allclose(result, identity, atol=1e-6)

    def test_inverted_ys_produce_inverted_lut(self) -> None:
        from src.features.scripting.api import _interpolate_curve_255

        # 旧约定的 "identity" [(0,0),(255,255)] 在 UI 约定下是反向曲线
        result = _interpolate_curve_255([(0, 0), (255, 255)])

        assert result[0] == pytest.approx(1.0, abs=1e-6)
        assert result[255] == pytest.approx(0.0, abs=1e-6)

    def test_single_point_follows_inverted_y(self) -> None:
        from src.features.scripting.api import _interpolate_curve_255

        result = _interpolate_curve_255([(128, 100)])
        assert np.allclose(result, (255.0 - 100.0) / 255.0, atol=1e-6)

    def test_script_to_ui_roundtrip_matches_curves_widget(self, qapp) -> None:
        """脚本计算的 LUT 必须与 UI 从 curves_data 重算的 LUT 一致。

        这是审计建议的脚本→UI→渲染 往返回归：此前两套公式方向相反，
        用户在 UI 中拖动任意控制点后图像会整体反相。
        """
        from src.features.scripting.api import _interpolate_curve_255
        from src.shared.ui.widgets.curves_widget import CurvesWidget

        curves = {
            "M": [(0, 255), (64, 180), (192, 40), (255, 0)],
            "R": [(0, 255), (128, 90), (255, 10)],
            "G": [(0, 240), (96, 200), (160, 60), (255, 0)],
            "B": [(0, 255), (255, 0)],
        }

        widget = CurvesWidget()
        widget.set_curves_data(curves, emit_changed=False)

        for channel, points in curves.items():
            api_lut = _interpolate_curve_255(points)
            widget_lut = widget._lut_cache[channel]
            assert np.allclose(api_lut, widget_lut, atol=1e-5), (
                f"channel {channel}: script LUT diverges from CurvesWidget LUT"
            )


# ---------------------------------------------------------------------------
# AUD-25901: batch() 不得原地覆盖源文件
# ---------------------------------------------------------------------------

class _FakeBridge:
    """batch() 仅需 is_aborted() 与 parent()（set_progress 用）。"""

    def is_aborted(self) -> bool:
        return False

    def parent(self):
        return None


class TestBatchOutputDirDefault:
    """output_dir=None 必须落到 graded/ 子目录而非源文件。"""

    def _make_api(self, saved: List[str]):
        from src.features.scripting.api import ToposAPI

        api = ToposAPI(engine=None, bridge=_FakeBridge())

        def _fake_open(path: str) -> None:
            pass

        def _fake_save(path: str, format=None, quality: int = 95) -> str:
            saved.append(path)
            return path

        api.open = _fake_open          # type: ignore[method-assign]
        api.save = _fake_save          # type: ignore[method-assign]
        api.set_progress = lambda current, total: None  # type: ignore[method-assign]
        return api

    def test_default_writes_to_graded_subdir_not_source(self) -> None:
        saved: List[str] = []
        api = self._make_api(saved)
        source = "/tmp/topos_aud_batch/shoot_001.jpg"

        results = api.batch([source], lambda topos: None)

        assert results == ["/tmp/topos_aud_batch/graded/shoot_001.jpg"]
        assert saved == ["/tmp/topos_aud_batch/graded/shoot_001.jpg"]
        assert saved[0] != source  # 源文件未被覆盖

    def test_default_graded_subdir_next_to_each_source(self, tmp_path) -> None:
        saved: List[str] = []
        api = self._make_api(saved)
        src_a = tmp_path / "a.jpg"
        src_b = tmp_path / "nested" / "b.png"
        src_b.parent.mkdir()

        api.batch([str(src_a), str(src_b)], lambda topos: None, format="PNG")

        assert saved[0] == str(tmp_path / "graded" / "a.png")
        assert saved[1] == str(tmp_path / "nested" / "graded" / "b.png")

    def test_explicit_output_dir_is_used_unchanged(self) -> None:
        saved: List[str] = []
        api = self._make_api(saved)
        source = "/tmp/topos_aud_batch/shoot_001.jpg"

        api.batch(
            [source], lambda topos: None,
            output_dir="/tmp/topos_aud_out", format="TIFF",
        )

        assert saved == ["/tmp/topos_aud_out/shoot_001.tif"]


# ---------------------------------------------------------------------------
# AUD-25909: BaseException 不得逃出 run() 重写
# ---------------------------------------------------------------------------

class _StubAPI:
    """_run_script/_run_action 只用到 _auto_undo_snapshot。"""

    def _auto_undo_snapshot(self) -> None:
        pass


class TestRunScriptBaseException:
    """SystemExit / KeyboardInterrupt 必须被捕获且 script_finished 必发。"""

    def _make_engine(self, qapp):
        from src.features.scripting.engine import ScriptEngine

        engine = ScriptEngine(engine=None)
        recorded = {
            "finished": [],
            "errors": [],
        }
        engine.script_finished.connect(
            lambda ok, msg: recorded["finished"].append((ok, msg))
        )
        engine.script_error.connect(
            lambda msg, line: recorded["errors"].append((msg, line))
        )
        return engine, recorded

    def test_system_exit_zero_reports_success(self, qapp) -> None:
        engine, recorded = self._make_engine(qapp)
        code = "import sys\nsys.exit(0)\n"

        # 不得让 SystemExit 逃出（否则测试直接失败）
        engine._run_script(code, None, _StubAPI())

        assert recorded["finished"] == [(True, "Script exited via sys.exit(0).")]

    def test_system_exit_nonzero_reports_failure_with_line(self, qapp) -> None:
        engine, recorded = self._make_engine(qapp)
        code = "x = 1\nimport sys\nsys.exit('boom')\n"

        engine._run_script(code, None, _StubAPI())

        assert len(recorded["finished"]) == 1
        ok, msg = recorded["finished"][0]
        assert ok is False
        assert "SystemExit" in msg and "boom" in msg
        # script_error 携带 sys.exit 所在行
        assert recorded["errors"][0][1] == 3

    def test_keyboard_interrupt_does_not_escape(self, qapp) -> None:
        engine, recorded = self._make_engine(qapp)
        code = "raise KeyboardInterrupt\n"

        engine._run_script(code, None, _StubAPI())

        assert len(recorded["finished"]) == 1
        ok, msg = recorded["finished"][0]
        assert ok is False
        assert "KeyboardInterrupt" in msg

    def test_regular_exception_path_unchanged(self, qapp) -> None:
        engine, recorded = self._make_engine(qapp)
        code = "a = 1\nb = 0\na / b\n"

        engine._run_script(code, None, _StubAPI())

        assert recorded["finished"] == [(False, "division by zero")]
        assert recorded["errors"][0][1] == 3  # 用户脚本帧行号

    def test_run_action_system_exit_does_not_escape(self, qapp) -> None:
        engine, recorded = self._make_engine(qapp)

        def _callback(api) -> None:
            raise SystemExit(2)

        engine._run_action(_callback, _StubAPI())

        assert len(recorded["finished"]) == 1
        ok, msg = recorded["finished"][0]
        assert ok is False
        assert msg == "SystemExit: 2"

    def test_run_action_exit_zero_reports_success(self, qapp) -> None:
        engine, recorded = self._make_engine(qapp)

        def _callback(api) -> None:
            raise SystemExit(0)

        engine._run_action(_callback, _StubAPI())

        assert recorded["finished"] == [(True, "Action exited via sys.exit(0).")]
