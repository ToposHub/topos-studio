sc.start_detection_for_asset(sa.asset_id);QTest.qWait(300);sd=sc._active_dialog
b=next(b for b in sd.findChildren(QAbstractButton) if '自动' in b.text() and '检测' in b.text())
QTest.mouseClick(b,Qt.MouseButton.LeftButton);QTest.qWait(500)
result={'button':b.text(),'dialog_visible':sd.isVisible(),'errors':scene_errors}
