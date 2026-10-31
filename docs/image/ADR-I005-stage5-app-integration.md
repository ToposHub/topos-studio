# ADR-I005：阶段 5 —— Topos Color 单图集成

日期：2026-09-05
状态：已接受
前置：ADR-I001..I004

## 决策与交付

| 项 | 决策/执行 |
| --- | --- |
| 格式注册 | `.tpic` 注册进 `format_registry.IMAGE_EXTENSIONS`；`is_tpic_image()` 以 TPIM magic 为最终依据（扩展名仅快速候选，spec §13）；`.topos` 不注册为图片（D1） |
| 内容路由 | factory 新增 `tpic_image` 后端；auto 顺序 = TPIM magic 命中 → tpic_image 优先（在 topos MOV 探测之前；互斥不冲突——magic 不同） |
| 绑定 | `src/shared/codec/topos_image_binding.py`：image ABI 的 ctypes 镜像（probe/validate/decode/read_metadata/capabilities），复用 `load_library`；解码直接写入 numpy 平面（零拷贝） |
| 媒体源 | `ToposImageSource`（MediaSource 协议）：单帧语义（total_frames=1、read_frame 仅 0）；pixel_format 注册名复用视频链（`gbrp10le/gbrp12le/yuv422p1xle`）——GPU 上传/合成/导出链零新增接线；色彩/Alpha 元数据进 `frame.extra` 与 VideoMetadata，同源透传 |
| 平面语义 | GBR=G,B,R（ADR-C017 C-130）；A 恒 plane 3；值域 ≤ 2^bit_depth−1（测试锁定，不降位） |
| 静态图工程关系 | `.topos` 工程包引用 `.tpic` 素材 = 普通文件引用，工程 schema 不变 |
| UI 边界 | UI 只经既有媒体链（DecodeQueue/渲染线程）消费 DecodedFrame；本类不做 OpenGL（.clinerules） |

## 验收

- `.tpic` 与 `.topos` 路由完全区分：`test_topos_project_not_routed_to_image` ✅
- decode→(GPU 链格式) 颜色/Alpha 语义：平面序与值域测试 + pixel_format 注册名复用 ✅
- 无 8-bit fallback、无 Alpha 静默压平：位深/alpha 元数据透传测试 ✅
- 失败/关闭资源释放：close/close-after-fail 路径 + 上下文即抛即清 ✅
- 12/12 新测试 + 34/34 既有 factory/topos 测试无回归 ✅
