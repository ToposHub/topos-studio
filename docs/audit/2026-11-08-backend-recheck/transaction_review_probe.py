"""Read-only transaction probes; virtual take metadata, no real recording/device.

Run from repo root: QT_QPA_PLATFORM=offscreen .venv/bin/python <this file>
An exit code of 0 means observations completed, NOT that product semantics passed.
"""
from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.recording_manager import compute_time_reference
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.timeline_interaction import ClipDragSession
from src.app.controllers.edit_controller import EditControllerMixin


def run() -> dict:
    app = QApplication.instance() or QApplication([])
    retained = []
    out = {"scope": "Model + real TimelineView/controller UI adapters; no native input, microphone or decoder", "findings": []}
    # A realistic take's first second is pre-roll before the anchor. Current
    # placement ignores its BWF start, placing file frame 0 at the anchor.
    seq = TimelineSequence('vo-probe', 'Voiceover', fps=24,
        audio_tracks=(EditTrack('a1', 'A1', 'audio', 0),))
    view = TimelineView(); retained.append(view); view.load_timeline(seq)
    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    ctrl.edit_page = SimpleNamespace(_current_timeline=seq)
    session = {'project_id':'p', 'timeline_id':seq.timeline_id, 'track_id':'a1', 'anchor_frame':240, 'generation':1}
    ctrl._voiceover_sessions = {'a1':session}
    time_ref, timecode = compute_time_reference(240, 24, 48000, pre_roll_captured_seconds=1, latency_ms=0)
    take = SimpleNamespace(duration_seconds=3, sample_rate=48000, time_reference_samples=time_ref, in_point_frame=240, start_timecode=timecode)
    asset = MediaAsset('take', '/tmp/virtual_take.wav', 'audio', 'Virtual take', metadata={'audio_info':{'channels':1,'sample_rate':48000},'duration':3})
    ctrl._place_voiceover_clip(session, asset, take)
    clip = view.get_timeline().audio_tracks[0].clips[0]
    correct_file_start_frame = round(time_ref / take.sample_rate * 24)
    # A marker captured at the anchor occurs at file t=1.0 seconds.
    marker_on_timeline = clip.timeline_start + 24 - clip.source_start
    out['findings'].append({'id':'VO_PREROLL_ALIGNMENT','input':{'anchor_frame':240,'pre_roll_seconds':1,'time_reference_samples':time_ref,'file_start_timeline_frame':correct_file_start_frame,'take_duration_seconds':3},'actual':{'timeline_start':clip.timeline_start,'source_start':clip.source_start,'timeline_duration':clip.timeline_duration,'anchor_marker_timeline_frame':marker_on_timeline},'expected':{'anchor_marker_timeline_frame':240,'preserve_preroll_option':{'timeline_start':216,'source_start':0},'trim_preroll_option':{'timeline_start':240,'source_start':24}},'defect':marker_on_timeline!=240})
    # Real drag propagation can target BOTH virtual new V/A tracks. Current
    # commit collapses all pseudo IDs into ONE CopyClipsToNewTrackCommand.
    v=ClipInstance('vc','source','v1',100,100,0,100,linked_clip_id='ac')
    a=replace(v,clip_id='ac',track_id='a1',linked_clip_id='vc')
    seq=TimelineSequence('copy-probe','Copy',fps=24,video_tracks=(EditTrack('v1','V1','video',0,clips=(v,)),),audio_tracks=(EditTrack('a1','A1','audio',0,clips=(a,)),))
    view=TimelineView();retained.append(view);view.load_timeline(seq)
    asset=MediaAsset('source','/tmp/virtual.mp4','video','Source',metadata={'fps':24,'total_frames':1000,'audio_info':{'channels':2}})
    view.set_project(EditProject('p','P',assets={'source':asset}))
    session=ClipDragSession(primary_clip_id='vc',linked_clip_ids=['ac'],original_positions={'vc':100,'ac':100},original_track_ids={'vc':'v1','ac':'a1'},current_track_ids={'vc':'__new_video_track_above__','ac':'__new_audio_track_below__'},copy_mode=True)
    plan=view._plan_group_copy(session,120)
    view._current_drag_session=session
    before=view.get_timeline()
    messages=[];view.status_message_requested.connect(messages.append)
    view._handle_copy_drag_finished('vc','v1',100,220)
    after=view.get_timeline()
    out['findings'].append({'id':'LINKED_COPY_TWO_AUTO_TRACKS','input':{'targets':session.current_track_ids,'timeline_start':220},'plan':plan,'actual':{'video_tracks':len(after.video_tracks),'audio_tracks':len(after.audio_tracks),'clip_count':sum(len(t.clips) for t in after.all_tracks),'history':len(view._command_stack._undo_stack),'unchanged':after is before,'status':messages},'expected':{'video_tracks':2,'audio_tracks':2,'clip_count':4,'history':1},'defect':len(after.video_tracks)!=2 or len(after.audio_tracks)!=2})
    # Basic B19 sync-lock behavior: ensure downstream linked members shift once.
    from src.features.edit.core.timeline_ops import build_linked_numeric_trim_command
    vc2=replace(v,clip_id='v2c',timeline_start=200,linked_clip_id='a2c')
    ac2=replace(a,clip_id='a2c',timeline_start=200,linked_clip_id='v2c')
    seq=replace(seq,video_tracks=(replace(seq.video_tracks[0],clips=(v,vc2)),),audio_tracks=(replace(seq.audio_tracks[0],clips=(a,ac2)),))
    cmd,delta=build_linked_numeric_trim_command(seq,'v1','vc','right',10,mode='ripple',max_source_frames=1000,linked_max_source_frames=1000,mismatch='reject')
    result=cmd.apply(seq);changed=result[0] if isinstance(result,tuple) else result
    out['b19_downstream_check']={'delta':delta,'starts':{cid:changed.get_clip_by_id(cid).timeline_start for cid in ('vc','ac','v2c','a2c')},'durations':{cid:changed.get_clip_by_id(cid).timeline_duration for cid in ('vc','ac')}}
    for view in retained: view.close()
    return out

if __name__ == '__main__':
    results=run()
    target=Path(__file__).with_name('transaction_review_results.json')
    target.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False,indent=2))
