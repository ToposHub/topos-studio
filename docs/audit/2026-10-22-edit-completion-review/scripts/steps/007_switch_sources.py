import logging
mw.switch_to_page('Edit');mw.dock_source_viewer.show();mw.dock_source_viewer.raise_();QTest.qWait(150)
source_warnings=[]
class CaptureWarnings(logging.Handler):
    def emit(self, record):
        if record.levelno>=logging.WARNING:
            source_warnings.append({'current':getattr(sv.current_asset,'display_name',None),'logger':record.name,'message':record.getMessage()})
capture_handler=CaptureWarnings()
loggers=['src.features.edit.core.edit_gpu_preview_renderer','src.shared.media.gpu_frame','src.shared.gpu.present_frame','src.app.controllers.file_controller']
for name in loggers:logging.getLogger(name).addHandler(capture_handler)
for filename in videos*3:
    a=mw._current_edit_project.get_asset_by_path(str(ROOT/'tests/test_videos'/filename))
    sv.open_asset(a);QTest.qWait(250);sv.seek(min(100,a.total_frames-1));QTest.qWait(150)
for name in loggers:logging.getLogger(name).removeHandler(capture_handler)
result={'switches':27,'capture_during_loop':False,'warnings':source_warnings,'final':sv.current_asset.display_name}
capture('007_after_switches')
