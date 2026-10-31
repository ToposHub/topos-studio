mw.switch_to_page('Deliver');QTest.qWait(700)
ws=mw._deliver_workspace();dp=ws.settings_panel
result={'workspace':type(ws).__name__,'settings_attrs':[(k,type(v).__name__) for k,v in vars(dp).items() if any(x in k for x in ('codec','path','range','start','end','resolution','width','height','export','queue','audio','subtitle','preset','name'))],'buttons':[{'text':b.text(),'tip':b.toolTip(),'name':b.objectName(),'enabled':b.isEnabled(),'visible':b.isVisible()} for b in dp.findChildren(QAbstractButton)],'workspace_attrs':[(k,type(v).__name__) for k,v in vars(ws).items() if any(x in k for x in ('queue','controller','panel'))]}
capture('026_delivery')
