from src.features.edit.ui.timeline_items import ClipItem
from src.features.edit.core.selection import TimelineSelection, SelectionItem, SelectionType

def summary():
 t=tv._timeline
 return None if t is None else {'revision':t.revision,'duration':t.duration_frames,'tracks':[{'name':x.name,'type':x.track_type,'locked':x.is_locked,'clips':[{'id':c.clip_id,'start':c.timeline_start,'end':c.timeline_end,'source':c.source_start,'duration':c.source_duration,'linked':c.linked_clip_id} for c in x.clips]} for x in t.all_tracks]}

def click(w):
 QTest.mouseClick(w,Qt.MouseButton.LeftButton)
 QTest.qWait(200)

sv.seek(50); QTest.qWait(300); click(sv.toolbar.btn_mark_in)
sv.seek(149); QTest.qWait(300); click(sv.toolbar.btn_mark_out)
ranges={'in':sv.in_point,'out':sv.out_point,'enabled':[tp.btn_insert.isEnabled(),tp.btn_overwrite.isEnabled(),tp.btn_append.isEnabled()]}
click(tp.btn_append)
QTest.qWait(800)
# Source IN/OUT append is an actual toolbar mouse click.
result={'range':ranges,'timeline':summary()}
capture('004_timeline_append')
