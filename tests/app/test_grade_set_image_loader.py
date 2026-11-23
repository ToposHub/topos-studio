"""Exercise photo decoding, cancellation and reuse across the Qt boundary."""
from __future__ import annotations

from pathlib import Path
from threading import Event, get_ident

import numpy as np
from PyQt6.QtCore import QObject

from src.app.controllers.grade_set_image_loader import GradeSetImageLoader
from src.shared.io.file_io import FileIO


def test_photo_loader_reuses_still_pixels_until_the_file_changes(qtbot, monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"first source")
    calls = []
    ui_thread = get_ident()

    def decode(path: str, *, use_preview_cache: bool) -> tuple:
        assert not use_preview_cache
        calls.append((path, get_ident()))
        return tuple(np.zeros((3, 4, 3), dtype=np.uint8) for _ in range(3))

    monkeypatch.setattr(FileIO, "load_image", decode)
    parent = QObject()
    loader = GradeSetImageLoader(parent)
    try:
        with qtbot.waitSignal(loader.ready) as first:
            loader.request(str(source))
        with qtbot.waitSignal(loader.ready) as second:
            loader.request(str(source))
        assert len(calls) == 1 and calls[0][1] != ui_thread
        assert first.args[2] is second.args[2]
        assert all(not pixels.flags.writeable for pixels in first.args[2].pixels)

        source.write_bytes(b"updated source")
        with qtbot.waitSignal(loader.ready) as changed:
            loader.request(str(source))
        assert len(calls) == 2 and not first.args[2].matches(str(source))
        assert changed.args[2].matches(str(source))
    finally:
        loader.shutdown()


def test_photo_loader_skips_outdated_clicks_and_cancels_cached_delivery(qtbot, monkeypatch,
                                                                      tmp_path: Path) -> None:
    files = [tmp_path / name for name in ("first.jpg", "second.jpg", "third.jpg")]
    for path in files:
        path.write_bytes(b"source")
    first, second, third = map(str, files)
    entered, release = Event(), Event()
    calls, presented = [], []

    def decode(path: str, *, use_preview_cache: bool) -> tuple:
        calls.append(path)
        if path == first:
            entered.set()
            assert release.wait(5)
        return tuple(np.zeros((3, 4, 3), dtype=np.uint8) for _ in range(3))

    monkeypatch.setattr(FileIO, "load_image", decode)
    parent = QObject()
    loader = GradeSetImageLoader(parent)
    loader.ready.connect(lambda _serial, path, _photo: presented.append(path))
    try:
        loader.request(first)
        qtbot.waitUntil(entered.is_set)
        loader.request(second)
        loader.request(third)
        with qtbot.waitSignal(loader.ready):
            release.set()
        assert calls == [first, third]
        assert presented == [third]
        loader.request(third)
        loader.cancel()
        qtbot.wait(10)
        assert presented == [third]
    finally:
        release.set()
        loader.shutdown()


def test_jpeg_can_open_while_a_raw_decode_is_still_running(qtbot, monkeypatch, tmp_path: Path) -> None:
    raw, jpeg = (tmp_path / name for name in ("first.arw", "second.jpg"))
    raw.write_bytes(b"source")
    jpeg.write_bytes(b"source")
    entered, release = Event(), Event()

    def decode(path: str, *, use_preview_cache: bool) -> tuple:
        if path == str(raw):
            entered.set()
            assert release.wait(5)
        return tuple(np.zeros((3, 4, 3), dtype=np.uint8) for _ in range(3))

    monkeypatch.setattr(FileIO, "load_image", decode)
    monkeypatch.setattr(FileIO, "read_camera_white_balance", lambda _path: [2.0, 1.0, 1.5, 1.0])
    parent = QObject()
    loader = GradeSetImageLoader(parent)
    try:
        loader.request(str(raw))
        qtbot.waitUntil(entered.is_set)
        with qtbot.waitSignal(loader.ready) as loaded:
            loader.request(str(jpeg))
        assert loaded.args[1] == str(jpeg)
        assert not release.is_set()
    finally:
        release.set()
        loader.shutdown()


def test_neighbor_prefetch_never_opens_a_photo_and_foreground_reuses_it(qtbot, monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "following.jpg"
    source.write_bytes(b"source")
    calls = []

    def decode(path: str, *, use_preview_cache: bool) -> tuple:
        calls.append(path)
        return tuple(np.zeros((3, 4, 3), dtype=np.uint8) for _ in range(3))

    monkeypatch.setattr(FileIO, "load_image", decode)
    parent = QObject()
    loader = GradeSetImageLoader(parent)
    presented = []
    loader.ready.connect(lambda _serial, path, _photo: presented.append(path))
    try:
        loader.prefetch(str(source))
        qtbot.waitUntil(lambda: str(source) in loader._cache)
        assert not presented
        with qtbot.waitSignal(loader.ready):
            loader.request(str(source))
        assert presented == calls == [str(source)]
    finally:
        loader.shutdown()


def test_clicking_a_different_still_does_not_wait_for_prefetch(qtbot, monkeypatch, tmp_path: Path) -> None:
    neighbor = tmp_path / "neighbor.jpg"
    clicked = tmp_path / "clicked.jpg"
    for path in (neighbor, clicked):
        path.write_bytes(b"source")
    entered, release = Event(), Event()

    def decode(path: str, *, use_preview_cache: bool) -> tuple:
        if path == str(neighbor):
            entered.set()
            assert release.wait(5)
        return tuple(np.zeros((3, 4, 3), dtype=np.uint8) for _ in range(3))

    monkeypatch.setattr(FileIO, "load_image", decode)
    parent = QObject()
    loader = GradeSetImageLoader(parent)
    try:
        loader.prefetch(str(neighbor))
        qtbot.waitUntil(entered.is_set)
        with qtbot.waitSignal(loader.ready) as loaded:
            loader.request(str(clicked))
        assert loaded.args[1] == str(clicked)
        assert not release.is_set()
    finally:
        release.set()
        loader.shutdown()
