from PyQt6.QtCore import QMimeData, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
mw._auto_create_edit_project('EditAudit20261021')
videos=['2K_h264.mp4','2K_H265.mov','2K_prores422.mov','4K_H264.mov','4K_H265.mov','4K_prores422Proxy.mov','2K_DNxHR HQX 10bit.mov','2K_topos_422_LT.mov','2K_Prores4444_Alpha.mov']
paths=[str(ROOT/'tests/test_videos'/v) for v in videos]
mime=QMimeData(); mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
enter=QDragEnterEvent(QPoint(100,150),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
app.sendEvent(mp,enter)
drop=QDropEvent(QPointF(100,150),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
app.sendEvent(mp,drop)
QTest.qWait(300)
result={'drag_accepted':enter.isAccepted(),'drop_accepted':drop.isAccepted(),'assets_so_far':len(mw._current_edit_project.assets),'children':[(type(x).__name__,x.objectName()) for x in mp.findChildren(QAbstractItemView)]}
