"""Capture the real Mixer widgets with static sample levels; no audio playback."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication

from src.features.edit.core.commands import AddTrackCommand
from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance, TimelineSequence
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel
from src.shared.ui.style_sheet import DARK_THEME


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", default="540x600")
    parser.add_argument("--locale", default="zh_CN")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--hide-inserts", action="store_true")
    args = parser.parse_args()
    width, height = (int(value) for value in args.size.lower().split("x"))
    app = QApplication([])
    app.setStyleSheet(DARK_THEME)
    I18N.load_locale(args.locale)
    panel = AudioMixerPanel()
    timeline = TimelineSequence("mixer-preview", "Mixer preview")
    for i, name in enumerate(("主对白", "Dialogue alternate", "音乐 · Music", "环境声 · Ambience")):
        tid = f"track-{i}"
        panel.register_track(tid, name, f"A{i + 1}")
        timeline, _ = AddTrackCommand.create("audio", name=name, track_id=tid).apply(timeline)
        strip = panel._track_mixers[tid]
        strip.set_track_color(timeline.audio_tracks[-1].color)
        effect_types = ("strip_eq", "compressor", "reverb", "delay", "imager") if i == 0 else ("reverb",) if i == 1 else ()
        strip.set_effect_chain(EffectChainParams(effects=tuple(
            TrackEffectInstance(effect_type=effect_type, params={"enabled": n != 1},
                                instance_id=f"{tid}-{n}", order=n)
            for n, effect_type in enumerate(effect_types)
        )))
        strip.update_audio_level([-14.0 - i * 3, -16.0 - i * 2])
        strip.set_volume_linear((0.8, 1.0, 0.62, 0.4)[i])
        strip.set_pan_linear((0.0, 0.3, 0.0, -0.5)[i])
    panel.update_master_level([-7.0, -8.0])
    panel.set_narrow_layout(args.compact)
    panel.set_inserts_visible(not args.hide_inserts)
    panel.resize(width, height)
    panel.show()
    app.processEvents()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not panel.grab().save(str(args.output)):
        raise RuntimeError(f"Unable to save {args.output}")
    print(args.output.resolve())
    panel.close()


if __name__ == "__main__":
    main()
