import json
from src.features.edit.core.captions.importer import parse_subtitle_text
from src.features.edit.core.captions.export import render_subtitle_track
from src.features.edit.core.timeline_v2 import EditTrack,SubtitleClip,Rational
track=EditTrack(track_id='s',track_type='subtitle',subtitle_clips=(SubtitleClip(clip_id='c',track_id='s',timeline_start=24,timeline_duration=24,text='Hello, world'),))
ass=render_subtitle_track(track,'ass',Rational(24,1))
samples=[('ass',ass),('ttml','<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:tickRate="10"><body><div><p begin="10t" end="20t">hello</p></div></body></tt>'),('ttml','<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:frameRate="30"><body><div><p begin="30f" end="60f">hello</p></div></body></tt>')]
for ext,text in samples:
 print(json.dumps({'extension':ext,'input':text,'actual':[vars(x) for x in parse_subtitle_text(text,ext,Rational(24,1))]},ensure_ascii=False))
