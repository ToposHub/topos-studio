from PyQt6.QtWidgets import QAbstractButton, QAbstractItemView, QLineEdit
mw.switch_to_page('Edit')
QApplication.setActiveWindow(mw)
QTest.qWait(700)
mp=mw.media_pool_panel; tp=mw.timeline_panel; tv=tp.timeline_view; ep=mw.edit_page
result={'window':[mw.width(),mw.height()], 'buttons':[{'class':type(w).__name__,'name':w.objectName(),'text':w.text(),'tip':w.toolTip(),'visible':w.isVisible(),'enabled':w.isEnabled()} for w in mw.findChildren(QAbstractButton)], 'panels':[(k,type(v).__name__) for k,v in vars(mw).items() if any(s in k for s in ('source','program','edit_prop','timeline','audio_mixer','caption'))], 'source':[(k,type(v).__name__) for k,v in vars(ep).items() if 'source' in k]}
capture('001_empty')
