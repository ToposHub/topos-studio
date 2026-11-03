# R01 留档：统一 Effects 资源目录与能力状态（2026-11-03）

## 实现

- `src/app/controllers/color_resource_catalog.py`：`EffectsResourceCatalog`
  正式 catalog presenter。权威 = PluginManager 注册表（dctl/ai）+ EffectCatalog
  （Composite 内建效果）；投影只读内存，不扫描目录、不编译 shader、不加载模型。
- `EffectCatalog.is_built()`：公共只读探针，未冷构建时投影方不强制扫描。
- `ColorResourcePreview`：效果条目改经 presenter 投影（原直接读 PluginManager
  的临时路径移除）；LUT 条目维持会话临时路径（R02 替换）。
- 生产装配 `warm_background()`：daemon 线程完成 EffectCatalog 冷构建（唯一
  磁盘扫描步骤）+ refresh，经 `catalog_changed` 信号重投影。

## 身份与能力规则

- 插件 id = plugin_id（跨会话稳定，与 legacy Layer 载荷一致）；内建 id =
  `effect:<effect_type>`；同 id 不同内容消歧 `id@hash8`；同内容多路径去重；
  分类/名称翻译不参与 ID。
- `available=False` 码：`ai_model_missing`（AI 模型文件缺失，`bake_capable=True`
  如实标注离线能力）、`plugin_shader_missing`、`color_backend_not_wired`
  （内建效果待 R06 接入 Color 节点后端，不冒充可用）。
- 未知参数类型原样保留（schema_hash 覆盖，浏览器显示 unsupported 不丢弃）。

## 实测（本机 RX 6800 / macOS 15.3.1）

| 指标 | 值 |
|---|---|
| 插件目录扫描（41 个 .tglsl/.tai） | 19 ms（应用启动既有路径） |
| EffectCatalog 冷构建（49 specs） | 761 ms（后台 warm 线程，非 GUI） |
| GUI 线程首次 entries() | 7 ms（0 条目：未预热不导入 composite 链） |
| 预热后 entries() 热缓存 | 1 ms |
| 投影规模 | 50 条目（40 available / 10 disabled） |

## 测试与原生证据

- `tests/app/test_color_resource_catalog.py`：14 项（重名/Unicode/热刷新/更新
  删除/未知 schema/AI 缺模型/内建未接线/refresh 不碰磁盘/缓存不重投影/搜索
  不扫描/缺失条目不能激活）全过。
- 回归：§7 UI 组 73 passed；EffectCatalog 相关既有套件 152 passed。
- 原生验证（`--resources --workflow`）：passed，21.1s；effects_catalog_count
  41→50（新增 9 内建禁用条目）；`project_and_engine_unchanged=passed`。

## 尚未支持（后续轮次）

- 内建 Composite 效果接入 Color 节点图执行（R06）后翻转
  `color_backend_not_wired`。
- 收藏/搜索偏好的用户级持久化（R10）；LUT 资源注册替换 `_lut_entry`（R02）。
