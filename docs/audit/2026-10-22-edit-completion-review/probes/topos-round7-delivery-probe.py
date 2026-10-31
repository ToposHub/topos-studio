from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import tempfile, json, sys
sys.path.insert(0, str(Path.cwd()))
import numpy as np, av
from src.shared.export.image_sequence_encoder import StandardImageSequenceEncoder
from src.features.edit.core.timeline_export import TimelineExportConfig, TimelineExporter, ExportCodec
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.shared.media.metadata import Rational
from src.features.deliver.core.delivery_capability import describe_delivery_capability

root=Path(tempfile.mkdtemp(prefix='topos-round7-delivery-data-'))
seq=TimelineSequence(timeline_id='s',name='S',width=16,height=16,fps=Rational(25,1))
exp=TimelineExporter(None,seq,Mock())
result={'directory':str(root)}
result['config_fields']={'bit_depth': 'bit_depth' in TimelineExportConfig.__dataclass_fields__, 'naming_config':'naming_config' in TimelineExportConfig.__dataclass_fields__}
cfg=TimelineExportConfig(output_path=str(root/'shot'),codec=ExportCodec.TIFF_SEQUENCE, has_alpha=True, start_frame=23,unique_filename_digits=9)
enc_cfg=exp._create_encoder_config(cfg)
enc=exp._create_encoder(cfg.output_path,enc_cfg)
result['production_config']={'pix_fmt':enc_cfg.pix_fmt,'has_alpha':enc_cfg.has_alpha,'start_index':enc._start_index,'digits':enc._digits,'supports_audio':getattr(enc,'supports_audio','ABSENT')}
for codec in ['png_sequence','tiff_sequence']:
    for dtype in [np.uint8,np.uint16]:
        d=root/f'{codec}-{dtype.__name__}'; d.mkdir()
        a=np.zeros((8,12,4),dtype=dtype); a[:,:,0]=100; a[:,:,1]=250; a[:,:,2]=30; a[:,:,3]=200
        encoder=StandardImageSequenceEncoder(str(d/'color'), SimpleNamespace(codec=codec))
        encoder.encode_frame(SimpleNamespace(data=a)); encoder.close()
        p=next(d.iterdir()); container=av.open(str(p)); f=next(container.decode(video=0))
        out=f.to_ndarray(format='rgba' if dtype==np.uint8 else 'rgba64le'); container.close()
        result[f'{codec}-{dtype.__name__}']={'format':f.format.name,'roundtrip_equal':bool(np.array_equal(a[:,:,[2,1,0,3]],out)), 'sample':out[0,0].tolist()}
d=root/'collision'; d.mkdir(); old=d/'shot_000000.png'; old.write_bytes(b'EXISTING USER FRAME')
encoder=StandardImageSequenceEncoder(str(d/'shot'),SimpleNamespace(codec='png_sequence'))
encoder.encode_frame(SimpleNamespace(data=np.zeros((8,12,3),np.uint8)))
result['collision']={'existing_frame_overwritten':old.read_bytes()!=b'EXISTING USER FRAME'}
encoder.abort(); result['collision']['existing_frame_deleted_on_abort']=not old.exists()
for codec in [ExportCodec.GIF,ExportCodec.PNG_SEQUENCE,ExportCodec.TIFF_SEQUENCE]:
    c=TimelineExportConfig(output_path=str(root/'x'),codec=codec,has_alpha=True)
    result[f'capability-{codec.value}']=describe_delivery_capability(c).to_dict()
print(json.dumps(result,indent=2))
