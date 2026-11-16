from pathlib import Path
import os,sys,json,time,threading
sys.path.insert(0,str(Path(__file__).resolve().parents[4]));os.environ['QT_QPA_PLATFORM']='offscreen'
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QSettings,QItemSelectionModel
from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.native_files_transport import NativeFilesTransport
app=QApplication([]); state={'starts':0,'polls':[]}
def entry(p):return {'name':p.rsplit('/',1)[-1],'path':p,'type':'image'}
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def respond(self,obj):
  raw=json.dumps(obj).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length',0)));state['starts']+=1;j='j'+str(state['starts']);self.respond({'success':True,'job':{'job_id':j}})
 def do_GET(self):
  u=urlsplit(self.path);q=parse_qs(u.query)
  if u.path=='/api/search':time.sleep(.25);self.respond({'files':[entry('/Users/review/B/old-search.png')],'total':1})
  elif u.path=='/api/list':self.respond({'files':[entry('/Users/review/C/new-directory.png')]})
  elif u.path=='/api/tasks/status':
   j=q['job_id'][0];state['polls'].append(j)
   if j=='j1':time.sleep(.2)
   self.respond({'job':{'job_id':j,'status':'completed','operation':'copy','successes':[j]}})
  else:self.respond({})
server=ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
base=f'http://127.0.0.1:{server.server_port}'
settings=QSettings('/tmp/native-files-review-transport.ini',QSettings.Format.IniFormat);settings.clear()
p=NativeFilesPanel(home_path='/Users/review',preferences=NativeFilesPreferences(settings));p.set_entries('/Users/review/A',[entry('/Users/review/A/a.png')])
t=NativeFilesTransport(lambda:base,'test');t.search_loaded.connect(p.show_library_results);t.loading.connect(p.set_loading);t.loaded.connect(p.set_entries)
t.search_library('old');t.load_directory('/Users/review/C')
def spin(sec):
 deadline=time.monotonic()+sec
 while time.monotonic()<deadline:app.processEvents();time.sleep(.003)
spin(.5)
print('LATE_SEARCH',p.current_path,[e.path for e in p.model.entries])
started=[];finished=[];t.task_started.connect(lambda j:started.append(j.get('job_id')));t.task_finished.connect(lambda j:finished.append(j.get('job_id')))
t.run_task('copy',['a']);spin(.04);t.run_task('copy',['b']);spin(1)
print('TASKS',json.dumps({'started':started,'finished':finished,'polls':state['polls']}))
p.search_edit.clear();p.set_entries('/Users/review/C',[entry('/Users/review/C/a.png')]);p.selection.select(p.proxy.index(0,0),QItemSelectionModel.SelectionFlag.ClearAndSelect|QItemSelectionModel.SelectionFlag.Rows)
p.apply_capabilities([{'key':'metadata.patch','available':False,'reason':'disabled'}]);print('CAP_DISABLED',p.rating_combo.isEnabled());p._update_info();print('CAP_AFTER_INFO',p.rating_combo.isEnabled())
t.shutdown();p.shutdown();server.shutdown()
