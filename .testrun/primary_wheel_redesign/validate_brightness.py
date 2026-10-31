"""Validate each master dial through production GPU rendering on the supplied clip."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import av
import numpy as np
from PIL import Image, ImageDraw
from PyQt6.QtWidgets import QApplication
from src.features.color.ui.color_wheel import GradingPanel
from src.features.color.core.the_decompiler import GradingMath
from src.features.edit.core.timeline_gpu_export_renderer import TimelineGpuExportRenderer, grade_params_to_uniforms
av.logging.restore_default_callback()
app = QApplication([])
panel = GradingPanel()
with av.open('tests/test_videos/2K_h264.mp4') as container:
    source = next(container.decode(video=0)).to_ndarray(format='bgr24')
cases = []
for mode in ('lift', 'gamma', 'gain'):
    for value in (-2500, 2500):
        panel.reset_all()
        wheel = getattr(panel, mode + '_wheel')
        wheel.brightness_slider._apply_discrete_value(value)
        params = panel.get_current_params()
        assert params[mode] == (0.0, 0.0, value / 10000)
        assert all(params[other] == (0.0, 0.0, 0.0) for other in ('lift', 'gamma', 'gain') if other != mode)
        cases.append((mode, value, params))

def render():
    renderer = TimelineGpuExportRenderer()
    results, previews = [], []
    try:
        for mode, value, params in cases:
            actual = renderer.render_packed_bgr(source, grade_params_to_uniforms(params), 1920, 1080, 1.0)
            assert actual is not None
            rgb = source[..., ::-1].astype(np.float32) / 255
            expected = GradingMath.apply_grading(rgb.reshape(-1, 3), params).reshape(rgb.shape)
            expected = np.rint(np.clip(expected[..., ::-1], 0, 1) * 255).astype(np.uint8)
            error = int(np.abs(actual.astype(np.int16) - expected.astype(np.int16)).max())
            delta = float(actual.astype(np.float32).mean() - source.astype(np.float32).mean())
            assert error <= 1
            assert delta * value > 0
            results.append({'wheel': mode, 'brightness': value / 10000,
                            'gpu_cpu_max_u8_error': error, 'mean_brightness_delta': delta})
            previews.append(actual)
        return results, previews
    finally:
        renderer.release()

with ThreadPoolExecutor(max_workers=1, thread_name_prefix='master-dial-validation') as pool:
    results, previews = pool.submit(render).result()
out = Path('.testrun/primary_wheel_redesign')
(out / 'brightness_validation.json').write_text(json.dumps(results, indent=2))
contact = Image.new('RGB', (1080, 684), '#27272b')
draw = ImageDraw.Draw(contact)
for row, mode in enumerate(('lift', 'gamma', 'gain')):
    for col, frame in enumerate((previews[row * 2], source, previews[row * 2 + 1])):
        contact.paste(Image.fromarray(frame[..., ::-1]).resize((360, 202)), (col * 360, row * 228 + 24))
        draw.text((col * 360 + 8, row * 228 + 6), mode + ' / ' + ('darker', 'original', 'brighter')[col], fill='#dddddd')
contact.save(out / 'real_video_brightness.jpg', quality=92)
print(json.dumps(results, indent=2))
