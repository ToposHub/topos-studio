# E 阶段第五批（E6）：音频共享设备互斥 + 截取作业预算/取消/安全输出

日期：2026-11-18 · 分支：fix-ui · 范围：播放音频的进程级互斥兜底 + 两个
片段截取入口换受约束的导出作业。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `audio_arbiter.py`（新） | `AudioOutputArbiter`：进程级音频输出唯一仲裁者——管线登记（`acquire`），新拥有者接管时旧拥有者经回调**退役**（不 join、不阻塞接管方）；陈旧释放不误伤新拥有者；纯 Python 线程安全。`shared_arbiter()` 进程单例 |
| `topos_audio.py` | `ToposAudioPipeline` 构造即登记仲裁者（`retire()` 只设标志 + 唤醒，线程自行收尾）；`stop()` 释放拥有权 |
| `libav_player.py` | `_AudioPipeline.start()` 登记仲裁者 + `retire()`；`stop()` 释放 |
| `clip_export.py`（新） | `ClipExportJob`：deadline 预算（默认 180s，超时按取消路径收尾并报"超预算"）；协作取消（terminate→有界 wait→kill）；stderr 只保留尾部 `max_capture` 字节（环形丢弃，异常源不吞内存）；**安全交付**——临时文件保留目标扩展名（muxer 推断）+ 成功 `os.replace` 原子改名，失败/取消/超时清理；单作业串行（重复 start 拒绝） |
| `topos_player.py` | `_export_clip` 换 `ClipExportJob`（此前裸 `subprocess.run`：无预算/不可取消/capture 无上限）；进行中再点一次=取消；`cancel_clip_export()` |
| `player_qt.py` | `execute_clip` 同款换轨（此前**直接写目标路径**——半成品可见 + 无预算不可取消）；进行中再点一次=取消 |

## 语义

* **切源互斥双层**：宿主级 `stop_other_players`（第一道，立即隐藏停播）
  + 音频仲裁者（兜底：`VideoProEngine` 与 `NativePlayerHost` 互相独立、
  两个活跃播放器时，同一默认设备上旧 sink 确定性退役）。
* **退役不阻塞接管方**：`retire` 只设标志 + 唤醒（管线线程 4-20ms 轮询
  自行收尾，sink/音源在其 finally 关闭）——与 base_player 有界泄漏纪律
  同款，不从接管线程 join。

## 验证（`tests/ui/test_audio_arbiter_e6.py`，10 项全绿）

* 仲裁者：接管回调触发/拥有权转移/陈旧释放忽略/自己重登记不退役自己；
* ToposAudioPipeline（fake sink 真线程）：被接管 → 线程收尾、拥有权移交；
* libav `_AudioPipeline`：登记/退役接线（fake clock）；
* ClipExportJob（真 ffmpeg，lavfi 合成）：成功原子交付（目标存在、无
  `.part` 残留）、单作业串行；取消（`-re` 120s 编码中途取消 → 进程终
  止、无残留）；deadline 0.6s → "超预算"收尾；失败（坏源）→ 有界
  `stderr_tail ≤ 2048`、无半成品；
* 播放器接线：Topos/Qt 播放器截取进行中再点一次 → 取消回调触发。

回归：E3/E4/EOF/topos_audio/GPU YUV/native host —— 66 通过（3 真媒体跳过）。

## 诚实边界

- **时钟未合并**：libav（墙钟主时钟）与 topos（音频位置主时钟）两套
  时基各自语义正确（E3 已把两家的"完成"统一到呈现+排空口径）；本批
  统一的是**设备占用与互斥**。"共享时钟服务"（单一 master 源抽象）未
  做——两家时基公式不同（墙钟 vs processedUSecs），强行抽象只会得到
  参数泥球，且现有 E3 排空判定已覆盖收尾正确性。
- 波形生成不在播放栈内（预算化实现已存在于
  `src/features/edit/core/audio_waveform.py`：CPU 预算 + cancel_all +
  缓存上限）——本批把"截取"侧补齐，波形侧维持既有实现不重复建设。
- 仲裁者管默认输出设备的进程内互斥；跨进程/设备枚举与切换未做。

## 隐私

lavfi 合成素材 + fake sink + 临时目录；无真实媒体、无隐私路径。
