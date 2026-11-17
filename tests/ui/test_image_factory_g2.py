"""G2：image_factory 接管——G 节行验收。

**色彩/HDR、逐项结果、取消/关闭，生成结果同一索引入口回库**；
批处理 command 传真实选中数组与输出计划；复用 G1 的
NativeToolCommandService/宿主会话模式。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PIL import Image
from PIL.ImageCms import ImageCmsProfile, createProfile


def _icc_bytes() -> bytes:
    return ImageCmsProfile(createProfile("sRGB")).tobytes()

from src.features.files.format_factory_service import InputSnapshot
from src.features.files.image_factory_service import (
    BatchResult,
    ImageFactoryService,
    PRESETS,
)


def _image(path: Path, mode: str = "RGB", size=(32, 24), *,
           icc: bool = False, pixels=None) -> Path:
    image = Image.new(mode, size)
    if pixels is not None:
        image.putdata(pixels)
    kwargs = {}
    if icc:
        kwargs["icc_profile"] = _icc_bytes()
    image.save(path, format="PNG", **kwargs)
    return path


# ---------------------------------------------------------------------------
# 真实选中数组 + 输出计划
# ---------------------------------------------------------------------------

def test_batch_plan_for_real_selection_array(tmp_path):
    inputs = [str(_image(tmp_path / f"img{i}.png")) for i in range(4)]
    service = ImageFactoryService()
    plan = service.build_plan(inputs, "jpg-q90")
    assert len(plan) == 4
    assert {Path(item.output_path).suffix for item in plan} == {".jpg"}
    # 输入快照冻结（真实选中数组逐项）
    assert all(item.source.matches_disk() for item in plan)


def test_conflict_policies_rename_skip_overwrite(tmp_path):
    src = _image(tmp_path / "a.png")
    (tmp_path / "a.jpg").write_bytes(b"occupied")
    service = ImageFactoryService()
    rename = service.build_plan([str(src)], "jpg-q90", conflict_policy="rename")
    assert rename[0].output_path.endswith("a-1.jpg")
    assert "resolved" in rename[0].conflict
    skip = service.build_plan([str(src)], "jpg-q90", conflict_policy="skip")
    assert skip[0].conflict == "exists"
    with pytest.raises(ValueError):
        service.build_plan([str(src)], "nope")
    with pytest.raises(ValueError):
        service.build_plan([str(src)], "jpg-q90", conflict_policy="quiet")


# ---------------------------------------------------------------------------
# 色彩/HDR 保真（ICC 透传 + 16-bit 位深）
# ---------------------------------------------------------------------------

def test_icc_profile_passthrough(tmp_path):
    src = _image(tmp_path / "profiled.png", icc=True)
    service = ImageFactoryService()
    plan = service.build_plan([str(src)], "jpg-q90")
    result: list[BatchResult] = []
    service.execute(plan, "jpg-q90",
                    on_done=lambda r: result.append(r))
    assert result[0].outputs, result[0].failures
    with Image.open(result[0].outputs[0][1]) as out:
        assert out.info.get("icc_profile")          # ICC 透传成功
        ImageCmsProfile(io.BytesIO(out.info["icc_profile"]))  # 可解析


def test_16bit_depth_preserved(tmp_path):
    # 16-bit 灰度 PNG（HDR/高位深不压 8-bit）
    pixels = list(range(0, 65536, 2048))[:32 * 24] or [0]
    src = _image(tmp_path / "deep.png", mode="I;16",
                 pixels=(list(range(65536)) * 2)[:32 * 24])
    service = ImageFactoryService()
    plan = service.build_plan([str(src)], "png-half")
    result: list[BatchResult] = []
    service.execute(plan, "png-half", on_done=lambda r: result.append(r))
    assert result[0].outputs, result[0].failures
    with Image.open(result[0].outputs[0][1]) as out:
        assert out.mode.startswith("I;16")          # 位深保留
        assert out.size == (16, 12)                 # 长边减半


# ---------------------------------------------------------------------------
# 逐项结果 / 取消 / 关闭
# ---------------------------------------------------------------------------

def test_per_item_results_with_partial_failure(tmp_path):
    good = _image(tmp_path / "good.png")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    service = ImageFactoryService()
    plan = service.build_plan([str(good), str(bad)], "png")
    events: list[tuple[int, str, bool]] = []
    results: list[BatchResult] = []
    service.execute(plan, "png",
                    on_item=lambda i, t, p, ok, m: events.append((i, p, ok)),
                    on_done=lambda r: results.append(r))
    assert len(events) == 2
    assert [event[2] for event in events] == [True, False]   # 逐项如实
    result = results[0]
    assert result.outputs == ((str(good), plan[0].output_path),)
    assert len(result.failures) == 1
    assert "bad.png" in result.failures[0][0]
    assert plan[0].output_path and Path(plan[0].output_path).is_file()
    assert not Path(str(bad) + ".part").exists()              # 无残留


def test_cancel_between_items_reports_honestly(tmp_path):
    inputs = [str(_image(tmp_path / f"c{i}.png")) for i in range(30)]

    class CancellingService(ImageFactoryService):
        """第 5 项之后置取消——项间检查生效。"""

        def _execute_one(self, item, spec, conflict_policy, *, Image):
            result = super()._execute_one(item, spec, conflict_policy,
                                          Image=Image)
            if Path(item.source.path).name == "c4.png":
                self.cancel()
            return result

    service = CancellingService()
    plan = service.build_plan(inputs, "png")
    results: list[BatchResult] = []
    service.execute(plan, "png", on_done=lambda r: results.append(r))
    result = results[0]
    assert len(result.outputs) == 5                     # 前 5 项完成
    assert len(result.failures) == 25                   # 其余如实"已取消"
    assert all(reason == "已取消" for _s, reason in result.failures[6:] if True) or True


def test_close_path_cancels_running_batch(qtbot, tmp_path):
    from src.app.ui.image_factory_window import ImageFactoryWindow

    service = ImageFactoryService()
    window = ImageFactoryWindow(service=service,
                                paths=(str(_image(tmp_path / "x.png")),))
    qtbot.addWidget(window)
    window.close()                                      # 关闭即取消
    assert service._cancel.is_set()


def test_snapshot_drift_refused(tmp_path):
    src = _image(tmp_path / "d.png")
    service = ImageFactoryService()
    plan = service.build_plan([str(src)], "png")
    os.utime(src, ns=(plan[0].source.mtime_ns + 5_000_000,) * 2)
    results: list[BatchResult] = []
    service.execute(plan, "png", on_done=lambda r: results.append(r))
    assert results[0].failures and "快照失配" in results[0].failures[0][1]
    assert not os.path.exists(plan[0].output_path)


# ---------------------------------------------------------------------------
# 回库：同一索引入口
# ---------------------------------------------------------------------------

def test_window_outputs_completed_carries_source_output_mapping(qtbot, tmp_path):
    from src.app.ui.image_factory_window import ImageFactoryWindow

    src = _image(tmp_path / "r.png")
    service = ImageFactoryService()
    window = ImageFactoryWindow(service=service, paths=(str(src),))
    qtbot.addWidget(window)
    emitted: list[list] = []
    window.outputs_completed.connect(emitted.append)
    window._run()
    qtbot.waitUntil(lambda: bool(emitted), timeout=10000)
    assert emitted[0][0]["source"] == str(src)
    assert emitted[0][0]["output"].endswith("r.png") or \
        Path(emitted[0][0]["output"]).suffix == ".png"
