result={}
pp=mw.edit_properties_panel
cid=clipitem.clip_id
sec=next(v for k,v in pp._sections.items() if k.endswith(':transform'))
drag=sec._param_widgets['translate_x']['widget']
QTest.mouseDClick(drag,Qt.MouseButton.LeftButton); QTest.qWait(100)
QTest.keyClick(drag._edit,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier);QTest.keyClicks(drag._edit,'-250');QTest.keyClick(drag._edit,Qt.Key.Key_Return);QTest.qWait(300)
result['transform_inline']={'x':tv._timeline.get_clip_by_id(cid).transform.translate_x,'field':drag.value()}
tv.undo();QTest.qWait(200);result['transform_undo']=tv._timeline.get_clip_by_id(cid).transform.translate_x
# Effect card double-click -> production effect catalog and command.
fx=mw.edit_effects_panel
for dock in mw.findChildren(__import__('PyQt6.QtWidgets',fromlist=['QDockWidget']).QDockWidget):
 if dock.widget() is fx:dock.show();dock.raise_()
QTest.qWait(250)
card=next(x for x in fx.findChildren(QAbstractButton) if getattr(x,'effect_type','')=='blur')
QTest.mouseDClick(card,Qt.MouseButton.LeftButton);QTest.qWait(400)
result['effect_added']=[{'id':e.effect_id,'type':e.effect_type,'params':dict(e.params)} for e in tv._timeline.get_clip_by_id(cid).effects]
for dock in mw.findChildren(__import__('PyQt6.QtWidgets',fromlist=['QDockWidget']).QDockWidget):
 if dock.widget() is pp:dock.raise_()
QTest.qWait(200)
result['effect_keys']=list(pp._keyframe_buttons)
capture('014_effect_properties')
