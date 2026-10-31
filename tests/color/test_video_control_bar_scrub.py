from src.features.color.ui.canvas import VideoControlBar


def test_video_control_bar_emits_live_scrub_signals(qapp) -> None:
    bar = VideoControlBar()
    bar.set_total_frames(100)
    started: list[int] = []
    moved: list[int] = []
    finished: list[int] = []
    seeks: list[int] = []
    bar.scrub_started.connect(started.append)
    bar.scrub_moved.connect(moved.append)
    bar.scrub_finished.connect(finished.append)
    bar.seek_requested.connect(seeks.append)

    bar.slider.setValue(10)
    bar._on_slider_pressed()
    bar.slider.setValue(12)
    bar._on_slider_changed(12)
    bar._on_slider_released()

    assert started == [10]
    assert moved[-1] == 12
    assert finished == [12]
    assert seeks == [12]
    assert bar.current_frame == 12


def test_video_control_bar_composite_mode_shows_fusion_ruler_and_io(qapp) -> None:
    bar = VideoControlBar()
    bar.set_total_frames(749)

    bar.show_for_video_with_io()
    bar.set_in_out_points(185, 748)
    bar.set_current_frame(575)

    assert bar.ruler_slider.isVisible() is True
    assert bar.slider.isVisible() is False
    assert bar.btn_mark_in.isVisible() is True
    assert bar.btn_mark_out.isVisible() is True
    assert bar.btn_clear.isVisible() is True
    assert bar.height() > 36
    assert bar.label_range_in.text() == "185.0"
    assert bar.label_range_out.text() == "748.0"
    assert bar.label_frame.text() == "575.0"

    bar.show_for_video()

    assert bar.ruler_slider.isVisible() is False
    assert bar.slider.isVisible() is True
    assert bar.btn_mark_in.isVisible() is False
