proj=mw._current_edit_project
asset=proj.get_asset_by_path(paths[0]); sv=mw.source_viewer_widget
mw.dock_source_viewer.show(); mw.dock_source_viewer.raise_(); sv.open_asset(asset)
QTest.qWait(700)
result={'assets':[{'file':a.display_name,'fps':a.fps,'frames':a.total_frames,'audio':a.audio_channels,'type':a.media_type} for a in proj.assets.values()], 'source':{'asset':sv.current_asset.asset_id,'frame':sv._current_frame,'visible':sv.isVisible(),'toolbar':sv.toolbar.isVisible()}, 'views':[(k,type(v).__name__) for k,v in vars(mp).items() if any(x in k for x in ('tree','grid','view'))]}
capture('009_source_loaded')
