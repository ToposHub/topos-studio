# batch-160 审查报告 — src/features/edit/core/captions/（style_preset.py, translation.py）

编号区间：AUD-16000–AUD-16099。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

两个文件均为纯 core 层模型/命令模块（无 Qt 控件、无 OpenGL、无信号），GPU 与信号防递归维度天然不适用。关键契约已对照 `captions/model.py`、`timeline_v2.py`、`commands.py`（CommandStack.execute/redo 3707/3769）、`ai_editing/llm_client.py`（complete 签名 104-110）核实。

## src/features/edit/core/captions/style_preset.py (行数: 486)

### 发现

- [AUD-16001] [Major] StylePresetRegistry 序列化丢失 category 字段（to_dict/from_dict 往返数据丢失）
  - 位置: src/features/edit/core/captions/style_preset.py:337-350, 364-370（字段定义 :67）
  - 证据:
    ```python
    # to_dict (337-350) —— 未写入 category
    return [
        {
            "preset_id": p.preset_id,
            "label": p.label,
            "style": p.style.to_dict(),
            "description": p.description,
            "is_builtin": False,
        }
        ...
    # from_dict (364-370) —— 未读取 category
    preset = CaptionStylePreset(
        preset_id=str(entry["preset_id"]),
        label=str(entry.get("label", entry["preset_id"])),
        style=CaptionStyle.from_dict(entry.get("style")),
        description=str(entry.get("description", "")),
        is_builtin=False,
    )
    ```
  - 影响: 用户/项目预设若设置了 category（"title"/"callout"/"credit"/…），保存→重载后 category 变为 None，Titles tab 按 category 分组（见 title_templates.py:63-72,129，该模块的同类 schema 明确持久化 "category"）对用户预设失效；属于持久化往返的字段级数据丢失。注：当前生产代码仅消费 BUILTIN_PRESETS/CaptionStylePreset，registry 持久化路径尚未接线，故实际暴露面有限。
  - 建议: to_dict 增加 `"category": p.category`，from_dict 读取并按 title_templates.py:63-64 的方式清洗（`str(category) if category else None`）；补一条带 category 的往返测试。
  - 置信度: 高

- [AUD-16002] [Suggestion] include_builtins=False 时 register 可让用户预设占用 builtin id，且该条目随后无法 remove
  - 位置: src/features/edit/core/captions/style_preset.py:303-320
  - 证据:
    ```python
    if preset.preset_id in _BUILTIN_IDS:
        existing = self._presets.get(preset.preset_id)
        if existing is None:
            # Shouldn't happen (builtins preloaded) but be safe.
            self._presets[preset.preset_id] = preset
            return
        ...
    def remove(self, preset_id: str) -> bool:
        if preset_id in _BUILTIN_IDS:
            return False
    ```
  - 影响: 以 `StylePresetRegistry(include_builtins=False)` 构造后，一个 `is_builtin=False`、id 恰为 "builtin.*" 的用户预设会通过 existing-is-None 分支写入；之后 `remove` 因 id 在 _BUILTIN_IDS 中永远返回 False，条目不可删除，违反类文档"Built-ins cannot be overwritten or removed"不变量。
  - 建议: existing-is-None 分支补充 `if not preset.is_builtin: raise ValueError(...)`（仅放行真正的内置对象），或在 include_builtins=False 时改用独立的用户 id 集合判断可删性。
  - 置信度: 高（逻辑确定；触发需 include_builtins=False 的边缘用法）

- [AUD-16003] [Suggestion] MappingLike 别名在使用点之后定义且恒为 Any，类型注解无实际约束
  - 位置: src/features/edit/core/captions/style_preset.py:353, 377-380
  - 证据:
    ```python
    def from_dict(data: Optional[Iterable[MappingLike]]) -> "StylePresetRegistry":
        ...
    # (380)
    MappingLike = Any
    ```
  - 影响: 注解可读性误导（看似 Mapping 契约实为 Any）；当前仅因 `from __future__ import annotations`（:30）延迟求值而未在导入期 NameError，属隐性脆弱写法。
  - 建议: 直接注解 `Optional[Iterable[Mapping[str, Any]]]`（标准库 collections.abc.Mapping 无导入成本顾虑），删除该别名。
  - 置信度: 高

- [AUD-16004] [Suggestion] from_dict 静默丢弃畸形用户预设条目，无任何日志
  - 位置: src/features/edit/core/captions/style_preset.py:372-373
  - 证据:
    ```python
    except (KeyError, ValueError, TypeError):
        continue  # skip malformed entry, don't crash load
    ```
  - 影响: 异常元组本身不宽（符合规范），且带注释说明意图；但跳过完全无声，项目文件中预设损坏/被 builtin id 占用时用户数据静默消失，无排查线索。
  - 建议: 跳过时 `logger.warning(...)` 记录 preset_id 与原因（项目内 core 层普遍有 logger 可循）。
  - 置信度: 高

- [AUD-16005] [Suggestion] 未使用的导入：field、Callable、EMPTY_STYLE
  - 位置: src/features/edit/core/captions/style_preset.py:32-38
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
    from src.features.edit.core.captions.model import (
        CaptionStyle,
        EMPTY_STYLE,
        STYLE_FIELDS,
    )
    ```
  - 影响: `field`、`Callable`、`EMPTY_STYLE` 在文件内均无引用（文件仅用 `@dataclass(frozen=True)` 与默认值），增加噪音并暗示残留的重构痕迹。
  - 建议: 删除三个未用导入。
  - 置信度: 高

### 通过项
- 线程安全：模块为不可变值对象 + 普通 dict 注册表，无线程创建/共享可变状态；现有调用方（edit_controller.py:8523、edit_effects_panel.py:1795、title_templates.py:17）只读使用 BUILTIN_PRESETS/CaptionStylePreset，无跨线程写。
- GPU 资源生命周期：不涉及 OpenGL/ModernGL。
- GLSL·HDR 正确性：不涉及着色器；预设颜色值为常规 0.0–1.0 字幕语义，无 HDR clamp 问题。
- 信号防递归：无 Qt 控件或信号。
- 状态提交一致性：预设为 frozen 值，apply/propagate 返回新 CaptionStyle 交由命令层提交，符合不可变模型；propagate 跳过已发散目标的策略与 docstring 一致。
- 输入验证：CaptionStylePreset.__post_init__ 校验 preset_id 非空、style 类型（:69-73）；register 重复注册幂等、覆盖内置 id 拒绝（有测试 test_caption_data_model.py::TestStylePresets）。
- apply_preset_to_cue 合并语义正确（preset set-field 优先、None 字段保留 cue 原值），tests/edit/test_caption_data_model.py:513-522 覆盖。

## src/features/edit/core/captions/translation.py (行数: 261)

### 发现

- [AUD-16006] [Major] 命令 apply 内执行实时 LLM 网络调用：redo 非确定性、redo 失败会丢失重做历史、阻塞调用线程
  - 位置: src/features/edit/core/captions/translation.py:218-246（网络调用链 :162 → :77-82）
  - 证据:
    ```python
    def apply(self, timeline: TimelineSequence):
        source = timeline.get_track_by_id(self.source_track_id)
        ...
        new_track = build_translated_subtitle_track(   # → translator(texts, language) (translation.py:162)
            source, self.target_language, self.translator, track_id=self.track_id,
        )
    # AiAssistantTranslationProvider.__call__ (77-82):
        raw = self._client.complete(
            messages, json_mode=True, temperature=0.2,
            max_tokens=max(256, len(texts) * 80),
        )
    ```
    而 CommandStack.redo（commands.py:3768-3769）先 pop 再 apply：`command = self._redo_stack.pop()` → `result = command.apply(timeline)`。
  - 影响: redo 会再次调用 translator：1) temperature=0.2 下重译文本与首次不同，undo→redo 后轨道内容与被撤销前不一致，违反命令重放的状态恢复语义；2) 若网络/模型在 redo 时抛错，命令已被 pop 且未回填 undo 栈，该重做条目静默丢失；3) 长批次同步网络调用阻塞执行命令栈的线程（通常为 UI 线程）数秒以上。
  - 建议: 首次 apply 时把译文快照存入命令（`field(compare=False, default=None)`），redo 直接用快照重建轨道，不再调用 translator；或把翻译上移为命令外的预处理步骤，命令只接收现成译文。
  - 置信度: 高

- [AUD-16007] [Suggestion] 空白源字幕未预校验：单条空 cue 会在 LLM 调用之后才令整轨翻译失败
  - 位置: src/features/edit/core/captions/translation.py:161-168, 88-89
  - 证据:
    ```python
    texts = tuple(cue.text for cue in source_cues)      # 未过滤/校验空文本
    translated = tuple(str(text).strip() for text in translator(texts, language))
    ...
    if any(not text for text in translated):
        raise ValueError("translator returned an empty subtitle cue")
    ```
  - 影响: SubtitleClip.text 默认 ""（timeline_v2.py:1298），时间线存在空 cue 时，模型对其返回空串 → 付费网络调用完成后整体抛 ValueError，用户得到全有或全无的失败。
  - 建议: 调用 translator 前对空/纯空白 cue 预检：提前报错或跳过（保留原文/空轨道），避免事后失败。
  - 置信度: 中

- [AUD-16008] [Suggestion] max_tokens 按 80 token/条启发式可能截断 JSON 响应导致整批失败
  - 位置: src/features/edit/core/captions/translation.py:81
  - 证据:
    ```python
    max_tokens=max(256, len(texts) * 80),
    ```
  - 影响: 输出为整包 JSON（全部译文 + 引号/转义），长 cue 或扩张型目标语言（如英→德/中）单条译文可超 80 token；一旦截断，_parse_translation_response 抛 "AI 翻译返回的不是合法 JSON"，整轨翻译失败且无分批重试。
  - 建议: 预算改为基于输入长度（如 sum(len(t) for t in texts) 的倍数）并留裕量，或对大批 cues 分块调用后合并校验。
  - 置信度: 中

- [AUD-16009] [Suggestion] 独立调用 build_translated_subtitle_track 返回的轨道 index 与源轨道重复；label/name 回退表达式重复书写
  - 位置: src/features/edit/core/captions/translation.py:198, 192, 196
  - 证据:
    ```python
        label=name or f"{source_track.name} ({language})",
    ...
        index=source_track.index,
    ...
        name=name or f"{source_track.name} ({language})",
    ```
  - 影响: 该函数在 `__all__` 中导出为公共 API；绕过命令直接使用时新轨道与源轨道 index 相同（仅命令路径 :227-230 的 replace 修正），且两处回退表达式若日后只改一处会失同步（当前值一致，无实际错误）。
  - 建议: `index` 参数化（默认追加语义）或文档注明仅命令路径修正 index；回退名提取为局部变量复用。
  - 置信度: 高（代码事实；当前影响低）

- [AUD-16010] [Suggestion] 弱类型注解：profile/credential_store 为 Any，workflow 适配器返回 Callable[[tuple[Any, ...], str], tuple[Any, ...]]，apply 缺返回注解
  - 位置: src/features/edit/core/captions/translation.py:93, 103-115, 218
  - 证据:
    ```python
    def make_ai_assistant_translation_provider(profile: Any, credential_store: Any) -> AiAssistantTranslationProvider:
    def make_ai_assistant_workflow_translation_provider(
        profile: Any, credential_store: Any,
    ) -> Callable[[tuple[Any, ...], str], tuple[Any, ...]]:
    ...
    def apply(self, timeline: TimelineSequence):
    ```
  - 影响: 适配器内部以 getattr/str() 兜底（:125-127），类型错误只能运行期暴露；apply 返回 (TimelineSequence, 新命令) 元组但无注解，与 commands.py 中 `-> Tuple[TimelineSequence, "XxxCommand"]` 惯例（如 commands.py:6944, 7028）不一致。
  - 建议: apply 补返回注解；workflow 适配器至少注解为 Callable[[tuple["SemanticSegment", ...], str], tuple["ScriptLine", ...]]（TYPE_CHECKING 导入避免运行期依赖）。
  - 置信度: 高

### 通过项
- 线程安全：模块内无共享可变状态，translator 显式注入；线程归属由调用方（ai_edit_controller/ai_editing_mixin 的 workflow 层）决定。
- GPU 资源生命周期：不涉及 OpenGL/ModernGL。
- GLSL·HDR 正确性：不涉及着色器；track default_style/safe_anchor 原样保留（:190-191），无 clamp。
- 信号防递归：无 Qt 控件或信号。
- 异常路径：_parse_translation_response 将 JSONDecodeError 转为带中文上下文的 ValueError（:31-34），代码围栏剥离（:28-30）正确；返回长度/空串校验完整（:84-89, :163-168）。
- 输入验证：track_type/target_language/translator callable 在进入网络调用前校验（:149-155）；cue.caption_metadata 恒非 None（SubtitleClip 默认 factory + __post_init__ 类型检查，timeline_v2.py:1355,1384）；EditTrack 构造参数满足其 __post_init__ 约束（track_type/index/subtitle 唯一性/无重叠，时间与源轨道一致故继承合法）。
- undo/redo 对称性：apply 追加轨道于尾部并按 enumerate 归一 index，undo 用 with_track_removed 移除末尾轨道不产生 index 空洞；_created_track_id 物化模式与 CommandStack.execute 的 (timeline, updated_cmd) 元组契约匹配（commands.py:3710-3712）。
- 确定性 ID：uuid5(NAMESPACE_URL, 源轨道/语言) 去重，对同一源轨+语言重复翻译给出明确 ValueError（:225-226）而非重复轨道。
- 提示注入防护：字幕文本经 json.dumps(ensure_ascii=False) 转义并显式声明为不可信数据（:56-75）；system prompt 约束返回纯 JSON。
- llm_client.complete 签名核实匹配（json_mode/temperature/max_tokens，llm_client.py:104-110）。

## 汇总
- Critical: 0
- Major: 2（AUD-16001 预设序列化丢失 category；AUD-16006 命令 apply 内实时 LLM 调用破坏 redo 确定性/失败丢历史/阻塞）
- Suggestion: 8（AUD-16002–16005, AUD-16007–16010）
