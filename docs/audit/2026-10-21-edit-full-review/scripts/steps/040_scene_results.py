a=sd.get_analysis()
result={'analysis':None if a is None else {'type':type(a).__name__,'candidates':len(a.candidates),'total_frames':getattr(a,'total_frames',None),'source':getattr(a,'source_path',None)},'errors':scene_errors,'buttons':[{'text':b.text(),'enabled':b.isEnabled()} for b in sd.findChildren(QAbstractButton) if b.text()]}
capture('040_scene_results',sd)
sd.close();QTest.qWait(250)
