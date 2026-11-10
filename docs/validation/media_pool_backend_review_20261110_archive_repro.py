"""Isolated reproductions: only writes synthetic files in a temporary directory.

R01/R02/R04（媒体池复查整改后验收）：
- R02 collect_project 不覆盖目标目录既有文件（同名媒体/LUT/manifest
  保留原字节，冲突追加序号改名）；
- R01 面板收集完成路径把发起时冻结的 timeline_sequences 随归档工程包
  保存（整理路径保存重映射后的新序列，见单元回归），重开归档时间线
  可编辑；
- R04 排队期间切工程：任务载荷使用发起时工程快照（不混用"替换工程
  + 旧时间线"）；代际过期后晚到的完成回调被丢弃（不弹成功提示）。
"""
from __future__ import annotations
import os
import sys
import json
import time
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['TOPOS_TEST_MODE'] = '1'
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.project import EditProject
from src.features.edit.core.project_archive import collect_project
from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from tempfile import TemporaryDirectory
temporary = TemporaryDirectory(prefix='topos-media-pool-review-')
work = Path(temporary.name)
app = QApplication.instance() or QApplication([])
source = work / 'source.mov'
source.write_bytes(b'NEW_SOURCE_CONTENT')
project = EditProject.create('Audit original').import_asset(str(source), 'video', {'fps':25.,'total_frames':100})
project = project.create_timeline('Audit timeline')
tid = next(iter(project.timelines))
aid = project.get_asset_by_path(str(source)).asset_id
clip = ClipInstance('c',aid,'v',0,20,10,20,_source_fps_num=25,_source_fps_den=1,_timeline_fps_num=25,_timeline_fps_den=1)
sequence = TimelineSequence(timeline_id=tid,name='Audit timeline',fps=Rational(25,1),video_tracks=(EditTrack('v','V','video',0,clips=(clip,)),))
output={}
# R02: collection must not overwrite files already in the target directory.
target=work/'existing_target'
(target/'media').mkdir(parents=True,exist_ok=True)
existing=target/'media'/'source.mov'
existing.write_bytes(b'PREEXISTING_ARCHIVE_KEEP_ME')
with patch('src.features.edit.core.project_archive.build_identity_manifest', return_value={}):
    archived,report=collect_project(project,target_dir=str(target),sequences=[sequence])
output['existing_target_overwritten'] = existing.read_bytes()==source.read_bytes()
output['conflict_renamed_copy_present'] = (target/'media'/'source_2.mov').read_bytes()==source.read_bytes()
# R02: a pre-existing dependencies manifest is preserved (never clobbered).
manifest=work/'existing_target'/'dependencies.json'
manifest.write_text('KEEP_PREEXISTING')
with patch('src.features.edit.core.project_archive.build_identity_manifest', return_value={'marker':'second-run'}):
    collect_project(project,target_dir=str(target),sequences=[sequence])
output['preexisting_manifest_kept'] = manifest.read_text()=='KEEP_PREEXISTING'
# R01: the real panel finish path persists frozen timeline sequences.
panel=MediaPoolPanel(); panel._offline_refresh_timer.stop(); panel.set_project(project)
queued=[]
panel._collect_timeline_sequences=lambda:[sequence]
def queue(kind: str, work_fn: Any, finish_fn: Any, **kwargs: Any) -> None: queued.append((kind,work_fn,finish_fn))
panel._run_media_management=queue
with patch('src.features.edit.ui.media_pool_panel.QFileDialog.getExistingDirectory',return_value=str(work/'archive_out')), \
     patch('src.features.edit.ui.media_pool_panel.QMessageBox'):
    panel._on_collect_project()
    archived,report=queued[0][1](lambda *a:None,lambda:False)
    queued[0][2](archived,report)
saved_path=work/'archive_out'/'Audit original.topos'
loaded=EditProjectIO().load_project(str(saved_path)) if saved_path.exists() else None
output['archive_save_returns_success']=saved_path.exists()
output['saved_timeline_refs']=list(loaded[0].timelines) if loaded else None
output['saved_timeline_sequences']=list((loaded[2] or {}).keys()) if loaded else None
# R04: queued work carries the initiating project snapshot (not the replacement).
replacement=EditProject.create('Changed project')
seen=[]
def fake_collect(actual: Any, **kwargs: Any) -> tuple[Any, None]:
    seen.append({'project_id':actual.project_id,'sequence_id':kwargs['sequences'][0].timeline_id})
    return actual, None
with patch('src.features.edit.ui.media_pool_panel.collect_project',side_effect=fake_collect):
    queued.clear()
    with patch('src.features.edit.ui.media_pool_panel.QFileDialog.getExistingDirectory',return_value=str(work/'later')):
        panel._on_collect_project()
    panel.set_project(replacement)   # 排队期间切工程（原缺陷触发方式）
    queued[0][1](lambda *_:None,lambda:False)
output['queued_collect_uses_replacement_project']=seen[0]['project_id']==replacement.project_id
output['queued_collect_keeps_old_sequence']=seen[0]['sequence_id']==tid
panel.clear_project();panel.close();panel.deleteLater();app.processEvents()
# R04: a late completion after the session generation moved on is dropped.
panel2=MediaPoolPanel(); panel2._offline_refresh_timer.stop(); panel2.set_project(project)
finished=[]
switched=threading.Event()
def blocking_work(progress_cb: Any, cancel_check: Any) -> tuple[str, None]:
    switched.wait(5)
    return ('late-done', None)
panel2._run_media_management('collect', blocking_work,
                             lambda payload, report: finished.append(payload),
                             estimate_bytes=1, target_dir=str(work),
                             project_id=project.project_id)
panel2._media_mgmt_session_generation += 1   # 切工程已发生（代际过期）
switched.set()
deadline=time.time()+10
while panel2._media_mgmt_running and time.time()<deadline:
    app.processEvents()
time.sleep(0.2); app.processEvents()
output['late_finish_after_project_switch_dropped']=not finished
panel2.clear_project();panel2.close();panel2.deleteLater();app.processEvents()
print(json.dumps(output,indent=2,ensure_ascii=False))

temporary.cleanup()
