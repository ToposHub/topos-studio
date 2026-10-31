from pathlib import Path
from unittest.mock import Mock
from tempfile import mkdtemp
import json,sys,time
sys.path.insert(0,str(Path.cwd()))
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_v2 import TimelineSequence,ClipInstance,EditTrack
from src.shared.media.metadata import Rational
from src.features.edit.core.timeline_export import TimelineExporter,TimelineExportConfig,ExportCodec

root=Path(mkdtemp(prefix='topos-round7-delivery-e2e-'))
path=str(Path('tests/test_videos/2K_h264.mp4').resolve())
v=ClipInstance('v','a','v0',0,2,0,2)
a=ClipInstance('a','a','a0',0,2,0,2)
seq=TimelineSequence(timeline_id='s',name='S',width=32,height=24,fps=Rational(25,1),video_tracks=(EditTrack('v0','V1','video',0,clips=(v,)),),audio_tracks=(EditTrack('a0','A1','audio',0,clips=(a,)),))
p=EditProject('p','P',assets={'a':MediaAsset('a',path,'video','V',{})})
results={}
for codec in [ExportCodec.PNG_SEQUENCE,ExportCodec.TIFF_SEQUENCE]:
    directory=root/codec.value;directory.mkdir()
    cfg=TimelineExportConfig(output_path=str(directory),codec=codec,timeline_id='s',resolution=(32,24),include_audio=True,has_alpha=True)
    ex=TimelineExporter(p,seq,Mock());out=ex.export_timeline(cfg)
    results[codec.value]={'success':out.success,'error':getattr(out,'error_message',None),'audio_omitted':out.audio_omitted,'output_path':cfg.output_path,'directory_children':[x.name for x in directory.iterdir()],'sibling_frames':[x.name for x in root.iterdir() if x.is_file()]}
time.sleep(1)
results['root']=str(root)
print(json.dumps(results,indent=2))
