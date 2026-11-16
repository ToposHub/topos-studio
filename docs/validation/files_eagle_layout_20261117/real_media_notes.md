# 用户提供目录的真实媒体补测

日期：2026-11-17。使用现有 `decode_preview`、`_LibavSession`、`ToposMovieSession` 做只读CPU解码；不创建GPU context、不启动声卡，也不把这些结果等同于连续播放、显示色彩或设备验收。

## 范围和实际结果

| 范围 | 数量 / 结果 |
| --- | --- |
| 用户照片目录（名称略）顶层原图 | 123 JPEG，全部预览解码和宽高比通过；120张4000×6000竖图、3张6000×4000横图 |
| 视频目录的附属图片 | 14 PNG，全部预览解码和宽高比通过 |
| 常规视频 | 34 MOV/MP4，通过RGB首帧、YUV中点、反向seek回首帧以及非16:9显示框比例检查；包含H.264、H.265、ProRes和DNxHR的2K/4K、8/10/12-bit样本 |
| Topos视频 | 12 MOV，RGB/YUV抽帧和前后seek均通过，但非16:9盒的RGB回退输出全部发生比例拉伸；已报告给主agent，未修改核心实现 |
| 专业RAW | 1 BRAW、1 R3D：现有support_matrix声明缺SDK，不计为解码通过 |
| 8K ProRes RAW HQ相机片 | 1 MOV，打开约0.55秒，首帧RGB在35秒内未完成；全矩阵40秒deadline亦超时，不计为通过 |

合计137张图片、49个视频条目。全矩阵耗时131.186秒；机器可读结果与进度输出（`real_media_results.json`、`real_media_probe.txt`）含真实文件名，**仅本地保留、不入仓**。全部被测可见源文件的size和mtime前后相同，脚本只写本验证目录。

隐藏`.cache`中的204张派生webp未重复当原图测试。照片目录另有5个`.topos`，经只读ZIP目录检查均只含`project.json`，作为工程侧车保留，没有误当视频。

照片EXIF方向均为1，实际样本没有覆盖orientation=6/8的旋转分支。JPEG解码中位约207ms，最大约307ms（本机本轮顺序CPU读取，不是冷缓存性能承诺）；UI应在后台按显示尺寸解码缩略图，不在主线程全尺寸解码整目录。

## Topos RGB比例问题

`src/shared/playback/topos_player.py::_frame_rgb_locked` 当前按输入box直接 `reformat(width=size[0], height=size[1])`，例如1920×1080源在320×240盒输出320×240。YUV路径保留1920×1080或auto2k尺寸，问题落在RGB回退分支。本轮只记录；可复用等比fit逻辑做局部核心修复，并补真实Topos+RGB回退的非16:9盒回归。

## 相机ProRes RAW边界

`H034C0035_240410_P05R39.MOV`：11,639,173,602字节、8192×4320、24fps、27.2917秒、655帧，codec为`prores_raw`（RAW HQ/aprh）。PyAV能读元数据，但报告像素格式未探明；首次`frame_rgb`在35秒子进程上限内没有返回。见[阶段定位](camera_video_diagnostic.txt)。这是有界超时观察，不据此声称文件损坏或所有后端均不能解码。需要明确能力诊断/后台超时策略，不能把常规ProRes通过推广到ProRes RAW。

## 补齐此前跳过的真实YUV回归

仅在测试进程内将`tests.real_media.TEST_VIDEOS`指向用户提供目录并设`STRICT=True`，没有改写仓库测试的持久素材路径：

```python
from pathlib import Path
import pytest
import tests.real_media as media
media.TEST_VIDEOS = Path('/Volumes/Project_1/开发项目/topos-color-multitask /topos-color01/tests/test_videos')
media.STRICT = True
pytest.main(['-q', 'tests/ui/test_gpu_yuv_frame.py'])
```

结果：**9 passed，0 skipped（2.14秒）**。此前缺失的ProRes422LT、DNxHR HQX 10bit、4K H264三个样本已实际覆盖；[输出](real_yuv_tests.txt)、[JUnit](real_yuv_tests.xml)。测试读取YUV数组，不执行GPU上传或绘制。

## 布局验证建议

这组素材以竖图为主，网格封面应采用contain完整展示，避免原卡片16:10 cover裁掉上下内容；信息栏也应完整显示选中图。在截图夹具中选十余张原图，后台以`QImageReader.setScaledSize`或`PIL.Image.thumbnail`生成有限预览，再交给既有模型/封面入口；不要以截图为由写入真实库或覆盖原文件。

重跑命令：`QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/files_eagle_layout_20261117/real_media_probe.py --photo-root <照片目录>`（私人目录路径经参数显式传入，不入仓）。脚本逐视频隔离，最长40秒，不遍历解码全片；输出会覆盖本验证目录内的本次报告。
