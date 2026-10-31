
from PyQt6.QtWidgets import QTabBar, QDockWidget
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.media_pool_panel import MIME_TYPE_MEDIA_POOL_ASSETS
mw.showNormal();mw.resize(1440,1000);mw.move(30,60);wait(350)
record('initial_layout',window=[mw.width(),mw.height()],timeline_dock=mw.dock_edit_timeline.height(),screenshot=shot('01-normal-empty'))
media_paths=[ROOT/'tests/test_videos/2K_h264.mp4',ROOT/'tests/test_videos/2K_prores422Proxy.mov',ROOT/'tests/test_videos/2K_topos_422_Proxy.mov']
mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(p)) for p in media_paths])
mp=mw.media_pool_panel
QApplication.sendEvent(mp,QDragEnterEvent(QPoint(180,160),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier))
drop=QDropEvent(QPointF(180,160),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);QApplication.sendEvent(mp,drop);wait(2000)
asset=mw._current_edit_project.get_asset_by_path(str(media_paths[0]))
record('real_media_import',drop=drop.isAccepted(),asset_count=len(mw._current_edit_project.assets),screenshot=shot('02-media'))
# Click the actual H264 tile and test Source without suppressing its paint errors.
row=mp.asset_model.row_for_asset(asset.asset_id);idx=mp.asset_model.index(row,0)
p=mp.grid_view.visualRect(idx).center();QTest.mouseDClick(mp.grid_view.viewport(),Qt.MouseButton.LeftButton,pos=p);wait(1400)
source_renderer=sv.viewer if hasattr(sv,'viewer') else getattr(sv,'_viewer',None)
record('source_first_frame',loaded=sv.current_asset.asset_id if sv.current_asset else None,viewer_type=type(source_renderer).__name__,paint_failed=getattr(source_renderer,'_gl_paint_failed',None),screenshot=shot('03-source-first-frame'))
click(sv.toolbar.btn_mark_in);sv.slider.setValue(100);wait(150);click(sv.toolbar.btn_mark_out)
record('source_valid_range',range=[sv.in_point,sv.out_point],buttons=[panel.btn_insert.isEnabled(),panel.btn_overwrite.isEnabled(),panel.btn_append.isEnabled()])
click(sv.toolbar.btn_clear)
record('source_clear_range',range=[sv.in_point,sv.out_point],buttons=[panel.btn_insert.isEnabled(),panel.btn_overwrite.isEnabled(),panel.btn_append.isEnabled()])
click(sv.toolbar.btn_mark_in);click(sv.toolbar.btn_mark_out)
record('source_single_frame_range',range=[sv.in_point,sv.out_point],buttons=[panel.btn_insert.isEnabled(),panel.btn_overwrite.isEnabled(),panel.btn_append.isEnabled()])
# Drop uses the production linked-A/V command; retain default layout until geometry needs enlargement.
mw.resizeDocks([mw.dock_program_viewer,mw.dock_edit_timeline],[600,350],Qt.Orientation.Vertical);wait(300)
tv._configure_layout_engine();rect=tv._layout_engine.track_rect(tv._timeline.video_tracks[0].track_id)
p=tv.mapFromScene(QPointF(tv._layout_engine.frame_to_x(0)+1,rect.center().y()))
mime=QMimeData();mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS,json.dumps([asset.asset_id]).encode())
QApplication.sendEvent(tv.viewport(),QDragEnterEvent(p,Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier))
QApplication.sendEvent(tv.viewport(),QDragMoveEvent(p,Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier))
drop=QDropEvent(QPointF(p),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);QApplication.sendEvent(tv.viewport(),drop);wait(2000)
click(panel.btn_zoom_fit)
record('linked_drop',accepted=drop.isAccepted(),state=timeline_state(),project_revision=mw._current_edit_project.revision,screenshot=shot('04-linked'))

def select_tab(text):
    for b in mw.findChildren(QTabBar):
        for i in range(b.count()):
            if b.tabText(i).lower()==text.lower():QTest.mouseClick(b,Qt.MouseButton.LeftButton,pos=b.tabRect(i).center());wait(100);return

def clip_point(cid,local_x=None,local_y=None):
    item=tv._items_map[cid];r=item.boundingRect()
    return tv.mapFromScene(item.mapToScene(QPointF(r.center().x() if local_x is None else local_x,r.center().y() if local_y is None else local_y)))

def frame_point(frame,tid=None):
    tv._configure_layout_engine();r=tv._layout_engine.track_rect(tid or tv._timeline.video_tracks[0].track_id)
    return tv.mapFromScene(QPointF(tv._layout_engine.frame_to_x(frame),r.center().y()))

def key(k,mod=Qt.KeyboardModifier.NoModifier):
    tv.setFocus();QTest.keyClick(tv,k,mod);wait(150)

select_tab('Source');sv.seek(100);wait(250)
record('source_visible',visible=sv.isVisible(),paint_failed=sv.viewer._gl_paint_failed,texture=str(sv.viewer._current_tex_id),screenshot=shot('05-source-visible'))
a=sv.current_frame;click(sv.toolbar.btn_play);wait(800);click(sv.toolbar.btn_play)
record('source_playback',before=a,after=sv.current_frame,paint_failed=sv.viewer._gl_paint_failed)
from PyQt6.QtCore import qVersion,QT_VERSION_STR
record('runtime',runtime=qVersion(),compiled=QT_VERSION_STR)



select_tab('Source')
sv.seek(0);click(sv.toolbar.btn_mark_in);sv.seek(49);click(sv.toolbar.btn_mark_out)
click(panel.btn_append);record('source_append_existing',state=timeline_state())
sv.seek(50);click(sv.toolbar.btn_mark_in);sv.seek(99);click(sv.toolbar.btn_mark_out);click(panel.btn_append)
record('source_append_next',state=timeline_state(),screenshot=shot('17-source-appended-av'))
# Source overwrite onto occupied A/V content uses actual toolbar click.
select_tab('Program');click(panel.btn_zoom_fit);click(panel.btn_select)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(20).x(),14));wait(150)
select_tab('Source');sv.seek(100);click(sv.toolbar.btn_mark_in);sv.seek(109);click(sv.toolbar.btn_mark_out)
before=timeline_state();click(panel.btn_overwrite);record('source_overwrite_occupied',before=before,after=timeline_state(),screenshot=shot('18-overwrite-success'))
# Insert into occupied linked clips shows whether new A/V is atomic.
before=timeline_state();click(panel.btn_insert);record('source_insert_internal',before=before,after=timeline_state(),screenshot=shot('19-insert-av'))

select_tab('Program');click(panel.btn_select);click(panel.btn_zoom_fit)
record('source_final',state=timeline_state(),project_revision=mw._current_edit_project.revision)


from src.features.edit.core.timeline_ops import validate_timeline
issues=validate_timeline(tv._timeline)
assert not issues,issues
allclips=[c for t in tv._timeline.all_tracks for c in t.clips]
assert all(c.linked_clip_id for c in allclips)
assert all(tv._timeline.get_clip_by_id(c.linked_clip_id).linked_clip_id==c.clip_id for c in allclips)
record('source_av_relations_validated',clip_count=len(allclips),issues=issues,all_reciprocal=True)
