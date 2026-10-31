result={}
# Re-test former E03/E04/E11 rather than blindly repeating historical defects.
def walk_menu(menu,prefix=''):
 out=[]
 for a in menu.actions():
  if a.isSeparator():continue
  name=prefix+'/'+a.text().replace('&','')
  if a.menu():out.extend(walk_menu(a.menu(),name))
  else:out.append({'path':name,'enabled':a.isEnabled(),'shortcut':a.shortcut().toString()})
 return out
menus=[]
for a in mw.menuBar().actions():
 if a.menu():menus.extend(walk_menu(a.menu(),a.text().replace('&','')))
result['menus']=menus
result['status_receivers']=tv.receivers(tv.status_message_requested)
result['playback_state_receivers']=tp.receivers(tp.playback_state_changed)
result['undo_enabled']=mw.undo_action_menu.isEnabled()
# Marker button and linked-selection toolbar now have real behaviors.
tv.set_playhead(130);tv.add_marker_at_playhead();QTest.qWait(150)
result['marker_count']=len(tv._timeline.markers)
click(tp.btn_marker);result['marker_panel_visible']=tp._marker_list_dock.isVisible()
click(tp.btn_marker)
linked=tp.btn_linked_selection.isChecked();click(tp.btn_linked_selection);result['linked_toggle']={'before':linked,'after':tp.btn_linked_selection.isChecked()};click(tp.btn_linked_selection)
result['stale_tooltips']={'marker':tp.btn_marker.toolTip(),'linked_selection':tp.btn_linked_selection.toolTip()}
# Program transport step/loop label and UI state are captured independently.
bar=mw.program_viewer_widget.get_transport_bar();click(bar.btn_loop);result['loop_checked']=bar.btn_loop.isChecked();click(bar.btn_loop)
capture('036_markers_and_menus')
