from dataclasses import replace
from src.features.edit.core.clip_params import SpeedParams,SpeedPoint
from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.edit.core.timeline_ops import timeline_duration_for_source
base=ClipInstance(clip_id='c',asset_id='a',track_id='v',timeline_start=0,timeline_duration=100,source_start=100,source_duration=1000,speed=SpeedParams(speed=8),_source_fps_num=30,_timeline_fps_num=24)
neutral=replace(base,speed=SpeedParams(speed=8,speed_points=(SpeedPoint(200,8),)))
mm=[(i,base.timeline_to_source_frame(i),neutral.timeline_to_source_frame(i)) for i in range(100) if base.timeline_to_source_frame(i)!=neutral.timeline_to_source_frame(i)]
print('neutral speed point FPS whole-clip duration:',timeline_duration_for_source(1000,neutral.speed,30,1,24,1))
print('mismatch count/max:',len(mm),max(abs(a-b) for _,a,b in mm),'first:',mm[:8])
