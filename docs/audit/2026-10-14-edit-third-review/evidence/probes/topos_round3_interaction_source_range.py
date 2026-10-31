from pathlib import Path
import os,sys,json,ast
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from types import SimpleNamespace
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from src.features.edit.ui.source_viewer import SourceViewerWidget,_GLWrapper,GL_FACADE_CONTRACT_ERRORS
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.features.edit.ui.edit_page import EditPage
app=QApplication([]);out={};sv=SourceViewerWidget();sv.viewer.hide();sv.resize(650,80);tp=TimelinePanel();page=SimpleNamespace(_source_viewer=sv,_timeline_panel=tp);ranges=[]
sv.range_changed.connect(lambda *a:ranges.append(a));sv.range_changed.connect(lambda *_:EditPage._sync_source_edit_buttons(page));sv._current_asset=SimpleNamespace(asset_id='synthetic_range');sv._current_frame=15;sv._total_frames=100;sv.toolbar.set_enabled(True);sv.toolbar.update_in_out_state(None,None);sv.show();app.processEvents()
def snap():return dict(in_point=sv.in_point,out_point=sv.out_point,can_edit=sv.toolbar._can_edit,buttons=[getattr(tp,'btn_'+x).isEnabled() for x in ['insert','overwrite','append']],get_source_edit_params=EditPage._get_source_edit_params(page))
out['initial']=snap();QTest.mouseClick(sv.toolbar.btn_mark_in,Qt.MouseButton.LeftButton);QTest.mouseClick(sv.toolbar.btn_mark_out,Qt.MouseButton.LeftButton);app.processEvents();out['single_frame']=snap();QTest.mouseClick(sv.toolbar.btn_clear,Qt.MouseButton.LeftButton);app.processEvents();out['clear']=snap();QTest.mouseClick(sv.toolbar.btn_mark_in,Qt.MouseButton.LeftButton);QTest.mouseClick(sv.toolbar.btn_mark_out,Qt.MouseButton.LeftButton);sv.close_source();out['close']=snap();out['range_changed']=ranges
p=Path('/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/source_viewer.py');tree=ast.parse(p.read_text());names=sorted({n.attr for n in ast.walk(tree) if isinstance(n,ast.Attribute) and isinstance(n.value,ast.Name) and n.value.id=='_gl'});out['gl_facade']=dict(used_apis=len(names),missing=[n for n in names if not hasattr(_GLWrapper,n)],contract_errors=GL_FACADE_CONTRACT_ERRORS,uniform1f_callable=callable(getattr(_GLWrapper,'glUniform1f',None)),uniform4f_callable=callable(getattr(_GLWrapper,'glUniform4f',None)),actual_gpu_pixels_tested=False)
sv.close();tp.close();Path('/tmp/topos_round3_interaction_source_range.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
