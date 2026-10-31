# bin/ — 播放器/缩略图工具链（不入库）

由 packaging/ffmpeg/build_ffmpeg_topos.sh 构建后拷入：
  cp /tmp/ffmpeg-topos/bin/{ffmpeg,ffprobe} .
  cp /tmp/topos-prefix/lib/libtopos_codec.dylib .
  install_name_tool -change /tmp/topos-prefix/lib/libtopos_codec.dylib @executable_path/libtopos_codec.dylib ffmpeg ffprobe
  codesign -f -s - ffmpeg ffprobe
