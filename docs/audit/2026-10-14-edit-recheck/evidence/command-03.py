timer.stop()
mw.raise_();mw.activateWindow();wait(200)
# Inspect real tab bars, then activate Source explicitly before confirming pixels.
record('tabbar_inventory',tabs=[{'visible':b.isVisible(),'current':b.currentIndex(),'tabs':[(b.tabText(i),i) for i in range(b.count())]} for b in mw.findChildren(QTabBar) if any(b.tabText(i) in ('Source','Program','Properties') for i in range(b.count()))])
select_tab('Source');wait(600)
record('source_visible_pixels',visible=sv.isVisible(),viewer_visible=sv.viewer.isVisible(),paint_failed=sv.viewer._gl_paint_failed,frame=sv.current_frame,texture=getattr(sv.viewer,'_current_tex_id',None),shared_texture=getattr(sv.viewer,'_shared_tex_id',None),screenshot=shot('09-source-visible'))
start=sv.current_frame;click(sv.toolbar.btn_play);wait(900);click(sv.toolbar.btn_play)
record('source_playback',before=start,after=sv.current_frame,paint_failed=sv.viewer._gl_paint_failed,screenshot=shot('10-source-play'))
select_tab('Properties');wait(200)
props=mw.edit_properties_panel
record('section_inventory',sections=[(k,[(key,type(info.get('widget')).__name__,type(info.get('slider')).__name__,getattr(info.get('widget'),'text',lambda:None)(),getattr(info.get('widget'),'value',lambda:None)(),info.get('widget').isVisible() if info.get('widget') else None) for key,info in sec._param_widgets.items()]) for k,sec in props._sections.items()])
# Exact actual numeric input for selected clips; select two via real clicks if needed.
select_tab('Program');click(panel.btn_select);click(panel.btn_zoom_fit)
clips=list(tv._timeline.video_tracks[0].clips)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=clip_point(clips[0].clip_id));wait(100)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ShiftModifier,pos=clip_point(clips[1].clip_id));wait(120)
select_tab('Properties');wait(200)
sections=[s for k,s in props._sections.items() if k.endswith(':transform')]
if sections:
    sec=sections[0]
    if getattr(sec,'_is_collapsed',False):sec.set_collapsed(False)
    w=sec._param_widgets['translate_x']['widget']
    record('transform_widget',type=type(w).__name__,visible=w.isVisible(),value=w.value(),context=[c.clip_id for c in props._current_clips])
    before=[(c.clip_id,c.transform.translate_x,c.opacity) for c in tv._timeline.video_tracks[0].clips]
    QTest.mouseDClick(w,Qt.MouseButton.LeftButton);wait(60)
    # DragValue opens its in-place line editor; use the actual active focus widget.
    editor=QApplication.focusWidget()
    if hasattr(editor,'selectAll'):editor.selectAll()
    QTest.keyClicks(editor,'99');QTest.keyClick(editor,Qt.Key.Key_Return);wait(350)
    record('multi_transform_edit',before=before,after=[(c.clip_id,c.transform.translate_x,c.opacity) for c in tv._timeline.video_tracks[0].clips],screenshot=shot('11-multi-transform'))
# Avoid the fit-edge input ambiguity: zoom out before ordinary trim to make its edge clearly interior.
select_tab('Program');click(panel.btn_select);click(panel.btn_zoom_out);click(panel.btn_zoom_out)
right=tv._timeline.video_tracks[0].clips[-1];item=tv._items_map[right.clip_id]
p=clip_point(right.clip_id,getattr(item,'_width',item.boundingRect().width())-2)
before=timeline_state();drag(p,frame_point(350));record('trim_interior_edge',start=[p.x(),p.y()],before=before,after=timeline_state(),screenshot=shot('12-trim-interior-edge'))
timer.start(500)
