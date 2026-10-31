# F2.6 多机位同步方式与长录制漂移 — 证据台账

- 日期：2026-10-24
- 结论：**已完成（波形路径的防错匹配 + TC 替代同步 + 首/中/末多段漂移
  判别 + 分析中途取消）**；速率漂移的**矫正**（改写已放置片段时序）按
  计划原文属条件依赖 F3.3（采样级漂移矫正），当前为"诊断并拒绝写回，
  保留原片"（不伪称已同步）。
- 提交：`6fad632a2`（功能）+ `c82449b3d`（既有测试顺序依赖缺陷修复）。

## 1. 验收对照

| 验收点 | 落地 | 证据 |
|---|---|---|
| 无开头共同音频可用替代同步 | TC 同步入口：面板"TC 同步"按钮 → `compute_timecode_alignments`（读各角度素材 metadata timecode——探测自流/容器/tmcd 轨——求差换算 offset）→ 同一确认对话框 → 同一 UpdateMulticamSyncCommand 事务；即时计算无 worker | test_multicam_sync_guards.py::TestTimecodeSync（4 项：差值换算/缺 TC/坏 TC/参考缺 TC） |
| 错时码、静音/反相/重复音乐不写错误偏移 | 防错门槛四连：低置信（\|conf\|<0.35，静音/不相关）、反相（负谷检测——argmax 只见正峰的盲区，负谷显著强于正峰按反相标记）、周期信号歧义（次峰/主峰比>0.85，重复音乐床多峰）、出界（\|lag\|≥段长）→ `rejection` 带原因；`filter_writable_offsets` 单点过滤 + Apply 侧状态栏逐条反馈；对话框把 rejection 呈现为原因行 | ::TestMismatchGuards（6 项）+ ::TestWritableFilter |
| 长访谈首末均可检查 | worker 多段采样：`segment_starts`（首/中/末，控制器按素材时长自动选段；段尾不足自然缺席）；一致（≤50ms）→ 中位 offset 可写；线性发散（三段）→ `rate_drift_detected` 诊断拒绝；两段不一致 → `inconsistent_segments` | ::TestMultiSegmentDrift（4 项） |
| 取消无写入 | 对话框层取消既有锁定（test_multicam_production:473-496）；新增分析中途取消：面板取消按钮 → `requestInterruption`（worker 逐任务检查点既有）→ 上报 cancelled（非失败语气）→ 引用清理零写入 | controller `_cancel_multicam_sync` + worker interrupted 分支 |
| 不默认追溯平移已编辑片段 | 保持既有行为（组 metadata 更新，已放置片段不动；三处文档化一致） | 回归护栏 |
| 未支持漂移矫正时诊断并保留原片 | `rate_drift_detected` 拒绝写回 + 状态栏原因——明确"未同步"而非沉默 | ::test_linear_drift_diagnosed_not_written |

## 2. 关键设计决策

1. **rejection 与 error 分离**：error=分析失败（无音轨/段过短）；
   rejection=分析完成但结果不可信（低置信/反相/歧义/出界/漂移）——
   对话框与摘要都能区分呈现"为什么没写"。
2. **反相检测走负谷**：`-ref` 的互相关真峰为负，argmax 会捡到噪声正峰
   （实测 conf=+0.03）；同时比较正峰与负谷（1.2 倍优势阈值）才可靠标记。
3. **TC 帧率近似声明**：TC fps/drop_frame 未持久化（仅字符串），帧字段按
   25fps 折算秒差（TC 逐帧差对帧率不敏感的场景误差 ≤1 帧@24fps）；
   持久化补全登记 F6.2 字体依赖清单同批。
4. **多段合成在中位 lag 上重算 offset**（combine 收 fps 参数），不从
   各段 offset 取均值——避免帧率取整偏差累积。
5. **TC 路径无置信度概念**（确定性元数据），confidence=1.0，防错靠
   "缺/坏 TC 按错误行呈现" + 用户预览取消。

## 3. 测试账本

- 新增 tests/edit/test_multicam_sync_guards.py：15 项。
- 修复 tests/edit/test_multicam_production.py 既有顺序依赖缺陷（全文件
  无 qapp fixture，单独运行时真实对话框构造 C 级 Abort；`usefixtures
  ("qapp")` 修复后 22 项全过——该缺陷先经 HEAD~1 双向验证排除本任务
  改动归因）。
- 护栏：multicam_sync/production/preflight/multicam/dialog 63+22 项绿。

## 4. 事故记录（过程透明）

验证期间误用 `git stash`（工作树本已干净），弹出了仓库中遗留的他人
旧 stash 引发大范围冲突；立即 `git reset --hard HEAD` 完整恢复（本人
全部工作已提交于 6fad632a2），旧 stash 条目原样保留未动，零数据丢失。
后续验证改用 `git checkout <ref> -- <file>` 临时检出的安全方式。

## 5. B 依赖核对

无 Plan B 依赖（纯 Python DSP + Qt UI + 既有解码器）。

## 6. 登记的后续片

1. 速率漂移**矫正**（条件依赖 F3.3 采样级矫正）：需 MulticamAngle 速率
   字段 + 已放置片段影响面预览。
2. 素材级 marker 同步（需先定义素材级 marker 模型——现只有时间线级）。
3. TC fps/drop_frame 持久化补全（探测侧已有三字段，project.py 落盘侧
   只存字符串）。
4. 同步方式/置信度/漂移诊断的组级持久化（现状态栏即焚；载体
   UpdateMulticamSyncCommand 可扩）。
5. 重同步后"已放置片段相对新偏移过期"的差异预览。
