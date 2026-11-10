import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('TOPOS_TEST_MODE', '1')
from pathlib import Path
from typing import Any
import sys, json
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.features.edit.ai_editing.analysis_service import AnalysisService
from src.features.edit.ai_editing.analysis_request import AnalysisRequest, AnalysisRecipe, RequestState
from src.features.edit.ai_editing.analysis_scheduler import ResourceLane
from src.features.edit.ai_editing.analysis_models import AssetAnalysis, AssetFingerprint
from src.features.edit.ai_editing.jobs import JobKind
from src.features.edit.ai_editing.transcription_job import TranscriptionSegment, segments_from_dicts

class Scheduler:
    def __init__(self) -> None: self.items=[]
    def submit(self,lane: Any,job: Any,**kw: Any) -> None: self.items.append((job,kw))
    def notify_finished(self,job_id: str) -> None: pass

service=AnalysisService()
req=AnalysisRequest.create(project_id='p',asset_ids=('a','b'),recipe_id=AnalysisRecipe.FAST_LOCAL).with_state(RequestState.RUNNING)
service._requests[req.request_id]=req
scheduler=Scheduler()
summary,_=service._execute_via_scheduler(req.request_id,req,scheduler=scheduler,lane=ResourceLane.ANALYSIS_LOCAL,fps='24/1',provider_ids=(),on_asset_done=None,on_request_done=None)
scheduler.items[0][1]['on_cancel']()
print(json.dumps({'case':'cancel_one_of_two_pending','summary_status':summary.status.value,'request_state':service.get_request(req.request_id).state.value,'second_job_status':scheduler.items[1][0].status.value}))

service=AnalysisService()
req=AnalysisRequest.create(project_id='p',asset_ids=('a',),recipe_id=AnalysisRecipe.FAST_LOCAL).with_state(RequestState.RUNNING)
service._requests[req.request_id]=req
scheduler=Scheduler()
summary,_=service._execute_via_scheduler(req.request_id,req,scheduler=scheduler,lane=ResourceLane.ANALYSIS_LOCAL,fps='24/1',provider_ids=(),on_asset_done=None,on_request_done=None)
outer,kw=scheduler.items[0]
outer.mark_running();outer.request_cancel()
def pretend_provider(asset_id: str,fingerprint: Any,**kwargs: Any) -> tuple[Any, str]:
    inner=service.jobs.submit(JobKind.ANALYSIS,label='inner');inner.mark_running()
    service.ingest(asset_id,AssetAnalysis(asset_id,fingerprint),())
    inner.complete({})
    return inner,''
service.request_analysis=pretend_provider
kw['work']()
print(json.dumps({'case':'cancel_running_before_provider_returns','outer_status':outer.status.value,'summary_status':summary.status.value,'evidence_stored':service.get_analysis('a') is not None}))
for raw in [float('nan'),float('inf')]:
    try:
        segment=TranscriptionSegment(raw,raw,'bad')
        print(json.dumps({'case':'nonfinite_segment_accepted','value':repr(raw)}))
    except Exception as exc: print(json.dumps({'case':'nonfinite_segment_rejected','error':type(exc).__name__}))
try:
    segments_from_dicts([None, {'start_seconds':0,'end_seconds':1,'text':'kept'}])
except Exception as exc:
    print(json.dumps({'case':'invalid_segment_rejected','error':type(exc).__name__}))

from tempfile import TemporaryDirectory
from unittest.mock import patch
from src.features.edit.core.project import EditProject, relink_asset, MediaAsset
from src.features.edit.core.commands import ProjectCommandStack, RenameAssetCommand
from src.features.edit.core import proxy_generator
with TemporaryDirectory() as td:
    root=Path(td)
    old=root/'old.mp4';old.write_bytes(b'old')
    new=root/'new.mp4';new.write_bytes(b'new')
    project=EditProject.create('Relink audit').import_asset(str(old),'video',{'fps':24.0,'total_frames':240})
    aid=next(iter(project.assets))
    stack=ProjectCommandStack()
    project=stack.execute(RenameAssetCommand.create(aid,'renamed'),project)
    project=relink_asset(project,aid,str(new))
    linked=project.assets[aid].path
    project=stack.undo(project)
    print(json.dumps({'case':'undo_prior_rename_reverts_unrecorded_relink','linked_before_undo':Path(linked).name,'path_after_undo':Path(project.assets[aid].path).name}))

with TemporaryDirectory() as td:
    root=Path(td)
    source=root/'shot.mp4';source.write_bytes(b'test-source')
    target=root/'shot_proxy.mp4';target.write_bytes(b'unrelated-user-file')
    asset=MediaAsset('a',str(source),'video','shot.mp4',{})
    class FailedProcess:
        returncode=1
        def communicate(self,timeout: Any=None) -> tuple[bytes, bytes]:return b'',b'forced error'
    with patch.object(proxy_generator.subprocess,'Popen',return_value=FailedProcess()), patch.object(proxy_generator,'find_ffmpeg',return_value='never-run'):
        proxy_generator._generate_video_proxy_cli(asset,str(target),proxy_generator.ProxyConfig())
    print(json.dumps({'case':'failed_proxy_removes_preexisting_filename','target_exists_after_failure':target.exists(),'source_exists':source.exists()}))

from dataclasses import replace
from types import SimpleNamespace
from src.features.edit.ui import media_pool_panel as mpp
with TemporaryDirectory() as td:
    root=Path(td)
    old=root/'old.mp4';old.write_bytes(b'old')
    other=root/'other.mp4';other.write_bytes(b'other')
    target=root/'new.mp4';target.write_bytes(b'new')
    p1=EditProject.create('first').import_asset(str(old),'video',{})
    aid=next(iter(p1.assets))
    p2=replace(p1,project_id='different-project',assets={aid:replace(p1.assets[aid],path=str(other))})
    class Panel:
        _project=p1
        project_changed=SimpleNamespace(emit=lambda *args: None)
        def _get_top_level_widget(self) -> None:return None
        def _invalidate_proxy_job(self,*args: Any) -> None:pass
        def _refresh_tree(self) -> None:pass
    panel=Panel()
    def modal_switch(*args: Any,**kwargs: Any) -> tuple[str, str]:
        panel._project=p2
        return str(target),''
    with patch.object(mpp.QFileDialog,'getOpenFileName',side_effect=modal_switch):
        mpp.MediaPoolPanel._on_relink_asset(panel,aid)
    print(json.dumps({'case':'relink_mutates_project_switched_during_modal','project_id':panel._project.project_id,'new_path':Path(panel._project.assets[aid].path).name}))
