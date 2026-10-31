# batch-371 审查报告 — src/shared/project

- 日期：2026-09-28
- 编号区间：AUD-37100 – AUD-37199
- 审查文件：
  - `src/shared/project/topos_project_io.py` (1,174 行)
  - `src/shared/project/topos_project_read_service.py` (216 行)
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性（undo·redo·revision·持久化）/ 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 佐证材料（只读交叉验证）：`src/shared/project/payload_cache.py`、`manifest.py`、`project_manager.py`、`save_router.py`、`src/features/edit/core/project_serializer.py`、`edit_project_io.py`、`layer_adapter.py`

---

## src/shared/project/topos_project_io.py (行数: 1174)

### 发现

- [AUD-37101] [Major] manifest 合并方向错误：每次保存重置 created_at/created_by/required_features/optional_features
  - 位置: src/shared/project/topos_project_io.py:380-391
  - 证据:
    ```python
    existing_manifest = payload.manifest
    if existing_manifest:
        # Preserve fields from existing manifest that ToposManifest.create
        # does not accept as params (last_saved_by, present_modules, etc.)
        preserved_manifest = dict(existing_manifest)
        preserved_manifest.update(manifest.to_dict())
        manifest_data = preserved_manifest
    ```
    佐证 manifest.py:186-194 —— `ToposManifest.create()` 总是设置 `created_at=now, modified_at=now`，且 `to_dict()`（manifest.py:101-119）包含 created_at/created_by/required_features/optional_features/edit_content_schema_version 全部键。
  - 影响: `preserved.update(fresh)` 使新建 manifest 的字段全部获胜：已有工程的 `created_at` 每次保存被重置为当前时间（工程创建时间永久漂移）；`created_by` 被重置为默认 "Topos Color"（Phase 3B 跨产品标识丢失）；`required_features`/`optional_features` 被重置为空列表（打开工程时必须启用的功能元数据丢失，加载端不再自动启用对应功能）；`edit_content_schema_version` 被无条件盖成 3，即使载荷未经过迁移链。注释声称要保留的字段中只有 last_saved_by（:391）和 present_modules（:506）在合并后被显式重设，其余全部被覆盖。
  - 建议: 反转合并方向或显式白名单：从 existing manifest 保留 created_at/created_by/required_features/optional_features（及未迁移时的 edit_content_schema_version），仅用新值覆盖 project_id/project_mode/features/primary_asset_id/active_timeline_id；为合并逻辑补充往返测试（保存两次后断言 created_at 不变）。
  - 置信度: 高

- [AUD-37102] [Major] load() 缓存目录替换非原子且有竞态；save() 读-改-写在并发下丢失更新
  - 位置: src/shared/project/topos_project_io.py:585-595（另见 :369、:514-526）
  - 证据:
    ```python
    staging_cache_dir = f"{cache_dir}.loading-{uuid.uuid4().hex}"
    try:
        os.makedirs(staging_cache_dir, exist_ok=False)
        with zipfile.ZipFile(filepath, 'r') as zf:
            ToposProjectIO._extract_archive_to_cache(zf, staging_cache_dir)
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)
        os.replace(staging_cache_dir, cache_dir)
    ```
  - 影响: (1) `shutil.rmtree(cache_dir)` 与 `os.replace` 之间是崩溃窗口：进程被杀/断电后 cache 目录整体缺失（可重新 load 重建，但会话中已持有的 assets_dir 绝对路径悬空）；(2) 并发两次 load 同一工程（如恢复预览 + 正式打开）会在 rmtree/replace 上竞争，一方读目录时另一方删除其下文件；(3) save() 的 Phase B（:369 读入现有 payload）+ 写回（:521-526）是典型 read-modify-write，无任何锁，两个并发保存后写者会静默丢弃先写者新增的模块数据。另：硬崩溃时 `.loading-<hex>` staging 目录不会被清理（except 分支只覆盖异常退出），随时间在 .topos_cache 旁累积。类全部为 staticmethod，无进程内/进程间互斥。
  - 建议: 引入按 filepath 的线程锁 + 文件锁（如 fcntl/portalocker）；将 rmtree+replace 改为"先 rename 旧目录到 .old-<uuid> 再 replace 新目录、成功后异步删除 .old"的交换模式；启动时清理遗留 `.loading-*`/`.old-*` 目录。
  - 置信度: 中（机制确定；并发场景是否实际发生取决于上层是否并行调用，本批文件内无法证实）

- [AUD-37103] [Major] 既有 .topos 不可读时静默按空 payload 覆盖保存，跨产品模块数据丢失
  - 位置: src/shared/project/topos_project_io.py:368-369
  - 证据:
    ```python
    # ---- Phase B: load existing payload to preserve unknown modules ----
    payload = ProjectArchivePayload.load_from_file(filepath)
    ```
    佐证 payload_cache.py:151-156：
    ```python
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            return ProjectArchivePayload.read_from_zip(zf)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Cannot read existing .topos for payload cache: %s", exc)
        return ProjectArchivePayload()
    ```
  - 影响: 既有包损坏（云同步半下载、磁盘错误、Windows 上被占用触发 OSError）时 load_from_file 返回空 payload，save() 继续执行并把仅含当前产品模块的新 ZIP 原子覆盖写回（:526 os.replace）——其它产品已保存的 timelines/AI plans/compositions 等模块被永久清除，仅有一条 warning 日志，调用方拿到 `True`（保存成功）。虽然原子写使包损坏概率低，云盘/移动备份场景下该路径真实可达。
  - 建议: load_from_file 失败时区分"文件不存在"（正常新建）与"存在但不可读"（异常），后者应让 save() 失败或至少要求显式 `force_overwrite_corrupt=True` 参数；并在返回前对比新旧模块集给出告警。
  - 置信度: 中（机制确定；触发依赖既有包损坏这一前置条件）

- [AUD-37104] [Major] 解压无总大小/条目数上限，恶意或损坏的 .topos 可耗尽磁盘（zip bomb）
  - 位置: src/shared/project/topos_project_io.py:290-291
  - 证据:
    ```python
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with zf.open(info, "r") as source, open(target, "wb") as output:
        shutil.copyfileobj(source, output)
    ```
  - 影响: `_extract_archive_to_cache` 已正确拒绝符号链接（:277-279）与路径逃逸（:281-283），但对解压总量无任何上限：精心构造的高压缩比 ZIP（quine bomb）可写满磁盘；保存路径同样通过 payload_cache.read_from_zip 的 `zf.read(name)`（payload_cache.py:101/127）将全部条目无上限读入内存。.topos 属外部交换文件，违反铁律"外部数据须验证"的完整要求。
  - 建议: 在 extract 循环中累计写入字节并在超过 `info.file_size` 之和或配置上限（如 2×max_cache_size_mb）时中止；对 `zf.read` 增加单条目大小上限；拒绝 file_size 与 compress_size 比值异常的条目。
  - 置信度: 中（利用需恶意文件；健壮性缺口确定）

- [AUD-37105] [Suggestion] save()/load() 顶层宽泛 except Exception 吞掉领域错误，traceback 走 stderr 而非 logger
  - 位置: src/shared/project/topos_project_io.py:534-538、808-812
  - 证据:
    ```python
    except Exception as e:
        logger.error(f"Failed to save project: {e}")
        import traceback
        traceback.print_exc()
        return False
    ```
  - 影响: 磁盘满、权限、JSON 序列化 TypeError、路径错误被压平为同一个 False/(None, None)，调用方无法区分失败类别（铁律"不得宽泛 except 掩盖领域错误"的边界张力——此处有日志，但 `traceback.print_exc()` 绕过 logging 配置，GUI 部署下丢失堆栈）。
  - 建议: 收窄为 (OSError, zipfile.BadZipFile, json.JSONDecodeError, ValueError) 等领域异常并 `logger.error(..., exc_info=True)`；对未预期异常重新抛出或单独记录 CRITICAL。
  - 置信度: 高

- [AUD-37106] [Suggestion] _relativize_*_paths 原地修改调用方传入的 dict，隐式副作用契约
  - 位置: src/shared/project/topos_project_io.py:1020-1026（另见 :993-1000、调用点 :400-404）
  - 证据:
    ```python
    for asset_id, asset_data in assets_data.items():
        if isinstance(asset_data, dict) and "path" in asset_data:
            asset_data["path_relative"] = ToposProjectIO._to_relative_path(asset_data["path"], base_dir)
            asset_data["path_original"] = asset_data["path"]
    return assets_data
    ```
  - 影响: save() 直接向调用方传入的 `full_assets_data`/`project_data` 注入 path_relative/path_original 键。当前两个调用方（project_serializer.py:123-136、edit_project_io.py）都构造新 dict，污染被限制在新建结构内；但任何未来调用方直接传内存活跃状态（如 undo 快照、媒体池实况 dict）都会被静默改写，且无文档声明该突变。
  - 建议: 对外文档注明突变语义，或在 set_module 前做浅拷贝（`{k: dict(v) for ...}`）；至少保持现状并加测试锁定契约。
  - 置信度: 高（机制确定，当前影响有限）

- [AUD-37107] [Suggestion] _write_json 为死代码；serialization_format/compress_project_file 配置在 ZIP 管线中无效
  - 位置: src/shared/project/topos_project_io.py:930-954（配置定义 :129-131）
  - 证据:
    ```python
    def _write_json(filepath: str, data: dict[str, Any], config: ToposProjectIOConfig) -> None:
        """写入 JSON 文件"""
        if config.serialization_format == "msgpack":
            try:
                import msgpack
                packed = msgpack.packb(data, use_bin_type=True)
    ```
  - 影响: 全仓 grep 确认 `ToposProjectIO._write_json` 无任何调用（save 实际经 payload.write_to_zip 恒写 JSON）。模块头注释（:36）与 config 声称"支持 MessagePack（可选）/gzip 压缩 project.json"，实际保存路径完全忽略这两个选项，而 `_read_json`（:957-990）却保留 .mp/.gz 读取分支——API 承诺与实现脱节，误导维护者。
  - 建议: 删除 _write_json 与无效配置项，或真正接入 payload_cache 的写路径；同步修正模块 docstring。
  - 置信度: 高

- [AUD-37108] [Suggestion] 缓存资产复制路径为不可达死路，且启用后存在整体保存失败与陈旧资产永不清理两个缺陷
  - 位置: src/shared/project/topos_project_io.py:1104-1108（路径入口 :487-503）
  - 证据:
    ```python
    for f in os.listdir(baked_src):
        src_path = os.path.join(baked_src, f)
        size = os.path.getsize(src_path)
        if total_size + size <= max_size_bytes:
            shutil.copy2(src_path, os.path.join(baked_dst, f))
    ```
  - 影响: 全仓 grep 确认无任何调用方传 `cache_assets_dir` 或 `include_cache_assets=True`（两大保存入口均 include_cache_assets=False / 不传目录），`_copy_cache_assets` + SUB_DIRS 建目录 + unknown_files 读入（:487-503）当前不可达。若未来启用：(1) 源目录含子目录或坏符号链接时 `os.path.getsize`/`copy2` 抛 OSError，异常冒泡到 save 顶层导致整个保存失败返回 False；(2) 旧包中 assets/* 原始字节已进入 payload.unknown_files（payload_cache.py:100-102），本次复制仅覆盖同名条目，已从缓存删除的文件会永久残留在 .topos 中随保存无限累积，且 cache_manifest 与实际文件集不一致。
  - 建议: 删除死路径或启用前修复：遍历时跳过非普通文件（os.path.isfile 且 exists）、对旧 assets/* 条目按新清单整体替换（先 remove 再加入）而非同名覆盖。
  - 置信度: 高（死路事实与缺陷机制均确定；因不可达当前无用户影响）

- [AUD-37109] [Suggestion] composite/viewer_state.json 仅在传入 compositions 数据时刷新，否则保持陈旧
  - 位置: src/shared/project/topos_project_io.py:420-426
  - 证据:
    ```python
    if full_compositions_data is not None:
        payload.set_module("composite/graphs.json", full_compositions_data)
        if ui_state_data and "composite_viewer_state" in ui_state_data:
            payload.set_module(
                "composite/viewer_state.json",
                ui_state_data.get("composite_viewer_state", {}),
            )
    ```
  - 影响: viewer_state 的写入嵌套在 `full_compositions_data is not None` 条件内：某产品只传 ui_state_data（含 composite_viewer_state）而不传 compositions 时，包内 composite/viewer_state.json 保留上一次的旧值，下次加载 full_data["composite_viewer_state"] 与实际合成图状态不匹配。
  - 建议: 将 viewer_state 的 set_module 提到与 graphs.json 同级的独立 `if ui_state_data is not None` 分支。
  - 置信度: 中（当前主要调用方两者同传；组合参数的边界行为确定）

- [AUD-37110] [Suggestion] 保存全程整包驻留内存，大工程内存峰值约为包体量级
  - 位置: src/shared/project/topos_project_io.py:498-503（另见 :369）
  - 证据:
    ```python
    for root, _dirs, files in os.walk(temp_dir):
        for fname in files:
            full_path = os.path.join(root, fname)
            arc_name = os.path.relpath(full_path, temp_dir)
            with open(full_path, "rb") as fh:
                payload.unknown_files[arc_name] = fh.read()
    ```
  - 影响: Phase B 的 read_from_zip 把现有包所有条目（含 assets 原始字节）读入内存，缓存资产再全量 `fh.read()` 进 unknown_files，zipfile 写出时又产生压缩缓冲——4K 代理/烘焙资产大的工程在保存瞬间内存峰值可达包体积量级（max_cache_size_mb 默认 500MB 上限），与 4K 实时基准下的内存预算冲突。现状因 include_cache_assets=False 影响缓解为"JSON 模块级驻留"。
  - 建议: 缓存资产用 zf.write(路径, arcname) 流式写入而非读入 dict；payload 对大字节条目改为记录源路径延迟写。
  - 置信度: 高（机制确定；量级依赖资产规模）

- [AUD-37111] [Suggestion] teaching_animation 模块缺失回退报告中 cache_preserved 检查的是全局 assets/ 目录
  - 位置: src/shared/project/topos_project_io.py:788-797
  - 证据:
    ```python
    full_data.setdefault("module_fallbacks", {})[
        "teaching_animation"
    ] = {
        "module_id": "teaching_animation",
        "state": "missing_module",
        "payload_preserved": "teaching_animation" in full_data,
        "cache_preserved": os.path.exists(
            os.path.join(cache_dir, "assets")
        ),
    }
    ```
  - 影响: 该回退条目针对 teaching_animation 模块，但 cache_preserved 判定的是通用 `assets/` 目录是否存在——任何含烘焙/缩略图的工程都会报 cache_preserved=True，与教学动画自身的渲染缓存（teaching_animation/render_cache_manifest.json 及其缓存文件）是否保留无关，UI 层据此展示的"缓存已保留"状态可能失真。
  - 建议: 检查 teaching_animation 专属缓存路径（如 cache_dir 下 "teaching_animation/" 或 render_cache 目录）。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：纯文件/ZIP IO，不触碰 OpenGL/ModernGL，无 GPU 对象泄漏面
- GLSL·HDR 正确性：无着色器逻辑与像素数据变换，不涉及浮点字面量/HDR clamp 约束
- 信号防递归：无 Qt 控件与信号，纯静态工具类
- 保存原子性：tempfile.mkstemp + os.replace，读者/备份不会观察到半写 ZIP（:514-529，后台 autosave 崩溃安全）
- 解压路径安全基础：拒绝符号链接（:277-279）与路径逃逸（:281-283），staging 目录一次性替换避免旧文件复活
- 外部 schema 校验：schema_version 高于支持版本时告警（:607-613）；v3 拒绝 single_asset+video、standalone_image+非图片主素材（:634-651）；v1 project_mode 迁移（:626-630）
- 状态提交一致性（模块保留）：未知/其它产品模块经 ProjectArchivePayload 原样写回，产品间互删防护成立（配合 :400-477 的按 None 保留语义）
- relink_assets 字段完整性：MediaAssetRef 仅 5 字段（topos_project.py:75-79），重建无字段丢失，且未匹配的 asset_id 被安全跳过

---

## src/shared/project/topos_project_read_service.py (行数: 216)

### 发现

- [AUD-37112] [Major] 回退路径将不可信 ZIP 路由进未加固的 extractall
  - 位置: src/shared/project/topos_project_read_service.py:150-156（佐证 project_manager.py:94-97）
  - 证据:
    ```python
    try:
        from src.shared.project.project_manager import ProjectManager
        pm = ProjectManager()
        result = pm.load_project(topos_path)
    ```
    project_manager.py:94-97：
    ```python
    with tempfile.TemporaryDirectory() as temp_dir:
        # Extract ZIP contents
        with zipfile.ZipFile(project_path, 'r') as zf:
            zf.extractall(temp_dir)
    ```
  - 影响: 任何使 `_load_from_topos_io` 返回 None 的文件（含精心构造的恶意 .topos：无 manifest、JSON 损坏、schema 拒绝等）都会进入旧解析器，其 `zf.extractall` 没有本批 io.py `_extract_archive_to_cache` 的符号链接拒绝与路径逃逸检查，zip-slip/符号链接逃逸在回退路径上重新暴露，削弱了主路径的加固成果。
  - 建议: 回退前用同一 `_extract_archive_to_cache` 级别的校验预检（或为 ProjectManager 增加加固版 extract）；至少对进入回退的文件先做条目名/符号链接审计。
  - 置信度: 中（可达性确定；实际利用需恶意文件）

- [AUD-37113] [Suggestion] "无调色/合成数据的合法新格式工程"被等同于"新格式解析失败"，触发多余的旧格式二次解压
  - 位置: src/shared/project/topos_project_read_service.py:131-148、86
  - 证据:
    ```python
    # 只有确实提取到了数据才返回新格式结果
    if layers or raw_params or composite_graph:
        return ToposProjectBundle(
            ...
        )

    return None
    ```
    （:86 `return self._load_from_project_manager(topos_path)`）新格式 project.json 为 ToposProject schema、无 "layers" 键（topos_project.py 全文无该键），project_manager.py:143 `if "layers" in data:` 必然为假 → 返回 None。
  - 影响: 一个完全合法但没有调色栈与合成数据的新格式工程会：先被 ToposProjectIO.load 完整解压解析一次，再被旧 ProjectManager 二次解压到临时目录解析一次，最终仍返回 None。语义上"新格式成功但无调色数据"与"新格式失败"不可区分，浪费一次完整解压（大包时为秒级），并放大 AUD-37112 的暴露面。
  - 建议: 用显式成功标志区分：ToposProjectIO.load 成功（project 非 None 或 full_data 含 manifest）即视为新格式成立，返回空 bundle 而不是 None；仅真正解析失败时回退旧格式。
  - 置信度: 高（控制流确定；用户可见后果限于性能与日志噪音）

- [AUD-37114] [Suggestion] 恒取字典序第一个 color stack，忽略 primary_color_stack_id/激活状态
  - 位置: src/shared/project/topos_project_read_service.py:193-214
  - 证据:
    ```python
    # 取第一个 stack
    for stack_id, stack_data in color_stacks.items():
        if not isinstance(stack_data, dict):
            continue
        ...
        break  # 只取第一个 stack
    ```
  - 影响: 多栈工程（save_router 委托入口 :1365 也走此路径）恢复的是 JSON 字典插入序的第一个栈，而非 project.primary_color_stack_id 指向的主栈；栈顺序变化（迁移、外部编辑）后可能恢复错误调色栈。save_router:639-642 的注释表明"reader 取第一条"是单资产流的既定约定，但该约定未覆盖编辑工程多栈场景。
  - 建议: 优先按 bundle.project.primary_color_stack_id（或 full_data["manifest"]）定位栈，找不到时回退第一条并记录 warning。
  - 置信度: 中

- [AUD-37115] [Suggestion] _load_from_topos_io 捕获全部异常仅记 debug 级日志
  - 位置: src/shared/project/topos_project_read_service.py:146-148
  - 证据:
    ```python
    except Exception as e:
        logger.debug(f"ToposProjectIO load failed, will fallback: {e}")
        return None
    ```
  - 影响: 新格式路径上的真实错误（ImportError、磁盘 IO、迁移缺陷）在生产日志级别（INFO+）下完全不可见，排障时只能看到下游 `_load_from_project_manager` 的笼统 error 或最终 None；违反铁律"不得宽泛 except 掩盖领域错误"。
  - 建议: 至少升级为 logger.warning，并用 exc_info=True；对预期内的"无数据"与异常分通道处理。
  - 置信度: 高

- [AUD-37116] [Suggestion] active_layer_index / raw_params 从外部 JSON 直接透传，无类型与范围验证
  - 位置: src/shared/project/topos_project_read_service.py:209-212
  - 证据:
    ```python
    # 提取 raw_params
    raw_params = stack_data.get("raw_params")

    # 提取 active_layer_index
    active_layer_index = stack_data.get("active_layer_index", 0)
    ```
  - 影响: 外部工程文件可将 active_layer_index 写成字符串、负数或超 layers 长度的值（铁律：外部数据须验证），下游按索引使用时产生 TypeError/IndexError 或静默选错图层。
  - 建议: `active_layer_index = max(0, min(int(active_layer_index), len(layers) - 1))` 包 try/except TypeError 兜底为 0；raw_params 校验为 dict。
  - 置信度: 中

- [AUD-37117] [Suggestion] ToposProjectBundle 弱类型：project: Any、layers: list 无元素类型
  - 位置: src/shared/project/topos_project_read_service.py:35-36
  - 证据:
    ```python
    project: Any | None = None  # ToposProject
    layers: list = field(default_factory=list)
    raw_params: dict | None = None
    ```
  - 影响: 类 docstring 定位为"强类型替代旧 dict 返回值"，但核心字段仍是 Any/裸 list/dict（无 ParamSpec），消费方无法获得静态检查收益，注释形式的类型标注也不被类型检查器利用。
  - 建议: 在 TYPE_CHECKING 下导入 ToposProject 与 Layer 做前向引用（`"ToposProject | None"`、`list[Layer]`、`dict[str, Any] | None`），保持运行时零依赖。
  - 置信度: 高

### 通过项
- 线程安全：类无可变实例状态（方法内局部变量），每次调用独立实例化 ProjectManager，无共享可变状态
- GPU 资源生命周期：不涉及 OpenGL/ModernGL
- GLSL·HDR 正确性：无着色器与像素数据路径
- 信号防递归：无 Qt 控件与信号
- 层反序列化容错：单层 layer_from_dict 失败仅 warning 并跳过，不中断整体加载（:200-206）
- 输入验证（结构层）：color_stacks/compositions/layer_dict 均有 isinstance(dict) 检查（:109、:123、:201）
- 回退链完整性：新格式 → 旧 ProjectManager → None 两层兜底，旧格式返回 dict 正确包装为 bundle 且字段映射无遗漏（:159-167）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-37101, AUD-37102, AUD-37103, AUD-37104, AUD-37112 |
| Suggestion | 12 | AUD-37105–37111, AUD-37113–37117 |

无 Critical 级发现。优先处理 AUD-37101（每次保存都在发生的 manifest 元数据损坏）与 AUD-37103（损坏包覆盖保存导致跨产品模块丢失）。
