proj=mw._current_edit_project
result={'assets':[{'file':Path(a.source_path).name,'fps':a.fps,'frames':a.total_frames,'metadata':dict(a.metadata),'audio':a.audio_channels} for a in proj.assets.values()]}
asset=proj.get_asset_by_path(paths[0]); sv=mw.source_viewer_widget
mw.dock_source_viewer.show(); mw.dock_source_viewer.raise_(); sv.open_asset(asset)
QTest.qWait(700)
result['source']={'asset':sv.current_asset.asset_id,'frame':sv._current_frame,'visible':sv.isVisible(),'toolbar':sv.toolbar.isVisible()}
capture('008_source_loaded')
