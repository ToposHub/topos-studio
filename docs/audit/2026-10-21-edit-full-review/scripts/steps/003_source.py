QTest.qWait(1500)
proj=mw._current_edit_project
result={'imported':[{'name':a.name,'type':str(a.media_type),'fps':a.fps,'frames':a.total_frames,'size':[a.width,a.height],'audio':a.audio_channels,'offline':getattr(a,'is_offline',None)} for a in proj.assets.values()], 'mp_views':[(k,type(v).__name__) for k,v in vars(mp).items() if any(x in k for x in ('tree','grid','view'))]}
asset=proj.get_asset_by_path(paths[0]); sv=mw.source_viewer_widget
# Open the real source dock and use its normal asset entry (media-pool double-click follows next).
mw.dock_source_viewer.show(); mw.dock_source_viewer.raise_(); sv.open_asset(asset)
QTest.qWait(800)
capture('003_source_loaded')
