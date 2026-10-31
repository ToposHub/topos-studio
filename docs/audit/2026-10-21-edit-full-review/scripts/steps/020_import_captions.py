# Supplemental subtitle fixtures, separate from user media.
srt=SESSION/'audit.srt';srt.write_text('1\n00:00:00,400 --> 00:00:02,000\nHello, world\n\n2\n00:00:02,100 --> 00:00:03,500\nSecond caption\n',encoding='utf-8')
ass=SESSION/'audit.ass';ass.write_text('[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.40,0:00:02.00,Default,,0,0,0,,Hello, world\n',encoding='utf-8')
mp._import_files([str(srt),str(ass)]);QTest.qWait(1200)
result={'assets':[(a.display_name,a.media_type) for a in mw._current_edit_project.assets.values() if a.media_type=='subtitle']}
from src.features.edit.ui.timeline_view import MIME_TYPE_MEDIA_POOL_ASSETS
for p in (srt,ass):
 a=mw._current_edit_project.get_asset_by_path(str(p))
 if a is None:continue
 m=QMimeData();m.setData(MIME_TYPE_MEDIA_POOL_ASSETS,json.dumps([a.asset_id]).encode())
 ev=QDragEnterEvent(QPoint(300,100),Qt.DropAction.CopyAction,m,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);app.sendEvent(tv.viewport(),ev)
 dv=QDropEvent(QPointF(300,100),Qt.DropAction.CopyAction,m,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);app.sendEvent(tv.viewport(),dv);QTest.qWait(400)
 result[p.suffix]={'accepted':dv.isAccepted(),'tracks':[{'name':t.name,'clips':[{'id':c.clip_id,'text':c.text,'start':c.timeline_start,'duration':c.timeline_duration} for c in t.subtitle_clips]} for t in tv._timeline.subtitle_tracks]}
result['caption_rows']=mw.caption_workspace_panel._model.rowCount()
mw.dock_caption_workspace.show();mw.dock_caption_workspace.raise_();QTest.qWait(200)
capture('020_caption_import')
