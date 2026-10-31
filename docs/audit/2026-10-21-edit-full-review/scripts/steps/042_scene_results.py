import time
end=time.monotonic()+25
while sd.get_analysis() is None and time.monotonic()<end and not scene_errors:QTest.qWait(200)
a=sd.get_analysis()
result={'analysis':None if a is None else {'type':type(a).__name__,'candidates':len(a.candidates),'frames':getattr(a,'frame_count',None),'total_frames':getattr(a,'total_frames',None)},'errors':scene_errors,'elapsed_wait':25-max(0,end-time.monotonic())}
capture('042_scene_detection_finished',sd);sd.close();QTest.qWait(150)
