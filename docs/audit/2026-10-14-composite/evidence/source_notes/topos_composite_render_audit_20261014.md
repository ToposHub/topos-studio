# 合成渲染、色彩、导出与 3D 审计（2026-10-14）

本报告由根审计的渲染子任务产生，未修改产品源码。范围：PyQt6 + ModernGL + GLSL 合成预览、图片/视频/图片序列导出、GPU 资源生命周期、缓存与 3D 材质路径。遵循 `.clinerules`；微型 GPU 复现仅在专属工作线程创建/操作独立 GL context。根任务负责完整测试套件、测试视频与 2K/4K 基准，本文不重复该负载。

## 证据口径与复现文件

- **A-GPU**：真实 ModernGL / 生产 GPUResourcePool，8×8 像素，独立渲染线程运行。
- **A-CPU**：真实 numpy / PyAV 色彩转换，没有打开视频编码器或产生交付视频。
- **A-注入**：调用真实生产控制流程，以 fake 解码帧、fake 编码器或保存器精确注入失败/取消；证明错误处理合同，不声称已用真实素材触发同一故障。
- **B-静态**：完整调用链和具体源码可证，但尚未在完整应用交互或真实模型上做视觉对照。需按验收标准复验。
- **C-能力不足**：当前明确未接线或不支持的能力，和 bug 分开记录。

可独立复运行的文件：

1. `/tmp/topos_composite_render_repro_20261014.py`，结果 `/tmp/topos_composite_render_repro_20261014.json`：Paint/AI 真实 GPU 上传；Paint alpha；图片 P3 转换；项目快门；序列取消、丢帧、cleanup、并行启动失败。
2. `/tmp/topos_composite_video_color_repro_20261014.py`，结果 `/tmp/topos_composite_video_color_repro_20261014.json`：实际 PyAV YUV 色域/范围转换；10-bit 前置量化；全黑成功。
3. `/tmp/topos_composite_volume_repro_20261014.py`，结果 `/tmp/topos_composite_volume_repro_20261014.json`：文件修改后体积缓存不更新（纹理上传器为 fake；体积计算与节点签名为真实生产逻辑）。

4. `/tmp/topos_composite_aov_repro_20261014.py`，结果 `/tmp/topos_composite_aov_repro_20261014.json`：AOV不做OETF，但PNG/TIFF量化前仍裁切signed/>1数据。

执行：项目 `.venv/bin/python <绝对脚本路径>`。单脚本耗时约 0.8–1.9 秒；所有输入均在临时目录，未修改测试素材。视频色彩脚本的 fake 编码器只采集提交帧，结果中的 `/tmp/not-created-*.mp4` 不是真实交付文件。

## 问题计划总表

| ID | 优先级建议 | 功能/问题 | 证据 | 影响 | 处理方向 | 建议工时 |
|---|---|---|---|---|---|---|
| R-01 | P1 | Paint/AI CPU float32 写入默认 float16 纹理，功能失效 | A-GPU | Paint 静默直通，AI 结果无法回到图中 | 上传统一按目标 dtype 转换，失败归还纹理 | 1–2 天 |
| R-02 | P1 | Paint 对 straight-alpha 输入把 alpha 也预乘，alpha 被平方 | A-GPU（隔离 f4） | 半透明素材全图 alpha 错，边缘变淡 | 只预乘 RGB、保留 alpha | 0.5 天 |
| R-03 | P1 | 合成视频 limited 范围重复压缩 | A-CPU | 默认 Rec.709 黑位抬高、白位降低 | RGB 中间值 full range；YUV 层统一压 legal | 1–2 天 |
| R-04 | P1 | BT.2020 编码辅助函数把 BGR 当 RGB | A-CPU | HDR10/HLG 红蓝色相和亮度错 | 辅助函数声明通道序，进入前转换 | 0.5–1 天 |
| R-05 | P1 | P3/BT.2020 图片序列转换对 BGR 用 RGB 原色矩阵 | A-CPU | 广色域图像颜色错，和视频路径不一致 | BGR↔RGB 只围绕色彩转换前三通道 | 0.5–1 天 |
| R-06 | P1 | 广色域 SDR / ProRes 10-bit 提交前已量化成 8-bit | A-注入 | 10-bit 文件仍带 8-bit 色阶断层 | 高位深按目标 pix_fmt 判定，提交 uint16 | 1 天 |
| R-07 | P2 | 自定义 HDR 描述符被固定 HDR10/HLG 标签覆盖 | B-静态 | P3+PQ / full-range HDR 像素与标签不符 | 元数据从描述符构建，HDR static metadata 单独添加 | 1 天 |
| R-08 | P1 | 中途取消已写一帧就报告完成 | A-注入 | 不完整视频/序列被视为成功，可能交付 | cancelled/partial/complete 分离 | 1 天 |
| R-09 | P1 | 视频全部渲染失败仍导出全黑并报告成功 | A-注入 | 错误成果被交付，丢帧数 UI 不可见 | 默认最终导出失败；显式允许补帧策略 | 1–2 天 |
| R-10 | P1 | 图片序列渲染/写盘失败跳过却成功 | A-注入 | 缺帧序列且进度/状态失真 | 失败帧中止或给明确 partial 状态 | 1 天 |
| R-11 | P1 | 串行图片序列缺少 GPU / 解码资源 finally 清理 | A-注入+B | 完成、取消、异常时持有资源；窗口/线程关闭风险 | 序列全入口 finally cleanup | 0.5–1 天 |
| R-12 | P2 | 并行序列 context 工厂失败不会回退串行 | A-注入 | 可串行导出的工程报 Render failed | 接入 factory_fallback 与原错误 | 0.5 天 |
| R-13 | P1 | 导出重建 eval context 丢失项目运动快门 | A-注入 | VectorBlur 预览/导出不同 | context 创建后注入 + 快门依赖进 cache key | 1 天 |
| R-14 | P1 | 导出遗漏全局 3D Scene Settings | B-静态 | 相机、光照、背景、输出开关回落旧默认 | 不可变 render context 带工程 3D 输出设置 | 2–3 天 |
| R-15 | P1 | 页面渲染输出读回固定丢弃 alpha | B-静态 | PNG/TIFF/EXR 无透明，预乘边缘暗 | BGRA/RGBA 读回合同、通道/预乘选项 | 2–3 天 |
| R-16 | P1 | 3D 材质不透明度 / alpha mask 无渲染作用 | B-静态 | 材质面板调透明无效，玻璃预设不透明 | alpha 因子、贴图 alpha、blend/mask 与 depth 策略 | 3–5 天 |
| R-17 | P1 | 有贴图时基色、金属、粗糙度 scalar 被覆盖 | B-静态 | 材质面板调值不起作用，模型外观不符 | 贴图与 scalar 按合同相乘，override 模式明确 | 1–2 天 |
| R-18 | P1 | 材质贴图色彩空间丢失、线性输入二次 gamma | B-静态 | linear EXR / 图节点 override 材质被误解码 | 保留 descriptor、按 slot 色彩空间解码一次 | 2–3 天 |
| R-19 | P1 | metallic_in 与 roughness_in 争用同 ORM sampler | B-静态 | 同时连接只剩一张纹理，参数彼此污染 | 分离 sampler 或显式 GPU 打包 ORM | 1–2 天 |
| R-20 | P2 | 材质缓存未按 program 区分 uniform 状态 | B-静态，需模型复验 | 同材质不同 shader 变体可能丢贴图/自发光 | program identity 纳入缓存或切换重置 | 1 天 |
| R-21 | P2 | 体积文件改了仍复用旧 GPU 内容 | A-注入 | 密度文件热更新不反映 | 内容签名加已解析文件 stat | 0.5 天 |
| R-22 | P1 | 动态噪声/粒子/帧表达式被判为静态，只能导出单帧 | A-CPU+B入口 | 动画图无视频/序列出口 | 统一 frame-dependency capability，不列少数类型 | 1–2 天 |

| R-23 | P1 | position/motion等数据AOV导出PNG/TIFF裁切有效数值 | A-CPU | 负向运动/世界坐标丢失，外部重建不可用 | AOV数据合同限制格式/显式可逆编码 | 1–2 天 |

依赖：R-02依赖R-01先解除上传失败遮蔽；R-03/04/05/06/07共用输出色彩合同，应一起回归；R-08/09/10共用导出状态与完整性；R-15/23共用通道/数据读回；R-16/17/18/19共用材质能力/schema；R-20待混合shader模型复现确认再实施。

P1 为明显功能失效/错误输出，建议优先修复后再做性能优化。工时是单工程师实现与 focused regression 的粗估，不含完整跨平台验收，根计划可按依赖重新排期。问题不应仅在测试 harness 中修；必须覆盖真实 GPUResourcePool、真实编码器以及 UI 状态消费。

## 详细问题、解决步骤与验收

### R-01：Paint 和 AI 结果上传的数据位宽不匹配

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/filter_evaluator.py:986`、`:998`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/base_evaluator.py:1667`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/gpu_resource_pool.py:964`。

**前提/症状**：任意存在笔触的 Paint，或 AI remove 等命中 CPU 结果并回写 GPU。`acquire_texture()` 默认为 `f2`（RGBA 每像素 8 字节），上传数组为 float32（每像素 16 字节）。真实 8×8 生产 pool 得到 `_moderngl.Error: data size mismatch 1024 != 512`，Paint 返回输入直通；AI `_upload_ai_result_texture()` 返回 None。失败时先取得的纹理也没有归还 pool。测试 `test_paint_node.py` 使用自定义 `_Pool.acquire_texture(... dtype="f4")`，掩盖生产默认值。

**解决**：1. 统一 CPU→纹理上传 helper，从目标 `tex.dtype` 或 render_mode 明确选 numpy dtype。2. 需要 f32 fidelity 的导出走 explicit `dtype="f4"`，预览可转 float16。3. Paint result 和 mask 两个输出同修；AI 缓存、clone/erase/solid 全覆盖。4. 获取成功、write 失败时 finally 归还新 texture，输出 structured diagnostic，不能仅日志+直通。5. 用真实生产 pool 替换/补充测试里的 fake f4 pool。

**验收**：8×8 和真实 2K 笔触可见；mask 有正确覆盖；AI 缓存命中能输出；无 data-size 异常；100 次失败注入后 active_count 不累增；预览/导出各验证一次。

### R-02：Paint 预乘时 alpha 被平方

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/filter_evaluator.py:948`。

**前提/症状**：straight-alpha 输入（如半透明 PNG/Constant）接 Paint，只需存在一个笔触，即使笔触不覆盖某像素。代码对全 RGBA 做 `img * img[...,3:4]`。隔离 f4 上传后的真实 GPU 读回：输入 `[0.2,0.2,0.2,0.5]`，未画区域输出 `[0.1,0.1,0.1,0.25]`，正确为 `[0.1,0.1,0.1,0.5]`。R-01 当前会掩盖它；修 R-01 后仍必须修本项。

**解决**：只对 `img[..., :3]` 乘 alpha；alpha 保持原值；clone/erase/solid 在预乘域执行。避免修改输入只读 view。统一 premult/straight helper，并保存 `alpha_premultiplied=True`。

**验收**：alpha 0、0.5、1 的未画区域原 alpha 不变；正常笔触计算符合 over；半透明输入接 Merge 与直通对照边缘一致。

### R-03：视频 limited range 重复压缩

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:545`、`:1005`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/export/video_encoder.py:2082`、`:2144`；provider range 在 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/color/color_transform_provider.py:542`。

**前提/症状**：默认 REC709_VIDEO 即 limited-range 输出。合成先 transform_color 到 limited RGB 信号，再乘 255；encoder 明确认为输入 RGB full range（src JPEG），再次压成 YUV MPEG legal。实际 PyAV 转换：线性黑 → RGB 16 → Y=29，线性白 → RGB235 → Y=217；正确 Y 黑白为 16/235。HDR BT.2020 numpy 路径同样将先压过的 RGB 再压一次。

**解决**：1. RGB 中间 descriptor 与最终容器 descriptor 分开，RGB 的 primaries/transfer 相同但 range=FULL。2. YUV 打包层唯一负责 legal range；stream tag 保留用户目标 range。3. 串行、并行、图片与视频 helper 不共用含混 range 合同。4. 实际编码后解码测 black/white、灰阶/色条，防止只断言标签。

**验收**：默认 8-bit Rec709 黑/白 Y16/235；10-bit limited Y64/940（允许编码器误差）；full-range 0/max；黑到白渐变不二次缩窄；serial/parallel 相同。

### R-04：BT.2020 编码辅助函数通道序错

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/export/video_encoder.py:2064`、`:2126`；合成提交 BGR 在 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:550`。

**前提/症状**：合成 HDR10/HLG 提交 bgr48le。`_convert_packed_frame` 最后把 original `data` 交 `_convert_rgb_to_yuv_bt2020`，后者按第 0/1/2 通道为 r/g/b。真实 PyAV 辅助转换：红 BGR `[0,0,65535]` 的 limited 10-bit Y=116，而正确 RGB 同色 Y=294。绿不变，红蓝交换，造成部分测试只用灰色时不暴露。

**解决**：BGR 入辅助函数前显式前三通道转 RGB；或 helper 接 `source_format` 并内部统一。uint8、uint16、rgb/bgr 都有同义测试，alpha 当前 unsupported 必须保持显式诊断。

**验收**：BT.2020 原色条实际 encode→decode与参考一致，红 Y约294、蓝 Y约116；sdr wide-gamut BT.2020 同修；不能只读取 primaries tag。

### R-05：图片序列原色转换对 BGR 用 RGB 矩阵

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:1384`；原色矩阵函数 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/color/color_transform_provider.py:469`。

**前提/症状**：P3/BT.2020 等跨原色的 PNG/TIFF 序列。注释称“channel-order agnostic”，实际矩阵有 RGB 语义。P3 红色 BGR16 实际 `[0,0,62886]`，正确 `[9080,13125,60127]`，通道最大偏差13125/65535。视频路径已在 transform 前后 swap，图片路径遗漏。

**解决**：包装“仅前三通道”的 BGR→RGB→transform→BGR；若升级 BGRA，要保持 alpha 末位。删掉误导注释，provider 合同固定 RGB，输入语义由 adapter 处理。

**验收**：红绿蓝、非对称色卡及灰色矩阵对照；BT709→P3→BT709 回环；图片与视频同 descriptor 保持一致。

### R-06：10-bit SDR 被预先量化 8-bit

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:411`、`:551`、`:1009`；对话框 codec pix_fmt `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_render_dialog.py:89`。

**前提/症状**：Display P3 SDR 自动升级 yuv420p10le，或 ProRes 422 10-bit。CPU打包只按 `is_hdr` 决定 uint16，所以 wide-gamut SDR 仍为 uint8 bgr24。1024级 P3 渐变仅剩237个唯一值；10-bit编码器无法恢复丢失色阶。

**解决**：选定 pix_fmt 后解析 bit depth，并设 `high_depth_input`，HDR仅决定transfer/meta；10/12/16位走bgr48le满16bit打包。black placeholder与parallel stage同改。UI明确最终位深，按codec支持预检。

**验收**：1024级无损/高质量渐变实际可分辨 >256码值；ProRes非HDR提交uint16；8bit SDR保持uint8；preview/export量化各有可解释合同。

### R-07：HDR元数据固定覆盖自定义描述符

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:421`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/codec/color_metadata_config.py:167`。

**前提/症状**：项目偏好支持 primaries/transfer/range 组合，用户选 P3+PQ 或 full range HDR；像素按该descriptor编码，但元数据一律hdr10()/hlg()（BT2020、limited）。此外mastering/MaxCLL固定1000nit和真实HDR范围不必一致。

**解决**：从descriptor构造primaries/transfer/matrix/range，只有适用HDR10时附static metadata；mastering/MaxCLL提供显式目标或从工程设置读取；无可靠数据时不编造实测峰值。无支持codec/pixfmt时预检阻止。

**验收**：P3PQ、BT2020fullPQ、标准HDR10/HLG各自tags与像素同源；输出峰值metadata与目标设置一致；预检列出编码器不支持组合。

### R-08：取消后不完整成果报告完成

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:573`、`:740`、`:1118`、`:1262`；worker `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_worker.py:136`。

**前提/症状**：点取消时已经写入1帧。`cancelled`只认written==0；parallel直接写success=True。真实生产序列控制流程注入取消：请求5帧、写1帧，返回success=True,error=""，UIfinished路径显示完成。源码注释将“written>0”误称fully-rendered，已有历史测试只证明串并行同错，并不能代表正确交付语义。

**解决**：结果明确status=complete/cancelled/failed/partial；complete必须written==total且无失败。部分文件可保留供续传，但必须带partial状态和帧清单，按用户选项处理；UI不能调用成功toast；取消恰好落在最后完整帧后允许complete。

**验收**：0帧、1/N帧、N/N帧分别验证；串并行语义相同；取消后没有仍运行的编码/渲染线程；未授权覆盖已有成果时避免清理旧文件。

### R-09：全部帧失败仍全黑成功，诊断未传UI

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:489`、`:1039`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_worker.py:136`；单帧诊断消费者 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_frame_renderer.py:202`。

**前提/症状**：渲染返回None（媒体运行时损坏、GL失败、节点结果错误）。导出用前帧/黑帧占位维持时码，但即使所有3帧均None，仍success=True，frames_dropped=3；worker只把path发finished，用户看不到丢帧/diagnostics。

**解决**：最终导出默认fail-fast并保留明确原错误；“容错补帧”作为显式策略，受丢帧比例/连续失败阈值约束；0成功帧永远failed；Result/worker/UI传递丢帧位置、节点diagnostics、原因。预检已有静态缺媒体阻止，但不能覆盖运行时问题。

**验收**：首帧、中间帧、全帧失败分别注入；默认导出中止；容错策略warning可见且summary列具体帧；正确帧不无故压缩时间线。

### R-10：图片序列存在缺帧仍报成功

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:704`、`:722`、`:1211`、`:1250`。

**前提/症状**：某帧None或FileIO.save_image=False。串行continue、并行sink return；只要有1张则成功，frames_dropped一直0。注入中间帧失败结果：仅x_0001.png和x_0003.png，success=True,written=2,error=""。parallel consumed进度还可到100%。

**解决**：渲染/保存失败转可辨错误并中止；或显式partial状态保存失败帧列表；结束校验written==requested且文件集合连续；进度区分rendered/encoded/written。写盘用临时文件+原子替换，清理仅本次新文件，避免覆盖老序列后失败删除原成果。

**验收**：None、False、exception、disk-full、权限失败均不给complete；成功文件数和连续编号完整；串并行进度和错误一致。

### R-11：串行图片序列缺少finally清理

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:690`–`:753`、`:1293`；worker `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_worker.py:71`。

**前提/症状**：parallel_frames=1或parallel退回serial后，创建persistent CompositeFrameRenderer，序列返回没有_cleanup_frame_renderer（视频有finally）；worker.run也没有finally调用。cleanup spy验证返回后cleaned=False且self._frame_renderer仍持有。GC不能代替渲染线程按顺序release纹理/节点/解码session，跨线程析构更不可依赖。

**解决**：序列串行入口try/finally _cleanup_frame_renderer；高层worker也用明确生命周期防早退出（避免重复清理应幂等）。先释放provider/node GPU引用，再销毁own context；wrap主context不能release。并行teardown保持owner-thread。

**验收**：正常、取消、写入False、异常、空range路径各验；resources/decoder session归零；重复20次序列导出resident memory回到稳定基线；不从UI线程清理。

### R-12：并行图片序列工厂失败没有serial fallback

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:1222`、`:1270`。

**前提/症状**：GL context/VRAM使所有并行worker factory失败。state初始化factory_failures但调用render_frames_parallel未传factory_fallback；计数永远0，fallback分支永远不触发。注入factory失败两次时，错误只有Render failed，serial_calls=[]；video stage有该回调，序列遗漏。

**解决**：接同video的factory_fallback，错误保留首次具体原因；只在0写入且非取消、非shader/graph领域错误的可降级context失败时回退serial；计数在锁下更新；若serial也失败诊断联合呈现。

**验收**：全部factory失败时serial执行且完成；部分worker失败剩余可用worker正常；取消不触发降级；资源释放在创建线程。

### R-13：Output项目快门导出丢失

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_export_renderer.py:236`、`:184`、`:490`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/vectorblur_node.py:57`。

**前提/症状**：Output.motion_blur_shutter非默认且VectorBlur.use_project_shutter=True。render_and_readback先给旧ctx赋值，随后_ensure_gpu_resources总是_install_eval_context创建新ctx，motion_blur_shutter=None。注入真实render_full_frame流程观测请求0.9，节点effective_shutter回落0.5。第一次没有旧ctx也同错；multi-pass入口根本没有这段赋值。

**解决**：初始化ctx后从当前snapshot注入，统一readback/gpu/multipass；每帧覆盖而不是旧ctx更新。将项目快门作为VectorBlur上下文依赖纳入cache key，Output位于下游时普通dirty传播不会自动使上游VectorBlur失效。

**验收**：项目快门0、0.25、0.9与node自身0.5对照；use_project=False保持自身；改变项目值立刻影响预览/导出并且cache无陈旧结果；full/tile/multipass一致。

### R-14：导出遗漏工程全局3D设置

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py:3595`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_export_renderer.py:184`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_frame_renderer.py:751`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/graph_snapshot.py:102`；预览注入 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_renderer.py:842`。

**前提/症状**：当前Scene3D真实节点是纯renderer，scene3d注释明确节点self.parameters只有旧默认，真实用户参数从ctx.global_3d_scene_settings读。预览request接此dict；GraphSnapshot没有viewer_state/3D设置；FrameRenderer序列化重建只snapshot nodes；ExportRenderer新ctx仅GL/provider/config，因此导出回落旧默认，相机导航/默认灯/背景/IBL/MSAA/output_*都可能与成品预览不同。连接Camera/Light可覆盖部分相机/灯，但不弥补背景/IBL/输出开关。

**解决**：定义不可变CompositionRenderSettings，来自持久化viewer_state.default_3d_scene（只带成品参数，排除临时导航/selection/isolate）；所有导出入口（图片、序列、视频、multi-pass、headless、AI输入）按同合同安装ctx；字段纳入相关cache key。输出_*和show_*预览参考设置分离读取，不能直接复制整个UI状态。

**验收**：工程设置和旧默认差异大的3D场景截图对比（相机yaw、ambient、IBL、bg_opaque、output_grid）；保存重开后相同；connected Camera/Light优先级相同；导出不包含选择轮廓/调试网格。

### R-15：页面导出固定剥除alpha

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_export_renderer.py:1577`、`:1441`、tiled stitch读取`:1048`；已有RGBA路径 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/file_output_node.py:416`、`:454`。

**前提/症状**：Keyer、Mask、Scene3D透明背景后，从页面渲染输出保存PNG/TIFF/EXR。full/tile CPU结果只BGR3，alpha不可恢复，预乘颜色还可能直接作为不透明色保存，边缘变暗。FileOutput节点支持channels/alpha_mode与RGBA，说明存在解决路径，但不能当页面Render Output已支持。视频opaque格式本就无alpha，不能静默丢弃可承载格式的alpha。

**解决**：readback明确channels/alpha representation，opaque预览仍可3ch；最终透明readback为BGRA float且transfer先unpremult后encode，再按target alpha合同premult；tile stitch保留alpha；UI PNG/TIFF/EXR有通道与straight/premult选择。复用FileOutput转换/写入合同；ProRes4444和透明video仅在encoder支持时开放。

**验收**：透明圆/半透明梯度导出重导入alpha逐像素匹配；PNG棋盘不是烧进图片；EXR保留alpha及>1HDR；premult/straight合成边缘同参考。

### R-16：材质面板不透明度没有实际渲染效果

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/material_panel.py:86`、`:357`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_model.py:599`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6641`；scene material透明排序`:1208`。

**前提/症状**：opacity调到0.3或应用玻璃预设，bridge将alpha字段写入MaterialData，但PBR fragment没有alphauniform，也不读baseColorTexture.a，固定fragColor=vec4(color,1)。beauty绘制循环不按材质透明开blend。MASK不discard；导入材质conversion也没有alphaMode/alphaCutoff/baseColorFactor.a映射。排序函数“透明保持原序”不能实现正确透明。

**解决**：完整OPAQUE/MASK/BLEND合同：因子与贴图alpha，MASK discard cutoff，BLEND预乘输出+ONE/ONE_MINUS_SRC_ALPHA、透明depth-write off/back-to-front sort；双面/culling按材质；导入保留alphaMode/cutoff/factor；AOV/shadow/pick对mask一致。

**验收**：opacity0真的不可见、0.3看到背景、1不变；交叠透明远近顺序稳定；叶片mask无矩形实片；导出与预览相同；shadow/pick不选到透明洞。

### R-17：有贴图时材质scalar被覆盖

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6516`、`:6525`、`:6633`；scalar uniforms `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py:4605`。

**前提/症状**：有base_color、ORM、emissive贴图的模型。shader用贴图值覆盖u_albedo、u_metallic、u_roughness、u_emissive；面板对应scalar虽存储和设uniform，最终不起作用（无贴图时有效）。模型导入的factor语义也未组合，明显影响同贴图多材质。

**解决**：define imported/multiply/override合同；默认纹理×scalar因子，roughness做安全下界；emissive factor/intensity各清晰；如果用户希望绝对替换材质，显式mode移除旧贴图或只选其一。

**验收**：同一白贴图调RGB可见；相同ORM粗糙度因子0.25/1差异；emissiveFactor0+emissiveTexture不发光；预览/导出按glTF材质factor一致。

### R-18：贴图颜色空间控制未进入渲染合同

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_model.py:627`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/model3d_node.py:970`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_texture_loader.py:180`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6518`、`:6634`。

**前提/症状**：材质面板slot可选sRGB/linear，但bridge只保留path/role/uvset，不保留slot.color_space；loader保持原值，PBR全部color slot pow2.2。线性EXR或已IDT的ImageInput接base_color_in也只传glo丢TextureResult.color_space，shader再pow2.2，发生二次gamma。sRGB目前也只有2.2近似，低亮度不精确。

**解决**：TextureDescriptor保留ColorSpaceDescriptor；ModelPayload override保留颜色/alpha/data语义，不仅intglo；load或shader只做一次正确IDT，采用项目provider/等效GLSL IEC piecewise，data slot永不gamma；slot UV/color字段进cachekey。旧工程迁移默认和当前外观有明确兼容策略。

**验收**：相同材质使用srgb8bit与linear EXR等效；0.5linear连接override仍0.5而不是0.2176；面板选择linear与sRGB有可预期差异；normal/ORM不改数据；HDR>1不clamp。

### R-19：独立metallic与roughness输入相互覆盖

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py:1060`、`:1129`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6526`。

**前提/症状**：Model3D同时连接metallic_in与roughness_in。两者映射slot="orm"，循环后者覆盖同textureunit。PBR从这最后一张图同时读G粗糙、B金属，也可能从R误作为AO；独立灰图无法独立控制两个属性。

**解决**：分别sampler与hasflag，并定义单通道读取；或GPU显式生成ORM（AO R、roughness G、metallic B），保留来源/通道schema；只有实际AO连接/声明ORM有O时采AO，不把未知R当遮蔽。

**验收**：metallic黑/roughness白=粗糙非金属；互换=光滑金属；单独接任一时另一scalar保留；AO默认1；连接次序不影响结果。

### R-20：材质cache key缺shader program身份

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py:4560`、`:4641`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_state.py:36`。

**前提/症状**：同MaterialData被非蒙皮/蒙皮或instanced等不同program的mesh连续使用，has_tangent相同。key只material/texture/params/has_tangent，program换了仍matches；uniform是program-local，第二program的sampler位置/has_map/intensity未正确设置，可能沿用0或历史材质。已修has_tangent项不能覆盖program差异。未用真实混合模型做视觉复现，因此证据B、建议先focused验证再改。

**解决**：cache区分GL texturebind与programuniform两层，或key含programglo；program切换至少初始化所有材质uniform（texturebind可复用）。重置/上下文重建清samplerprogram集合。

**验收**：同材质在skinned/non-skinned/instanced连续渲染与关闭cache参考像素一致；有贴图、无贴图、normal/emissive变体全测；统计cache收益不能换来输出差异。

### R-21：体积文件热更新命中内部旧内容

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/volume3d_node.py:195`、`:242`、`:246`；runtime_file_signature`:151`。

**前提/症状**：播放头不变，同path的.npz密度文件被覆盖。外部runtime_file_signature变了会促使重求值，但内部content_sig不含mtime/size，只path/frame/params；有旧GPUtexture则提前return、load_volume不运行。真实节点算法+fake上传小复现：density1→2，file signature确实变；upload_count仍1、alpha仍0.6321206，正确0.8646647。

**解决**：先解析序列路径，os.stat纳秒mtime/size进内部content_sig（缺文件/出错也invalid）；避免int秒mtime遗漏同秒修改；来源缓存和节点内缓存同维度。

**验收**：静态.npz原地覆盖/同秒同size变化可刷新；sequence当前帧文件更新可刷新；文件删除不输出旧图；不修改时同纹理重用。

### R-22：动态procedural与表达式图被静态导出

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:56`、`:72`、`:198`；入口 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/composite_controller.py:991`；dynamic capability `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/noise_node.py:78`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/particle_node.py:362`。

**前提/症状**：独立合成noise.animate=True、particle，或transform表达式translate_x="frame"，没有video_frame/keyframe。真实detect_composite_mode三个输入都IMAGE，resolve_range=(0,0,24)，页面Dialog按IMAGE只提供单图。节点本身支持按frame动画，但渲染输出无法选择范围。

**解决**：调用统一frame-dependency分析/节点capability，不复制少数类型清单；检查expressions引用frame/time、序列文件pattern、particle、model自动动画；UI允许用户手动选择静态单帧/范围，duration无metadata时仍可输入范围；range来源包括表达式/工程工作区，不能假造动画长度。

**验收**：noise.animate、particle、frame/time表达式、volume/image序列、model动画能选择视频与序列；非time表达式不被误判；用户可手动导出一帧或范围；默认range合理且可编辑。

### R-23：数据AOV使用显示格式时被无提示裁切

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:1392`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6259`、`:6276`；格式选择 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_render_dialog.py:399`。

**前提/症状**：position、motion_vector等含signed或>1数值的pass选择PNG/TIFF。当前已有非beauty保护，**不会**施加sRGB/PQ OETF，这个旧风险不要重复记为bug；但dtype转换仍直接clip(0,1)。真实helper输入[-2,0.5,3]，PNG输出[0,127,255]，TIFF[0,32767,65535]；EXR保留[-2,0.5,3]。negative motion、世界坐标不能恢复。normal当前编码normal*0.5+0.5，可存0..1；depth为规范化深度，也要明确linear/near/far元数据，不能混淆世界距离。

**解决**：每个AOV声明semantic、范围、通道数、精度、可逆编码；position/motion/deep默认要求float EXR，并在preflight禁用不支持格式；如用户只想可视化PNG，显式选择visualization mode（附比例/偏移），不能作为原始pass。4通道/coverage与alpha读回问题一并按R15升级，输出保持精确数据。

**验收**：负向/正向运动vector及大于1、负worldposition在EXR中逐值保留；PNG原始数据模式被明确阻止或可逆metadata足够恢复；normal/depth不额外OETF；AOV导出不按beauty色彩偏好改值。

## 能力不足与建议优化（不直接归为已证 bug）

| 编号 | 不足及证据 | 当前可复用路径 | 解决建议 | 验收 |
|---|---|---|---|---|
| N01 | 合成Render Output无音频开关、source audio/mix接线；render_video没有add_audio_stream/write_audio_samples调用 | shared FFmpegVideoEncoder已有audio API；Edit时间线已有混音/区间语义 | 输入有audio先提示输出静音；添加保留输入/时间线混音选项，经既有AudioMixer按范围裁切重采样写入；retime需要明确音频stretch/offset合同 | 含音频测试视频输出audio stream，音画同步，非零start和retime/cancel各验 |
| N02 | 图片序列只有format，settings.options={}；16-bit PNG、EXR压缩/half/full、channels、alpha选项不可达 | FileOutput节点已经有format/depth/channels/alpha_mode和写入选项 | 抽公共输出schema和转换服务，页面对话框调用，避免第三套转换 | 16bit PNG实际uint16、EXRhalf/full可读取、unsupported组合被预检 |
| N03 | Volume3D为CPU固定前向投影的.npz吸收；.vdb显式依赖拒绝；pointcloud CPU2D正交点图，没有接Scene3D几何/相机 | volume_grid/point_cloud纯数据模型已有；相关节点DESCRIPTION已说明MVP | UI明确独立2D rasterizer能力；若要3D交互，ScenePayload+GL_POINTS/volume pass，通过统一camera/occlusion；OpenVDB先做跨平台packaging gate | 相机旋转/深度遮挡有效；volume预算内，未依赖时明确错误 |
| N04 | 材质高级字段transmission/clearcoat/sheen/subsurface/normal_strength/UVTransform有model序列化，但当前PBR无完整消费；面板玻璃preset只emit4个scalar | material_model版本与override已支持存储 | 首先声明shader能力，未支持项禁用并显示原因；玻璃preset在R16实现前不宣称完整玻璃；按feature增shader/AOV/export parity | 各可用控件产生可证像素变化；不支持项有能力标识 |
| N05 | pool有acquire_tiered_texture(export→f4)，大部分effect还走legacy.acquire_texture默认f2；linear_f32读回≠全链32bit计算 | GPUResourcePool tiering API已有 | 在RenderPlan附精度需求，HDR数据AOV/最终EXR按需f4，预览保f2；明确定义precision选项/预算；先测再迁移，避免盲目翻倍显存 | 数值gradients与CPU参考误差达标，4K内存可控、HDR峰值不溢出 |
| N06 | RenderExport blocking wait可到120s；shared context route仍同步等待，是否UI线程取决于controller入口 | CompositeRenderer command queue+done_event已有 | 复核所有调用线程；图片导出以QObject signal/future完成，UI只接进度与结果；对超时取消排队任务而不是让后台继续做已遗弃结果 | 4K图片导出期间UI可拖动/取消，timeout任务可退场，结果不会覆盖后续请求 |
| N07 | 预检对cycle/output/missing media有防线，但codec/transfer/depth/alpha/音频与模型能力缺少同一可视报告 | graph_preflight.py及node diagnostic badge已有 | 在现有report补目标格式能力诊断；按真实选项组合而非全局泛化警告；dynamic运行失败经worker传回 | 一个preflight可定位节点与不支持输出组合，并给可执行替代选项 |

| N08 | Cryptomatte目前只构建manifest，metadata.status=manifest_only；object_id仍byte编码pick ID，未实现标准多层hash/coverage EXR writer | render3d/cryptomatte.py已有hash/manifest纯数据合同，Scene3D可输出object_id | UI明确“ID matte / manifest”与完整Cryptomatte交付的能力差别；实施float32 hash/coverage排名层与EXR metadata writer，避免half精度/三通道丢失 | 外部Nuke/Blender读取标准layer/manifest，抗锯齿边缘coverage正确，未知名称稳定 |

N06 是线程/响应性能复验项，本文没有在完整UI线程证明卡顿，不能直接作为已确认产品bug。context实际native对象释放也需独立长测：ExportRenderer目前ctx=None而不显式release，私有standalone context与wrapped共享context应分ownership，不能用同一cleanup处理；不凭源码单独断言泄漏。

## 覆盖与尚无法验证

已检查主调用链：UI CompositeController → CompositeRenderDialog → CompositeRenderWorker → CompositeRenderOutput → CompositeFrameRenderer → GraphSnapshot → CompositeExportRenderer / GraphEvaluator → GPUResourcePool → readback → shared FileIO / FFmpegVideoEncoder；同时检查preview CompositeRenderer eval context、MotionBlur上下文、Paint/AI上传、3D Model/Scene/PBR/material loader/caches/volume/pointcloud。

已验证的失败/取消路径包括：序列中途取消、某帧None、并行context factory异常、Paint/AI上传位宽、资源cleanup遗漏与体积source改动。真实GPUPool复现和CPU/PyAV实际转换区别于fake frame的业务状态注入，见证据口径。

尚待根测试或后续复验：真实glTF/FBX alpha/blend/mask视觉比较；共享材质跨program模型；所有3D AOV及cryptomatte外部DCC读取；真实audio多声道与retime；Windows/Linuxcompute shader（本机macOSGL4.1没有compute）；多GPU/VideoToolbox/nativeGPUlease所有故障；所有容器codec高位深与alpha组合；真实4K持续播放内存、取消重入、多窗口关闭时机；图内所有effect的像素参考金标准。现有单测通过不能替代这些验收。

建议根计划先合并R01/R02与R03–R07色彩合同，再修R08–R13交付完整性/生命周期；R-14–R19解决3D与透明输出一致性；R-20–R22缓存和动画识别；最后按实测4K数据优化N项。本文不复述旧审计报告，只保留当前源码与本次运行可证内容。
