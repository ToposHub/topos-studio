"""Batch-D wave-D1 group-8 regression tests (AUD-3100..3104, AUD-34001/34002).

Each test pins the root-cause fix for its remediation entry:

* AUD-3100 — Composite transport-bar slot wiring is symmetric across the
  timeline / comp-clip playback paths: whichever path takes over (or neither)
  first tears down the closures of both paths, so the shared viewport bar
  never drives ``controller.seek`` and ``page.comp_seek`` simultaneously.
* AUD-3101 — the static-preview retry-exhausted branch no longer reschedules
  itself forever; exact-seek controllers stop after one forced seek, legacy
  controllers get a hard cap, and giving up disarms the clip-switch guard.
* AUD-3102 — the Edit→Color color-tag probe runs on a background thread and
  marshals back to the UI thread via a queued signal (deterministic here via
  an injected inline spawner).
* AUD-3103 — the shared topmost-enabled-video-clip resolver backs both the
  Edit→Color playhead route and the Composite playhead follow.
* AUD-3104 — preview ODT failures surface as warnings (with stack) and
  ``set_display_space`` rejections fall back to transfer-only.
* AUD-34001 — ``read_port_config`` narrows its except clause, validates the
  host against a loopback whitelist and the port value/range.
* AUD-34002 — ``FlaskWorker.stop`` works before the server exists; a worker
  that survives ``terminate()`` is not dropped while still running.
"""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from typing import Any, Callable, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject

from src.app.controllers import page_controller as pc
from src.app.controllers.page_controller import PageManagementMixin
# ═══════════════════════════════════════════════════════════════════════
# Fake infrastructure (mirrors tests/app/test_page_controller_coverage.py)
# ═══════════════════════════════════════════════════════════════════════


class _FakeSignal:
    def __init__(self) -> None:
        self.slots: List[Callable[..., Any]] = []

    def connect(self, slot: Callable[..., Any]) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: Callable[..., Any]) -> None:
        for index, existing in enumerate(self.slots):
            if existing == slot:
                del self.slots[index]
                return
        raise TypeError("no such slot connected")

    def emit(self, *args: Any) -> None:
        for slot in list(self.slots):
            slot(*args)


_BAR_SIGNAL_NAMES: Tuple[str, ...] = (
    "seek_requested", "scrub_started", "scrub_moved", "scrub_finished",
    "play_toggled", "next_frame", "prev_frame", "first_frame",
    "last_frame", "mark_in_requested", "mark_out_requested",
    "clear_in_out_requested", "loop_toggled",
)


class _FakeBar:
    def __init__(self) -> None:
        for name in _BAR_SIGNAL_NAMES:
            setattr(self, name, _FakeSignal())
        self.btn_loop = SimpleNamespace(isChecked=lambda: True)
        self.show_calls = 0

    def show_for_video_with_io(self) -> None:
        self.show_calls += 1

    def set_total_frames(self, total: int) -> None:
        self.total_frames = int(total)

    def set_current_frame(self, frame: int) -> None:
        self.current_frame = int(frame)

    def set_frame_rate(self, fps: float) -> None:
        self.frame_rate = float(fps)

    def set_time_info(self, info: str) -> None:
        pass

    def set_in_out_points(self, in_point: Any, out_point: Any) -> None:
        pass

    def on_frame_changed(self, frame: int) -> None:
        self.current_frame = int(frame)

    def on_play_state_changed(self, playing: bool) -> None:
        self.playing = bool(playing)


class _FakeController:
    def __init__(self) -> None:
        self.frame_changed = _FakeSignal()
        self.state_changed = _FakeSignal()
        self.current_frame = 0
        self.is_playing = False
        self.timeline_duration = 48
        self.fps = 24.0
        self.playback_range: Optional[Tuple[int, int]] = None
        self._playback_range_start: Optional[int] = None
        self._playback_range_end: Optional[int] = None
        self.seek_calls: List[int] = []

    def seek(self, frame: int) -> None:
        self.seek_calls.append(int(frame))

    def set_playback_range(self, start: Any, end: Any) -> None:
        self.playback_range = (start, end)

    def clear_playback_range(self) -> None:
        self.playback_range = None

    def set_loop(self, enabled: bool) -> None:
        pass

    def play(self) -> None:
        self.is_playing = True

    def pause(self) -> None:
        self.is_playing = False


class _PageHost(QObject, PageManagementMixin):
    """Minimal MainWindow stand-in (QObject → the probe dispatcher works)."""

    def __init__(self) -> None:
        super().__init__()
        self.current_page = "Composite"
        self.feature_registry = None
        self._edit_available = True
        self._composite_available = True
        self._deliver_available = True
        self._page_context_manager = MagicMock()
        self._contextual_page_switch_in_progress = False
        self._current_edit_project = None
        self._current_timeline_sequences = {}
        self.timeline_panel = None
        self.edit_page = None
        self.deliver_page = None
        self.video_controller = None
        self.current_file_path = None
        self.current_file_is_video = False
        self.canvas = MagicMock()
        self.engine = SimpleNamespace()
        self.mask_panel = SimpleNamespace(
            btn_brush=SimpleNamespace(setChecked=MagicMock()),
        )
        self.qualifier_panel = SimpleNamespace(set_picker_active=MagicMock())
        self.status_bar = SimpleNamespace(showMessage=MagicMock())
        self.dock_properties = None
        self.edit_properties_panel = None
        self.composite_page = None
        self.composite_viewer = None
        self.viewport_container = None
        self.dock_viewport = None
        self._composite_adapter = None
        self.page_buttons = {}
        self.page_layouts = {}
        self.update_all_dock_titlebars = MagicMock()
        self._page_switch_from_page = None


def _comp_clip_page(active: bool) -> MagicMock:
    page = MagicMock()
    page._runtime_context = None
    page._is_image_color_mode = False
    page._is_comp_clip = active
    page.is_comp_clip_playback_active.return_value = active
    page.comp_clip_total_frames.return_value = 48
    page.comp_clip_fps.return_value = 24.0
    page._composite_current_frame = 5
    page._comp_playing = False
    page._comp_in_frame = 0
    page._comp_out_frame = 47
    page.composite_frame_changed = _FakeSignal()
    return page


def _timeline_host(*, comp_clip_active: bool = False) -> Tuple[_PageHost, _FakeBar, _FakeController]:
    host = _PageHost()
    bar = _FakeBar()
    controller = _FakeController()
    host.composite_viewer = SimpleNamespace(video_control_bar=bar)
    page = _comp_clip_page(active=comp_clip_active)
    page._comp_clip_timeline_start = 0
    page.comp_clip_total_frames.return_value = 48
    host.composite_page = page
    host.timeline_panel = SimpleNamespace(
        get_playback_controller=lambda: controller,
        get_playhead_frame=lambda: 0,
        get_selected_clip_ids=lambda: [],
    )
    return host, bar, controller


# ═══════════════════════════════════════════════════════════════════════
# AUD-3100 — cross-path transport-bar teardown
# ═══════════════════════════════════════════════════════════════════════


def test_aud3100_timeline_takeover_removes_comp_clip_slots() -> None:
    """Comp-clip closures must not coexist with timeline closures on the bar."""
    host, bar, _controller = _timeline_host(comp_clip_active=True)

    # Entry 1: comp-clip playback active → 13 comp-clip closures wired.
    host._configure_composite_comp_clip_controls()
    for name in _BAR_SIGNAL_NAMES:
        assert len(getattr(bar, name).slots) == 1, name

    # Entry 2: timeline-backed entry (comp-clip inactive). The timeline path
    # must tear the comp-clip closures down instead of stacking on them.
    host._configure_composite_timeline_playback_controls(True)
    for name in _BAR_SIGNAL_NAMES:
        assert len(getattr(bar, name).slots) == 1, (
            f"{name}: comp-clip slot leaked across paths")

    # Emitting a transport signal drives only the timeline controller now.
    host.composite_page.comp_seek.reset_mock()
    bar.seek_requested.emit(10)
    host.composite_page.comp_seek.assert_not_called()


def test_aud3100_comp_clip_inactive_entry_tears_down_own_slots() -> None:
    """Early-return guards (playback inactive) must still tear down slots."""
    host, bar, _controller = _timeline_host()

    host._configure_composite_comp_clip_controls()
    host.composite_page.is_comp_clip_playback_active.return_value = False
    host.composite_page._is_comp_clip = False

    host._configure_composite_comp_clip_controls()

    for name in _BAR_SIGNAL_NAMES:
        assert getattr(bar, name).slots == [], (
            f"{name}: stale comp-clip slot kept after inactive re-entry")
    assert host.composite_page.composite_frame_changed.slots == []


def test_aud3100_image_color_mode_entry_tears_down_own_slots() -> None:
    host, bar, _controller = _timeline_host()

    host._configure_composite_comp_clip_controls()
    host.composite_page._is_image_color_mode = True

    host._configure_composite_comp_clip_controls()

    for name in _BAR_SIGNAL_NAMES:
        assert getattr(bar, name).slots == [], name


def test_aud3100_disabled_timeline_entry_tears_down_comp_clip_slots() -> None:
    """leave_page_hook calls the timeline wiring with enabled=False."""
    host, bar, _controller = _timeline_host()

    host._configure_composite_comp_clip_controls()
    host._configure_composite_timeline_playback_controls(False)

    for name in _BAR_SIGNAL_NAMES:
        assert getattr(bar, name).slots == [], name


def test_aud3100_comp_clip_rewire_stays_idempotent() -> None:
    host, bar, _controller = _timeline_host(comp_clip_active=True)

    host._configure_composite_comp_clip_controls()
    host._configure_composite_comp_clip_controls()

    for name in _BAR_SIGNAL_NAMES:
        assert len(getattr(bar, name).slots) == 1, name
    assert len(host.composite_page.composite_frame_changed.slots) == 1


# ═══════════════════════════════════════════════════════════════════════
# AUD-3101 — bounded retry after exhaustion
# ═══════════════════════════════════════════════════════════════════════


class _SingleShotShim:
    """QTimer stand-in capturing ``singleShot`` scheduling deterministically."""

    def __init__(self) -> None:
        self.calls: List[Tuple[int, Callable[..., Any]]] = []

    def singleShot(self, ms: int, callback: Callable[..., Any]) -> None:
        self.calls.append((int(ms), callback))


def _single_shot_recorder(monkeypatch: pytest.MonkeyPatch) -> _SingleShotShim:
    shim = _SingleShotShim()
    monkeypatch.setattr(pc, "QTimer", shim)
    return shim


def _legacy_video_controller() -> SimpleNamespace:
    return SimpleNamespace(
        is_playing=False,
        is_video_loaded=lambda: True,
        _last_static_preview_delivered=False,
        static_preview_shows_frame=lambda frame: False,
        request_static_preview_frame=MagicMock(return_value=False),
        seek_to_frame=None,
        _use_dual_path=False,
        _coordinator=None,
        withdraw_static_preview_pending=MagicMock(),
    )


def _preview_context(source_frame: int = 50) -> Any:
    return SimpleNamespace(
        clip_id="clip-p", asset_id="asset-p", source_frame=source_frame
    )


def test_aud3101_legacy_retry_drains_to_hard_cap_and_disarms(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = _single_shot_recorder(monkeypatch)
    host = _PageHost()
    host.video_controller = _legacy_video_controller()
    host._color_static_preview_token = 1
    host._color_clip_switch_guard_armed = True

    with caplog.at_level(logging.WARNING, logger=pc.logger.name):
        host._show_color_timeline_clip_static_preview(
            _preview_context(source_frame=77), retry_count=8, token=1)

    # The whole chain terminates at the hard cap instead of looping forever.
    fired = 0
    with caplog.at_level(logging.WARNING, logger=pc.logger.name):
        while recorder.calls:
            _, retry = recorder.calls.pop(0)
            fired += 1
            retry()
            assert fired <= pc._COLOR_STATIC_PREVIEW_MAX_RETRIES, (
                "retry chain exceeded the hard cap (AUD-3101 regression)")

    assert recorder.calls == []
    assert host._color_clip_switch_guard_armed is False
    assert host.video_controller.withdraw_static_preview_pending.called
    assert any("static preview gave up" in r.message for r in caplog.records)


def test_aud3101_exact_seek_failure_disarms_without_rescheduling(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A controller whose exact seek raises must not loop at 10Hz either."""
    recorder = _single_shot_recorder(monkeypatch)
    host = _PageHost()
    host._color_static_preview_token = 1
    host._color_clip_switch_guard_armed = True

    def _boom(frame: int, **_kwargs: Any) -> None:
        raise ValueError("decoder wedged")

    host.video_controller = SimpleNamespace(
        is_playing=False,
        is_video_loaded=lambda: True,
        _last_static_preview_delivered=False,
        static_preview_shows_frame=lambda frame: False,
        request_static_preview_frame=MagicMock(return_value=False),
        seek_to_frame=_boom,
        _use_dual_path=False,
        _coordinator=None,
        withdraw_static_preview_pending=MagicMock(),
    )

    with caplog.at_level(logging.WARNING, logger=pc.logger.name):
        host._show_color_timeline_clip_static_preview(
            _preview_context(source_frame=77), retry_count=8, token=1)

    assert recorder.calls == []  # no 100ms storm after a failed exact seek
    assert host._color_clip_switch_guard_armed is False


# ═══════════════════════════════════════════════════════════════════════
# AUD-3102 — background color-tag probe
# ═══════════════════════════════════════════════════════════════════════


def test_aud3102_probe_dedupes_inflight_path(qapp: Any) -> None:
    host = _PageHost()
    spawned: List[Callable[..., Any]] = []
    host._color_tag_probe_spawn = spawned.append  # type: ignore[method-assign]
    asset = SimpleNamespace(path="/tmp/dup.mov", metadata={})

    host._launch_color_tag_probe(asset)
    host._launch_color_tag_probe(asset)

    assert len(spawned) == 1, "same-path probe must not be dispatched twice"
    assert host._color_tag_probe_generation == 1


def test_aud3102_new_asset_bumps_generation(qapp: Any) -> None:
    host = _PageHost()
    spawned: List[Callable[..., Any]] = []
    host._color_tag_probe_spawn = spawned.append  # type: ignore[method-assign]

    host._launch_color_tag_probe(SimpleNamespace(path="/tmp/a.mov", metadata={}))
    host._launch_color_tag_probe(SimpleNamespace(path="/tmp/b.mov", metadata={}))

    assert len(spawned) == 2
    assert host._color_tag_probe_generation == 2
    assert host._color_tag_probes_inflight == {"/tmp/a.mov", "/tmp/b.mov"}


def test_aud3102_non_qobject_host_falls_back_to_sync_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a QObject host there is no thread-boundary signal: the launch
    degrades to the legacy synchronous probe instead of a background thread
    (a background thread could not safely touch the canvas)."""
    probed = SimpleNamespace(
        color_transfer="bt709", color_primaries="bt709", color_range="tv")
    monkeypatch.setattr(
        "src.shared.media.factory.MediaSourceFactory.probe_video_metadata",
        staticmethod(lambda path: probed),
    )
    monkeypatch.setattr(
        "src.shared.color.color_transform_provider."
        "get_color_provider_diagnostics",
        lambda config_manager=None: {},
    )
    spawned: List[Callable[..., Any]] = []
    metadata: dict = {}
    asset = SimpleNamespace(path="/tmp/x.mov", metadata=metadata)
    host = SimpleNamespace(
        _color_tag_probe_spawn=spawned.append,
        canvas=SimpleNamespace(update_color_monitor=MagicMock()),
    )

    PageManagementMixin._launch_color_tag_probe(host, asset)

    assert spawned == [], "no background thread without a dispatcher"
    assert metadata["color_transfer"] == "bt709"  # synchronous write-back
    host.canvas.update_color_monitor.assert_called_once()
    source = host.canvas.update_color_monitor.call_args.kwargs["source"]
    assert source["primaries"] == "bt709"


def test_aud3102_probe_completion_backfills_and_refreshes(
    monkeypatch: pytest.MonkeyPatch, qapp: Any,
) -> None:
    monkeypatch.setattr(
        "src.shared.color.color_transform_provider."
        "get_color_provider_diagnostics",
        lambda config_manager=None: {},
    )
    probed = SimpleNamespace(
        color_transfer="bt2020", color_primaries="bt2020", color_range="tv")
    monkeypatch.setattr(
        "src.shared.media.factory.MediaSourceFactory.probe_video_metadata",
        classmethod(lambda cls, path: probed),
    )
    host = _PageHost()
    host._color_tag_probe_spawn = (  # type: ignore[method-assign]
        lambda worker: worker())
    update = MagicMock()
    host.canvas = SimpleNamespace(update_color_monitor=update)
    metadata: dict = {}
    asset = SimpleNamespace(path="/tmp/probe.mov", metadata=metadata)

    host._launch_color_tag_probe(asset)

    # Only the completion refresh happened here (the placeholder refresh
    # belongs to _update_color_monitor_for_asset).
    update.assert_called_once()
    source = update.call_args.kwargs["source"]
    assert source == {"transfer": "bt2020", "primaries": "bt2020", "range": "tv"}
    assert metadata == {
        "color_transfer": "bt2020",
        "color_primaries": "bt2020",
        "color_range": "tv",
    }
    assert host._color_tag_probes_inflight == set()


# ═══════════════════════════════════════════════════════════════════════
# AUD-3103 — topmost-enabled resolver
# ═══════════════════════════════════════════════════════════════════════


class _Track:
    def __init__(self, clips: List[Any], index: int) -> None:
        self.clips = clips
        self.index = index

    def get_clip_at(self, frame: int) -> Any:
        for clip in self.clips:
            if clip.timeline_start <= frame < clip.timeline_start + 30:
                return clip
        return None


def test_aud3103_resolver_prefers_topmost_enabled() -> None:
    bottom = SimpleNamespace(clip_id="bottom", timeline_start=0, is_enabled=True)
    top = SimpleNamespace(clip_id="top", timeline_start=0, is_enabled=True)
    timeline = SimpleNamespace(
        video_tracks=[_Track([bottom], 0), _Track([top], 2)])

    clip = PageManagementMixin._topmost_enabled_video_clip_at(timeline, 10)

    assert clip is not None and clip.clip_id == "top"


def test_aud3103_resolver_treats_disabled_topmost_as_gap() -> None:
    bottom = SimpleNamespace(clip_id="bottom", timeline_start=0, is_enabled=True)
    top = SimpleNamespace(clip_id="top", timeline_start=0, is_enabled=False)
    timeline = SimpleNamespace(
        video_tracks=[_Track([bottom], 0), _Track([top], 2)])

    clip = PageManagementMixin._topmost_enabled_video_clip_at(timeline, 10)

    assert clip is not None and clip.clip_id == "bottom"


def test_aud3103_resolver_unsorted_track_order_still_topmost() -> None:
    low = SimpleNamespace(clip_id="low", timeline_start=0, is_enabled=True)
    high = SimpleNamespace(clip_id="high", timeline_start=0, is_enabled=True)
    # Natural order is NOT by index; the resolver must sort by index itself.
    timeline = SimpleNamespace(
        video_tracks=[_Track([high], 1), _Track([low], 0)])

    clip = PageManagementMixin._topmost_enabled_video_clip_at(timeline, 5)

    assert clip is not None and clip.clip_id == "high"


def test_aud3103_both_follow_paths_share_resolver() -> None:
    """The Composite follow helper delegates to the shared resolver."""
    low = SimpleNamespace(clip_id="low", timeline_start=0, is_enabled=True)
    high = SimpleNamespace(clip_id="high", timeline_start=0, is_enabled=True)
    timeline = SimpleNamespace(
        video_tracks=[_Track([low], 0), _Track([high], 1)])
    host = _PageHost()

    assert (
        host._find_clip_under_composite_playhead(timeline, 5)
        is PageManagementMixin._topmost_enabled_video_clip_at(timeline, 5)
    )


# ═══════════════════════════════════════════════════════════════════════
# AUD-3104 — preview color-space exception discipline
# ═══════════════════════════════════════════════════════════════════════


def _color_space_host() -> Tuple[_PageHost, MagicMock, MagicMock]:
    host = _PageHost()
    host.config_manager = MagicMock()
    set_display_space = MagicMock()
    set_display_transfer = MagicMock()
    host.composite_viewer = SimpleNamespace(
        set_display_space=set_display_space,
        set_display_transfer=set_display_transfer,
    )
    host.composite_page = None  # still-image kind, no HDR graph scan
    return host, set_display_space, set_display_transfer


def test_aud3104_set_display_space_failure_falls_back_to_transfer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from src.shared.color.types import ColorTransfer
    monkeypatch.setattr(
        "src.shared.color.color_space_descriptor.descriptor_from_config",
        lambda *a, **k: SimpleNamespace(transfer=ColorTransfer.PQ),
    )
    host, set_display_space, set_display_transfer = _color_space_host()
    set_display_space.side_effect = ValueError("viewer rejected gamut")

    with caplog.at_level(logging.WARNING, logger=pc.logger.name):
        host._apply_composite_preview_color_space()

    set_display_space.assert_called_once()
    set_display_transfer.assert_called_once_with(3)  # PQ → transfer-only enum
    assert any(
        "falling back to transfer-only" in r.message for r in caplog.records)


def test_aud3104_domain_failure_logs_warning_with_stack(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def _corrupt(*_a: Any, **_k: Any) -> None:
        raise ValueError("corrupt config")

    monkeypatch.setattr(
        "src.shared.color.color_space_descriptor.descriptor_from_config",
        _corrupt,
    )
    host, set_display_space, set_display_transfer = _color_space_host()

    with caplog.at_level(logging.WARNING, logger=pc.logger.name):
        host._apply_composite_preview_color_space()  # must not raise

    set_display_space.assert_not_called()
    set_display_transfer.assert_not_called()
    records = [r for r in caplog.records
               if "preview color-space apply failed" in r.message]
    assert records, "domain failure must be visible at WARNING level"
    assert records[0].exc_info is not None, "warning must carry the stack"


def test_aud3104_import_error_stays_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def _import_error(*_a: Any, **_k: Any) -> None:
        raise ImportError("legacy build")

    monkeypatch.setattr(
        "src.shared.color.color_space_descriptor.descriptor_from_config",
        _import_error,
    )
    host, set_display_space, _transfer = _color_space_host()

    with caplog.at_level(logging.WARNING, logger=pc.logger.name):
        host._apply_composite_preview_color_space()  # must not raise

    set_display_space.assert_not_called()
    assert not [r for r in caplog.records
                if "preview color-space" in r.message], (
        "legacy-component ImportError stays at debug level")


# ═══════════════════════════════════════════════════════════════════════
# AUD-34001 — read_port_config validation
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def adapter_module():
    from src.app.integrations.file_explorer import adapter
    return adapter


def _write_port_config(
    adapter_module: Any, tmp_path: Any, payload: Any, raw: Optional[str] = None,
) -> None:
    config_path = os.path.join(str(tmp_path), ".port_config.json")
    if raw is not None:
        with open(config_path, "w") as f:
            f.write(raw)
    else:
        with open(config_path, "w") as f:
            json.dump(payload, f)


def _patched_paths(
    monkeypatch: pytest.MonkeyPatch, adapter_module: Any, tmp_path: Any,
) -> None:
    monkeypatch.setattr(adapter_module, "file_explorer_path", str(tmp_path))
    monkeypatch.setattr(adapter_module, "_default_port", lambda: 8188)


def test_aud34001_valid_config_passthrough(
    monkeypatch: pytest.MonkeyPatch, adapter_module: Any, tmp_path: Any,
) -> None:
    _patched_paths(monkeypatch, adapter_module, tmp_path)
    _write_port_config(adapter_module, tmp_path, {"host": "localhost", "port": 9000})

    assert adapter_module.read_port_config() == ("localhost", 9000)


def test_aud34001_non_loopback_host_rejected(
    monkeypatch: pytest.MonkeyPatch, adapter_module: Any, tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patched_paths(monkeypatch, adapter_module, tmp_path)
    _write_port_config(adapter_module, tmp_path, {"host": "0.0.0.0", "port": 9000})

    with caplog.at_level(logging.WARNING):
        host, port = adapter_module.read_port_config()

    assert host == "127.0.0.1"
    assert port == 9000
    assert any("rejected host" in r.message for r in caplog.records)


@pytest.mark.parametrize("bad_port", ["9000", 0, 70000, True, None])
def test_aud34001_bad_port_values_fall_back(
    monkeypatch: pytest.MonkeyPatch, adapter_module: Any, tmp_path: Any,
    bad_port: Any,
) -> None:
    _patched_paths(monkeypatch, adapter_module, tmp_path)
    _write_port_config(adapter_module, tmp_path, {"port": bad_port})

    assert adapter_module.read_port_config() == ("127.0.0.1", 8188)


def test_aud34001_broken_json_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, adapter_module: Any, tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patched_paths(monkeypatch, adapter_module, tmp_path)
    _write_port_config(adapter_module, tmp_path, None, raw="{not json")

    with caplog.at_level(logging.WARNING):
        result = adapter_module.read_port_config()

    assert result == ("127.0.0.1", 8188)
    assert any("failed to read" in r.message for r in caplog.records)


def test_aud34001_non_dict_root_falls_back(
    monkeypatch: pytest.MonkeyPatch, adapter_module: Any, tmp_path: Any,
) -> None:
    _patched_paths(monkeypatch, adapter_module, tmp_path)
    _write_port_config(adapter_module, tmp_path, None, raw="[1, 2, 3]")

    assert adapter_module.read_port_config() == ("127.0.0.1", 8188)


def test_aud34001_missing_file_returns_defaults(
    monkeypatch: pytest.MonkeyPatch, adapter_module: Any, tmp_path: Any,
) -> None:
    _patched_paths(monkeypatch, adapter_module, tmp_path)

    assert adapter_module.read_port_config() == ("127.0.0.1", 8188)


def test_aud34001_os_error_falls_back(
    monkeypatch: pytest.MonkeyPatch, adapter_module: Any, tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # exists() reports the config as present but the directory is gone →
    # open() raises OSError ( FileNotFoundError ) inside the guarded block.
    missing = str(tmp_path / "missing")
    monkeypatch.setattr(adapter_module, "file_explorer_path", missing)
    monkeypatch.setattr(adapter_module, "_default_port", lambda: 8188)
    monkeypatch.setattr(adapter_module.os.path, "exists", lambda _p: True)

    with caplog.at_level(logging.WARNING):
        assert adapter_module.read_port_config() == ("127.0.0.1", 8188)
    assert any("failed to read" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════
# AUD-34002 — worker stop lifecycle
# ═══════════════════════════════════════════════════════════════════════


def test_aud34002_stop_flag_short_circuits_run_before_server(
    monkeypatch: pytest.MonkeyPatch, qapp: Any, adapter_module: Any,
) -> None:
    monkeypatch.setattr(adapter_module, "file_explorer_ensure_runtime", None)
    created = MagicMock()

    def _fake_create(app: Any, host: str, port: int) -> Any:
        created()
        return MagicMock()

    monkeypatch.setattr(adapter_module, "create_http_server", _fake_create)

    worker = adapter_module.FlaskWorker(app=object(), host="127.0.0.1", port=8188)
    worker.server = None
    worker.stop()  # stop arrives before run() → flag only (server is None)
    assert worker._stop_requested is True

    worker.run()  # must return without creating a server

    created.assert_not_called()


def test_aud34002_stop_during_bootstrap_skips_serve_loop(
    monkeypatch: pytest.MonkeyPatch, qapp: Any, adapter_module: Any,
) -> None:
    """stop() landing between server creation and the serve loop exits it."""
    serve = MagicMock()
    server = SimpleNamespace(run=serve, stop=MagicMock(), port=8188)
    monkeypatch.setattr(
        adapter_module, "create_http_server", lambda *a, **k: server)

    worker = adapter_module.FlaskWorker(app=object(), host="127.0.0.1", port=8188)
    # The bootstrap hook races a stop() in: by the time the server exists the
    # stop request must already be honored.
    monkeypatch.setattr(
        adapter_module, "file_explorer_ensure_runtime",
        lambda **_k: worker.stop())
    monkeypatch.setattr(worker, "_write_port_config", lambda: None)

    worker.run()

    serve.assert_not_called()
    server.stop.assert_called_once()  # finally-block pairing


class _ZombieWorker:
    """Duck-typed worker that never exits (simulates terminate() failure)."""

    def __init__(self) -> None:
        self.stop_calls = 0
        self.terminate_calls = 0
        self.delete_later_calls = 0
        self.finished = _FakeSignal()

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def stop(self) -> None:
        self.stop_calls += 1

    def wait(self, ms: int) -> bool:
        return False

    def terminate(self) -> None:
        self.terminate_calls += 1

    def deleteLater(self) -> None:  # noqa: N802 - Qt naming
        self.delete_later_calls += 1


def test_aud34002_zombie_worker_reference_kept_until_finished(
    qapp: Any, adapter_module: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_cls = adapter_module.FileExplorerAdapter
    monkeypatch.setattr(adapter_cls, "_instance", None)
    adapter = adapter_cls.__new__(adapter_cls)
    zombie = _ZombieWorker()
    adapter.worker = zombie

    adapter.stop_server()

    assert zombie.terminate_calls == 1
    # The reference is NOT dropped while the thread is still running.
    assert adapter.worker is zombie
    # Finishing the zombie clears the reference (deferred cleanup).
    zombie.finished.slots[0]()
    assert adapter.worker is None


def test_aud34002_normal_stop_clears_reference(
    qapp: Any, adapter_module: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ObedientWorker:
        def __init__(self) -> None:
            self.stop_calls = 0
            self.finished = _FakeSignal()

        def isRunning(self) -> bool:  # noqa: N802
            return True

        def stop(self) -> None:
            self.stop_calls += 1

        def wait(self, ms: int) -> bool:
            return True

    adapter_cls = adapter_module.FileExplorerAdapter
    monkeypatch.setattr(adapter_cls, "_instance", None)
    adapter = adapter_cls.__new__(adapter_cls)
    worker = _ObedientWorker()
    adapter.worker = worker

    adapter.stop_server()

    assert worker.stop_calls == 1
    assert adapter.worker is None

    # Idempotent on repeat calls.
    adapter.worker = None
    adapter.stop_server()
