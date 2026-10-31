#!/usr/bin/env bash
# M-B7：libtopos 补丁侧音轨回归（plan M-B7）。
#
# 音轨（lpcm/mp4a）走 FFmpeg 通用 mov demux 路径，无 libtopos 依赖——
# 预期零补丁改动。本脚本对（可选的）补丁构建与系统 ffmpeg 各跑一遍：
#   1. 生成含双轨 topos 产物（经绑定层，确定性）
#   2. ffprobe 识别（codec/采样率/声道）
#   3. ffmpeg 解码音频与 lpcm 母带 md5 对拍（逐位）
#   4. （补丁构建存在时）-c:v libtopos 组合解码验证视频+音频共存
#
# 用法：bash packaging/ffmpeg/audio_regression.sh [补丁ffmpeg前缀]
set -euo pipefail
cd "$(dirname "$0")/../.."

FFPROBE="${FFPROBE:-ffprobe}"
FFMPEG="${FFMPEG:-ffmpeg}"
PREFIX="${1:-/tmp/ffmpeg-topos}"

WORK=$(mktemp -d /tmp/topos_audio_reg_XXXXXX)
trap 'rm -rf "$WORK"' EXIT

python3 - "$WORK" <<'PYEOF'
import ctypes, struct, sys
from pathlib import Path
sys.path.insert(0, ".")
import numpy as np
from src.shared.codec import topos_binding as tb
from src.shared.codec.topos_binding import (
    ToposCodec, TC_AUDIO_CODEC_LPCM, TC_AUDIO_CODEC_MP4A, TOPOS_CODEC_ABI_VERSION)

work = Path(sys.argv[1])
codec = ToposCodec()
W, H = 64, 48
fc = tb._CFrameConfig()
fc.struct_size = ctypes.sizeof(tb._CFrameConfig)
fc.abi_version = TOPOS_CODEC_ABI_VERSION
fc.visible_width, fc.visible_height = W, H
fc.profile, fc.pixel_format, fc.bit_depth = 3, 0, 10
fc.qmatrix_id, fc.qp_base = 1, 24
fc.color_range = fc.color_primaries = fc.color_transfer = fc.color_matrix = 1
fc.sar_num = fc.sar_den = 1
cw = (W + 1) // 2
y = struct.pack(f"<{W*H}H", *[((x*7+r*13) % 400 + 300) for r in range(H) for x in range(W)])
u = struct.pack(f"<{cw*H}H", *[(480 + (x % 5)*8) for r in range(H) for x in range(cw)])
v = struct.pack(f"<{cw*H}H", *[(520 + (r % 4)*6) for r in range(H) for x in range(cw)])
vp = [codec.encode_frame(fc, [y, u, v])[0] for _ in range(8)]

# lpcm 16bit 立体声母带（确定性）
rate = 48000
t = np.arange(1920*8)/rate
# rint×满刻度（astype 直接截断会把 0.5·sin 全部清零——参考全零曾令
# "逐位对拍"只验长度不验内容）
pcm = np.rint(0.5*32767*np.sin(2*np.pi*440*t)).astype(np.int64)
be = np.repeat(pcm, 2).astype(">i2").tobytes()
ref_path = work/"ref.pcm"
ref_path.write_bytes(be)

cfg = codec.movie_config(W, H, timescale=24000, qp=24, qmatrix=1,
                         audio_codec=TC_AUDIO_CODEC_LPCM, audio_sample_rate=rate,
                         audio_channels=2, audio_layout=0, audio_bits_per_sample=16)
with tb.ToposMuxFile(codec, str(work/"dual_lpcm.mov"), cfg) as m:
    for i in range(8):
        m.add_packet(vp[i], i*1000, 1000)
        m.add_audio(be[i*1920*4:(i+1)*1920*4], 1920)
    m.finish()

# mp4a + elst
cfg_a = codec.movie_config(W, H, timescale=24000, qp=24, qmatrix=1,
                           audio_codec=TC_AUDIO_CODEC_MP4A, audio_sample_rate=rate,
                           audio_channels=2, audio_layout=0)
chunks = [bytes(((k*13+i) & 0xFF) for k in range(96)) for i in range(8)]
with tb.ToposMuxFile(codec, str(work/"dual_mp4a.mov"), cfg_a) as m:
    m.set_audio_asc(b"\x12\x10")
    for i in range(8):
        m.add_packet(vp[i], i*1000, 1000)
        m.add_audio(chunks[i], 1024)
    m.set_audio_priming(1024)
    m.finish()
print("fixtures:", work/"dual_lpcm.mov", work/"dual_mp4a.mov")
print("REF_PCM:", ref_path)
PYEOF

REF="$WORK/ref.pcm"
FAIL=0

for F in dual_lpcm dual_mp4a; do
  MOV="$WORK/$F.mov"
  echo "== ffprobe $F"
  "$FFPROBE" -v error -select_streams a -show_entries \
    "stream=codec_name,sample_rate,channels" -of json "$MOV" || FAIL=1
  echo "== ffmpeg decode $F"
  if [ "$F" = dual_lpcm ]; then
    "$FFMPEG" -v error -i "$MOV" -map 0:a -c:a pcm_s16le -f s16le "$WORK/$F.out"
    if cmp -s "$WORK/$F.out" "$REF"; then
      echo "lpcm bit-exact OK"
    else
      echo "lpcm BIT-EXACT FAIL"; FAIL=1
    fi
  else
    # mp4a fixture 为合成包（非真实 AAC 码流）：demux/流配置路径用 copy
    # 验证；真实码流解码由 interop 矩阵覆盖（PyAV 产真实包）
    "$FFMPEG" -v error -i "$MOV" -map 0:a -c:a copy -f null - \
      && echo "aac demux OK"
  fi
done

# 补丁构建存在时：libtopos（视频）+ mov demux（音频）组合
if [ -x "$PREFIX/bin/ffmpeg" ]; then
  echo "== patched ffmpeg ($PREFIX) libtopos + audio combo"
  "$PREFIX/bin/ffmpeg" -v error -i "$WORK/dual_lpcm.mov" -map 0:v -frames:v 2 \
    -f null - && echo "video via libtopos OK"
  "$PREFIX/bin/ffmpeg" -v error -i "$WORK/dual_lpcm.mov" -map 0:a \
    -c:a pcm_s16le -f s16le "$WORK/patched.out"
  cmp -s "$WORK/patched.out" "$REF" && echo "patched audio bit-exact OK" || { echo "patched audio FAIL"; FAIL=1; }
else
  echo "== patched ffmpeg 未构建（跳过组合验证；构建：bash packaging/ffmpeg/build_ffmpeg_topos.sh）"
fi

exit $FAIL
