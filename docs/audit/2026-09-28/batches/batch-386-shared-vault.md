# 批次报告：batch-386 [P2] src/shared/vault/

任务区间：AUD-38600–AUD-38699。审查范围：3 个文件，392 行。
汇总：Critical 0 / Major 2 / Suggestion 7。
背景核实：本包为 DORMANT（client.py / bridge.py 顶部均有 2026-08-19 审计的休眠标记；src/ 内无生产引用，仅 tests/vault/ 两个测试文件引用）。
依赖契约核实：bridge.py 使用的 SegmentKind 成员（SHOT/FACE/CHARACTER/TRANSCRIPT/VISUAL/AUDIO）与
SemanticSegment(segment_id/asset_id/source_range/kind/payload/confidence/provenance)、
FrameRange(start/duration) 均与 src/features/edit/ai_editing/editorial_models.py 实际定义一致（SemanticSegment 会校验 confidence∈[0,1]、FrameRange 会校验 start≥0、duration>0）。

## src/shared/vault/bridge.py (行数: 232)

### 发现

- [AUD-38600] [Major] 运算符优先级缺陷：clip_id 为空时调用方显式传入的 segment_id 被静默丢弃
  - 位置: src/shared/vault/bridge.py:57
  - 证据:
    ```python
        clip_id = clip.get("clip_id", "")
        sid = segment_id or f"vault_clip_{clip_id}" if clip_id else f"vault_shot_{start_frame}"
    ```
  - 影响: 三元表达式优先级低于 `or`，实际解析为 `(segment_id or f"vault_clip_{clip_id}") if clip_id else f"vault_shot_{start_frame}"`。已用 Python 实测：segment_id="user-given"、clip_id=""、start_frame=42 时结果为 `vault_shot_42`。与 docstring 契约"segment_id: 显式 segment_id；None 则用 clip_id"不符——clip 字典缺 clip_id 时，显式 ID 被忽略，退化为按起始帧生成的回退 ID。
  - 建议: 加括号明确优先级：`sid = segment_id or (f"vault_clip_{clip_id}" if clip_id else f"vault_shot_{start_frame}")`，并补一条"显式 segment_id + 空 clip_id"单测。
  - 置信度: 高

- [AUD-38601] [Major] relevance → confidence 换算未验证外部值：合法 0.0 被 `or` 误当缺失置为 1.0，越界/非数值直接抛 ValueError 穿透
  - 位置: src/shared/vault/bridge.py:74、213、227
  - 证据:
    ```python
            confidence=float(clip.get("relevance", 1.0) or 1.0),
    ```
    ```python
                confidence=float(hit.get("vault_relevance", 1.0) or 1.0),
    ```
    以及被写入的目标模型 src/features/edit/ai_editing/editorial_models.py:617-618：
    ```python
            if not 0.0 <= self.confidence <= 1.0:
                raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
    ```
  - 影响: 双重问题。(1) vault 服务端 SearchHit 的 relevance 字段默认值就是 0.0（apps/topos_vault/server/query_service.py:77、128，`_calc_relevance` 亦有返回 0.0 的路径），`or 1.0` 会把合法的 0.0（最不相关）反转为 1.0（最高置信），AI 剪辑 Executor 可能把零相关命中当最强证据；(2) relevance 为非数值字符串时 `float()` 抛 ValueError/TypeError，为 1.2 或 NaN 等越界值时 `SemanticSegment.__post_init__` 抛 ValueError——本模块设计定位是纯映射、优雅降级（见 client.py 同思路），异常却直接穿透，中断整批命中转换。
  - 建议: 用 `is None` 判缺失：`raw = clip.get("relevance"); confidence = min(max(float(raw), 0.0), 1.0) if raw is not None else 1.0`。
  - 置信度: 高（机制与 0.0 触发路径均已核实；越界值触发依赖服务端数据质量）

- [AUD-38602] [Suggestion] 秒→帧换算用 `or` 兜底且静默钳制，掩盖 vault 时间数据错误
  - 位置: src/shared/vault/bridge.py:50-53（98-101、147-150 同型）
  - 证据:
    ```python
        start_sec = float(clip.get("start_sec", 0.0) or 0.0)
        end_sec = float(clip.get("end_sec", start_sec) or start_sec)
        start_frame = max(0, int(round(start_sec * fps)))
        end_frame = max(start_frame + 1, int(round(end_sec * fps)))
    ```
  - 影响: end_sec=0.0（合法 falsy 值）被当作缺失替换为 start_sec；end<start、负秒等坏数据被 `max(start_frame + 1, …)` 静默钳成 1 帧片段，下游无从感知 vault 数据异常（"外部数据须验证"精神下至少应留痕）。
  - 建议: 用 `is None` 判缺失；end_sec < start_sec 时记 logger.warning 再兜底。
  - 置信度: 高

- [AUD-38603] [Suggestion] `.get(key, default)` 挡不住 JSON null，tags/keyframes 可能以 None 进入 payload
  - 位置: src/shared/vault/bridge.py:63-64、199
  - 证据:
    ```python
            "tags": clip.get("tags", []),
            "keyframes": clip.get("keyframes", []),
    ```
    ```python
        tags = hit.get("vault_tags", [])
    ```
  - 影响: HTTP 返回中键存在但值为 null 时默认值不生效，payload 里得到 None；下游对 tags/keyframes 迭代或取 len 时抛 TypeError。
  - 建议: 改为 `clip.get("tags") or []` 形式。
  - 置信度: 高

- [AUD-38604] [Suggestion] 回退 segment_id 易碰撞，可能被下游按 ID 归并时静默丢段
  - 位置: src/shared/vault/bridge.py:57、108、154、203、217
  - 证据:
    ```python
        sid = f"vault_face_{face_id}" if face_id else f"vault_face_{start_frame}"
    ```
    ```python
            sid = f"vault_visual_{asset_id}"
    ```
  - 影响: 缺 face_id/line_id 的两条记录若 start_frame 相同则 segment_id 重复；多个图片/音频命中共享同一 asset_id 时 `vault_visual_{asset_id}` / `vault_audio_{asset_id}` 完全相同——下游若按 segment_id 去重或字典化会静默丢失段。
  - 建议: 回退 ID 混入 path、行号或序号等唯一成分。
  - 置信度: 中（取决于下游消费方式，本包内未见）

- [AUD-38605] [Suggestion] face_track 置信度硬编码 0.9，未读取 provider 实际值
  - 位置: src/shared/vault/bridge.py:126
  - 证据:
    ```python
            confidence=0.9,  # 人脸检测置信度由 provider 给，这里给保守值
    ```
  - 影响: 注释自述"置信度由 provider 给"，却未从 face_track 字典读取任何置信度字段（vault 若提供则被丢弃），所有 FACE/CHARACTER 段权重相同，削弱 AI Executor 的证据分级。
  - 建议: `confidence=float(face_track.get("confidence") or 0.9)` 并按 AUD-38601 的方案 clamp 到 [0,1]。
  - 置信度: 中

### 通过项
- 线程安全：四个函数均为纯函数、无共享可变状态，可在任意线程调用
- GPU 资源生命周期：不涉及（无 OpenGL/ModernGL 资源）
- GLSL·HDR 正确性：不涉及（无着色器、无像素数据）
- 信号防递归：不涉及（无 Qt 控件/信号）
- 状态提交一致性：不涉及 undo/redo/revision/持久化（DORMANT，src/ 内无生产引用）
- 依赖契约：所用 SegmentKind 成员与 SemanticSegment/FrameRange 字段签名均与 editorial_models.py 一致
- fps 防御：`max(fps, 1.0)` 防 0/负帧率（49、97、146 行）
- 类型注解：全部公开函数参数与返回值均有注解

## src/shared/vault/client.py (行数: 135)

### 发现

- [AUD-38606] [Suggestion] 三处宽泛 `except Exception`，is_available() 连 debug 日志都没有
  - 位置: src/shared/vault/client.py:77-78、104-106、118-120
  - 证据:
    ```python
            try:
                data = self._get("/api/health")
                return data.get("status") == "ok"
            except Exception:
                return False
    ```
    ```python
            except Exception as e:
                logger.debug("vault search 失败（降级返回空）：%s", e)
                return []
    ```
  - 影响: 降级设计本身有 docstring 说明且合理，但 catch-all 同时吞掉程序性错误——如服务端返回 JSON 数组时 `data.get` 抛 AttributeError、编码错误、`_get` 内 json 解析失败等——与"不得宽泛 except 掩盖领域错误"的约束相悖；is_available() 无任何日志，vault 异常时无从排查。
  - 建议: 收窄捕获范围（URLError/timeout/JSONDecodeError/AttributeError 等），或至少给 is_available() 补 logger.debug 与其他两处对齐。
  - 置信度: 高

- [AUD-38607] [Suggestion] VaultStats 字段的 `.get` 默认值同样挡不住 JSON null，破坏声明类型
  - 位置: src/shared/vault/client.py:113-116
  - 证据:
    ```python
                return VaultStats(
                    total=data.get("total", 0),
                    by_type=data.get("by_type", {}),
    ```
  - 影响: /api/stats 返回 null 字段时 total/by_type 为 None，违反 dataclass 声明的 `int` / `dict[str, int]`，下游对 stats.by_type 迭代或 total 参与运算时 TypeError。
  - 建议: `data.get("total") or 0`、`data.get("by_type") or {}`。
  - 置信度: 高

### 通过项
- 线程安全：构造后无可变状态（base_url/timeout 仅 __init__ 写入），urllib 每次请求独立连接，实例可跨线程共享
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 状态提交一致性：只读客户端（无写路径、无持久化），符合三进程分离架构注释
- 异常与取消路径：全部公开方法失败时降级返回 False/[]/None 不抛出（设计如此，见 AUD-38606 的收窄建议）
- 输入验证（部分通过）：search() 对空/空白 query 提前返回 []
- 超时设计：默认 1.5s 短超时，不会阻塞主项目
- 性能：每次请求新建连接，对辅助性低频查询可接受
- 类型注解：类、方法、dataclass 字段均有注解

## src/shared/vault/__init__.py (行数: 25)

### 发现

- [AUD-38608] [Suggestion] 包级入口缺少 DORMANT 标记头，与两个子模块不一致
  - 位置: src/shared/vault/__init__.py:1-2
  - 证据:
    ```python
    """vault 与主项目的共享层。

    主项目（src/）通过本包与 vault（apps/topos_vault/）交互：
    ```
    对比 client.py:1-2 / bridge.py:1-2：
    ```python
    # DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
    # 接线前请勿在生产依赖；若半年内无接线计划建议删除。
    ```
  - 影响: `import src.shared.vault` 时最先接触的包入口没有休眠警示，2026-02-19 半年期到期评估时包入口文件易被漏掉；已核实 src/ 生产代码确实无引用（仅 tests/vault/ 引用），DORMANT 状态判断与子模块一致。
  - 建议: 在 __init__.py 模块 docstring 前补同样的 DORMANT 注释头。
  - 置信度: 高

### 通过项
- 通过（无逻辑）：纯 re-export，导出 VaultClient、VaultStats 及 4 个桥接函数共 6 个符号，`__all__` 与实际导入一致
