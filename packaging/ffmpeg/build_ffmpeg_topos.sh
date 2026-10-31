#!/usr/bin/env bash
# =============================================================
# 构建带 Topos 解码支持的 FFmpeg（libtopos wrapper）
#
# 产物：自带的 ffmpeg / ffprobe（LGPL 构建 + 动态链接 libtopos_codec），
# 使 ffmpeg / ffprobe / ffplay 生态原生解封装 + 解码 TPIC（Topos MOV）。
#
# 用法：
#   bash packaging/ffmpeg/build_ffmpeg_topos.sh [FFMPEG_VERSION]
#   （默认 FFMPEG_VERSION=n8.0.1；补丁按该版本锚点编写）
#
# 依赖：clang、make、nasm、pkg-config（macOS: brew install nasm pkg-config）、
#       已构建的 native/topos_codec（build/release/topos_codec.dylib）。
#
# 网络说明：从 codeload.github.com 下载源码；离线时把源码 tarball 放到
#   $HOME/Downloads/ffmpeg-<ver>.tar.gz 亦可（脚本会优先使用）。
# =============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FFVER="${1:-n8.0.1}"
SRC_TGZ="${FFVER}.tar.gz"
WORK="${TOPOS_FFMPEG_WORK:-/tmp}"
PREFIX="${TOPOS_FFMPEG_PREFIX:-$WORK/ffmpeg-topos}"
TOPOS_PREFIX="$WORK/topos-prefix"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"

DYLIB="$REPO_ROOT/native/topos_codec/build/release/topos_codec.dylib"
[ -f "$DYLIB" ] || { echo "ERROR: 未找到 $DYLIB，先构建 native/topos_codec"; exit 1; }

echo "==> 1/5 获取 FFmpeg 源码 $FFVER"
mkdir -p "$WORK"
if [ ! -f "$WORK/$SRC_TGZ" ]; then
    curl -sL --max-time 300 -o "$WORK/$SRC_TGZ" \
        "https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/refs/tags/$FFVER"
fi
rm -rf "$WORK/FFmpeg-$FFVER"
tar xzf "$WORK/$SRC_TGZ" -C "$WORK"

echo "==> 2/5 应用 Topos 解码器补丁"
cd "$WORK/FFmpeg-$FFVER"
patch -p1 < "$REPO_ROOT/packaging/ffmpeg/patches/0001-libtopos-decoder.patch"

echo "==> 3/5 准备 libtopos 前缀（include + 带绝对 install_name 的 dylib）"
mkdir -p "$TOPOS_PREFIX/include" "$TOPOS_PREFIX/lib"
ln -sf "$REPO_ROOT/native/topos_codec/include/topos_codec.h" \
       "$TOPOS_PREFIX/include/topos_codec.h"
cp -L "$DYLIB" "$TOPOS_PREFIX/lib/libtopos_codec.dylib"
install_name_tool -id "$TOPOS_PREFIX/lib/libtopos_codec.dylib" \
                  "$TOPOS_PREFIX/lib/libtopos_codec.dylib"
codesign -f -s - "$TOPOS_PREFIX/lib/libtopos_codec.dylib" 2>/dev/null || true

echo "==> 4/5 configure"
./configure --prefix="$PREFIX" \
    --cc=clang --cxx=clang++ \
    --enable-libtopos \
    --extra-cflags="-I$TOPOS_PREFIX/include" \
    --extra-ldflags="-L$TOPOS_PREFIX/lib" \
    --extra-libs=-ltopos_codec \
    --disable-doc

echo "==> 5/5 make -j$JOBS && make install"
make -j"$JOBS"
make install

echo
echo "构建完成：$PREFIX/bin/{ffmpeg,ffprobe}"
echo "验证：$PREFIX/bin/ffmpeg -decoders | grep topos"
echo "播放：$PREFIX/bin/ffplay <file.topos>   （含 ffplay 时）"
