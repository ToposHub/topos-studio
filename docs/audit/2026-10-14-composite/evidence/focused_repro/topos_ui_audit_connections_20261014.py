import os,sys
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['TOPOS_TEST_MODE']='1';sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import QApplication
from unittest.mock import MagicMock
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.nodes.geo3d_node import Geo3DNode
app=QApplication.instance() or QApplication([])
p=CompositePage();p._properties_panel=MagicMock();p._evaluate_graph=MagicMock();p._evaluate_graph_now=MagicMock();p._refresh_asset_dependency_panel=MagicMock();p._update_minimap=MagicMock();p.update_viewer_handles=MagicMock();p._sync_layer_stack_after_node_edit=MagicMock()
def edges():return [(c.src_node_id,c.dst_node_id) for c in p.node_graph.connections]
def wires():return [(c[1],c[3]) for c in p.node_editor._connection_items]
def reset(ns):
 p.node_graph.clear()
 for n in ns:p.node_graph.add_node(n)
 p.node_editor.build_from_graph(p.node_graph);p._undo_stack.clear()
a,b,o=ConstantNode(node_id='a'),ConstantNode(node_id='b'),OutputNode(node_id='o');reset([a,b,o])
p.node_editor._actions.connect('a','result','o','image');p._undo_stack.clear()
p.node_editor._actions.connect('b','result','o','image');p.undo()
print('U-14 replace input undo edges=',edges(),'expected a->o')
g1,g2,s=Geo3DNode(node_id='g1'),Geo3DNode(node_id='g2'),Scene3DNode(node_id='s');reset([g1,g2,s]);act=p.node_editor._actions
act.connect('g1','display','s','in');act.connect('g2','display','s','in');p._undo_stack.clear()
act.disconnect('g2','display','s','in');print('U-15 delete second multi edge model=',edges(),'visual=',wires())
reset([g1,g2,s]);act=p.node_editor._actions;act.connect('g1','display','s','in');act.connect('g2','display','s','in');p._undo_stack.clear()
act.disconnect_by_dst('s','in');print('U-15 drag multi input model=',edges(),'visual=',wires())
# Copy/paste chain records connection before nodes_pasted starts group.
reset([a,o]);act=p.node_editor._actions;act.connect('a','result','o','image');p._undo_stack.clear()
for it in p.node_editor._node_items.values():it.set_selected(True)
act.copy_selected_nodes();newids=act.paste_nodes();print('U-16 paste undo desc=',p._undo_stack.undo_description(),'nodes=',len(p.node_graph.nodes))
p.undo();print('U-16 after undo paste remaining old nodes=',len(p.node_graph.nodes),'next undo=',p._undo_stack.undo_description())
p.cleanup();p.deleteLater();app.processEvents()
