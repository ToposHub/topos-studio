"""Open real 4K media with the Files lightbox player host and check frame geometry."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT))
if sys.platform == "darwin":
    import os
    sys.setdlopenflags((sys.getdlopenflags() & ~getattr(os, "RTLD_GLOBAL", 0)) | os.RTLD_LOCAL)

from src.shared.playback.player_host import NativePlayerHost


def wait_for(predicate, label: str) -> None:
    end = time.monotonic() + 8
    while not predicate():
        if time.monotonic() >= end:
            raise RuntimeError(label)
        QTest.qWait(20)


def case(host: NativePlayerHost, path: Path, route: str) -> dict:
    errors = []
    host.failed.connect(errors.append)
    host.play(str(path))
    assert host.current_route == route
    player = host._current
    assert player is not None
    wait_for(lambda: player._presented_frame_size == (3840, 2160), f"{path}: full frame {errors}")
    assert player._controls.isVisible()
    assert "3840×2160" in player.lbl_playback_resolution.text()
    transport = player._controls.geometry()
    assert player.rect().contains(transport)
    host.set_lightbox_tools(False)
    assert player._controls.isVisible()
    if player._playing:
        QTest.mouseClick(player.btn_play, Qt.MouseButton.LeftButton)
    assert not player._playing and player._controls.isVisible()
    reports = [{"mode": "full", "size": player._presented_frame_size,
                "text": player.lbl_playback_resolution.text()}]
    for idx, expected in ((2, (1920,1080)), (4,(960,540)), (1,(3840,2160))):
        menu = player._build_resolution_menu()
        if idx == 1:
            action = next(a for a in menu.actions() if a.isCheckable())
        else:
            action = next(a for a in menu.actions() if a.text() == f"1/{idx}")
        action.trigger()
        wait_for(lambda: player._presented_frame_size == expected,
                 f"{path}: mode 1/{idx}, observed {player._presented_frame_size}")
        reports.append({"mode": f"1/{idx}", "size": player._presented_frame_size,
                        "text": player.lbl_playback_resolution.text()})
        assert not player._playing
        menu.deleteLater()
    actual = min(int(player._session.frame_count) - 1, 1604)
    player.slider.setSliderPosition(actual)
    player.slider.sliderReleased.emit()
    wait_for(lambda: player._shown_index == actual, f"{path}: paused transport seek")
    assert player._controls.isVisible()
    assert not errors
    host.failed.disconnect(errors.append)
    return dict(route=route, size_checks=reports, seek_frame=actual,
                transport_visible=True, gpu_canvas=bool(getattr(player.canvas, "gpu_active", False)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prores", type=Path)
    args = parser.parse_args()
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication([])
    panel = QWidget()
    panel.setWindowTitle("Files player resolution verification")
    panel.resize(800, 540)
    panel.show()
    host = NativePlayerHost(panel, lightbox=True)
    tests = [("topos", ROOT/'tests/test_videos/TOPOS 422Proxy/北海公园 - TOPOS 422Proxy - 4K 细节优化.mov')]
    if args.prores is not None:
        tests.append(("libav", args.prores.resolve(strict=True)))
    results = []
    try:
        for route, path in tests:
            results.append(case(host, path, route))
            print(results[-1], flush=True)
            QTest.qWait(400)
            picture = panel.grab()
            if not picture.isNull():
                picture.save(str(OUT/f"library_{route}_full_resolution.png"))
        (OUT/"library_full_playback.json").write_text(json.dumps(results, indent=2, ensure_ascii=False)+"\n")
    finally:
        host.shutdown()
        panel.close()
        app.processEvents()


if __name__ == "__main__":
    main()
