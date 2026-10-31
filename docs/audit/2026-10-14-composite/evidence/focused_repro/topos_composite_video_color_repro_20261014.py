import sys,json,pathlib,tempfile
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02')
from src.features.composite.render.composite_render_output import CompositeRenderOutput
from src.shared.color.color_space_descriptor import REC709_VIDEO, DISPLAY_P3_SRGB, LINEAR_SCENE
from src.shared.color.color_transform_provider import BuiltinColorProvider
from src.shared.export.video_encoder import FFmpegVideoEncoder, FFmpegEncoderConfig, ColorMetadataConfig
from src.shared.media.metadata import Rational
from src.shared.media.frame import DecodedFrame
class Config:
 def get(self,k,default=None):return 'builtin' if k=='composite.color_engine' else default
cfg=Config(); evidence={}
# Real PyAV RGB -> limited YUV conversion, without opening or encoding files.
enc=FFmpegVideoEncoder('/tmp/not-created-audit.mp4',FFmpegEncoderConfig(codec='h264',width=8,height=8,fps=Rational(24,1),color_metadata=ColorMetadataConfig(primaries='bt709',transfer='bt709',matrix='bt709',range='limited')))
y_result=[]
for value in (0.,1.):
 linear=np.full((8,8,3),value,np.float32)
 signal=BuiltinColorProvider().transform(linear,LINEAR_SCENE,REC709_VIDEO)
 packed=(signal*255).astype(np.uint8)
 f=DecodedFrame(frame_index=0,pts=0,time_seconds=0.,data=packed,pixel_format='bgr24',width=8,height=8)
 avf=enc._convert_packed_frame(f,'yuv420p')
 y=np.frombuffer(avf.planes[0],np.uint8).reshape(8,avf.planes[0].line_size)[:,:8]
 y_result.append({'linear_value':value,'rgb_signal':int(packed[0,0,0]),'encoded_y':int(y[0,0])})
evidence['composite_double_limited_range']={'actual':y_result,'expected_black_white_y':[16,235]}
# BT.2020 code path gets the original packed BGR array although helper expects RGB.
enc2020=FFmpegVideoEncoder('/tmp/not-created-audit-hdr.mp4',FFmpegEncoderConfig(codec='hevc',width=8,height=8,fps=Rational(24,1),pix_fmt='yuv420p10le',color_metadata=ColorMetadataConfig.hdr10()))
redbgr=np.zeros((8,8,3),np.uint16);redbgr[...,2]=65535
redrgb=redbgr[...,::-1].copy()
f=DecodedFrame(frame_index=0,pts=0,time_seconds=0.,data=redbgr,pixel_format='bgr48le',width=8,height=8)
actual=enc2020._convert_packed_frame(f,'yuv420p10le')
expected=enc2020._convert_rgb_to_yuv_bt2020(redrgb,enc2020._config.color_metadata,'yuv420p10le')
def first_y(avf):return int(np.frombuffer(avf.planes[0],np.uint16)[0])
evidence['hdr_bt2020_bgr_swap']={'actual_red_y10':first_y(actual),'correct_red_y10':first_y(expected)}
class FakeEncoder:
 last=None
 def __init__(self,path,config):self.config=config;self.frames=[];FakeEncoder.last=self
 def open(self):pass
 def close(self):pass
 def encode_frame(self,frame):self.frames.append(frame)
with patch('src.shared.export.video_encoder.FFmpegVideoEncoder',FakeEncoder):
 out=CompositeRenderOutput(cfg);out._render_one_frame=lambda **kw:np.tile(np.linspace(0,1,1024,dtype=np.float32)[None,:,None],(2,1,3))
 res=out.render_video({'nodes':[]},'/tmp/not-created-wg.mp4',1024,2,24,0,0,output_descriptor=DISPLAY_P3_SRGB,parallel_frames=1)
 f=FakeEncoder.last.frames[0]
 evidence['wide_gamut_sdr_truncates_before_encode']={'result':vars(res),'configured_pix_fmt':FakeEncoder.last.config.pix_fmt,'submitted_dtype':str(f.data.dtype),'submitted_format':f.pixel_format,'unique_values':int(len(np.unique(f.data[:,:,0])))}
 out=CompositeRenderOutput(cfg);out._render_one_frame=lambda **kw:None
 res=out.render_video({'nodes':[]},'/tmp/not-created-all-black.mp4',8,8,24,0,2,parallel_frames=1)
 evidence['all_render_failed_black_video_success']={'result':vars(res),'frame_values':[int(f.data.max()) for f in FakeEncoder.last.frames]}
print(json.dumps(evidence,ensure_ascii=False,indent=2))
pathlib.Path('/tmp/topos_composite_video_color_repro_20261014.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
