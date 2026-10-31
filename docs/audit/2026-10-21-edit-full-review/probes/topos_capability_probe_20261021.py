import json
from src.features.edit.core.timeline_export import ExportCodec,TimelineExportConfig
from src.features.deliver.core.delivery_capability import describe_delivery_capability
for c in [ExportCodec.GIF,ExportCodec.TOPOS_IMAGE_SEQUENCE,ExportCodec.PRORES_4444,ExportCodec.H264]:
 cfg=TimelineExportConfig(output_path='/tmp/audit_unused.mp4',codec=c,include_audio=True,has_alpha=c==ExportCodec.PRORES_4444)
 cap=describe_delivery_capability(cfg)
 print(json.dumps({'input_codec':c.value,'reported_codec':cap.codec,'container':cap.container,'audio_capable':cap.audio_capable,'alpha_capable':cap.alpha_capable,'audio_limit':cap.audio_limit},ensure_ascii=False))
