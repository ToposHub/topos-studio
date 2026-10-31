mw.showNormal()
mw.resize(1440, 1000)
mw.move(30, 60)
wait(300)
record('ui_inventory', window=[mw.width(), mw.height()], source=type(sv).__name__, media=[(w.objectName(),w.text(),w.toolTip(),w.isVisible()) for w in mw.media_pool_panel.findChildren(QToolButton)], source_buttons=[(w.objectName(),w.text(),w.toolTip(),w.isVisible()) for w in sv.findChildren(QToolButton)])
# Import through the real external file-drop event path on the media pool.
media_paths = [ROOT/'tests/test_videos/2K_h264.mp4', ROOT/'tests/test_videos/2K_prores422Proxy.mov', ROOT/'tests/test_videos/2K_topos_422_Proxy.mov']
mime=QMimeData()
mime.setUrls([QUrl.fromLocalFile(str(p)) for p in media_paths])
mp=mw.media_pool_panel
enter=QDragEnterEvent(QPoint(180,160),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
QApplication.sendEvent(mp,enter)
drop=QDropEvent(QPointF(180,160),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
QApplication.sendEvent(mp,drop)
wait(2000)
record('media_drop', enter_accepted=enter.isAccepted(),drop_accepted=drop.isAccepted(), project_exists=mw._current_edit_project is not None, screenshot=shot('02-imported'))
