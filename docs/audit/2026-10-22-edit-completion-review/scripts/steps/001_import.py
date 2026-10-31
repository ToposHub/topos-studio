from PyQt6.QtCore import QMimeData, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
app = QApplication.instance()
mw.switch_to_page('Edit')
QApplication.setActiveWindow(mw)
QTest.qWait(300)
mp=mw.media_pool_panel; tp=mw.timeline_panel; tv=tp.timeline_view; ep=mw.edit_page
mw._auto_create_edit_project('Round7Audit20261022')
videos=['2K_h264.mp4','2K_H265.mov','2K_prores422.mov','4K_H264.mov','4K_H265.mov','4K_prores422Proxy.mov','2K_DNxHR HQX 10bit.mov','2K_topos_422_LT.mov','2K_Prores4444_Alpha.mov']
paths=[str(ROOT/'tests/test_videos'/v) for v in videos]
mime=QMimeData(); mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
enter=QDragEnterEvent(QPoint(100,150),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
app.sendEvent(mp,enter)
drop=QDropEvent(QPointF(100,150),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
app.sendEvent(mp,drop)
QTest.qWait(1800)
result={'drag':enter.isAccepted(),'drop':drop.isAccepted(),'assets':[a.display_name for a in mw._current_edit_project.assets.values()]}
capture('001_import')
