import os,sys,copy
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['TOPOS_TEST_MODE']='1';sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import QApplication
from unittest.mock import MagicMock
from dataclasses import replace
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.blur_node import BlurNode
from src.shared.project.save_router import SaveRouter
from src.features.edit.core.project import EditProject
app=QApplication.instance() or QApplication([])
page=CompositePage();page._properties_panel=MagicMock();page._evaluate_graph=MagicMock();page._evaluate_graph_now=MagicMock();page._refresh_asset_dependency_panel=MagicMock();page._update_minimap=MagicMock();page.update_viewer_handles=MagicMock()
t=TransformNode(node_id='t');o=OutputNode(node_id='o')
graph={'composite_id':'A','nodes':[t.to_dict(),o.to_dict()],'connections':[], 'fps':24.0,'duration_frames':48,'backdrops':{'bd':{'id':'bd','title':'My backdrop','position':[0,0],'size':[300,200]}},'sticky_notes':{'sn':{'id':'sn','text':'My note','position':[0,0],'size':[200,100]}}}
page.load_composite_data(graph)
print('U-06 decorations models/visuals=',len(page.node_graph.backdrops),len(page.node_editor._backdrop_items),len(page.node_graph._sticky_notes),len(page.node_editor._sticky_note_items))
page._undo_stack.clear();page.node_editor._actions.toggle_bypass('t');page.undo()
print('U-07 bypass after undo=',page.node_graph.get_node('t').disabled,'has_undo=',page._undo_stack.can_undo())
page._undo_stack.clear();page._apply_project_settings({'width':640,'height':360,'fps':30.0,'duration_seconds':5.0});page.undo()
print('U-08 settings after undo=',page.node_graph.get_node('o').parameters['width'],page._composite_fps,page._composite_duration_frames,'has_undo=',page._undo_stack.can_undo())
# Decorator loader retains old canvas lock state if next graph has no viewport_state
page.node_editor.set_node_locked('t',True)
graph2=copy.deepcopy(graph);graph2['composite_id']='B';graph2.pop('viewport_state',None)
page.load_composite_data(graph2)
print('U-09 stale canvas lock=',page.node_editor.is_node_locked('t'))
# Upstream/downstream chain traversal
page.node_graph.clear();a=ConstantNode(node_id='a');b=GradeNode(node_id='b');c=BlurNode(node_id='c');d=OutputNode(node_id='d')
for n in(a,b,c,d):page.node_graph.add_node(n)
for x,y in((a,b),(b,c),(c,d)):assert page.node_graph.connect(x.id,'result',y.id,'image')
page.node_editor.build_from_graph(page.node_graph)
print('U-10 upstream from d=',sorted(page.node_editor._get_upstream_nodes('d')),'downstream from a=',sorted(page.node_editor._get_downstream_nodes('a')))
# Ctrl+S uses graph-only payload and independent viewport state; composition source identity erased.
p=EditProject.create('audit');p,_,_,gid=p.create_empty_comp_clip();old=copy.deepcopy(p.compositions[gid]);old.update({'timeline_id':'timeline-1','clip_id':'clip-1','source_clip_id':'clip-1','custom_extension':{'preserve':True}});p=replace(p,compositions={gid:old})
page.node_graph._viewer_state={'selected_node_id':'b','asset_search_roots':['/tmp/assets'],'output_target_name':'matte','output_target_node_id':'d'}
payload=page.node_graph.to_dict();captured=[];router=SaveRouter();router.set_callback('on_edit_project_updated',captured.append);router._save_edit_project_to_topos=MagicMock(return_value=True)
assert router._save_composite_to_edit_project('/tmp/not-written.topos',p,payload,{'zoom_level':1.5},gid)
new=captured[-1].compositions[gid]
print('U-11 removed composition fields=',sorted(set(old)-set(new)))
print('U-12 saved viewer_state=',new.get('viewer_state'))
# A group view remains attached after opening another composition and filters away new items.
page.node_graph.create_group('g',['b','c'],{'title':'G'})
assert page.node_editor.enter_group_view('g')
newnode=TransformNode(node_id='new-only')
page.load_composite_data({'composite_id':'C','nodes':[newnode.to_dict()],'connections':[]})
print('U-13 group view after load=',page.node_editor.group_view_stack,'new node visible=',page.node_editor._node_items['new-only'].isVisible())
page.cleanup();page.deleteLater();app.processEvents()
