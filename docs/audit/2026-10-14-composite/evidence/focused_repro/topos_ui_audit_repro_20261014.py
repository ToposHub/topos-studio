import os, sys, copy
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['TOPOS_TEST_MODE']='1'
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication
from unittest.mock import MagicMock
from src.app.controllers.composite_controller import CompositeControllerMixin
from src.features.edit.core.project import EditProject
from dataclasses import replace
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.output_node import OutputNode
app=QApplication.instance() or QApplication([])
class C(CompositeControllerMixin):
    def __init__(self,p):
        self._current_edit_project=p
        self.composite_page=None
        self.edit_project_dirty=False
        self._refresh_edit_preview_after_composite_change=MagicMock()
p=EditProject.create('audit'); p,_,_,gid=p.create_empty_comp_clip()
c=C(p); incoming=copy.deepcopy(p.compositions[gid]); incoming['layers']={'layer-1':{'layer_id':'layer-1','visible':False}}; incoming['layer_stacks']={'stack-1':{'layer_ids':['layer-1']}}
c._sync_composite_graph_to_edit_project(gid,incoming)
print('U-01 layer sync result:',c._current_edit_project.compositions[gid].get('layers'), c._current_edit_project is p)
page=CompositePage(); page._evaluate_graph=MagicMock(); page._evaluate_graph_now=MagicMock(); page.update_viewer_handles=MagicMock(); page._refresh_asset_dependency_panel=MagicMock(); page._update_minimap=MagicMock()
t=TransformNode(node_id='shared'); graph={'composite_id':'A','nodes':[t.to_dict()], 'connections':[], 'duration_frames':48,'fps':24.0}
page.load_composite_data(graph)
page._on_parameter_changed('shared','rotation',20.0); page._flush_param_changes()
print('U-02 undo A available:',page._undo_stack.can_undo())
graph_b=copy.deepcopy(graph); graph_b['composite_id']='B'; graph_b['nodes'][0]['parameters']['rotation']=70.0
page.load_composite_data(graph_b); page.undo()
print('U-02 B rotation after A undo:',page.node_graph.get_node('shared').parameters['rotation'])
page._undo_stack.clear(); n1=TransformNode(node_id='b1'); n2=TransformNode(node_id='b2'); page.node_graph.add_node(n1);page.node_graph.add_node(n2)
page._on_parameters_changed_batch(['b1','b2'],'rotation',10.0,{'b1':0.0,'b2':0.0})
page._on_parameters_changed_batch(['b1','b2'],'scale',2.0,{'b1':1.0,'b2':1.0})
page._flush_batch_param_changes(); page.undo()
print('U-03 after batch undo:',[(n.parameters['rotation'],n.parameters['scale']) for n in (n1,n2)])
# Undo before debounce: one old settled action plus new pending edit.
page._undo_stack.clear(); page._pending_param_change=None
page._on_parameter_changed('b1','rotation',20.0); page._flush_param_changes()
page._on_parameter_changed('b1','scale',3.0); page.undo()
print('U-04 immediate undo:',n1.parameters['rotation'],n1.parameters['scale'],'pending=',page._pending_param_change)
page._flush_param_changes(); print('U-04 after pending flush undo description:',page._undo_stack.undo_description())
page.load_composite_data({'composite_id':'empty','nodes':[], 'duration_frames':2,'fps':30.0})
print('U-05 empty load retains nodes:',list(page.node_graph.nodes),'fps/duration=',page._composite_fps,page._composite_duration_frames)
page.cleanup(); page.deleteLater(); app.processEvents()
