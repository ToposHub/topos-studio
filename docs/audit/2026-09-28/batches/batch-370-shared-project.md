# 批次报告：batch-370 [P2] src/shared/project//

编号区间：AUD-37000–AUD-37099
审查文件：schema_v1_spec.py (876) / topos_project.py (926) / topos_project_controller.py (622)
统计：Critical 0 · Major 6 · Suggestion 7

交叉验证说明：`validate_json`/`SCHEMA_REGISTRY_V1` 仅经 `src/shared/project/__init__.py` re-export，src/ 内无生产调用点；`ToposProjectController` 仅被 `tests/project/test_topos_project_controller.py` 使用；`topos_project.revision` 为活跃字段（`src/features/edit/core/edit_project_io.py:511` 读取）；工程加载路径在 `src/shared/project/topos_project_io.py:577` 的 try 内（except 位于 :808）。

## src/shared/project/schema_v1_spec.py (行数: 876)

### 发现

- [AUD-37001] [Major] validate_json 在 jsonschema 缺失时静默放行（fail-open），外部数据验证被无声绕过
  - 位置: src/shared/project/schema_v1_spec.py:868-875
  - 证据:
    ```python
    try:
        import jsonschema
        jsonschema.validate(data, schema)
        return True
    except ImportError:
        # jsonschema 未安装，返回 True（跳过验证）
        return True
    except jsonschema.ValidationError:
        return False
    ```
  - 影响: 该函数是本模块对外暴露的唯一 schema 校验入口，职责是验证 .topos 包等外部数据。校验器缺失时返回 True，调用方会误以为数据已通过校验，直接违背"外部数据须验证"铁律（fail-open 而非 fail-closed）。当前 src/ 内无生产调用点（仅 `__init__.py` re-export），风险为潜伏性；一旦被接线使用即生效。
  - 建议: jsonschema 缺失时至少 logger.warning 并返回 False（或抛出明确异常），将 jsonschema 列入依赖清单使其缺失成为安装错误而非静默跳过。
  - 置信度: 高（行为确定；影响基于"无当前调用点"标注为潜伏）

- [AUD-37002] [Suggestion] schema v1 枚举与 v2/v3 数据模型漂移，spec 无法通过校验接纳现行数据
  - 位置: src/shared/project/schema_v1_spec.py:128, 215-218, 310-313
  - 证据:
    ```python
    # PROJECT_SCHEMA_V1 project_mode 枚举（:128）
    "enum": ["single_asset", "edit_project", "composite_shot", "mixed_project"]
    # PROJECT_SCHEMA_V1 MediaAssetRef media_type 枚举（:215-218）
    "media_type": {
        "type": "string",
        "enum": ["image", "video", "audio", "image_sequence"]
    },
    # MEDIA_POOL_SCHEMA_V1 media_type 枚举（:310-313）同上 4 值
    ```
    而数据模型 `src/shared/project/topos_project.py:87-91` 允许 `"subtitle", "comp_clip", "nested_sequence", "adjustment", "solid", "motion_graphic"`；`topos_project.py:39-44` 的 ProjectMode 为 `standalone_image/edit_project/composite_shot/mixed_project`。
  - 影响: 若以 v1 schema 校验现行工程输出（含 comp_clip 素材或 standalone_image 模式），全部会被判为非法；spec 作为"结构与数据一致性"的定义已失真，误导后续维护者。v1→v2 迁移仅在 `topos_project_io.py:626-630` 对 project_mode 做了代码级处理，spec 侧未同步。
  - 建议: 为 v2/v3 数据建立对应 schema_vN_spec（或在 v1 spec 中标注 legacy 并补齐 v2 枚举），使 enum 与 `MediaAssetRef.__post_init__`、`ProjectMode` 的合法值保持同源。
  - 置信度: 高（不匹配为代码事实；因 validate_json 未被使用，当前无实际校验失败）

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：纯静态 schema 声明文件，无运行时状态与渲染路径，均不适用且无风险
- schema 结构自洽：$defs 引用、required、minItems/maxItems 等约束书写规范（如 DeliverPresetRef resolution 的 2 元素约束）
- `get_schema` 实现正确（缺失返回 None）
- 类型注解完整

## src/shared/project/topos_project.py (行数: 926)

### 发现

- [AUD-37003] [Major] 全部 7 个 with_* 工厂方法丢失 revision 字段，任何一次派生都会把乐观锁 revision 静默重置为 0
  - 位置: src/shared/project/topos_project.py:666-686（with_name；688-842 的 with_asset_added/with_color_stack_added/with_timeline_added/with_composition_added/with_active_timeline/with_ui_state 同样缺失）
  - 证据:
    ```python
    def with_name(self, name: str) -> ToposProject:
        """创建新实例，修改名称"""
        return ToposProject(
            project_id=self.project_id,
            name=name,
            ...
            created_at=self.created_at,
            modified_at=time.time(),
        )   # 未传 revision=self.revision → 落回默认值 revision: int = 0（:455）
    ```
    字段声明（:452-455）明确注释 revision 为"工程逻辑 revision（乐观锁，单调递增）"。
  - 影响: revision 是状态提交一致性的乐观锁字段：`from_dict`（:521）在加载时保留它，`edit_project_io.py:511` 在 ToposProject→EditProject 转换时读取它。经过任一 with_* 方法（如重命名、加素材、切活动时间线）后 revision 归零，破坏单调递增约定——并发提交入口可能把过期 revision=0 视作合法基线，导致跨版本冲突检测失效/lost update。
  - 建议: 所有 with_* 方法透传 `revision=self.revision`（或按语义统一 `revision=self.revision + 1`）；可加测试断言 with_* 派生不降低 revision。
  - 置信度: 高（代码事实确定；当前 src/ 生产代码未见直接调用这些方法，风险为公共 API 层面的潜伏缺陷）

- [AUD-37004] [Major] frozen 数据类持有可变 dict 字段且 ui_state/settings 跨实例共享引用，"不可变确保线程安全"的设计承诺不成立
  - 位置: src/shared/project/topos_project.py:393, 432-441（对照模块 docstring :8-10 "不可变数据类（frozen=True）确保线程安全"）
  - 证据:
    ```python
    @dataclass(frozen=True)
    class ToposProject:
        ...
        assets: dict[str, MediaAssetRef] = field(default_factory=dict)
        ...
        ui_state: dict[str, Any] = field(default_factory=dict)
        settings: dict[str, Any] = field(default_factory=dict)
    ```
    且 with_ui_state（:822-842）直接 `ui_state=ui_state` 传入新实例，from_dict（:513-514）直接引用解析出的 dict。
  - 影响: frozen 仅阻止属性重绑定，不阻止 `project.assets[k] = ...` 或 `project.ui_state["x"] = 1` 这类原地变更；with_* 派生的多个实例共享同一 ui_state/settings dict 对象，经任一引用的原地修改会同时"污染"所有派生实例。多线程（渲染线程/AI 任务读写工程）下的可见性与竞态无任何保护，模块声称的线程安全是假的。
  - 建议: 为暴露只读视图（MappingProxyType 或返回副本的属性）；with_* 中对 ui_state/settings 做深拷贝或不可变化处理；或在文档中明确撤销线程安全承诺并约定所有变更必须走工厂方法。
  - 置信度: 高（结构事实确定；未发现当前 src/ 内有跨线程原地变更调用点，实际竞态为潜在）

- [AUD-37005] [Major] DeliverPresetRef.from_dict 对长度不足的 resolution 抛 IndexError，单个坏预设字段导致整个工程加载失败
  - 位置: src/shared/project/topos_project.py:379-380
  - 证据:
    ```python
    resolution_data = data.get("resolution", [1920, 1080])
    resolution = (resolution_data[0], resolution_data[1]) if isinstance(resolution_data, list) else (1920, 1080)
    ```
  - 影响: 外部工程包 project.json 中 `"resolution": []` 或 `[1920]`（手工编辑/损坏/第三方生成）会在 `ToposProject.from_dict`（:501 调用）内抛未捕获 IndexError；加载路径整体被 `topos_project_io.py:577` 的 try 捕获（except :808）后整个工程无法打开——一个非关键预设字段毁掉全部工程数据的可加载性，违反"外部数据须验证"铁律的稳健性要求。
  - 建议: 校验 `len(resolution_data) >= 2` 且元素为 int，不合法时回退 (1920, 1080) 并 logger.warning，而非让异常上抛。
  - 置信度: 高（崩溃路径为代码事实；外层 except 的兜底行为基于 topos_project_io.py 阅读推断）

- [AUD-37006] [Suggestion] with_asset_added 的 root bin 解析取"第一个无父 bin"，且重复添加同 asset_id 仍递增 asset_count 导致计数漂移
  - 位置: src/shared/project/topos_project.py:690-703
  - 证据:
    ```python
    new_assets = dict(self.assets)
    new_assets[asset.asset_id] = asset

    new_bins = dict(self.bins)
    root_bin_id = next((bid for bid, b in self.bins.items() if b.parent_id is None), None)
    if root_bin_id and root_bin_id in new_bins:
        ...
        asset_count=root_bin.asset_count + 1,
    ```
  - 影响: 存在多个无父 bin 时按插入序任取其一，计数累加到任意 bin；asset_id 已存在时（重复导入）assets 数量不变但 asset_count 仍 +1，索引计数与实际素材集漂移，UI 展示与 bins.json 数据失真。root_bin_id 属性（:883-888）同样有"取第一个"假设。
  - 建议: 仅当 `asset.asset_id not in self.assets` 时递增计数；root bin 解析改为显式标记（如 bin 元数据 is_root）而非 parent_id 推断。
  - 置信度: 高

- [AUD-37007] [Suggestion] ToposProject.from_dict 对字段类型不合规的外部数据缺乏校验，异常类型不可控
  - 位置: src/shared/project/topos_project.py:496-521
  - 证据:
    ```python
    assets = {k: MediaAssetRef.from_dict(v) for k, v in data.get("assets", {}).items()}
    ...
    revision=int(data.get("revision", 0) or 0),
    ```
  - 影响: `"assets": []`（list 而非 dict）→ `.items()` 抛 AttributeError；`"revision": "abc"` → int() 抛 ValueError。均由远端加载的外层兜底吞掉后整体加载失败，报错信息与根因脱节，不利于诊断损坏包。与 AUD-37005 同属外部数据稳健性缺口。
  - 建议: from_dict 入口做最小形状校验（各集合字段 isinstance(data.get(...), dict)），不合规字段降级为空并告警。
  - 置信度: 高（行为确定）

### 通过项
- 输入验证（构造侧）：各 dataclass `__post_init__` 对空 ID、非法枚举、fps/分辨率非法值校验完整（如 :199-202 fps<=0、width/height<=0 拒绝）
- 状态提交一致性（序列化侧）：from_dict/to_dict 对 revision 字段往返保留（:490/:521），ui_state/settings 等字段对齐
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：纯数据模型文件，不涉及渲染与控件，无风险
- 类型注解完整；is_virtual_asset_path（:29-31）类型防御正确

## src/shared/project/topos_project_controller.py (行数: 622)

### 发现

- [AUD-37008] [Major] from_dict 毫秒→分钟整除截断可产生 0 间隔自动保存定时器，且间隔参数全程无下界校验
  - 位置: src/shared/project/topos_project_controller.py:584-590（对照 start_auto_save :468-475、set_auto_save_interval :489-499）
  - 证据:
    ```python
    self._auto_save_enabled = data.get("auto_save_enabled", False)
    self._auto_save_interval_ms = data.get("auto_save_interval_ms", 5 * 60 * 1000)
    ...
    # 如果启用了自动保存，启动定时器
    if self._auto_save_enabled:
        self.start_auto_save(self._auto_save_interval_ms // 60000)
    ```
    而 `start_auto_save` 中 `self._auto_save_interval_ms = interval_minutes * 60 * 1000` 后直接 `self._auto_save_timer.start(self._auto_save_interval_ms)`。
  - 影响: 外部恢复的状态中 `auto_save_interval_ms` 为 30000（30 秒）时 `30000 // 60000 == 0` → `QTimer.start(0)` 每个事件循环迭代触发一次 `_on_auto_save_timer`；若有 dirty 状态则每个循环执行一次保存（含同步 I/O），形成保存风暴与 UI 冻结。同时整除截断使任何非整分钟间隔（如 1.5 分钟）静默漂移。`start_auto_save`/`set_auto_save_interval` 对 `interval_minutes <= 0` 亦无校验。
  - 建议: from_dict 直接以毫秒启动定时器（新增接受 ms 的内部启动路径）；对最终 interval 施加 `max(interval_ms, 下界)` 校验，非法值回退默认并告警。
  - 置信度: 中（代码路径确定；该控制器当前仅在测试中使用，未接入生产代码）

- [AUD-37009] [Major] "线程安全，所有状态变更通过信号"的契约与实现不符：方法体直接原地改 dict，无任何锁
  - 位置: src/shared/project/topos_project_controller.py:146-148（docstring 声明）对照 :243/:254/:291/:327（直接 `self._page_dirty_states[page] = new_state`）
  - 证据:
    ```python
    ## 线程安全

    所有状态变更通过 Qt 信号，确保在 UI 线程执行。
    ```
    ```python
    def mark_dirty(self, page: PageType, reason: str | None = None) -> None:
        ...
        self._page_dirty_states[page] = new_state
    ```
  - 影响: 信号在这里仅是变更后的通知，不是状态变更的执行通道；从工作线程调用 mark_dirty/clear_dirty 会在该线程原地写 `_page_dirty_states`，与 UI 线程 `is_dirty()` 的 `any(... for ... in values())` 迭代并发时可能 RuntimeError（dict changed size during iteration）或状态撕裂。文档承诺会诱导调用方放心跨线程使用。
  - 建议: 要么用 threading.Lock 保护状态字典读写，要么删除/改写线程安全声明，并要求外部通过 queued signal 槽进入（提供 mark_dirty 的槽入口）。
  - 置信度: 中（声明与实现不符为事实；未发现当前跨线程调用点）

- [AUD-37010] [Suggestion] _on_auto_save_timer 先发射 auto_save_triggered 再无条件同步 save()：潜在双重保存，且保存回调在 UI 线程定时器内同步执行
  - 位置: src/shared/project/topos_project_controller.py:521-537
  - 证据:
    ```python
    # 发射自动保存触发信号
    self.auto_save_triggered.emit()

    # 执行保存
    self.save()
    ```
  - 影响: 若外部消费者连接 auto_save_triggered 并自行保存（信号存在的自然意图），将与紧随的 `self.save()` 形成一次触发两次保存；保存回调（.topos ZIP 写入，可能含大体积资产清单）在 QTimer 回调内于 UI 线程同步执行，大工程下冻结界面，违背项目"耗时操作不入 UI 线程"的分层原则。
  - 建议: 二选一：只发信号由消费者执行异步保存；或去掉信号只走 save() 并把 save() 改为投递到任务队列异步执行。
  - 置信度: 中（双重保存依赖假设的消费者；同步阻塞为结构性事实）

- [AUD-37011] [Suggestion] save() 以宽泛 except Exception 吞掉领域错误且仅记录消息字符串（无堆栈）
  - 位置: src/shared/project/topos_project_controller.py:427-430
  - 证据:
    ```python
        except Exception as e:
            logger.error(f"Save exception: {e}")
            self.save_completed.emit(False)
            return False
    ```
  - 影响: 磁盘满、权限、序列化领域错误等根因只剩一条消息文本（`{e}` 常为空串），堆栈丢失，违背铁律"不得宽泛 except 掩盖领域错误"的字面要求；排障成本高。行为上返回 False 并发射 save_completed(False)、保留 dirty，无数据丢失。
  - 建议: 改为 `logger.exception("Save failed")` 保留堆栈；对 OSError 与领域异常分层处理，其余异常应上抛或至少记录完整类型信息。
  - 置信度: 高

- [AUD-37012] [Suggestion] save_page 成功分支为空操作死代码，且文档语义（清除该页 dirty）与实现（save() 清除全部页面 dirty）不符
  - 位置: src/shared/project/topos_project_controller.py:432-457
  - 证据:
    ```python
        success = self.save()

        if success:
            # 如果保存回调保存了所有页面数据，则清除所有 dirty
            # 否则仅清除指定页面
            # 这里假设保存回调保存了所有 dirty 页面的数据
            pass

        return success
    ```
  - 影响: `if success: pass` 为死代码；save() 内部调用 `clear_all_dirty(reason="saved")`，若保存回调实际只持久化部分页面，未被保存页面的 dirty 也被清除，未保存更改追踪丢失（docstring :436-438 承诺的是"清除该页面的 dirty 状态"）。当前控制器未接入生产，风险潜伏。
  - 建议: 删除死代码；明确回调契约——全量保存走 save()，页面级保存则回调接收 page 参数并仅 clear_dirty(page)。
  - 置信度: 高（行为与代码事实确定）

- [AUD-37013] [Suggestion] 未使用的导入（Optional、field）与方法内重复局部 import time
  - 位置: src/shared/project/topos_project_controller.py:23, 25（:232/:277/:315/:393 重复 `import time`）
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from enum import Enum
    from typing import Any, Callable, Optional
    ```
  - 影响: DirtyState 未用 field，全文用 `X | None` 语法未用 Optional，属死导入；四个方法各自局部 `import time` 冗余，增加噪音。
  - 建议: 删除未用导入，time 提升为模块级导入。
  - 置信度: 高

### 通过项
- 信号防递归：dirty_changed/global_dirty_changed 仅在状态翻转时发射，控制器不自连信号，无递归风险
- 状态提交一致性（dirty 语义）：save 成功清 dirty、失败保留 dirty 的主路径正确；clear_dirty 仅在所有页 clean 时才发 global_dirty_changed(False)，转换信号语义正确
- 异常与取消路径：save() 无回调/无 dirty 时提前返回的行为明确（:395-402）
- reset() 停止定时器并清状态，无 QTimer 泄漏（QTimer 以 self 为父对象）
- from_dict 对未知 page_type 用 ValueError 分支过滤并告警（:581-582），该处输入验证正确
- 类型注解完整
