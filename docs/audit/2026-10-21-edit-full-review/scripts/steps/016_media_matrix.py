result=[]
for row in range(mp.asset_model.rowCount()):
 idx=mp.asset_model.index(row,0); a=mp.asset_model.data(idx,Qt.ItemDataRole.UserRole)
 if not hasattr(a,'asset_id'): continue
 mp.grid_view.scrollTo(idx);QTest.qWait(60)
 p=mp.grid_view.visualRect(idx).center()
 QTest.mouseDClick(mp.grid_view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p);QTest.qWait(450)
 current=sv.current_asset
 entry={'file':a.display_name,'opened':current is not None and current.asset_id==a.asset_id,'source_frame':sv._current_frame}
 if entry['opened']:
  click(sv.toolbar.btn_next); entry['next']=sv._current_frame
  click(sv.toolbar.btn_prev); entry['prev']=sv._current_frame
  sv.seek(min(100,a.total_frames-1));QTest.qWait(250);entry['seek']=sv._current_frame
  capture('016_source_'+str(row),sv)
 result.append(entry)
asset=mw._current_edit_project.get_asset_by_path(paths[0]);sv.open_asset(asset);QTest.qWait(300)
save_record('016_media_matrix_observations',result)
