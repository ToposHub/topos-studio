import json, os, shutil, sys
from pathlib import Path
out = Path(__file__).resolve().parent
root = Path(os.environ.get('PROBE_ROOT',out.parents[1]))
sys.path.insert(0,str(root))
label = sys.argv[1]
base = out / label
base.mkdir(exist_ok=True)
origin = Path.home()/'Documents/ToposProjects'/(os.environ.get('PROBE_PROJECT','测试2')+'.topos')
target = base / origin.name
shutil.copy(origin,target)
cfg = json.loads(Path('/Volumes/heng/Documents/vscode/topos-studio/.topos/config.json').read_text())
cfg.update(project_library_directory=str(base), project_library_directories=[str(base)], recent_projects=[],show_welcome_on_startup=False)
if os.environ.get('PROBE_PAGE'):
 cfg['current_page']=cfg['current_page_studio_video']=os.environ['PROBE_PAGE']
os.environ['TOPOS_CONFIG_PATH']=str(base / 'config.json')
(base/'config.json').write_text(json.dumps(cfg))
os.environ.setdefault('TOPOS_PRODUCT_MODE','studio')
from PyQt6.QtCore import QPoint, QTimer
from src.app import main_window
from src import main as app_main
from time import monotonic
import numpy as np
import Quartz as q
sys.path.insert(1,str(out.parent/'scrub-resolve-2026-11-25'))
from screen import capture
class Window(main_window.MainWindow):
 def show(self):
  super().show()
  if hasattr(self,'paintprobe'):return
  self.paintprobe=[]
  if os.environ.get('PROBE_MANAGER'):
   from PyQt6.QtWidgets import QApplication
   def select():
    dlg=QApplication.activeModalWidget()
    if not hasattr(dlg,'project_selected'):
     QTimer.singleShot(100,select);return
    dlg.project_selected.emit(str(target))
   QTimer.singleShot(1050,select)
  self.program_viewer_widget.get_viewer().frame_presented.connect(lambda f:self.paintprobe.append(f))
  QTimer.singleShot(4100,lambda: self.snap('open'))
 def snap(self,stage):
  import traceback
  try:
   canvas=self.canvas.viewport()
   program=self.program_viewer_widget.get_viewer()
   panel=self.timeline_panel
   ctx=self._page_context_manager.get_current_context()
   r=self.timeline_preview_renderer
   print('OBS',json.dumps(dict(stage=stage,page=self.current_page,context=str(ctx)[:300],head=panel.get_playhead_frame(),edit_current=self.edit_page._current_frame,renderer=type(r).__name__,last_emitted=getattr(r,'_last_emitted_frame',None),gpu=r and r._has_gpu_preview_pipeline(),video_loaded=self.video_controller and self.video_controller.is_video_loaded(),color_frame=getattr(self.video_controller,"_last_submitted_video_frame_index",None),painted=self.paintprobe[-8:],color_visible=canvas.isVisible(),program_visible=program.isVisible(),program_frame=program._current_frame_index,program_gl=program._gl_initialized,central=self.centralWidget().objectName())), flush=True)
   wins=[w for w in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll,0) if w.get('kCGWindowName')==self.windowTitle() and w.get('kCGWindowOwnerPID')==os.getpid()]
   if wins:
    win=wins[0]; wid=win['kCGWindowNumber']; im=capture(q.CGRectNull,wid);im.thumbnail((1200,820));im.save(base/(stage+'.jpg'))
    v=canvas if self.current_page=='Color' else program
    pt=v.mapToGlobal(QPoint(0,0))
    pix=capture(q.CGRectMake(pt.x()+v.width()*.25,pt.y()+v.height()*.25,v.width()*.5,v.height()*.5),wid)
    print('ROI',stage,np.mean(np.asarray(pix),axis=(0,1)),np.std(np.asarray(pix),axis=(0,1)),flush=True)
  except Exception:
   traceback.print_exc()
  if stage=='open' and os.environ.get('PROBE_REOPEN'):
   self._open_project_from_manager(str(target)); QTimer.singleShot(2400,lambda:self.snap('reopen'));return
  if stage in ('open','reopen'): self.page_buttons['Color'].click(); QTimer.singleShot(3200,lambda:self.snap('color'))
  elif stage=='color': self.page_buttons['Edit'].click(); QTimer.singleShot(2200,lambda:self.snap('back'))
  else: os._exit(0)
main_window.MainWindow=Window
sys.argv=[sys.argv[0]] if os.environ.get('PROBE_MANAGER') else [sys.argv[0],'--no-startup-dialog','--project',str(target)]
app_main.main()
