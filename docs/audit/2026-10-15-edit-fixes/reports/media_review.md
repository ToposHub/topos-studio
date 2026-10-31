# 第四轮修复复核：素材、代理、多机位和嵌套

复核基线：`8330e42df`。本报告覆盖共享工作区本轮变更，复核于 2026-10-15 至 2026-10-16 进行；未提交 Git。

## 复核后补齐的缺陷

| 项目 | 原因与可复现情形 | 修复及验证 |
|---|---|---|
| Replace 后台请求的源快照仍共享可变字典 | `MediaAsset` 为 frozen，但 `metadata` 内字典可变；记录素材本体后，原地修改 feature payload 会同时改掉比较双方，旧结果仍可提交。文件选择/确认对话框也运行事件循环，期间切工程可让请求登记到另一个工程。 | 在第一个对话框前深拷贝素材和记录工程/会话身份；确认后和后台完成时再次核验。新增原地修改、确认时换工程两项零提交测试。 |
| 代理编码第一阶段遗漏同路径文件变化 | token、工程代次和 path 都不变时，外部应用可覆盖原文件；旧编码结果完成后按新文件的 stat 注册。 | 入队时保存源文件 `(size, mtime_ns)`，编码完成再比较，不匹配走失败处理，不发布 ready。注销单任务/全部任务同步清身份。二阶段已有 request ID/源身份校验继续保留。 |
| 多机位网格取帧没有经过真实片段采样及目标相机时间基 | 2x/倒放或 24fps→48fps 时仍用 `source_start + timeline_delta`；静帧监看与切角源帧不同。 | 新增 `map_angle_source_frame`；先使用 `clip.timeline_to_source_frame()`，减活动相机同步偏移，按双方 FPS 换算后加目标偏移。命令侧由 core review 同步复用。新增正向/倒放网格与同步时钟往返测试。 |
| 多机位切角拒绝会逃出 Qt 槽 | 锁定轨道或不支持的时间映射抛 ValueError，按钮可保留乐观高亮。 | `_switch_multicam_angle` 捕获领域拒绝并显示原因，finally 从真值刷新组、活动角度及监看帧。 |
| Multicam / Nest 创建缺少完整 Undo 事务 | 创建组仅 Insert 入栈，成员组 metadata 未随 Undo 恢复；Nest 直接写父序列、子缓存、素材与引用，后续普通编辑历史不连续。 | 接入 UI review 新增的 `TimelineProjectTransactionCommand`。Multicam 纯计划生成序列和资产差异；Nest 一并携带父序列、嵌套素材、子引用/缓存。统一 timeline history 原子提交，按受影响 key preflight，保留其他工程内容；冲突拒绝覆盖。新增两类创建→普通 opacity 编辑→逆序 Undo→JSON Redo 测试，校验 ID 稳定和无孤儿资产/引用/子序列。真实 View 桥验证见 UI review 测试。 |
| Nest 纯构造入口可以搬走锁定轨道的内容 | `build_nested_from_selection` 只检查素材存在与字幕类型，没有检查轨道锁，控制器直接调用即可移走锁轨片段。 | 在构造子序列/素材之前拒绝锁轨选择，明确 ValueError；新增 builder 测试确认原轨片段不变。 |
| 波形重同步直接写 metadata，未进入项目历史 | `_apply_multicam_sync_offsets` 裸 `with_media_asset_added`，未推进工程 revision/dirty，也不能撤销。 | 新增 `UpdateMulticamSyncCommand`，通过素材池 ProjectCommandStack 提交并正常发布。仅修改 `multicam_group` 键；Undo 保留后写入的日志等字段；组被另一个操作改动时，先完整拒绝再改任何成员。JSON Undo/Redo、revision/dirty/发布、冲突历史完整性均有测试。无变化不产生历史。 |
| 嵌套裁头后轨道自动化被夹到第 0 帧 | 子 volume Bezier 在 `[0,100]`，嵌套 Source In=50 且从父 frame0 开始时，旧代码将 child0 平移至 -50 后强夹 0，父 frame0 读到旧首值，而不是 child50 的插值。多个负帧关键帧还会撞在 0。 | 虚拟轨改用 `KeyframeTrack.shifted()`，保留负坐标插值锚点及把手，逐帧比对父 `[0,40)` 与 child `[50,90)` 的采样值。 |
| 父嵌套倒放/速度曲线绕过已声明的音频限制 | `effective_speed()` 只返回基础正速度，reverse1x 和 base1x 的曲线会误以为普通 1x，混入未变速的正向音频。 | 与已有“父嵌套变速音频暂不支持”的策略一致，reverse/曲线也走明确日志诊断并跳过，避免播放错误方向/时序的音轨；能力表同步说明限制。此项是安全门补齐，不代表已实现父嵌套音频重采样。 |

## 原有本轮修复的重点核验

- Replace 成功探测在后台执行；UI 定时器仍响应。坏源、探测期间/之后文件变化、旧请求、跨工程/换源、时间线快照不完整均为零提交。
- 使用全部实时序列核验短源、媒体类型、视频源 FPS 和音频流，不把无法获取快照视为素材未使用。新源派生元数据整体替换，保留 feature/未知扩展，深层数据不共享。
- 代理分辨率二阶段 callback 先匹配 `request_id`、素材、路径、会话代次，再消费 pending；旧结果不会吃掉新的请求。
- 嵌套音频的子片段裁窗沿用时间线源映射，可保留混合 FPS、倒放及子片段速度曲线的源采样；多个实例及递归子实例使用独立 clip/track/Bus 命名空间。
- 多机位创建先纯 dry plan，再发布；锁轨、缺视图、提交失败不污染成员组 metadata。后台同步在结果对话框前后均核验请求、工程、成员完整 payload 和磁盘身份；旧回调不能清除新任务。

## 验证记录

1. 原范围复核：`test_round4_media_replacement.py`、`test_round4_nested_sampling.py`、`test_round4_multicam_preflight.py`、`test_multicam_production.py`、`test_nested_audio_flatten.py`：**88 passed**。
2. 后续七文件组合（含旧 nested production / multicam sync）：**145 passed / 5.07s**。后续又新增嵌套负时间轨自动化验证及能力表更新。
3. 补齐轨道自动化后的重点组合：

   ```sh
   QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
     tests/edit/test_round4_nested_sampling.py \
     tests/edit/test_nested_audio_flatten.py \
     tests/edit/test_capability_registry.py \
     tests/edit/test_round4_multicam_preflight.py \
     tests/edit/test_round4_media_replacement.py
   ```

   **103 passed / 4.36s**。
4. 最后补齐 Nest 锁轨与 Sync 修改时间后，将上述五文件与 `test_nested_sequence.py`、`test_nested_sequence_production.py`、`test_multicam_production.py`、`test_multicam_sync.py` 一起验证：**184 passed / 5.74s**。原始日志及 JUnit：`../media-review-tests.log`、`../media-review-tests.xml`。

各轮范围有重叠，不能把数量相加为独立测试总数。完整剪辑测试和真实 GUI 交互由主任务汇总。`git diff --check` 与相关 Python 文件编译检查通过。

## 仍然明确的能力边界

父嵌套片段自身的音频变速/倒放/曲线重采样、跨父子时基的完整音频/自动化重采样、子轨转场，多机位多路实时监看、时间码/标记同步、音频跟随切角、重同步追溯已有剪辑，仍属于已有 PARTIAL 能力范围。能力表已移除“创建没有 Undo”的过时描述，但没有将整个 Multicam/Nest 标为完整实现。这里未进行耳听、长片压力或硬件录音验证。

## 后续交付门禁补修

本报告中“日志诊断并跳过”仅描述当时的 preview 兼容路径。最终版本已经补齐带音频交付的严格 preflight，不能静默导出无声成功；File/Deliver 的实际 Stem 入口也传递冻结的序列解析器。详见 [nested_audio_gate_final.md](nested_audio_gate_final.md)；最终交付验收以该报告及主证据 README 为准。
