from pathlib import Path
import os, sys, json, threading, time
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
os.environ['QT_QPA_PLATFORM']='offscreen'
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QSettings,QBuffer,QIODevice,QItemSelectionModel
from PyQt6.QtGui import QImage,QColor
from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.native_files_batch_rename import BatchRenameDialog
import src.app.ui.native_files_preview as preview_module
app=QApplication([])
settings=QSettings('/tmp/native-files-review-empty.ini',QSettings.Format.IniFormat); settings.clear()
p=NativeFilesPanel(home_path='/Users/review',preferences=NativeFilesPreferences(settings))
def ent(path,tags=''):
    return {'name':path.rsplit('/',1)[-1], 'path':path,'type':'image','tags':tags}
def png(color):
    im=QImage(8,8,QImage.Format.Format_RGB32);im.fill(QColor(color));b=QBuffer();b.open(QIODevice.OpenModeFlag.WriteOnly);im.save(b,'PNG');return bytes(b.data())
a='/Users/review/A/old.png'; b='/Users/review/B/new.png'
p.set_entries('/Users/review/A',[ent(a)])
p.show_library_results({'files':[ent(b)],'total':1})
p.set_thumbnail(b,png('red'))
print('VIRTUAL',json.dumps({'model_paths':[e.path for e in p.model.entries],'thumbnail_accepted':b in p._images,'entry_row_for_new':p._entry_rows.get(b),'lookup_old_returns':p._entry_for_path(a).path,'operation_directory':p._current_directory()}))
p.search_edit.setText('sunset aerial')
p.show_library_results({'files':[ent('/Users/review/B/sunset.png','aerial')],'total':1})
print('SEARCH_AND',len(p.model.entries),p.proxy.rowCount())
p.set_entries('/Users/review/A',[ent(a)])
p.selection.select(p.proxy.index(0,0),QItemSelectionModel.SelectionFlag.ClearAndSelect|QItemSelectionModel.SelectionFlag.Rows)
p.apply_capabilities([{'key':'metadata.patch','available':False,'reason':'disabled'}])
print('CAP_BEFORE',p.rating_combo.isEnabled())
p._update_info();print('CAP_AFTER_INFO',p.rating_combo.isEnabled())
d=BatchRenameDialog([a,b]); d.show_preview({'plan':[{'src_name':'old.png','dst_name':'old_001.png'}],'errors':[]})
d.template_edit.setText('completely_different_{seq:3}')
out=[];d.execute_requested.connect(out.append);d.execute_button.click();print('UNPREVIEWED_RENAME',out)
# Hold both decoder jobs to reproduce the same-path old completion arriving first.
old_started=threading.Event(); old_release=threading.Event(); new_started=threading.Event();new_release=threading.Event()
original=preview_module.decode_preview
calls=[0]
def controlled(data):
    calls[0]+=1
    if data==b'old': old_started.set();old_release.wait(2);return QImage(1,1,QImage.Format.Format_RGB32),'old'
    new_started.set();new_release.wait(2);return QImage(2,2,QImage.Format.Format_RGB32),'new'
preview_module.decode_preview=controlled
v=preview_module.PreviewDecoder();images=[];v.image_ready.connect(lambda path,image:images.append(image.width()))
v.start(a,b'old');old_started.wait(1);v.start(a,b'new');new_started.wait(1)
old_release.set()
for _ in range(30):app.processEvents();time.sleep(.005)
new_release.set()
for _ in range(30):app.processEvents();time.sleep(.005)
print('SAME_PATH_DECODER_EMITTED_WIDTHS',images)
v.shutdown();p.shutdown();d.close()
