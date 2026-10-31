# batch-369 审查报告 — src/shared/project/

编号区间：AUD-36900–AUD-36999
审查日期：2026-09-28
文件：payload_cache.py (228)、project_manager.py (710)、save_router.py (1,529)

跨文件核实说明（只读 grep 证实）：
- `ProjectArchivePayload` 被 `src/shared/project/topos_project_io.py` 的 save()（:369 load → :525 write）实际使用，且 save() 本身是 mkstemp+os.replace 原子写。
- `ProjectManager` 全仓库唯一实例化点是 `src/shared/project/topos_project_read_service.py:154`（仅调用 `load_project` 读旧格式）；`save_project / save_project_to_path / schedule_autosave / backup_project` 在全仓库无任何调用方，autosave 现由 file_controller 的 QTimer → `save_router.save()` 承担。
- `EditProject`（src/features/edit/core/project.py:956 `@dataclass(frozen=True)`）除 save_router 重建时传入的字段外，还有 `smart_bins`、`render_cache_manifests`、`render_cache_settings`、`proxy_policy`、`revision` 等字段。
- `on_edit_project_updated` 回调已在 file_controller.py:1200 注册，会用新项目替换内存中的当前项目。

---

## src/shared/project/payload_cache.py (行数: 228)

### 发现

- [AUD-36901] [Major] 保存时 assets/ 大二进制全量读入内存并整包重写，内存峰值与逐次全量重压缩随工程规模线性放大
  - 位置: src/shared/project/payload_cache.py:99
  - 证据:
    ```python
    # Skip assets directory entries (large binary data)
    if name.startswith("assets/"):
        payload.unknown_files[name] = zf.read(name)
        continue
    ```
    （topos_project_io.py:369 `payload = ProjectArchivePayload.load_from_file(filepath)` → :525 `payload.write_to_zip(zf)`，每次保存含 autosave 均执行）
  - 影响: 注释写 "Skip assets" 但实际把所有 assets 条目（含烘焙 EXR）完整读进 RAM；ToposProjectIO.save 每次保存（含周期性 autosave）都要整包读入 + 全部条目（未修改的 assets 原样字节）再走 ZIP_DEFLATED 重新压缩写出。多烘焙 4K HDR EXR 的工程每次保存产生数百 MB 内存峰值与无谓的重复压缩 CPU 开销。
  - 建议: 未修改的 assets/ 条目改为 zip-to-zip 流式转写（或 ZIP_STORED 直拷原始压缩数据）；payload 缓存只保留 JSON 模块的解析结果与小文件。
  - 置信度: 中（结构性事实为高；实际影响取决于工程资产规模）

- [AUD-36902] [Major] load_from_file 把"文件存在但读取失败"与"文件不存在"同化为空 payload，后续保存会静默丢弃其他产品的模块数据
  - 位置: src/shared/project/payload_cache.py:148-156
  - 证据:
    ```python
    if not filepath or not os.path.exists(filepath):
        return ProjectArchivePayload()

    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            return ProjectArchivePayload.read_from_zip(zf)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Cannot read existing .topos for payload cache: %s", exc)
        return ProjectArchivePayload()
    ```
  - 影响: 瞬时 I/O 错误（Windows 文件被占用、网络盘抖动、BadZipFile）只打 warning 并返回空 payload；调用方（topos_project_io.save）随后用空 payload 重建 ZIP，其他产品写入的 edit/timelines、color_stacks 等模块数据被整体丢弃且无任何失败信号——正是该类文档字符串声称要防止的事故。
  - 建议: 区分"不存在"（返回空 payload）与"存在但不可读"（抛出或返回 None 让 save() 终止）；后者必须让保存失败而不是继续覆写。
  - 置信度: 中

- [AUD-36903] [Suggestion] manifest.json/project.json 解析失败时 write_to_zip 产出重复同名 zip 条目
  - 位置: src/shared/project/payload_cache.py:104-116, 169-181
  - 证据:
    ```python
    if name == "manifest.json":
        try:
            payload.manifest = json.loads(zf.read(name))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload.unknown_files[name] = zf.read(name)
        continue
    ...
    # 1. manifest
    self._write_json(zf, "manifest.json", self.manifest)
    ...
    # 4. unknown / binary files
    for path, raw in self.unknown_files.items():
        zf.writestr(path, raw)
    ```
  - 影响: manifest 损坏时 `manifest` 保持默认 `{}`，第 1 步写出空 dict 条目，第 4 步又写出原始字节条目，ZIP 内出现两个 `manifest.json`；后续读取取决于 NameToInfo 的覆盖顺序，语义含混、部分解压工具行为不一致。
  - 建议: 解析失败时把 `manifest`/`project_json` 标记为"保留原始字节"（写入时跳过第 1/2 步），或在 write_to_zip 里跳过与 unknown_files 重名的路径。
  - 置信度: 高

- [AUD-36904] [Suggestion] 解析失败分支对同一 zip 条目重复 zf.read；未知 .json 解析结果未校验顶层类型为 dict
  - 位置: src/shared/project/payload_cache.py:106-108, 121-123, 129-134
  - 证据:
    ```python
    payload.manifest = json.loads(zf.read(name))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload.unknown_files[name] = zf.read(name)
    ...
    if name.endswith(".json"):
        try:
            payload.module_payloads[name] = json.loads(raw)
    ```
  - 影响: 失败路径每个条目读两次字节（大文件翻倍 I/O）；顶层为数组/数字的 .json 会以非 dict 值进入 `module_payloads: dict[str, dict[str, Any]]`，违反自身类型约定，下游按 dict 访问时抛 KeyError/TypeError。
  - 建议: 先读一次存局部变量再 parse；`json.loads` 后 `isinstance(parsed, dict)` 校验，失败落入 unknown_files。
  - 置信度: 高

### 通过项
- 线程安全：纯数据结构，无共享可变状态被并发访问的迹象（由调用方单线程使用）
- GPU 资源生命周期：不涉及 OpenGL/ModernGL
- GLSL/HDR 正确性：不涉及
- 信号防递归：不涉及 Qt 信号
- 异常路径：json 解析失败均回退原始字节保留，无静默丢数据（除 36902/36903 所列）
- 类型注解：整体完整（dataclass 字段、方法签名均有注解）

---

## src/shared/project/project_manager.py (行数: 710)

### 发现

- [AUD-36910] [Major] 写路径 save_project/save_project_to_path 非原子直接覆写 .topos，宽泛 except 吞掉全部失败且无成败返回值（潜伏数据丢失模式；全仓库当前无调用方）
  - 位置: src/shared/project/project_manager.py:446-463, 588-597
  - 证据:
    ```python
    # Create ZIP archive
    with zipfile.ZipFile(project_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(project_file_path, project_file_name)
    ...
    except Exception as e:
        logger.error(f"to save project: {e}")
        import traceback
        traceback.print_exc()
    ```
  - 影响: 以 `'w'` 模式直接打开既有 .topos，写一半崩溃/磁盘满即留下截断包，唯一副本被毁（对比 topos_project_io.save 的 mkstemp+os.replace 原子写）；同时所有异常被吞、函数返回 None，调用方无从感知保存失败。当前该写路径已无调用方（仅 load 侧被读服务复用），属潜伏风险——一旦被复用即成数据丢失入口。
  - 建议: 若确认废弃则删除写路径；若保留，改为临时文件 + os.replace 原子替换，并返回 bool 供调用方判断，异常分层捕获。
  - 置信度: 高（代码事实；风险为条件性，因无调用方）

- [AUD-36911] [Major] schedule_autosave 用 threading.Timer 在后台线程遍历 engine.layers 并改写 plugin dict，无任何锁（潜伏数据竞争）
  - 位置: src/shared/project/project_manager.py:608-623, 348, 389-401
  - 证据:
    ```python
    self.autosave_timer = threading.Timer(2.0, self._perform_autosave)
    self.autosave_timer.start()
    ...
    "layers": [l.to_dict(assets_dir=assets_dir) for l in engine.layers],
    ...
    for layer in engine.layers:
        for plugin in layer.plugins:
            ...
            plugin["ai_result_file"] = filename
    ```
  - 影响: `_perform_autosave` 在 Timer 线程读取共享 ImageEngine 状态并与 UI 线程的增删图层/参数修改竞争（迭代中列表变更、快照不一致）；还会从后台线程改写 plugin dict。另有：`engine_ref` 强引用阻止引擎回收、Timer 非守护且退出时未 cancel、`pending_save` 字段声明后从未使用、cancel() 与已触发的保存之间无互斥（可重叠保存同一 zip）。当前无调用方（autosave 已改走 save_router + UI 线程 QTimer），属潜伏风险。
  - 建议: 随 36910 一并删除；或改为 UI 线程 QTimer 防抖（与 file_controller.py:6945 现行实现一致）。
  - 置信度: 高（代码事实；风险为条件性）

- [AUD-36912] [Suggestion] save_project 与 save_project_to_path 约 130 行近乎完全复制
  - 位置: src/shared/project/project_manager.py:306-463, 465-597
  - 证据:
    ```python
    data = {
        "version": "2.0",
        "_format_version": 1 if serialization_format == "json" else 2,
        "image_path": image_path,
        "layers": [l.to_dict(assets_dir=assets_dir) for l in engine.layers],
    ```
    （两个方法中的 data 构建、烘焙/插件 EXR 拷贝、msgpack/gzip 序列化、zip 写出逻辑逐行雷同）
  - 影响: 双份维护，修一处漏一处的典型温床（两处已有细微差异：save_project 末尾多 preview 逻辑）。
  - 建议: 抽出 `_build_and_write_project(output_path, engine, image_path, ...)` 单一实现。
  - 置信度: 高

- [AUD-36913] [Suggestion] 多处函数签名缺类型注解，新旧注解风格混用
  - 位置: src/shared/project/project_manager.py:270, 599, 625
  - 证据:
    ```python
    def _save_preview_cache(self, image_path, engine):
    ...
    def schedule_autosave(self, image_path, engine):
    ...
    def backup_project(self, image_path: str, engine, backup_dir: str = "", max_backups: int = 5) -> None:
    ```
  - 影响: 违反项目强类型约定；同文件上半部用 `Optional[Dict[str, Any]]`、save_router 用 `str | None`，风格割裂。
  - 建议: 补齐参数/返回注解并统一为新式 `X | None`。
  - 置信度: 高

- [AUD-36914] [Suggestion] 烘焙缓存命中判断仅比较文件大小，同名同大小不同内容会误判为已最新
  - 位置: src/shared/project/project_manager.py:156-157, 165-166, 177-178
  - 证据:
    ```python
    baked_dst_path = os.path.join(FileIO.BAKED_CACHE_DIR, layer.baked_file_name)
    if not (os.path.exists(baked_dst_path) and os.path.getsize(baked_dst_path) == os.path.getsize(baked_src_path)):
        shutil.copy2(baked_src_path, baked_dst_path)
    ```
  - 影响: baked_file_name 以内容无关方式命名时（如按图层 id），同尺寸的旧缓存被保留，加载到过期烘焙结果。
  - 建议: 文件名含内容哈希，或命中校验改用（部分）哈希。
  - 置信度: 中

- [AUD-36915] [Suggestion] _save_preview_cache 以 os.getcwd() 为缓存根目录，且缩略图数据格式约定不明
  - 位置: src/shared/project/project_manager.py:273-297
  - 证据:
    ```python
    cache_dir = os.path.join(os.getcwd(), ".cache")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    ...
    img_data = engine.get_render_result()
    ...
    cv2.imwrite(cache_path, img_data, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    ```
  - 影响: 缓存位置随启动目录漂移，不同启动方式产生多份 .cache；exists/makedirs 存在 TOCTOU；cv2.imwrite 期待 BGR uint8，若 get_render_result 返回 RGB 或 HDR float，缩略图将通道反转或全黑（该路径当前无调用方，属低危）。
  - 建议: 缓存目录锚定到用户数据目录；makedirs(exist_ok=True)；写盘前显式转换色彩空间与位深。
  - 置信度: 低（cwd 与 TOCTOU 为事实；色彩空间问题取决于 get_render_result 实现，未核实）

### 通过项
- 读取路径（load_project/_load_zip_project/_load_project_file）：JSON/gz/msgpack 多格式回退顺序清晰，坏文件返回 None 不崩溃；extractall 在 Python 3.6+ 已过滤 `..` 路径穿越
- 状态提交一致性：legacy 读写字段对称（layers/raw_params/source_bake/flip_state/composite_graph/composite_state/video_denoise 两端一致）
- GPU 资源生命周期：不直接触碰 OpenGL（get_render_result 仅取 numpy 结果）
- 信号防递归：不涉及 Qt 信号
- 输入验证：load 侧对不存在的 project_path、非 ZIP 魔数（PK 检测）均有守卫

---

## src/shared/project/save_router.py (行数: 1,529)

### 发现

- [AUD-36920] [Critical] Composite 保存用显式字段列表重建 EditProject，漏传 smart_bins/render_cache_manifests/render_cache_settings/proxy_policy/revision，一次保存即清空这些数据（内存 + 落盘双重丢失）
  - 位置: src/shared/project/save_router.py:916-930
  - 证据:
    ```python
    updated_project = edit_project.__class__(
        project_id=edit_project.project_id,
        name=edit_project.name,
        assets=edit_project.assets,
        bins=edit_project.bins,
        timelines=edit_project.timelines,
        compositions=new_compositions,
        color_stacks=edit_project.color_stacks,
        comp_clips=edit_project.comp_clips,
        root_bin_id=edit_project.root_bin_id,
        active_timeline_id=edit_project.active_timeline_id,
        active_comp_clip_id=edit_project.active_comp_clip_id,
        created_at=edit_project.created_at,
        modified_at=now,
    )
    ```
    而 EditProject（src/features/edit/core/project.py:981-1004，`@dataclass(frozen=True)`）还定义有：
    ```python
    smart_bins: dict[str, SmartBin] = field(default_factory=dict)
    render_cache_manifests: dict[str, Any] = field(default_factory=dict)
    render_cache_settings: Any = None
    proxy_policy: Any = None
    ...
    revision: int = 0
    ```
  - 影响: 被漏掉的字段全部有默认值，构造不报错、静默归零。随后 :933 `on_edit_project_updated`（file_controller.py:1200 已注册）用被削空的项目替换内存当前工程，:938 再把它落盘——用户在 Composite 页按一次 Save，P1-12 的用户自建 Smart Bin、渲染缓存清单与设置、P1-14 代理策略全部丢失，TAEP P0 的项目 revision 归零破坏 AI plan base_revision 乐观锁一致性。同文件 color 路径（:824-829）对生产 frozen dataclass 正确使用 `replace()`，只有 composite 路径始终走显式构造（color 路径 :834-848 的非 dataclass 回退分支有同样遗漏，但注释确认生产环境不进入该分支）。
  - 建议: composite 路径改用 `dataclasses.replace(edit_project, compositions=new_compositions, modified_at=now)`；如需兼容测试替身，与 color 路径一样保留 replace 优先分支；为 EditProject 增加一个 `with_overrides` 工厂统一所有"拷贝更新"入口，杜绝字段清单式重建。
  - 置信度: 高

- [AUD-36921] [Major] 自动新建调色栈后未把 stack_id 同步回会话/clip（对比 composite 路径的 on_composite_id_created），无 stack_id 的保存路径反复保存会堆叠孤儿栈
  - 位置: src/shared/project/save_router.py:751-756, 790-822, 852
  - 证据:
    ```python
    if not stack_id:
        stack_id = self._call_callback("get_current_stack_id")
    if not stack_id:
        # 如果没有 stack_id，创建新的
        stack_id = str(uuid.uuid4())
    ...
    new_stacks[stack_id] = new_stack
    ...
    self._call_callback("on_edit_project_updated", updated_project)
    ```
    （对比 composite 路径 :897-901 有 `self._call_callback("on_composite_id_created", composite_id)` 回传新 ID）
  - 影响: 新建栈的 stack_id 既不回写 ColorSession 也不回写 clip.color_stack_id/asset 默认栈；同一素材在无 stack_id 的回退路径下每按一次 Save 都生成一个新 UUID 栈，工程内堆叠互不引用的孤儿调色栈，重载后剪辑可能取不到最新调色。上游 page_controller 的 `_ensure_clip_has_color_stack`（clip 进 Color 时）已缓解 clip 主路径，asset 级/无会话回退路径仍暴露。
  - 建议: 仿照 composite 路径增加 `on_stack_id_created` 回调把新 stack_id 回写到会话与 clip/asset；或保存前强制要求 stack_id 存在（缺失视为配置错误返回 False）。
  - 置信度: 中

- [AUD-36922] [Suggestion] backup_project 存在恒假死条件与重复检查，且在调用线程同步执行完整 save()（UI 阻塞风险）
  - 位置: src/shared/project/save_router.py:1167-1186
  - 证据:
    ```python
    source_file_path = self._call_callback("get_current_file_path")
    topos_path = self._resolve_backup_source_path()

    if not topos_path:
        logger.warning("Backup skipped: no current project path")
        return False

    if not topos_path or not os.path.exists(topos_path):   # "not topos_path" 恒假
        if not self.save():
    ```
    （`_resolve_backup_source_path` 内部已再次调用 get_current_file_path，:1179 的 fallback 与其重复）
  - 影响: 死条件与双重路径解析降低可读性、掩盖真实流程；sync 备份在 UI 线程调用时会先做一次完整工程保存（zip 写出）再复制，大工程明显卡顿（异步版 backup_project_async 已存在，此同步版仍被保留）。
  - 建议: 删除恒假条件与重复解析；sync 版仅做复制（必要时先调 save 的决策交给调用方）或统一走异步版。
  - 置信度: 高

- [AUD-36923] [Suggestion] _backup_in_progress 检查-置位非原子，且未在 __init__ 声明
  - 位置: src/shared/project/save_router.py:1136-1151
  - 证据:
    ```python
    if getattr(self, "_backup_in_progress", False):
        logger.debug("Backup already running; skipping overlapping request")
        return False
    ...
    self._backup_in_progress = True

    def _run() -> None:
        try:
            self._write_backup_copy(topos_path, backup_dir, max_backups)
        finally:
            self._backup_in_progress = False
    ```
  - 影响: check-then-set 之间无锁，两个近乎同时的请求可都通过检查，起两个备份工作线程（后果轻：多一份备份）；标志用 getattr 默认值访问而非构造时声明，依赖动态属性。已核实与保存并发无torn-read 风险：ToposProjectIO.save 为 mkstemp+os.replace 原子替换。
  - 建议: __init__ 中声明 `_backup_in_progress = False`；用 threading.Lock 做"未运行则占用"的原子测试置位。
  - 置信度: 高（机制），影响低

- [AUD-36924] [Suggestion] _save_composite_to_edit_project 异常分支未记录堆栈，排障信息弱于同类路径
  - 位置: src/shared/project/save_router.py:944-946
  - 证据:
    ```python
        except Exception as e:
            logger.error(f"Composite save to edit project failed: {e}")
            return False
    ```
    （对比 :863-867 color 路径、:662-666 单素材路径均有 `traceback.print_exc()`）
  - 影响: 合成保存失败只剩一行异常消息，无法定位是 ID 同步、构造还是序列化抛错。
  - 建议: 统一 `logger.exception(...)`（顺带替代 print_exc 直写 stdout 的做法）。
  - 置信度: 高

### 通过项
- 线程安全：备份工作线程与保存线程间无 torn-read（ToposProjectIO.save 原子替换已核实）；_write_backup_copy 使用 mkstemp+os.replace+冲突重试，自身正确
- GPU 资源生命周期：不触碰 OpenGL；引擎状态提取（layer.to_dict）为纯数据序列化
- GLSL/HDR 正确性：不涉及
- 信号防递归：不涉及 Qt 信号（回调模式无递归触发点）
- 状态提交一致性：color 路径对 frozen dataclass 用 replace() 保字段、更新后经 on_edit_project_updated 同步内存再落盘，顺序正确（composite 路径见 AUD-36920）
- 异常与取消路径：各 save 入口均返回 bool，异常不外泄崩溃（弱项见 AUD-36924）
- 输入验证：_detect_media_type 明确拒绝 video 走单素材工程（Schema v2 红线）；_convert_to_topos_path 剥离 .autosave/.bak 后缀防止 demo.topos.autosave → demo.topos.topos；video asset 无 timelines 时 stack 工厂选择 create_standalone_image 的边界已按 asset_id 判定
- 性能模式：备份冲突用时间戳+微秒+碰撞计数；备份清理按 mtime 排序限量
- 类型注解：整体完整（回调、handler、target 均有注解；_call_callback 的 *args/**kwargs 宽松可接受）

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 1 | AUD-36920 |
| Major | 5 | AUD-36901, AUD-36902, AUD-36910, AUD-36911, AUD-36921 |
| Suggestion | 9 | AUD-36903, AUD-36904, AUD-36912, AUD-36913, AUD-36914, AUD-36915, AUD-36922, AUD-36923, AUD-36924 |
