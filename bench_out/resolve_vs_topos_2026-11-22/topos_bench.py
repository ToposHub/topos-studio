from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('TOPOS_CONFIG_PATH', str(OUT / 'topos_runtime' / 'config.json'))
os.environ.setdefault('TOPOS_EDIT_PLAYBACK_TRACE', 'summary')
MEDIA = ROOT / 'tests' / 'test_videos'
FILES = {'h264': '4K_H264.mov', 'dnxhr': '4K_DNxHR HQX 10bit.mov'}
LUT = OUT / 'Topos_Resolve_AB_33.cube'
PROJECT = OUT / 'Topos_UHD25_AB.topos'
FRAMES = 400

def build() -> None:
    from src.features.color.core.color_graph_service import ColorGraphData, ColorGraphService
    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef, probe_media_metadata
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence
    from src.shared.color.lut_resource_registry import get_lut_resource_session

    result = get_lut_resource_session().import_files([str(LUT)])[0]
    assert result.entry, result
    service = ColorGraphService()
    insert = service.apply_intent(ColorGraphData.create_serial(), 'insert_corrector', {})
    assert insert.ok, insert
    node = next(n for n in insert.graph.nodes if n.node_type == 'corrector')
    grade = service.apply_intent(insert.graph, 'set_node_lut', {
        'node_id': node.node_id, 'lut_ref': result.entry.uri, 'strength': 1.0})
    assert grade.ok, grade

    project = EditProject.create('Topos vs Resolve UHD25 2026-11-22')
    timelines={}
    for src, filename in FILES.items():
        path=MEDIA/filename
        metadata=probe_media_metadata(str(path))
        assert metadata, path
        asset = MediaAsset(asset_id=src, path=str(path), media_type='video',
                           display_name=filename, metadata=metadata)
        project=project.with_media_asset_added(asset)
        project, stack_id=project.create_color_stack(asset_id=src, name='Shared 33 cube grade')
        project=project.update_color_stack(stack_id, color_graph=grade.graph)
        for graded in (False,True):
            mode='LUT' if graded else 'Original'
            tid=f'{src}_{mode}'
            title=f'{src.upper()} - {mode}'
            clips=[]
            for index, source_start in enumerate((25,275)):
                clip=ClipInstance(clip_id=f'{tid}-{index}', asset_id=src, track_id=f'{tid}-v1',
                                  timeline_start=index*200, timeline_duration=200,
                                  source_start=source_start, source_duration=200)
                clips.append(clip.with_color_stack(stack_id) if graded else clip)
            track=EditTrack(track_id=f'{tid}-v1', name='V1', track_type='video', index=0, clips=tuple(clips))
            timelines[tid]=TimelineSequence(timeline_id=tid,name=title,fps=Rational(25,1),
                                            width=3840,height=2160,video_tracks=(track,))
            project=project.with_timeline_ref_added(
                TimelineSequenceRef(timeline_id=tid,name=title,fps=25,width=3840,height=2160,
                                    created_at=time.time()),set_active=tid=='h264_Original')
    io=EditProjectIO()
    assert io.save_project(str(PROJECT),project,timeline_sequences=timelines,create_backup=False,use_ai_callback=False)
    _, _, loaded=io.load_project(str(PROJECT))
    assert set(loaded)==set(timelines)
    print(PROJECT, list(loaded), flush=True)

def export(source: str, graded: bool, codec: str, repeat: int) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

    fmt=QSurfaceFormat()
    fmt.setVersion(4,1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts,True)
    app=QApplication([])
    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.features.edit.core.timeline_export import ExportCodec, TimelineExportConfig, create_timeline_exporter
    from src.features.deliver.ui.deliver_page import ExportWorker

    project, _, sequences=EditProjectIO().load_project(str(PROJECT))
    mode='LUT' if graded else 'Original'
    seq=sequences[f'{source}_{mode}']
    suffix='.mp4' if codec=='h264' else '.mov'
    name=f'Topos_{source}_{mode}_to_{codec}_{repeat}'
    path=OUT/(name+suffix)
    settings={'codec_profile':'high','quality':50_000_000,'quality_mode':'vbr'} if codec=='h264' else {}
    config=TimelineExportConfig(timeline_id=seq.timeline_id,output_path=str(path),
        codec=ExportCodec.H264 if codec=='h264' else ExportCodec.DNXHR_HQX,include_audio=False,**settings)
    def progress(done: int, total: int, percent: float) -> None:
        if done % 50 == 0 or done == total:
            print('progress',name,done,total,round(time.monotonic()-started,2),flush=True)
    exporter=create_timeline_exporter(project,seq,progress_callback=progress)
    worker=ExportWorker(exporter,config)
    def finished(success: bool, filename: str, error: str) -> None:
        wall=time.monotonic()-started
        outcome=getattr(worker,'outcome',None) or getattr(worker,'_outcome',None)
        data={'app':'Topos','name':name,'file':path.name,'source':source,'codec':codec,
              'graded':graded,'repeat':repeat,'wall_s':wall,'success':success,'error':error,
              'stage_telemetry':exporter.export_stage_telemetry_snapshot,
              'outcome':asdict(outcome) if outcome is not None else {},'settings':settings}
        with (OUT/'topos_exports.jsonl').open('a') as f:
            f.write(json.dumps(data,default=str)+'\n')
        print('END',name,round(wall,3),success,error,flush=True)
        app.exit(0 if success else 1)
    worker.export_complete.connect(lambda success: finished(success, str(path), ""))
    worker.export_error.connect(lambda error: finished(False, str(path), error))
    worker.progress_changed.connect(progress)
    print('START',name,flush=True)
    started=time.monotonic()
    worker.start()
    status=app.exec()
    worker.wait()
    sys.exit(status)

def playback(repeat: int) -> None:
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    from src.app import main_window
    from src.shared.video.playback_frame_trace import get_trace_collector

    class BenchmarkWindow(main_window.MainWindow):
        def show(self) -> None:
            super().showMaximized()
            self._bench_cases=iter((s,g,n+1) for n in range(repeat) for s in FILES for g in (False,True))
            QTimer.singleShot(2200,self._prepare_case)

        def _prepare_case(self) -> None:
            case=next(self._bench_cases,None)
            if case is None:
                QApplication.quit()
                return
            self._bench_case=case
            source, graded, _ = case
            self.switch_to_page('Edit')
            self._on_timeline_switch_requested(f'{source}_{"LUT" if graded else "Original"}')
            self.program_viewer_widget._toolbar._set_preview_quality(1.0)
            self.timeline_panel.set_playhead_frame(0)
            QTimer.singleShot(1700,self._run_case)

        def _run_case(self) -> None:
            panel=self.timeline_panel
            viewer=self.program_viewer_widget.get_viewer()
            trace=get_trace_collector()
            self._bench_frames=[]
            if not hasattr(self,'_bench_connected'):
                viewer.frame_presented.connect(self._record_frame)
                self._bench_connected=True
            trace.clear()
            trace.start_session(page='edit')
            self._bench_started=time.monotonic()
            self._bench_active=True
            panel.toggle_playback()
            self._bench_timer=QTimer(self)
            self._bench_timer.setInterval(100)
            self._bench_timer.timeout.connect(self._check_stop)
            self._bench_timer.start()
            print('PLAY',self._bench_case,panel.get_current_frame(),panel.is_playing(),
                  viewer.isVisible(),panel._total_frames,flush=True)

        def _record_frame(self, frame_index: int) -> None:
            if self._bench_active and (not self._bench_frames or self._bench_frames[-1][1]!=frame_index):
                self._bench_frames.append([time.monotonic()-self._bench_started,frame_index])

        def _check_stop(self) -> None:
            panel=self.timeline_panel
            wall=time.monotonic()-self._bench_started
            if panel.is_playing() and wall < 35:
                return
            if wall < 1:
                return
            if panel.is_playing():
                panel.pause_playback()
            self._bench_active=False
            self._bench_timer.stop()
            source,graded,repeat=self._bench_case
            report={'source':source,'graded':graded,'repeat':repeat,'wall_s':wall,
                    'playhead':panel.get_current_frame(),'frames':self._bench_frames,
                    'trace':get_trace_collector().get_summary()}
            with (OUT/'topos_playback.jsonl').open('a') as f:
                f.write(json.dumps(report,default=str)+'\n')
            print('DONE',self._bench_case,len(self._bench_frames),round(wall,3),
                  panel.get_current_frame(),flush=True)
            QTimer.singleShot(800,self._prepare_case)

    main_window.MainWindow=BenchmarkWindow
    from src import main as app_main
    sys.argv=[sys.argv[0],'--no-startup-dialog','--project',str(PROJECT)]
    app_main.main()

def main() -> None:
    cli=argparse.ArgumentParser()
    cli.add_argument('action',choices=['build','export','playback'])
    cli.add_argument('--source',choices=FILES,default='h264')
    cli.add_argument('--codec',choices=FILES,default='h264')
    cli.add_argument('--graded',action='store_true')
    cli.add_argument('--repeat',type=int,default=1)
    args=cli.parse_args()
    cfg=Path(os.environ['TOPOS_CONFIG_PATH'])
    cfg.parent.mkdir(exist_ok=True)
    if not cfg.exists():
        cfg.write_text(json.dumps({'video_preview_adaptive_quality':False,'render_cache_auto_mode':'off'}))
    if args.action=='build':
        build()
    elif args.action=='playback':
        playback(args.repeat)
    else:
        export(args.source,args.graded,args.codec,args.repeat)

if __name__=='__main__':
    main()
