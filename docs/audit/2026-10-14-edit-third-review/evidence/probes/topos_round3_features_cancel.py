import sys,json
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from src.features.edit.core.timeline_v2 import EditTrack,TimelineSequence,Rational
from src.features.edit.core.timeline_export import TimelineExporter,TimelineExportConfig
from src.features.edit.core.loudness_analyzer import analyze_loudness as real_analyze
import src.features.edit.core.loudness_analyzer as loudness_module
sequence=TimelineSequence(timeline_id='tl',name='T',audio_tracks=(EditTrack(track_id='bus',name='Bus',track_type='audio',index=0,is_bus=True),))
h=SimpleNamespace(_encoder=object(),_sequence=sequence,_audio_render_sequence=sequence,_sequence_resolver=None,_project=None,_cancel_requested=False,_get_sequence_fps=lambda:24.,_get_sequence_fps_rational=lambda:Rational(24,1),_write_loudness_report=MagicMock())
calls=[]
def cancellable_spy(samples,*args,**kw):
    calls.append({'progress_passed':kw.get('progress') is not None,'cancel_seen_at_entry':h._cancel_requested})
    h._cancel_requested=True
    return real_analyze(samples,*args,**kw)
cfg=TimelineExportConfig(loudness_target_lufs=-23.,loudness_report_path='/tmp/topos_round3_features_cancel_report.json')
with patch.object(loudness_module,'analyze_loudness',cancellable_spy):
    pcm=TimelineExporter._render_audio_master(h,0,2,cfg)
print(json.dumps({'cancel_requested':h._cancel_requested,'analyzer_calls':calls,'returned_pcm':pcm is not None,'report_write_calls':h._write_loudness_report.call_count,'last_report_published':hasattr(h,'_last_loudness_report')},indent=2))
