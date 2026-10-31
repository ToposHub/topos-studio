# ADR-I021：Topos Image SDK 分发、发布和回滚

## 状态

O9 现有发布流水线收口（2026-09-08）。

## 决策

Topos Image 与 video codec 共用 `libtopos_codec` 发布物，但 image 公共头
`topos_image.h` 必须和 `topos_codec.h` 一起进入 SDK `include/`。`package_codec.py`
与 `build_sdk.sh` 现在都执行这一点；发布清单继续记录版本、ABI、机器、编译器、
commit、逐文件大小和 SHA-256，加载/round-trip smoke 失败则发布失败。

macOS 的 ad-hoc 或正式 codesign、notarytool/staple 由既有环境变量门控；Linux/
Windows 由 CI 的 Release + `dll_load_test.py`/`check_exports.py` 矩阵负责；
`package_codec.py` 现在把 public symbol gate、codec load round-trip 和 manifest
写入绑定为同一发布步骤。Python CI 按 runner 实际动态库后缀解析
`TOPOS_CODEC_LIB`，并额外跑 Topos Image ABI preview smoke。没有
真实 runner、签名凭据或公证凭据时只能记录 skipped/ad-hoc，不能把它写成正式
签名发布。

## 回滚

- v1 TPIM/TPIC reader 和旧 golden 保留；新能力通过 capability mask 失败，而不是
  静默猜测或改写源 `.toos`/MOV；
- 缩略图缓存由 source stat/hash sidecar 失效，发布回滚不会复用不同 codec build
  的预览结果；
- 发行清单是不可变证据，遇到加载失败回退到上一 SDK/应用包，不覆盖源素材。

## 本机门禁

```text
python3 -m pytest -q tests/native/test_release_scripts.py
bash native/topos_codec/scripts/build_sdk.sh --smoke
python3 scripts/topos_abi_smoke.py \
  native/topos_codec/tests/conformance/golden_image_v1_gbr10.bin \
  --library native/topos_codec/build/release/topos_codec.dylib
```

完整 universal/Windows/Linux 构建、签名、公证仍必须在对应 CI/开发者环境执行，
本机 macOS 结果不能代替其它平台证据。
