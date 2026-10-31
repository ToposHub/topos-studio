#!/bin/sh
# topos_imageio.cpp 无 SDK 语法门禁 —— 阶段 8 教训（ADR-I009）：本文件曾以
# "未安装 OIIO SDK 自动跳过构建"状态入库，携带 2 处缺括号语法错误 +
# 2 处类型错误（元素引用当指针 / 未赋值 w,h 导致平面全别名）数月未暴露。
# 本脚本用 stub 头做 -fsyntax-only 检查，在拿到真 SDK（R-I15）之前持续
# 防止"未编译代码腐化"。真 SDK 构建仍是最终权威。
set -e
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../.." && pwd)
clang++ -std=c++17 -fsyntax-only -Wall -Wextra -Wpedantic -Wno-unused-parameter \
    -I"$HERE/syntax_check_stub" -I"$REPO/native/topos_codec/include" \
    "$HERE/topos_imageio.cpp"
echo "topos_imageio.cpp: syntax OK (stub OIIO)"
