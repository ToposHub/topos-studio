# oiio_topos —— OpenImageIO 插件（Topos Image 阶段 8）

提供 OIIO `ImageInput` / `ImageOutput` 插件，使任何**能加载该插件**的 OIIO
宿主（oiiotool、基于 OIIO 的自研管线等）可以读写 `.toos`。

## ⚠️ 能力边界（必须如实宣传）

| 事实 | 说明 |
| --- | --- |
| 不自动出现在任何宿主 | 只有按 OIIO 插件搜索路径（`OIIO_LIBRARY_PATH` 或安装到 OIIO 的
  plugin 目录）部署了本插件的机器才可用。**Blender / Maya / Nuke /
  Photoshop 不会因为本插件存在而自动支持 .toos**——各宿主须独立交付 |
| OIIO 无 C ABI | 本插件按构建时的 OIIO 头版本编译，产物带 OIIO 版本标识；
  ABI 不匹配由宿主加载机制拒绝。逐 OIIO 版本 × OS × 架构分别构建 |
| v1 能力 | 仅 scanline RGB(A)；`tiles`/`mipmap`/多图 `supports()`=false |
| 位深 | 10/12-bit ↔ 16-bit 容器显式换算（读侧左移对齐；写侧四舍五入+饱和；
  alpha 恒按 A16 容器 0..65535 直通——ADR-I009 刻度契约）；元数据
  `topos:ValidBitDepth` 透传，无色彩猜测 |
| YUV | v1 插件只写 GBR（oiiotool 端请先色彩转换）。读侧：YUV 4:4:4（pf=1）
  可读（channelnames Y,U,V）；YUV 4:2:2（pf=0 / Preview）显式拒读——
  子采样色度平面与本插件的满分辨率整平面打包不兼容 |

## 构建

```bash
# 需要 OpenImageIO 开发头文件（v2.x）
cmake -S . -B build -DTOPOS_BUILD_OIIO_PLUGIN=ON -DCMAKE_PREFIX_PATH=<oiio-prefix>
cmake --build build --target topos.imageio
```

未安装 OIIO SDK 时该目标自动跳过（`TOPOS_BUILD_OIIO_PLUGIN` 默认 OFF），
核心库构建不受影响（spec §1：核心零第三方依赖）。

### 无 SDK 语法门禁（防"未编译代码腐化"）

本插件曾以未构建状态入库并携带语法/类型错误数月（ADR-I009 复核发现：
2 处缺右括号、元素引用误当指针、未赋值变量导致平面全别名）。在拿到真
SDK（R-I15）之前，用 stub 头做持续语法检查：

```bash
./plugins/oiio_topos/syntax_check.sh    # -fsyntax-only -Wall -Wextra -Wpedantic
```

stub（`syntax_check_stub/OpenImageIO/imageio.h`）按 OIIO 2.x 公共 API 手写
镜像，仅用于类型检查、不得链接；真 SDK 构建仍是最终权威。

## 验收方式（本机无 OIIO SDK，列入阶段 12/分发环境执行）

```bash
oiiotool --info beauty.toos          # probe 元数据（imageProfile/ValidBitDepth）
oiiotool beauty.toos -o out.exr      # GBR 10-bit → EXR 往返（按格式能力）
oiiotool in.exr -o out.toos          # EXR → .toos（uint16 scanline RGB(A)）
```

对照 `docs/image/test_vector_manifest.json` 的 golden 做像素级比对。
