# batch-368 审查报告 — src/shared/project/（P2）

编号区间：AUD-36800–AUD-36818（区间 AUD-36800–AUD-36899 内）
审查范围：8 个文件，2,125 行，全部完整 Read。
统计：Critical 0 / Major 6 / Suggestion 13。

---

## src/shared/project/__init__.py (行数: 38)

### 发现

（无）

### 通过项
- 纯 re-export 模块，无逻辑：导出 ColorStackData、SaveRouter/SaveContext/SaveTarget、ToposProject、ToposManifest/ProjectMode、ToposProjectIO/ToposProjectIOConfig、ToposProjectController/PageType/DirtyState、SCHEMA_REGISTRY_V1/get_schema/validate_json、ProjectArchivePayload、ProjectDocument
- `__all__` 与实际导入一一对应，无遗漏/多余导出
- 不涉及线程/GPU/GLSL/信号/持久化逻辑

---

## src/shared/project/color_stack.py (行数: 340)

### 发现

- [AUD-36800] [Suggestion] from_dict 未对外部数值字段做类型校验/归一
  - 位置: src/shared/project/color_stack.py:308-310
  - 证据:
    ```python
    lut_path=data.get("lut_path"),
    lut_intensity=data.get("lut_intensity", 1.0),
    created_at=data.get("created_at", 0.0),
    modified_at=data.get("modified_at", 0.0),
    ```
    而 `__post_init__` 中（111-112 行）：
    ```python
    if self.lut_intensity < 0.0 or self.lut_intensity > 1.0:
        raise ValueError(f"lut_intensity must be between 0.0 and 1.0")
    ```
  - 影响: JSON 为外部数据（.topos 工程包）。`lut_intensity` 为字符串时 `__post_init__` 的 `<` 比较抛 TypeError（而非清晰的 ValueError）；`created_at`/`modified_at` 为字符串时静默原样存储并在 to_dict 回写，破坏时间戳类型契约。
  - 建议: from_dict 对 lut_intensity 做 `float()` 归一并捕获 (TypeError, ValueError)，对 created_at/modified_at 同样 `float(... or 0.0)` 归一。
  - 置信度: 高

- [AUD-36801] [Suggestion] frozen dataclass 的 dict 字段经 replace/from_dict 后为共享可变引用
  - 位置: src/shared/project/color_stack.py:59-105, 196-198
  - 证据:
    ```python
    grading_params: dict[str, Any] = field(default_factory=lambda: {
        "lift": (0, 0, 0),
        ...
    })
    ...
    def with_grading_params(self, params: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新调色参数"""
        return replace(self, grading_params=params, modified_at=time.time())
    ```
  - 影响: "frozen + with_* 复制"暗示不可变快照语义，但 `replace`/`from_dict` 均直接持有调用方 dict。调用方原地修改 dict 会同时改变所有"副本"（含 undo 历史/对比场景），且难以排查。
  - 建议: 在 `__post_init__` 中对 dict 字段做浅拷贝（`object.__setattr__`），或文档明确"调用方不得原地修改"。
  - 置信度: 高（机制确定；实际影响取决于调用方纪律）

- [AUD-36802] [Suggestion] 调色参数默认值混用 int 字面量，违反"浮点字面量显式 .0"铁律
  - 位置: src/shared/project/color_stack.py:60-73
  - 证据:
    ```python
    "lift": (0, 0, 0),
    "gamma": (0, 0, 0),
    "gain": (0, 0, 0),
    "exposure": 0,
    "temp": 0,
    ```
  - 影响: exposure/temp/tint/contrast 等浮点参数以 int 0 落盘并最终进入调色管线；与 `.clinerules`"浮点字面量显式 .0"约束不一致，下游若做严格类型/schema 校验会产生 int/float 分歧。
  - 建议: 默认值统一写 `0.0` / `(0.0, 0.0, 0.0)`。
  - 置信度: 中（Python 运行时无害，属规范一致性问题）

### 通过项
- 状态提交一致性：to_dict/from_dict 双向对称；schema_version 恒写、layers 非空才写、旧工程回退平铺键的策略与模块契约一致
- 异常路径：`__post_init__` 对 stack_id/lut_intensity 早失败；无宽泛 except
- with_* 工厂统一走 dataclasses.replace，新增字段自动携带（193-194 行注释明确了此历史教训）
- 类型注解完整（含返回类型）
- 线程安全：不可变值对象，无共享可变模块状态
- GPU/GLSL/信号防递归不涉及

---

## src/shared/project/document.py (行数: 32)

### 发现

（无）

### 通过项
- 纯 Protocol 声明（`document_type: str`、`dirty: bool`、`save_to_path`、`load_from_path`），无实现逻辑
- 跨 feature 保存/加载解耦协议定义清晰，类型注解完整

---

## src/shared/project/edit_project_adapter.py (行数: 140)

### 发现

（无）

### 通过项
- 异常路径：所有未注册调用统一抛 RuntimeError 且消息指明修复方式（"Call edit_project_adapter.register() at app startup"），无静默吞错
- 分层边界：shared 不 import features，通过启动期注册反转依赖（文档化的适配器模式）
- 单例仅在启动注册一次，运行期只读；无锁可接受
- 类型注解完整；`is_edit_project` 未注册时返回 False 为文档化行为
- GPU/GLSL/HDR/信号防递归不涉及

---

## src/shared/project/layer_adapter.py (行数: 43)

### 发现

- [AUD-36803] [Suggestion] `_get_layer_class` 与 `layer_from_dict` 类型注解不完整
  - 位置: src/shared/project/layer_adapter.py:20, 29
  - 证据:
    ```python
    def _get_layer_class():
        """Lazily resolve the Layer class from features.color."""
    ...
    def layer_from_dict(data: dict, assets_dir: Optional[str] = None) -> Any:
    ```
  - 影响: `_get_layer_class` 无参数/返回类型注解；`data: dict` 未参数化（应为 `dict[str, Any]`）。与项目"所有函数有类型注解"约束不一致。
  - 建议: 补 `def _get_layer_class() -> type:` 与 `data: dict[str, Any]`。
  - 置信度: 高

- [AUD-36804] [Suggestion] 懒加载 import_module 使 shared→features 运行时依赖推迟到首次反序列化才暴露
  - 位置: src/shared/project/layer_adapter.py:23-25
  - 证据:
    ```python
    if _layer_cls is None:
        module = import_module("src.features.color.core.layer")
        _layer_cls = module.Layer
    ```
  - 影响: 硬编码模块路径在 features.color 重构/改名后于首次 `layer_from_dict` 调用时才抛 ModuleNotFoundError/AttributeError，错误位置远离根因；测试或裁剪构建中 shared 单独导入也会延迟失败。
  - 建议: 改用与 edit_project_adapter 相同的注册反转模式，或至少在 import 失败时抛带上下文的明确异常并记 log。
  - 置信度: 中

### 通过项
- 线程安全：全局 `_layer_cls` 缓存的并发双重导入为幂等竞态，无害
- 异常路径：无吞错
- GPU/信号/HDR/持久化不涉及

---

## src/shared/project/library.py (行数: 648)

### 发现

- [AUD-36805] [Major] read_project_meta 的 float() 转换位于受保护 try 之外，单个畸形档案使整库扫描失败
  - 位置: src/shared/project/library.py:407-408（对照 373-379, 445）
  - 证据:
    ```python
    try:
        with zipfile.ZipFile(path, "r") as zf:
            manifest = _read_zip_json(zf, "manifest.json") or {}
            project = _read_zip_json(zf, "project.json") or {}
    except (zipfile.BadZipFile, OSError) as exc:
        ...
    ```
    而 float 转换在 try 块之外（407-408 行）：
    ```python
    created_at=float(manifest.get("created_at", 0.0) or project.get("created_at", 0.0)),
    modified_at=float(manifest.get("modified_at", 0.0) or file_mtime),
    ```
    scan_video_projects 无独立防护（445 行）：`meta = read_project_meta(absolute)`
  - 影响: manifest.json 中 `created_at` 为非数值字符串（手改/第三方工具生成的 .topos）时抛 ValueError，违反本函数"Returns None if the archive is malformed"的契约；上层 `scan_video_projects` 不捕获（两个调用方 project_manager_dialog.py:88、menu_toolbar_controller.py:948 的 except Exception 兜底后返回空列表），导致 Project Manager 整库显示为空。
  - 建议: 将 ProjectMeta 构造（含 float 转换）并入同一 try，或捕获 (ValueError, TypeError) 返回 None。
  - 置信度: 高（机制确定；触发需外部畸形数据）

- [AUD-36806] [Major] get_project_backup_dir 的 mkdir 无 OSError 防护，扫描/列表/重命名路径直接抛异常
  - 位置: src/shared/project/library.py:152
  - 证据:
    ```python
    if configured:
        directory = Path(configured).expanduser()
    else:
        directory = Path(project_path).expanduser().parent / PROJECT_BACKUP_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory
    ```
    对照同文件 get_project_library_dir（95-98 行）有防护：
    ```python
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create project library dir %s: %s", p, exc)
    ```
    无防护调用点：list_project_backups:263、scan_video_projects:428、_move_project_backups:541。
  - 影响: 用户配置的 `backup_directory` 只读/权限不足/卷已卸载时，`mkdir` 抛 OSError 且不被捕获——备份列表、库扫描（含 `__library__.topos` 哨兵探测 428 行）、重命名流程全部中断。另外扫描（读操作）经此调用产生创建 `backups/` 目录的写副作用。
  - 建议: mkdir 包 try/except OSError 降级为 warning（与 get_project_library_dir 一致）；扫描路径改为纯探测（exists 判断）不创建目录。
  - 置信度: 高（机制确定；触发需特定环境）

- [AUD-36807] [Major] rename_project 失败回滚丢失"部分搬移"状态，伴随文件滞留新名称下
  - 位置: src/shared/project/library.py:522-525, 579-587
  - 证据: `_move_related_project_files` 在 os.replace 成功后才 append，异常时部分搬移结果不返回：
    ```python
    for old, new in related:
        if old.exists():
            os.replace(old, new)
            moved.append((old, new))
    ```
    rename_project 的回滚使用（可能为空的）已赋值列表：
    ```python
    try:
        sidecar_moves = _move_related_project_files(source, target)
        backup_moves = _move_project_backups(source, target, config)
        source.unlink()
    except Exception:
        _rollback_moves(backup_moves)
        _rollback_moves(sidecar_moves)
    ```
  - 影响: 多文件搬移中途失败（权限/磁盘满）时，已搬移的 `.autosave`/`.bak`/时间戳备份不被回滚（moves 列表因异常未返回而保持空/部分），而 `target.unlink()` 又删除了重命名后的主档案——用户工程回到旧名，但其 autosave 与备份历史滞留在不存在的新名称下，自动保存保护丢失。
  - 建议: 搬移辅助函数内部自行 try/except 并返回已完成 moves（含失败前部分），或由调用方以逐文件 try 包裹并收集。
  - 置信度: 高（代码路径确定）

- [AUD-36808] [Suggestion] duplicate_project 在归档缺 project_id 键时副本与原件共享 ID，破坏"独立副本"契约
  - 位置: src/shared/project/library.py:498-501, 605-610
  - 证据:
    ```python
    if info.filename == "project.json" and name is not None:
        payload["name"] = name
    if project_id is not None and "project_id" in payload:
        payload["project_id"] = project_id
    ```
    duplicate_project 传入 `project_id=str(uuid.uuid4())`（609 行），但仅当载荷中已存在 `project_id` 键才改写。
  - 影响: 旧格式（无 project_id 键）工程复制后仍与原件同 ID（或同为缺失）；在共享自定义备份目录场景下，list_project_backups 的 ID 过滤（282 行 `if project_id and backup_project_id and ...`）因两侧为空而失效，两工程的备份互相可见，存在跨工程恢复/删除风险。
  - 建议: duplicate_project 对缺失 project_id 的载荷也应强制写入新 ID（去掉 `"project_id" in payload` 前置条件，或对 project.json/manifest.json 均无条件写入）。
  - 置信度: 中（需旧格式归档触发；现行 ToposManifest 校验非空 project_id，新工程不受影响）

- [AUD-36809] [Suggestion] restore_project_backup 仅捕获 OSError，非 OSError 失败泄漏自身 mkstemp 临时文件
  - 位置: src/shared/project/library.py:323-339
  - 证据:
    ```python
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{project.stem}.restore.", suffix=".topos.tmp", dir=str(project.parent)
    )
    os.close(fd)
    try:
        _rewrite_project_archive(backup, temporary_name, name=project.stem)
        os.replace(temporary_name, project)
        ...
        return True
    except OSError as exc:
    ```
  - 影响: 源备份在 is_zipfile 预检后、读取前损坏（TOCTOU/截断）时 `_rewrite_project_archive` 抛 `zipfile.BadZipFile`（非 OSError 子类），异常穿透本函数；restore 自建的空临时文件 `.xxx.restore.*.topos.tmp` 残留在用户工程目录（_rewrite 只清理它自己的内部临时文件）。
  - 建议: 捕获范围扩为 `(OSError, zipfile.BadZipFile)` 或 `except Exception` 统一清理后 re-raise。
  - 置信度: 高（机制确定；触发频率低）

- [AUD-36810] [Suggestion] _move_to_trash 缺 send2trash 时静默退化为永久删除
  - 位置: src/shared/project/library.py:616-625
  - 证据:
    ```python
    try:
        from send2trash import send2trash  # type: ignore
    except ImportError:
        send2trash = None
    if send2trash is not None:
        send2trash(str(target))
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    ```
  - 影响: delete_project 文档承诺"Move ... to trash"，但在 send2trash 不可用的环境（依赖损坏安装/裁剪打包）下静默 `rmtree`/`unlink` 永久删除工程与全部备份，无任何确认或日志。send2trash 虽为硬依赖（requirements.txt:73），该回退路径仍是隐蔽的数据丢失通道。
  - 建议: ImportError 时记 warning 日志并改为抛异常（或要求调用方确认永久删除），不应静默降级。
  - 置信度: 高（代码事实；触发需依赖缺失环境）

- [AUD-36811] [Suggestion] config 访问处多处宽泛 `except Exception` 掩盖配置层错误
  - 位置: src/shared/project/library.py:86-89（同型：106-109, 116-119, 143-147, 347-354）
  - 证据:
    ```python
    custom = ""
    if config is not None:
        try:
            custom = config.get("project_library_directory") or ""
        except Exception:
            custom = ""
    ```
  - 影响: 违反"不得宽泛 except 掩盖领域错误"铁律：配置系统真实故障（损坏文件、序列化错误）被降级为静默默认值，库目录/备份目录悄悄切到默认位置，问题难以定位。
  - 建议: 收窄为配置层实际抛出的异常类型，或至少 `logger.debug(..., exc_info=True)` 保留线索（347-354 的 register 函数已如此，可对齐）。
  - 置信度: 中

### 通过项
- 状态提交一致性：备份写入统一 copy→tmp→`os.replace` 原子替换（_archive_current_project、_rewrite_project_archive），失败清理临时文件
- 数据保护：restore 前强制先快照当前工程，快照失败拒绝恢复并返回 False（318-322 行注释明确该不变量）
- 输入验证（zip 层）：外部 zip 读取统一捕获 BadZipFile/OSError（_read_zip_json、_read_project_id、extract_cover_thumbnail）；`zipfile.is_zipfile` 预检 restore 源
- 备份归属：共享备份目录按 project_id 过滤，防止跨工程恢复/删除（276-283 行注释明确威胁模型）
- delete_project 顺序：主工程最后删除，伴生文件失败时保留可编辑工程
- rename 同名原路径分支：tmp 写入后原子替换自身，失败不损原档案
- 性能：read_project_meta 仅读 manifest/project 两个成员，不做全量加载；扫描结果按时间去重排序
- 线程安全：模块无共享可变状态（文件系统 TOCTOU 竞态影响低，未计缺陷）
- 类型注解基本完整

---

## src/shared/project/manifest.py (行数: 248)

### 发现

- [AUD-36812] [Suggestion] from_dict 静默接受高于当前版本的 schema_version，且版本/时间戳字段无类型校验
  - 位置: src/shared/project/manifest.py:133-135, 146-147
  - 证据:
    ```python
    schema_version = data.get("schema_version", 1)
    if schema_version < TOPOS_SCHEMA_VERSION:
        schema_version = TOPOS_SCHEMA_VERSION
    ```
    ```python
    created_at=data.get("created_at", 0.0),
    modified_at=data.get("modified_at", 0.0),
    ```
  - 影响: manifest.json 为外部数据。未来版本（v4+）档案被当前构建静默当作可理解格式加载并可能回存——本类只序列化已知字段，未知 v4 字段丢失；另外 `schema_version`/`created_at` 为字符串时（`str < int`）抛 TypeError 而非清晰错误。
  - 建议: `schema_version > TOPOS_SCHEMA_VERSION` 时抛明确异常或记 warning 拒载；对数值字段做 `int()`/`float()` 归一与异常包装。
  - 置信度: 中（前向档案当前不存在；机制确定）

- [AUD-36813] [Suggestion] with_modified/with_active_timeline 手工逐字段重建，存在新增字段漏带风险
  - 位置: src/shared/project/manifest.py:202-240
  - 证据:
    ```python
    def with_modified(self) -> ToposManifest:
        """创建新实例，更新修改时间"""
        return ToposManifest(
            format=self.format,
            schema_version=self.schema_version,
            ...
            modified_at=time.time(),
    ```
  - 影响: 当前两方法核对携带全部 15 个字段（含 edit_content_schema_version），无现存错误；但同库 color_stack.py:193-194 注释明确指出"逐字段手写构造漏带（历史上 layers 这类新字段最容易这样丢）"——本文件正是该 bug 模式的暴露面，新增字段时极易漏带导致静默重置。
  - 建议: 改为 `dataclasses.replace(self, modified_at=time.time())` / `replace(self, active_timeline_id=timeline_id, modified_at=time.time())`。
  - 置信度: 高（风险模式确定；当前无实际错误）

### 通过项
- 输入验证：`__post_init__` 校验 format/schema_version>=1/project_id 非空/project_mode 合法
- 状态提交一致性：v1/v2→v3 迁移、`single_asset`→`standalone_image` 旧模式迁移路径明确；edit_content_schema_version 与包传输版本正交的注释契约清晰
- to_dict/from_dict 字段对称完整
- 不可变数据类；类型注解完整
- 线程安全/GPU/GLSL/信号防递归不涉及

---

## src/shared/project/offline_asset_manager.py (行数: 636)

### 发现

- [AUD-36814] [Major] update_offline_status 重建 ToposProject 时遗漏 revision 字段，乐观锁计数静默归零
  - 位置: src/shared/project/offline_asset_manager.py:601-619
  - 证据:
    ```python
    return ToposProject(
        project_id=project.project_id,
        name=project.name,
        project_mode=project.project_mode,
        assets=new_assets,
        ...
        created_at=project.created_at,
        modified_at=time.time(),
    )
    ```
    而 ToposProject 定义（src/shared/project/topos_project.py:449-455）：
    ```python
    # TAEP P0: 工程逻辑 revision（乐观锁，单调递增）。每次成功落地一次原子
    # timeline 变更后递增；不属于 schema 版本。...
    revision: int = 0
    ```
  - 影响: 离线状态刷新返回的新工程 revision 回退为默认 0，违背"单调递增"不变量；乐观锁失效后并发提交/AI 脚本守卫可能被绕过，造成他人提交被覆盖（丢更新）。注：ToposProject 路径的 relink_asset/batch_relink 经 ToposProjectIO.relink_assets（topos_project_io.py:907-924）存在同样遗漏，属他批次文件，此处一并提示。
  - 建议: 构造时显式携带 `revision=project.revision`（或改用 dataclasses.replace）。
  - 置信度: 高（字段遗漏确凿；运行时丢更新影响为中）

- [AUD-36815] [Major] get_offline_asset_list / update_offline_status 缺 is_virtual_asset_path 防护，topos:// 虚拟素材被误判离线
  - 位置: src/shared/project/offline_asset_manager.py:313-316, 588-589（对照 216-219）
  - 证据: get_offline_asset_list：
    ```python
    if isinstance(project, ToposProject):
        for asset in project.assets.values():
            if not os.path.exists(asset.path):
                offline_list.append(asset)
    ```
    update_offline_status（589 行）：`is_offline = not os.path.exists(asset.path)`；
    而同文件 check_offline_assets（216-219 行）有防护：
    ```python
    is_offline = (
        not is_virtual_asset_path(asset.path)
        and not os.path.exists(asset.path)
    )
    ```
  - 影响: `topos://` 应用内生成资产（is_virtual_asset_path，topos_project.py:29-31）在磁盘上必然不存在——经这两个入口被计入离线列表/被置 `is_offline=True`；auto_search_for_missing（552-563 行，数据来自 get_offline_asset_list）还会为虚拟素材按文件名搜索并生成错误的 relink 映射。同一模块内三处离线判定口径不一致。
  - 建议: 两处均补 `not is_virtual_asset_path(asset.path) and ...`，或将判定收敛为单一私有函数复用。
  - 置信度: 高

- [AUD-36816] [Major] check_offline_assets 对未知工程类型静默返回空报告，与 relink/batch_relink 的显式报错不一致
  - 位置: src/shared/project/offline_asset_manager.py:248-249（对照 353-354, 401-402）
  - 证据:
    ```python
    else:
        total_assets = 0
    ```
    随后照常构造 offline_count=0 的 OfflineAssetReport；而同类分支在 relink_asset/batch_relink 中：
    ```python
    else:
        raise ValueError(f"Unsupported project type: {type(project)}")
    ```
  - 影响: edit_project_adapter 未注册（features/edit 导入失败）或调用方传入错误对象/None 时，得到 "All assets are online"（total=0）的假阴性报告，打开工程时跳过重链接提示，问题被掩盖直到渲染时素材缺失。
  - 建议: else 分支抛 ValueError 与其余入口对齐；空工程与类型错误必须可区分。
  - 置信度: 高

- [AUD-36817] [Suggestion] auto_search_for_missing 同步递归扫描家目录大路径，UI 线程调用会长时间阻塞
  - 位置: src/shared/project/offline_asset_manager.py:557-563（对照 495-533）
  - 证据:
    ```python
    for asset in offline_list:
        filename = os.path.basename(asset.path)
        found_path = OfflineAssetManager.find_matching_file(
            filename, search_dirs, recursive=True, max_depth=3
        )
    ```
  - 影响: search_dirs 含 ~/Documents、~/Downloads 等（_suggest_search_dirs 441-448），max_depth=3 逐层 listdir+isfile 为同步 IO；素材多、目录大时可达秒级~分钟级，若在 UI 线程调用则冻结界面（4K 实时交互基准下不可接受）。
  - 建议: 移入线程池/任务队列执行，或限制每目录扫描预算并提供取消点。
  - 置信度: 中（取决于调用方线程上下文与目录规模）

- [AUD-36818] [Suggestion] 未使用的导入与循环变量（MediaAssetRef、asset_id）
  - 位置: src/shared/project/offline_asset_manager.py:346, 582, 215, 233
  - 证据:
    ```python
    from .topos_project import ToposProject, MediaAssetRef
    ...
    for asset_id, asset in project.assets.items():
    ```
    （346、582 两处导入 MediaAssetRef 后未使用；215、233 两处循环变量 asset_id 未使用，实际取 asset.asset_id。）
  - 影响: 静态噪音，掩盖真实依赖意图；lint 告警。
  - 建议: 删除未用导入，循环改 `for asset in project.assets.values():`。
  - 置信度: 高（静态事实）

### 通过项
- check_offline_assets 两条工程分支均含虚拟素材（topos://）豁免与 online/offline 计数一致性（offline+online=total）
- relink_asset/batch_relink/update_offline_status 对不支持类型显式抛 ValueError（check_offline_assets 例外见 AUD-36816）
- 分层边界：EditProject 操作全部经 edit_project_adapter 分发，shared 不 import features；ToposProject 延迟导入避免环
- batch_relink 结果统计以重链后 is_offline 为准（ToposProjectIO.relink_assets 成功即置 False，计数口径成立）
- 报告/结果对象为 frozen dataclass，to_dict 完整
- _recursive_search 捕获 OSError 防越权目录中断
- 类型注解完整（含 Union 类型）
- GPU/GLSL/HDR/信号防递归不涉及

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 6 | AUD-36805, AUD-36806, AUD-36807, AUD-36814, AUD-36815, AUD-36816 |
| Suggestion | 13 | AUD-36800–36804, AUD-36808–36813, AUD-36817, AUD-36818 |

重点主题：library.py 外部档案健壮性与多文件搬移原子性；offline_asset_manager.py 离线判定口径不一致与 revision 丢失；manifest.py 手工字段重建的演进风险。
