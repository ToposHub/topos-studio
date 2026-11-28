# Eagle布局参考、真实素材补测与四布局验证

> 文中提及的部分旧计划或测试附件已清理，正文保留当时的记录。

日期：2026-11-17。起点为 `fix-ui @ 7f025973`，最终状态为本轮工作区（包含上一轮Files复核的小修）。

## 实际观察与实现

通过本机Eagle窗口观察了自适应、瀑布流、网格及列表，包含网格“适应/填充”、缩略图大小、名称、侧栏和检查器关系。实现采用Topos既有模型/服务/选择和主题，不复制Eagle的数据库、插件或程序代码。

| Eagle布局 | Topos交付 |
| --- | --- |
| 自适应 | 原比例等高行，按可用宽度铺满；末行保持合适尺寸，不把少数素材放得巨大 |
| 瀑布流 | 等宽列按最短列接续，高度由原画幅决定 |
| 网格 | 等大图片区、名称对齐，默认完整适应；“更多→布局方式→网格填充方式”提供显式裁切填充 |
| 列表 | 懒加载真实缩略图；名称、尺寸、扩展名、类型、大小、修改日期、评分，表头排序与调宽 |

切换入口为Files上方布局下拉框，“更多”菜单也提供布局入口。密度滑杆控制图库目标尺寸/列表行高；布局共享多选、拖拽URL、键盘、预览身份、过滤和排序。常用位置移至侧栏上部，右侧信息栏可滚动，标记位于技术字段之前，图片不显示空时长/帧率行；封面resize不重填编辑中的标签/备注。保留Topos橙色选择、黑色1px分隔线与窄窗口开合。

实现与偏好兼容详见[架构说明](../../architecture/files-gallery-layouts.md)。新增布局用稳定字符串持久化，旧卡片0迁网格、旧列表1迁列表，新用户默认自适应。

## 用户目录的真实媒体结果

| 测试 | 结果 |
| --- | --- |
| 123张照片 + 14张PNG | 137张全部解码/比例通过；120张照片为4000×6000竖幅 |
| 34常规视频 | RGB/YUV抽帧、前后seek、比例通过；覆盖2K/4K、H.264/H.265/ProRes/DNxHR及8/10/12-bit |
| 12 Topos MOV | 抽帧/seek通过；RGB回退在非16:9盒会拉伸，未计为比例通过 |
| BRAW/R3D各1 | 能力表缺SDK，未冒充支持 |
| 1个8K ProRes RAW HQ | 读元数据约0.55秒；首帧RGB超过35秒未返回，矩阵40秒截止亦超时 |
| 原YUV素材缺失项 | 本次视频目录在独立pytest进程中临时绑定，9 passed、0 skipped，原3项skip均补证 |

完整结果和重跑方法见[真实媒体说明](real_media_notes.md)与[真实YUV输出](real_yuv_tests.txt)；机器可读的 `real_media_results.json`、`real_media_probe.txt` 含真实文件名，仅本地保留不入仓。Topos RGB比例与8K RAW诊断已补入[后续整改计划§6](../../plans/2026-11-17-files-replacement-followup-plan.md)。读取和解码均为CPU路径，未创建GPU context或声卡，未做连续播放/HDR/跨平台验收。被测源size/mtime前后未变；隐藏派生webp未重复算作原图，`.topos`工程侧车未误当视频。

## 真实素材截图

同一组21项混合素材：9竖幅、3横幅照片、3 PNG和6种真实视频缩略图，按照片/静图/视频交错，以清楚显示四模式差异；它是只读虚拟展示位置，不冒充该混合目录真实存在或HTTP链路验收。

**截图与 `mixed_samples.json`、`mixed_thumbs/` 因含用户私人照片与真实文件名，仅本地保留、不入仓**（见 `.gitignore` 隐私保护条目）。本地核对项：

| 模式 | 本地截图 |
| --- | --- |
| 自适应 | `mixed_adaptive.png` |
| 瀑布流 | `mixed_masonry.png` |
| 网格适应 / 填充 | `mixed_grid.png`、`mixed_grid_fill.png` |
| 列表 | `mixed_list.png` |
| 760px / 480px | `mixed_narrow.png`、`mixed_compact.png` |
| 123张照片基线 / 优化后 | `photos_before.png`、`photos_after.png` |
| 矮窗口信息栏 | `photos_short_inspector.png` |

`photos_before`使用HEAD中的旧视觉模块；`photos_after`为本轮代码，具体模式以截图下拉框为准。截图使用真实源文件派生的有界缩略图和临时QSettings；不修改真实素材/原缓存，不启动旧Flask或GPU。

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/files_eagle_layout_20261117/prepare_mixed_samples.py --photos <照片目录> --videos <视频目录>
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/files_eagle_layout_20261117/capture_four_layouts.py
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/files_eagle_layout_20261117/capture_layout.py --variant after --photo-dir <照片目录> --video-dir <视频目录>
```

## 自动化回归

新几何与虚拟视图测试覆盖混合比例、无交叠、全项命中、尾页、metadata与晚到比例、10000条三模式虚拟化、resize锚点、实际Qt绘制、鼠标/键盘/框选；panel测试覆盖共享选区与MIME/预览身份、真实缩略图像素、grid-only裁切、偏好迁移、短窗滚动和编辑内容保留。

最终回归命令与统计见[regression.txt](regression.txt)、JUnit。测试进程内将`tests.real_media.TEST_VIDEOS`绑定到用户视频目录且`STRICT=True`，不把测试路径写进产品配置，不以缺素材skip缩减此组覆盖。没有把本组测试当成旧扩展可退役、原生GPU/音频设备或文件事务验收。

最终集合：**293 passed，0 skipped（17.48秒）**。随后开启平滑缩放呈现，相关画廊与四模式像素/交互测试另行复验；不改变几何、模型与媒体读取口径。

重跑：`QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/files_eagle_layout_20261117/run_regression.py`。

性能补测见[layout_performance.json](layout_performance.json)：100/1000/10000条合成元数据、三种图库模式，离屏Qt重排与可见项绘制；不读真实缩略图、不做IO/GPU。10000条resize中位约40～55ms，只绘15～19项。此数值验证虚拟化和元数据缓存作用，不作为60FPS、冷缓存或完整播放性能承诺。
