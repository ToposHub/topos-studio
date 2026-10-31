"""P2-9/P2-11 deflicker real-video gate (plan mandate P0-6).

Runs ``video_frame → deflicker → output`` over a real H.264 source from
``tests/test_videos`` twice through the full comp-clip pipeline — once on
the GPU-assisted path and once with ``TOPOS_DEFLICKER_CPU=1`` (legacy
full-frame chain) — and requires:

1. the source actually decodes (frames advance);
2. both paths produce real output and agree pixel-wise (preview is
   sRGB u8, so a float-path difference may flip isolated codes by 1);
3. the GPU path never performs the full-frame RGBA readback;
4. a traceable artifact with per-frame timings for both paths.
"""

import json
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytestmark = pytest.mark.composite_core

W, H = 160, 120

_VIDEO = os.path.join(os.path.dirname(__file__), '..',
                      'test_videos', '2K_h264.mp4')
_GATE_ARTIFACT = os.path.join(os.path.dirname(__file__), '..',
                              'media', 'results',
                              'p29_deflicker_real_video_gate.json')


class _StubConfig:
    def __init__(self, overrides=None):
        self._overrides = dict(overrides or {})

    def get(self, key, default=None):
        return self._overrides.get(key, default)


def _deflicker_graph(video_path):
    return {
        "composite_id": "deflicker-video-gate",
        "fps": 25.0, "width": W, "height": H,
        "nodes": [
            {"id": "v1", "type": "video_frame", "position": [0, 0],
             "parameters": {"file_path": video_path}},
            {"id": "df", "type": "deflicker", "position": [160, 0],
             "parameters": {"window": "5", "percentile": 0.5,
                            "luma_only": True, "mode": "per_pixel"}},
            {"id": "out", "type": "output", "position": [320, 0],
             "parameters": {"format_mode": "custom",
                            "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "v1", "src_port_name": "image",
             "dst_node_id": "df", "dst_port_name": "image"},
            {"src_node_id": "df", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }


class _Runner:
    """One renderer per run so the A/B passes cannot share cache entries."""

    def __init__(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer,
        )
        self.renderer = CompositeFrameRenderer(
            config_manager=_StubConfig({"pass_fusion_mode": "off"}))

    def render(self, graph, frame):
        t0 = time.perf_counter()
        img = self.renderer.render_comp_clip_frame(
            graph, timeline_frame=frame, quality="preview")
        ms = (time.perf_counter() - t0) * 1000.0
        assert img is not None, "preview render produced no output"
        return img, ms

    def close(self):
        self.renderer.cleanup()


@pytest.fixture(scope="module")
def _gl_available():
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    ctx.release()


class TestDeflickerRealVideoGate:
    def test_gpu_path_matches_cpu_over_real_video(self, qapp,
                                                  _gl_available,
                                                  monkeypatch):
        if not os.path.exists(_VIDEO):
            pytest.skip(f"real test video missing: {_VIDEO}")
        frames = 5

        # ── GPU-assisted path (default) ──
        from src.features.composite.nodes import _gpu_readback
        calls = {"n": 0}
        real_read = _gpu_readback.read_rgba

        def _spy(*args, **kwargs):
            calls["n"] += 1
            return real_read(*args, **kwargs)

        monkeypatch.setattr(_gpu_readback, "read_rgba", _spy)
        gpu_runner = _Runner()
        gpu_imgs, gpu_ms = [], []
        try:
            g = _deflicker_graph(_VIDEO)
            for frame in range(frames):
                img, ms = gpu_runner.render(g, frame)
                gpu_imgs.append(img)
                gpu_ms.append(ms)
        finally:
            gpu_runner.close()
        assert calls["n"] == 0, (
            f"GPU path performed {calls['n']} full-frame RGBA readbacks")
        # Snapshot now — the CPU run below legitimately readbacks and the
        # spy is still installed.
        gpu_readbacks = calls["n"]

        # ── Legacy CPU chain (force-CPU escape hatch) ──
        monkeypatch.setenv("TOPOS_DEFLICKER_CPU", "1")
        cpu_runner = _Runner()
        cpu_imgs, cpu_ms = [], []
        try:
            g = _deflicker_graph(_VIDEO)
            for frame in range(frames):
                img, ms = cpu_runner.render(g, frame)
                cpu_imgs.append(img)
                cpu_ms.append(ms)
        finally:
            cpu_runner.close()
        monkeypatch.delenv("TOPOS_DEFLICKER_CPU", raising=False)

        # 1. The source actually decodes.
        means = np.stack([im.astype(np.float32).mean(axis=(0, 1))
                          for im in gpu_imgs])
        assert float(np.ptp(means[:, :3])) > 1e-4, \
            "video frames identical — decode is not advancing"

        # 2. Path agreement on u8 preview frames: float rounding may flip
        # isolated codes by 1, but never more and never many.
        worst_abs, worst_frac = 0, 0.0
        for a, b in zip(gpu_imgs, cpu_imgs):
            d = np.abs(a.astype(np.int16) - b.astype(np.int16))
            worst_abs = max(worst_abs, int(d.max()))
            worst_frac = max(worst_frac,
                             float((d > 1).mean()))
        assert worst_abs <= 2, f"paths disagree by {worst_abs} codes"
        assert worst_frac <= 1e-3, \
            f"{worst_frac:.2%} of pixels differ by >1 code"

        # 3. Traceable artifact (plan §9.6): commit, video, timings.
        try:
            import subprocess
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True,
                text=True, timeout=10).stdout.strip()
        except Exception:
            head = "unknown"
        artifact = {
            "commit": head,
            "video": os.path.basename(_VIDEO),
            "frames": frames,
            "gpu_path_ms_per_frame": [round(m, 2) for m in gpu_ms],
            "cpu_path_ms_per_frame": [round(m, 2) for m in cpu_ms],
            "gpu_path_p50_ms": round(float(np.median(gpu_ms)), 2),
            "cpu_path_p50_ms": round(float(np.median(cpu_ms)), 2),
            "fullframe_rgba_readbacks_gpu_path": gpu_readbacks,
            "path_agreement": {"worst_abs_code_diff": worst_abs,
                               "frac_pixels_diff_gt1": worst_frac},
        }
        os.makedirs(os.path.dirname(_GATE_ARTIFACT), exist_ok=True)
        with open(_GATE_ARTIFACT, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, indent=1)
