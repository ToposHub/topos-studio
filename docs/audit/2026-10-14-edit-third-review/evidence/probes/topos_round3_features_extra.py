import sys, json, time
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import MagicMock, patch
ROOT=Path('/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
sys.path.insert(0,str(ROOT))
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence, SubtitleClip, Rational, Send
from src.features.edit.core.captions.model import CaptionCueMetadata,CaptionWordTiming
from src.features.edit.core.commands import ProjectCommandStack,SwitchMulticamAngleCommand,UpdateClipSourceCommand,UpdateTimelineSettingsCommand
from src.features.edit.core.project import EditProject,MediaAsset,TimelineSequenceRef,probe_media_metadata
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.app.controllers.edit_controller import EditControllerMixin
from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.core.nested_sequence import build_nested_from_selection
from src.features.edit.core.multicam import build_multicam_group_from_assets,stamp_multicam_group_on_assets,METADATA_ASSET_GROUP_KEY
import src.features.edit.ui.media_pool_panel as pool_module
import src.features.edit.ui.new_timeline_dialog as settings_module
out={}
def clip(cid,tid,start=0,duration=96,**kw):
    return ClipInstance(clip_id=cid,asset_id=kw.pop('asset_id','old'),track_id=tid,timeline_start=start,timeline_duration=duration,source_duration=kw.pop('source_duration',duration),**kw)
def track(tid,typ,*cs,**kw):return EditTrack(track_id=tid,name=tid,track_type=typ,index=0,clips=cs,**kw)
def seq(v=(),a=(),s=(),**kw):return TimelineSequence(timeline_id='tl',name='T',fps=Rational(24,1),video_tracks=v,audio_tracks=a,subtitle_tracks=s,**kw)
def view(t,has_audio=True):
    asset=MediaAsset('src','/tmp/topos_round3_features_source48.mp4','video','S',{'fps':48.,'total_frames':48,**({'audio_info':{'channels':2}} if has_audio else {})})
    p=EditProject('p','P').with_media_asset_added(asset)
    h=SimpleNamespace(_timeline=t,_project=p,_playhead_frame=0,get_target_track_id=lambda:'v1',get_target_audio_track_id=lambda:'a1')
    h._resolve_source_fps_metadata=lambda aid:TimelineView._resolve_source_fps_metadata(h,aid)
    h.commands=[]
    def execute(cmd):
        r=cmd.apply(h._timeline);h._timeline=r[0] if isinstance(r,tuple) else r;h.commands.append(cmd)
    h._execute_command=execute
    return h
for op,has_audio in [('append',True),('append',False),('overwrite',True),('overwrite',False)]:
    h=view(seq(v=(track('v1','video'),),a=(track('a1','audio'),)),has_audio)
    getattr(TimelineView,op+'_clip')(h,'src',0,47,track_id='v1')
    c=h._timeline.video_tracks[0].clips[0]
    out['source_'+op+('_av' if has_audio else '_silent')]={'duration':c.timeline_duration,'source_fps':c._source_fps_num,'timeline_fps':c._timeline_fps_num,'source_at_12':c.timeline_to_source_frame(c.timeline_start+12),'expected_duration':24,'expected_source_at_12':24,'audio_count':len(h._timeline.audio_tracks[0].clips)}
for op in ['overwrite','append']:
    h=view(seq(v=(track('v1','video'),)),True);h.get_target_audio_track_id=lambda:None
    getattr(TimelineView,op+'_clip')(h,'src',0,47,track_id='v1')
    out['source_'+op+'_no_audio_track']={'video_clips':len(h._timeline.video_tracks[0].clips),'audio_tracks':len(h._timeline.audio_tracks),'source_has_audio':True}

c=clip('mc','v1',24,72,asset_id='A',source_start=24,source_duration=200,_source_fps_num=24,_source_fps_den=1,_timeline_fps_num=24,_timeline_fps_den=1)
t=seq(v=(track('v1','video',c),))
cmd=SwitchMulticamAngleCommand.create('v1','mc',24,'B',angle_fps_num=48,angle_fps_den=1)
changed,ucmd=cmd.apply(t);n=changed.get_clip_by_id('mc');back=ucmd.undo(changed);bc=back.get_clip_by_id('mc')
out['multicam_boundary_apply_undo']={'apply':[n.asset_id,n.source_start,n.source_duration,n._source_fps_num,n.timeline_duration],'undo':[bc.asset_id,bc.source_start,bc.source_duration,bc._source_fps_num,bc.timeline_duration],'before':[c.asset_id,c.source_start,c.source_duration,c._source_fps_num,c.timeline_duration],'undo_payload_equal':t.to_dict()==back.to_dict()}
sourcecmd=UpdateClipSourceCommand('u','u','v1','mc','B',_new_source_fps_num=48,_new_source_fps_den=1)
first,updated=sourcecmd.apply(t);restored=updated.undo(first);redone,updated2=updated.apply(restored)
out['source_swap_redo']={'new_fps_saved_on_updated_command':updated._new_source_fps_num,'json':updated.to_dict(),'undo_fps':restored.get_clip_by_id('mc')._source_fps_num,'redo_source_duration':redone.get_clip_by_id('mc').source_duration,'redo_fps':redone.get_clip_by_id('mc')._source_fps_num}
cpts=replace(c,_source_pts_index=tuple(i/24 for i in range(240)))
pts_t=seq(v=(track('v1','video',cpts),))
ptarget,_=UpdateClipSourceCommand('pts','pts','v1','mc','B',new_source_start=48,_new_source_fps_num=48,_new_source_fps_den=1).apply(pts_t)
pt=ptarget.get_clip_by_id('mc')
out['source_swap_old_pts']={'pts_preserved':pt._source_pts_index==cpts._source_pts_index,'source_start':pt.source_start,'source_at_timeline36':pt.timeline_to_source_frame(36),'expected_new_cfr48_source':72}
c48=replace(c,_source_fps_num=48,source_duration=144,fade_in_duration=40)
t48=seq(v=(track('v1','video',c48),))
try:
    ch,_=SwitchMulticamAngleCommand.create('v1','mc',24,'B',angle_fps_num=24,angle_fps_den=1).apply(t48)
    out['multicam_smaller_fps_fade']={'exception':None,'clip':ch.get_clip_by_id('mc').to_dict()}
except Exception as exc:out['multicam_smaller_fps_fade']={'exception':type(exc).__name__,'message':str(exc)}

# Actual controller obtains target metadata and commits switch command.
group=build_multicam_group_from_assets(['A','B']);mp=EditProject('mp','MP')
for ma in stamp_multicam_group_on_assets([MediaAsset('A','/tmp/a.mp4','video','A',{'fps':24,'total_frames':200}),MediaAsset('B','/tmp/b.mp4','video','B',{'fps':48,'total_frames':400})],group):mp=mp.with_media_asset_added(ma)
holder=SimpleNamespace(timeline=t,commands=[])
def switch_commit(cmd):
    val=cmd.apply(holder.timeline);holder.timeline=val[0];holder.commands.append(val[1])
mh=SimpleNamespace(_current_edit_project=mp,edit_page=SimpleNamespace(_current_frame=24),timeline_panel=SimpleNamespace(get_timeline_view=lambda:SimpleNamespace(execute_command=switch_commit)),_multicam_clip_at_playhead=lambda gid='':holder.timeline.get_clip_by_id('mc'),status_bar=MagicMock(),_refresh_multicam_panel=lambda:None,_on_multicam_playhead_frame=lambda n:None)
EditControllerMixin._switch_multicam_angle(mh,group.group_id,'B')
m=holder.timeline.get_clip_by_id('mc');out['multicam_controller_target']={'source_start':m.source_start,'source_duration':m.source_duration,'source_fps':m._source_fps_num,'expected':[48,144,48]}

before=replace(t,revision=2);ref=TimelineSequenceRef('tl','T',24.,1920,1080,48000)
project=EditProject('p','P',timelines={'tl':ref},active_timeline_id='tl',revision=7)
after=replace(before,fps=Rational(48,1))
scmd=UpdateTimelineSettingsCommand.create('tl',before,after)
p1,s1,_=scmd.apply(project);p2,s2=scmd.undo(p1);p3,s3,_=scmd.apply(p2)
out['settings_domain_revisions']={'project':[project.revision,p1.revision,p2.revision,p3.revision],'sequence':[before.revision,s1.revision,s2.revision,s3.revision]}
try:
    ProjectCommandStack().execute(scmd,project);out['settings_stack_protocol']={'exception':None}
except Exception as exc:out['settings_stack_protocol']={'exception':type(exc).__name__,'message':str(exc)}
settings={'name':'T48','fps':48.,'width':1920,'height':1080,'sample_rate':48000}
class FakeSettings:
    def __init__(self,*args,**kwargs):pass
    def exec(self):return 1
    def get_settings(self):return settings
sh=SimpleNamespace(_current_edit_project=project,_current_timeline_sequences={'tl':before},config_manager=None,media_pool_panel=MagicMock(),timeline_panel=MagicMock(),edit_page=MagicMock(),timeline_preview_renderer=None,_invalidate_motion_graphic_caches_for_timeline_change=lambda *a:None)
for fn in ['_with_timeline_fps']:
    setattr(sh,fn,lambda *args,_fn=fn:getattr(EditControllerMixin,_fn)(sh,*args))
sh._scale_timeline_frame=EditControllerMixin._scale_timeline_frame;sh._rational_for_timeline_fps=EditControllerMixin._rational_for_timeline_fps;sh._format_settings_from_dialog=EditControllerMixin._format_settings_from_dialog;sh._timeline_settings_for_dialog=EditControllerMixin._timeline_settings_for_dialog
with patch.object(settings_module,'NewTimelineDialog',FakeSettings):EditControllerMixin._on_timeline_settings_requested(sh,'tl')
out['settings_controller']={'sequence_fps':float(sh._current_timeline_sequences['tl'].fps),'sequence_revision':sh._current_timeline_sequences['tl'].revision,'project_revision':sh._current_edit_project.revision,'timeline_execute_calls':sh.timeline_panel.execute_command.call_count,'timeline_load_calls':sh.timeline_panel.load_timeline.call_count}

# Actual replace slot; file/confirm dialogs are controlled substitutes.
bad=Path('/tmp/topos_round3_features_invalid.mp4');bad.write_bytes(b'not-a-video')
png=Path('/tmp/topos_round3_features_replacement.png')
from PIL import Image
Image.new('RGB',(2,2),(20,40,60)).save(png)
old=MediaAsset('asset','/tmp/topos_round3_features_old.mov','video','old.mov',{'fps':24,'total_frames':100,'future_asset':{'x':1},METADATA_ASSET_GROUP_KEY:group.to_dict()},color_metadata={'transfer':'PQ','primaries':'BT2020'},thumbnail_key='old-thumb')
def replace_slot(target,probe=None):
    panel=SimpleNamespace(_project=EditProject('p','P',assets={'asset':old},revision=7),_command_stack=ProjectCommandStack(),_get_top_level_widget=lambda:None,_collect_timeline_sequences=lambda:[],_invalidate_proxy_job=lambda aid:None,project_changed=MagicMock(),_refresh_tree=lambda:None)
    with patch.object(pool_module.QFileDialog,'getOpenFileName',return_value=(str(target),'')),patch.object(pool_module.QMessageBox,'question',return_value=pool_module.QMessageBox.StandardButton.Yes),patch.object(pool_module.QMessageBox,'warning') as warnings:
        start=time.perf_counter()
        if probe is None:MediaPoolPanel._on_replace_media(panel,'asset')
        else:
            with patch.object(pool_module,'probe_media_metadata',probe):MediaPoolPanel._on_replace_media(panel,'asset')
        elapsed=time.perf_counter()-start
    a=panel._project.assets['asset']
    return {'type':a.media_type,'metadata':a.metadata,'color_metadata':a.color_metadata,'thumbnail_key':a.thumbnail_key,'revision':panel._project.revision,'path_changed':a.path==str(target),'warning_count':warnings.call_count,'elapsed':elapsed,'history_count':len(panel._command_stack._undo_stack)}
out['replace_invalid_actual']=replace_slot(bad)
out['replace_image_actual']=replace_slot(png)
def broken_probe(path):raise RuntimeError('controlled probe failure')
out['replace_probe_exception']=replace_slot(png,broken_probe)
def slow_probe(path):time.sleep(.12);return {'width':2,'height':2}
out['replace_sync_probe']=replace_slot(png,slow_probe)

# Proxy stage 2 old result vs newly registered identical path, and mismatch pop.
ph=SimpleNamespace(_proxy_session_generation=3,_proxy_jobs={},_proxy_job_tokens={},_proxy_job_generations={},_pending_proxy_meta={})
ph._apply_proxy_ready=MagicMock();same='/tmp/topos_round3_features_same_proxy.mov';src='/tmp/topos_round3_features_same_src.mov'
ph._pending_proxy_meta[same]=(src,200.,20)
MediaPoolPanel._on_proxy_resolution_probed(ph,same,src,(111,222))
out['proxy_stage2_same_path_requeue']={'apply_calls':ph._apply_proxy_ready.call_args_list.__str__(),'remaining_new_pending':same in ph._pending_proxy_meta,'old_result_accepted':ph._apply_proxy_ready.call_count==1}
ph._apply_proxy_ready.reset_mock();ph._pending_proxy_meta[same]=(src,300.,30)
MediaPoolPanel._on_proxy_resolution_probed(ph,same,'/tmp/topos_round3_features_older_src.mov',(1,1))
MediaPoolPanel._on_proxy_resolution_probed(ph,same,src,(640,360))
out['proxy_stage2_mismatch_consumes_new']={'apply_calls':ph._apply_proxy_ready.call_count,'pending_after_old_callback':same in ph._pending_proxy_meta}

# Extras survive old roundtrip, but reconstructive nest helper drops track extras.
payload=track('a1','audio',clip('au','a1',24,48)).to_dict();payload['future_track']={'x':1}
et=EditTrack.from_dict(payload);nt=build_nested_from_selection(seq(a=(et,)),('au',),child_timeline_id='child')
out['nest_unknown_track_extras']={'parent_extra':et.to_dict().get('future_track'),'child_extra':nt.child_timeline.audio_tracks[0].to_dict().get('future_track')}

# UI relink uses helper directly, bypassing newly revisioned project stack.
rh=SimpleNamespace(_project=EditProject('p','P',assets={'asset':old},revision=7),_get_top_level_widget=lambda:None,_invalidate_proxy_job=lambda aid:None,project_changed=MagicMock(),_refresh_tree=lambda:None)
with patch.object(pool_module.QFileDialog,'getOpenFileName',return_value=(str(bad),'')):MediaPoolPanel._on_relink_asset(rh,'asset')
fh=SimpleNamespace(_current_edit_project=EditProject('p','P',assets={'asset':old},revision=7),_current_edit_project_filepath=None,timeline_panel=MagicMock(),timeline_preview_renderer=None,edit_project_dirty=False,_sync_timeline_sequences_after_project_change=lambda *a:None,_sync_asset_references_after_project_change=lambda *a:False,_sync_active_composite_after_project_change=lambda *a:None,setWindowTitle=lambda *a:None)
FileOpsMixin._on_media_pool_project_changed(fh,rh._project)
out['ui_relink_commit_revision']={'panel_revision':rh._project.revision,'host_revision':fh._current_edit_project.revision,'new_path':fh._current_edit_project.assets['asset'].path}
print(json.dumps(out,indent=2,default=str))
