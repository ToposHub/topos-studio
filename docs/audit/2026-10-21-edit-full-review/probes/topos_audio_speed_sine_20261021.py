import wave,json
import numpy as np
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.timeline_export import create_timeline_exporter,TimelineExportConfig
sr=48000
x=np.sin(2*np.pi*440*np.arange(sr*6)/sr)*.2
with wave.open('/tmp/topos_audio440_audit.wav','wb') as f:
 f.setnchannels(1);f.setsampwidth(2);f.setframerate(sr);f.writeframes((x*32767).astype('<i2').tobytes())
p=EditProject.create(name='audit').with_media_asset_added(MediaAsset(asset_id='s',path='/tmp/topos_audio440_audit.wav',media_type='audio',display_name='sine',metadata={'fps':25.,'audio_channels':1}))
for speed in [1.,2.,.5]:
 c=ClipInstance(clip_id='c',asset_id='s',track_id='a1',timeline_start=0,timeline_duration=50,source_start=0,source_duration=int(50*speed),speed=SpeedParams(speed=speed,preserve_pitch=False))
 seq=TimelineSequence(timeline_id='s',fps=Rational(25,1),audio_tracks=(EditTrack(track_id='a1',track_type='audio',index=0,clips=(c,)),))
 e=create_timeline_exporter(p,seq)
 out=e._render_audio_master(0,50,TimelineExportConfig(output_path='/tmp/unused.mp4'),encoder=object())[0]
 freq=np.fft.rfftfreq(out.size,1/sr);spectrum=abs(np.fft.rfft(out*np.hanning(out.size)))
 peak=float(freq[np.argmax(spectrum)])
 print(json.dumps({'speed':speed,'preserve_pitch':False,'expected_dominant_hz':speed*440,'actual_dominant_hz':peak,'peak':float(max(abs(out)))}))
