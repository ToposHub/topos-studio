"""Render layout fixtures with illustrative static levels; never plays audio.

Run from the repository root:
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-14-audio-mixer/render_mixer_preview.py
"""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from PyQt6.QtWidgets import QApplication
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel
from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
app=QApplication([])
I18N.load_locale('zh_CN')
panel=AudioMixerPanel()
panel.resize(800,460)
names=['主对白','Dialogue alternate','音乐 · Music','环境声 · Ambience','音效 · SFX','备用']
for i,name in enumerate(names):
 tid=f't{i}'
 panel.register_track(tid,name,f'A{i+1}')
 strip=panel._track_mixers[tid]
 strip.set_track_color(['#67a6da','#67a6da','#a68cdb','#68bca5','#d5aa69','#758da1'][i])
 effects=(['strip_eq','compressor','reverb','delay','imager','limiter'] if i==0 else ['strip_eq','reverb'] if i==1 else ['compressor','delay'] if i==2 else [])
 strip.set_effect_chain(EffectChainParams(effects=tuple(TrackEffectInstance(effect_type=et,params={'enabled':n!=1},instance_id=f'{tid}-{n}',order=n) for n,et in enumerate(effects))))
 strip.update_audio_level([-14.0-i*3,-16.0-i*2])
 strip.set_volume_linear([.8,1,.62,.4,.7,.5][i])
 strip.set_pan_linear([0,.3,0,-.5,.7,0][i])
panel._track_mixers['t0'].set_solo(True)
panel._track_mixers['t5'].set_mute(True)
panel.update_master_level([-7,-8])
panel.show()
app.processEvents()
for suffix in ('standard','compact','faders'):
 if suffix=='compact':panel.set_narrow_layout(True)
 elif suffix=='faders':panel.set_narrow_layout(False);panel.set_inserts_visible(False)
 app.processEvents()
 path=Path(__file__).resolve().parent/f'mixer-{suffix}.png'
 panel.grab().save(str(path))
 print(path)
for t,s in panel._track_mixers.items():
 print(t,s.width(),s._volume_fader.mapTo(panel,s._volume_fader.rect().topLeft()).y(),s._volume_fader.height())
panel.close()
