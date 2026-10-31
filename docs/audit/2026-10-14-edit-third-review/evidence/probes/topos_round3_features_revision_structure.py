import sys,json
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import MagicMock,patch
sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from src.features.edit.core.project import EditProject,TimelineSequenceRef
from src.features.edit.core.timeline_v2 import TimelineSequence,Rational
from src.app.controllers.edit_controller import EditControllerMixin
import src.features.edit.ui.new_timeline_dialog as dlg_module
ref=TimelineSequenceRef('old','Old',24.,1920,1080,48000)
p=EditProject('p','P',timelines={'old':ref},active_timeline_id='old',revision=7)
s=TimelineSequence(timeline_id='old',name='Old',fps=Rational(24,1))
h=SimpleNamespace(_current_edit_project=p,_current_timeline_sequences={'old':s},media_pool_panel=MagicMock(),timeline_panel=MagicMock(),edit_page=MagicMock(),timeline_preview_renderer=None,_init_timeline_preview_renderer=lambda:None)
EditControllerMixin._on_timeline_switched(h,'old')
out={'actual_switch_same_timeline':{'before':p.revision,'after':h._current_edit_project.revision},'helper_switch':p.set_active_timeline('old').revision,'helper_create_timeline':p.create_timeline('New').revision,'helper_ref_add':p.with_timeline_ref_added(TimelineSequenceRef('new','New',24.,1920,1080,48000)).revision}
settings={'name':'Round3 Roll','fps_num':24,'fps_den':1,'width':1920,'height':1080,'sample_rate':48000}
class FakeDialog:
    def __init__(self,*a,**kw):pass
    def exec(self):return 1
    def get_settings(self):return settings
h._current_edit_project=p;h._format_settings_from_dialog=EditControllerMixin._format_settings_from_dialog
h._on_timeline_switched=lambda tid:EditControllerMixin._on_timeline_switched(h,tid)
with patch.object(dlg_module,'NewTimelineDialog',FakeDialog):EditControllerMixin._on_timeline_create_requested(h)
out['actual_create_and_switch']={'before':7,'after':h._current_edit_project.revision,'active':h._current_edit_project.active_timeline_id,'sequence_count':len(h._current_timeline_sequences)}
print(json.dumps(out,indent=2))
