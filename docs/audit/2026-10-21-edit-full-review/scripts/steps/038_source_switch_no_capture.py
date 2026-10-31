import logging
mw.switch_to_page('Edit');mw.dock_source_viewer.show();mw.dock_source_viewer.raise_();QTest.qWait(200)
source_errors=[]
class AuditLogHandler(logging.Handler):
 def emit(self,record):
  if record.levelno>=logging.WARNING:source_errors.append({'file':getattr(sv.current_asset,'display_name',None),'logger':record.name,'message':record.getMessage()})
log_capture=AuditLogHandler()
for lname in ('src.features.edit.core.edit_gpu_preview_renderer','src.shared.media.gpu_frame','src.shared.gpu.present_frame','src.app.controllers.file_controller'):logging.getLogger(lname).addHandler(log_capture)
for filename in videos*2:
 a=mw._current_edit_project.get_asset_by_path(str(ROOT/'tests/test_videos'/filename))
 if a:sv.open_asset(a);QTest.qWait(300);sv.seek(min(100,a.total_frames-1));QTest.qWait(180)
for lname in ('src.features.edit.core.edit_gpu_preview_renderer','src.shared.media.gpu_frame','src.shared.gpu.present_frame','src.app.controllers.file_controller'):logging.getLogger(lname).removeHandler(log_capture)
result={'switches':18,'screenshot_during_switches':False,'warnings':source_errors}
