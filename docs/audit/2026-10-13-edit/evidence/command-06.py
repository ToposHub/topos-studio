timer.stop()
# Create a clean scratch timeline as a fixture, through the existing project service.
from src.features.edit.ai_editing.edit_project_service import EditProjectService
import inspect
record('service_methods',methods=[(n,str(inspect.signature(getattr(EditProjectService,n)))) for n in dir(EditProjectService) if 'timeline' in n and callable(getattr(EditProjectService,n))],window_tabs=[[(b.tabText(i),i) for i in range(b.count())] for b in mw.findChildren(QTabBar)],undo_methods=[n for n in dir(mw) if 'undo' in n.lower() or 'redo' in n.lower()])
# Observe the normal global keyboard and direct-stack undo separately.
tv._on_track_lock_changed(tv._timeline.video_tracks[0].track_id,False);wait(100)
record('undo_before',state=timeline_state(),stack=tv._command_stack.can_undo())
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
record('undo_ctrl',state=timeline_state())
key(Qt.Key.Key_Z,Qt.KeyboardModifier.MetaModifier)
record('undo_meta',state=timeline_state())
# Short ProRes and TOPOS clips are dropped into the real timeline at the tail.
from src.features.edit.ui.media_pool_panel import MIME_TYPE_MEDIA_POOL_ASSETS
for filename in ['2K_prores422Proxy.mov','2K_topos_422_Proxy.mov']:
    media=mw._current_edit_project.get_asset_by_path(str(ROOT/'tests/test_videos'/filename))
    tail=max(c.timeline_end for t in tv._timeline.video_tracks for c in t.clips)
    click(panel.btn_zoom_fit)
    p=frame_point(tail+5)
    mime=QMimeData();mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS,json.dumps([media.asset_id]).encode())
    enter=QDragEnterEvent(p,Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(tv.viewport(),enter)
    drop=QDropEvent(QPointF(p),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(tv.viewport(),drop);wait(1800)
    click(panel.btn_zoom_fit)
    QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(tail+25).x(),14));wait(800)
    key(Qt.Key.Key_Space);wait(1500);key(Qt.Key.Key_Space)
    record('codec_playback_'+filename,drop_accepted=drop.isAccepted(),playhead=tv.get_playhead_frame(),state=timeline_state(),screenshot=shot('10-'+filename.replace('.','-')))
timer.start(500)
