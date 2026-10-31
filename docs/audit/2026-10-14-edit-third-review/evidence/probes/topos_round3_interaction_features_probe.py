import sys, json
from types import SimpleNamespace
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock
ROOT = Path('/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
sys.path.insert(0, str(ROOT))
import numpy as np
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence, SubtitleClip, Send
from src.features.edit.core.timeline_v2 import Rational
from src.features.edit.core.captions.model import CaptionCueMetadata, CaptionWordTiming
from src.features.edit.core.commands import SwitchMulticamAngleCommand
from src.features.edit.core.nested_sequence import build_nested_from_selection
from src.features.edit.core.loudness_analyzer import _TruePeakEstimator, analyze_loudness
from src.features.edit.core.project import MediaAsset, EditProject
from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.features.edit.core.commands import ReplaceMediaCommand, ProjectCommandStack
from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.core.multicam import build_multicam_group_from_assets,group_with_sync_offsets,stamp_multicam_group_on_assets,multicam_group_from_asset
from src.features.edit.core.serialization import EditProjectSerializer
import src.features.edit.ui.media_pool_panel as media_pool_module
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType

results = {}
def clip(cid, tid, start=0, duration=96, **kw):
    return ClipInstance(clip_id=cid, asset_id=kw.pop('asset_id', 'old'), track_id=tid, timeline_start=start, timeline_duration=duration, source_duration=kw.pop('source_duration',duration), **kw)
def track(tid, typ, *cs, **kw):
    return EditTrack(track_id=tid, name=tid, track_type=typ, index=0, clips=cs, **kw)
def seq(v=(),a=(),s=(),**kw):
    return TimelineSequence(timeline_id='tl', name='T', fps=Rational(24,1), video_tracks=v, audio_tracks=a, subtitle_tracks=s, **kw)

sub = SubtitleClip(clip_id='sub', track_id='s1', timeline_start=24, timeline_duration=24, text='word', caption_metadata=CaptionCueMetadata(word_timings=(CaptionWordTiming('word',24,48),)))
before = seq(s=(track('s1','subtitle',subtitle_clips=(sub,)),))
host = SimpleNamespace(_scale_timeline_frame=EditControllerMixin._scale_timeline_frame, _rational_for_timeline_fps=EditControllerMixin._rational_for_timeline_fps)
after = EditControllerMixin._with_timeline_fps(host,before,Rational(48,1))
ns = after.subtitle_tracks[0].subtitle_clips[0]
results['fps_subtitle']={'cue':[ns.timeline_start,ns.timeline_duration],'word':[ns.caption_metadata.word_timings[0].start_frame,ns.caption_metadata.word_timings[0].end_frame],'expected_cue':[48,48],'expected_word':[48,96]}

k = KeyframeTrack('x',keyframes=(Keyframe(0,0.0,InterpolationType.BEZIER,tangent_mode='broken',handle_right=(20,0)),Keyframe(24,1.0,handle_left=(-2,0),tangent_mode='broken')))
sk = k.time_scaled(Fraction(2))
results['fps_bezier']={'before':k.get_value(6),'scaled_equivalent':sk.get_value(12),'scaled_handle':sk.keyframes[0].handle_right,'expected_handle':[40,0]}

c = clip('mc','v1',source_start=0,_source_fps_num=24,_source_fps_den=1,_timeline_fps_num=24,_timeline_fps_den=1)
t = seq(v=(track('v1','video',c),))
n,cmd=SwitchMulticamAngleCommand.create('v1','mc',24,'cam48').apply(t)
nc=n.get_clip_by_id(cmd._right_clip_id)
results['multicam_mixedfps']={'target_source_start':nc.source_start,'target_source_fps':nc._source_fps_num,'target_source_duration':nc.source_duration,'expected_for_48fps':[48,48,144]}

bus=track('bus','audio',is_bus=True,volume=0.25)
au=track('a1','audio',clip('au','a1',24,48),output_bus_id='bus',sends=(Send(target_bus_id='bus',level=0.5),))
nest=build_nested_from_selection(seq(a=(au,bus)),('au',),child_timeline_id='child')
results['nest_bus']={'child_tracks':[x.track_id for x in nest.child_timeline.audio_tracks],'route':nest.child_timeline.audio_tracks[0].output_bus_id,'send':nest.child_timeline.audio_tracks[0].sends[0].target_bus_id}

def source_view(t):
    asset=MediaAsset('src','/tmp/source.mp4','video','source.mp4',metadata={'fps':48.0,'duration_frames':48,'audio_info':{'channels':2}})
    p=EditProject('p','P').with_media_asset_added(asset)
    view=MagicMock(); view._timeline=t; view._project=p; view._playhead_frame=0
    view.get_target_track_id=lambda:'v1'; view.get_target_audio_track_id=lambda:'a1'
    out=[]
    def execute(command):
        val=command.apply(view._timeline)
        view._timeline=val[0] if isinstance(val,tuple) else val
        out.append(command)
    view._execute_command=execute
    return view,out

sv,out=source_view(seq(v=(track('v1','video'),),a=(track('a1','audio'),)))
TimelineView.insert_clip(sv,'src',0,47,playhead=0,track_id='v1')
results['source_insert_mixedfps']={'v_duration':sv._timeline.video_tracks[0].clips[0].timeline_duration,'a_duration':sv._timeline.audio_tracks[0].clips[0].timeline_duration,'expected':24}
sv,out=source_view(seq(v=(track('v1','video',clip('ov','v1',0,96)),),a=(track('a1','audio',clip('oa','a1',0,48)),)))
TimelineView.append_clip(sv,'src',0,47,track_id='v1')
results['source_append_align']={'video_start':sv._timeline.video_tracks[0].clips[-1].timeline_start,'audio_start':sv._timeline.audio_tracks[0].clips[-1].timeline_start,'expected_common_start':96}
sv,out=source_view(seq(v=(track('v1','video',clip('ov','v1',0,96)),),a=(track('a1','audio',clip('oa','a1',0,96)),)))
try:
    TimelineView.overwrite_clip(sv,'src',0,23,playhead=24,track_id='v1')
    results['source_overwrite_overlap']={'exception':None}
except Exception as exc:
    results['source_overwrite_overlap']={'exception':type(exc).__name__,'message':str(exc),'unchanged':sv._timeline.video_tracks[0].clips[0].asset_id=='old'}
sv,out=source_view(seq(v=(track('v1','video',clip('ov','v1',0,96)),),a=(track('a1','audio',clip('oa','a1',0,96)),)))
TimelineView.insert_clip(sv,'src',0,23,playhead=24,track_id='v1')
results['source_insert_inside']={'video_count':len(sv._timeline.video_tracks[0].clips),'audio_count':len(sv._timeline.audio_tracks[0].clips),'new_audio_source_count':sum(c.asset_id=='src' for c in sv._timeline.audio_tracks[0].clips)}

rng=np.random.default_rng(42); sig=rng.standard_normal((1,512))*.7
def peak(sig, chunk):
    est=_TruePeakEstimator(1)
    for start in range(0,sig.shape[-1],chunk): est.process(sig[:,start:start+chunk])
    return est.peak
results['truepeak_partitions']={str(chunk):peak(sig,chunk) for chunk in (1,2,3,4,8,97,512)}
small=np.array([[0.,1.,1.,0.]])
results['truepeak_tail']={'short':peak(small,4),'explicit_zero_flush':peak(np.pad(small,((0,0),(0,24))),28),'reported_short_dbtp':analyze_loudness(small,block_size=4).true_peak_dbtp}
late=np.pad(small,((0,0),(60,0)))
early=np.pad(small,((0,0),(0,60)))
results['truepeak_edge_partitions']={str(chunk):peak(early,chunk) for chunk in (1,2,3,4,5,8,64)}
results['truepeak_ones']={str(chunk):peak(np.ones((1,64)),chunk) for chunk in (1,2,64)}
results['truepeak_late']={'estimator':peak(late,64),'report_dbtp':analyze_loudness(late,block_size=64).true_peak_dbtp,'after_zero_flush':peak(np.pad(late,((0,0),(0,24))),88)}

payload=seq(v=(track('v1','video',clip('c','v1')),)).to_dict()
payload['future_sequence']={'enabled':True};payload['video_tracks'][0]['future_track']={'x':1};payload['video_tracks'][0]['clips'][0]['future_clip']={'x':1}
restored=TimelineSequence.from_dict(payload)
roundtrip=restored.to_dict()
edited=restored.with_clip_updated(replace(restored.video_tracks[0].clips[0],timeline_start=48)).to_dict()
results['unknown_roundtrip']={'sequence_preserved':'future_sequence' in roundtrip,'track_preserved':'future_track' in roundtrip['video_tracks'][0],'clip_preserved':'future_clip' in roundtrip['video_tracks'][0]['clips'][0],'clip_after_edit_preserved':'future_clip' in edited['video_tracks'][0]['clips'][0]}

source_path=Path('/tmp/topos_round3_interaction_features_source.mp4');source_path.write_bytes(b'x')
proxy_path=Path('/tmp/topos_round3_interaction_features_proxy.mp4');proxy_path.write_bytes(b'x')
asset=MediaAsset('asset',str(source_path),'video','source.mp4',metadata={'fps':24,'duration_frames':96})
p1=EditProject('p1','P1').with_media_asset_added(asset)
p2=EditProject('p2','P2').with_media_asset_added(asset)
panel=SimpleNamespace(_project=p1,_updating=False,_pending_timeline_frame_previews={},_pending_timeline_image_previews={},_offline_status_signature=MediaPoolPanel._offline_signature(p2),_active_import_batch_id=None,_proxy_jobs={'asset':str(source_path)},_proxy_job_tokens={'asset':17},_proxy_token_counter=17,_analysis_status_cache={},tree=MagicMock(),bin_tree=MagicMock(),grid_view=None,project_changed=MagicMock())
for name in ('_refresh_bin_tree','_refresh_tree','_update_status_label','_notify_analysis_project_changed','_refresh_analysis_badges','_update_disk_proxy_services'):
    setattr(panel,name,lambda *a:None)
panel._has_same_pool_content=lambda p:False
panel._offline_signature=MediaPoolPanel._offline_signature
panel._proxy_job_token_valid=lambda aid,token:MediaPoolPanel._proxy_job_token_valid(panel,aid,token)
panel._invalidate_proxy_job=lambda aid:MediaPoolPanel._invalidate_proxy_job(panel,aid)
MediaPoolPanel.set_project(panel,p2)
MediaPoolPanel._on_proxy_finished(panel,'asset',str(proxy_path),token=17)
results['proxy_switch_project']={'project_id':panel._project.project_id,'proxy_status':panel._project.assets['asset'].proxy_status,'proxy_path':panel._project.assets['asset'].proxy_path,'old_job_accepted':panel._project.assets['asset'].has_proxy}
panel._pending_proxy_meta={str(proxy_path):(str(source_path),0.0,1)}
panel._apply_proxy_ready=lambda *args:MediaPoolPanel._apply_proxy_ready(panel,*args)
disk_service=MagicMock();frame_service=MagicMock();frame_service.get_shared_clips_info.return_value={}
old_disk=media_pool_module.get_disk_proxy_service;old_frames=media_pool_module.get_frame_provider_service
try:
    media_pool_module.get_disk_proxy_service=lambda:disk_service
    media_pool_module.get_frame_provider_service=lambda:frame_service
    MediaPoolPanel._on_proxy_resolution_probed(panel,str(proxy_path),str(source_path),(640,360))
finally:
    media_pool_module.get_disk_proxy_service=old_disk;media_pool_module.get_frame_provider_service=old_frames
results['proxy_late_resolution']={'cache_mark_ready_calls':disk_service.mark_ready.call_count,'project_checked':False}

jpg=Path('/tmp/topos_round3_interaction_features_replacement.jpg');jpg.write_bytes(b'jpg')
p=replace(p1,revision=7)
stack=ProjectCommandStack();replaced=stack.execute(ReplaceMediaCommand.create('asset',str(jpg)),p)
undo=stack.undo(replaced);redo=stack.redo(undo)
host=SimpleNamespace(_current_edit_project=p,_current_edit_project_filepath=None,timeline_panel=MagicMock(),timeline_preview_renderer=None,edit_project_dirty=False,_sync_timeline_sequences_after_project_change=lambda *a:None,_sync_asset_references_after_project_change=lambda *a:False,_sync_active_composite_after_project_change=lambda *a:None,setWindowTitle=lambda *a:None)
FileOpsMixin._on_media_pool_project_changed(host,replaced)
results['project_replace_revision_probe']={'revisions':[p.revision,replaced.revision,undo.revision,redo.revision,host._current_edit_project.revision],'media_type':replaced.assets['asset'].media_type,'fps':replaced.assets['asset'].fps,'duration':replaced.assets['asset'].total_frames,'metadata_preserved':replaced.assets['asset'].metadata==p.assets['asset'].metadata}
results['legacy_json_revision']={'before':p.revision,'after':EditProjectSerializer.from_json(EditProjectSerializer.to_json(p)).revision}

group=group_with_sync_offsets(build_multicam_group_from_assets(['camA','camB']),{'camA':0,'camB':10})
mp=EditProject('mp','M')
for ma in stamp_multicam_group_on_assets([MediaAsset('camA','/tmp/a.mp4','video','A',metadata={}),MediaAsset('camB','/tmp/b.mp4','video','B',metadata={})],group): mp=mp.with_media_asset_added(ma)
mc=clip('synced','v1',20,80,asset_id='camB',source_start=30)
mt=seq(v=(track('v1','video',mc),))
mh=SimpleNamespace(_current_edit_project=mp,media_pool_panel=None,_refresh_multicam_panel=lambda:None,_on_multicam_playhead_frame=lambda x:None,edit_page=SimpleNamespace(_current_frame=40),status_bar=MagicMock())
EditControllerMixin._apply_multicam_sync_offsets(mh,group,{'camB':{'offset_frames':20}})
actual_group=multicam_group_from_asset(mh._current_edit_project.assets['camB'])
new_offset=next(a.sync_offset for a in actual_group.angles if a.asset_id=='camB')
mt2,mcmd=SwitchMulticamAngleCommand.create('v1','synced',40,'camA',0,active_sync_offset=new_offset).apply(mt)
results['multicam_resync_existing']={'unchanged_clip_source_start':mc.source_start,'old_active_offset':10,'new_active_offset':new_offset,'back_to_A_source':mt2.get_clip_by_id(mcmd._right_clip_id).source_start,'before_resync_expected':40,'revision_after_sync':mh._current_edit_project.revision}
print(json.dumps(results,indent=2))
