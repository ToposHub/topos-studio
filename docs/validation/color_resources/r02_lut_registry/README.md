# R02 留档：LUT 资源注册、导入、迁移与重定位（2026-11-03）

## 实现

- `src/shared/color/lut_resource_registry.py`（无 Qt，headless 可测）：
  - `validate_lut_file`：解析级验证 `.cube/.3dl/.look`——维度/数据长度/DOMAIN
    合法性/非有限值/尺寸护栏；结构化错误码（unsupported_format、read_failed、
    parse_failed、non_finite_values、invalid_domain、too_large、cancelled…）。
  - `LutResourceSession`：内容 SHA-256 + 资源 ID `lut_<sha32>`（与机器路径
    无关）；同内容多路径去重、同名不同内容内容寻址分路径共存；导入 = 验证 →
    原子暂存拷贝（temp + os.replace，失败无半文件）→ 注册条目；取消逐项可见。
  - 复用既有资源容器：`ResourceEntry/ResourceIndex` + `topos-res://` 引用；
    `save_payload()` 产出 (resources_data, resource_files) 交工程包既有原子
    写入（DESK-03D 取消/失败不留半包）。
  - 迁移/重定位：`migrate_legacy`（旧绝对路径 → 资源引用，旧字段保留）；
    `status_of`（registered/missing）——缺失明示，绝不静默移除。
- `edit_project_io`：save 时会话载荷并入（会话空 → 保持 None 语义不写资源
  模块）；load 时 `bind_project` 合并包内索引（失败不阻断打开）。
- `ColorResourcePreview`：`import_preview`（假导入）退役，`import_luts` 后台
  线程真实注册 + `import_feedback` 开始/成功/失败反馈 + 目录树重投影；
  LUT 条目 id = `topos-res://…`。

## 测试与证据

- `tests/color/test_lut_resource_registry.py`：19 项全过——2³ identity、通道
  交换非 identity、1D shaper、非 0–1 DOMAIN（-0.25~1.5）、损坏数据、NaN、
  非法 DOMAIN、不支持扩展、读失败、取消、非 ASCII 文件名、同内容去重、
  同名共存、失败/取消不发布半条目、暂存写失败无条目、legacy 迁移与缺失
  明示、**真实包保存 → 移动目录重开 → 删除原源文件后仍按内容解析**、
  空会话保持无资源协议保存。
- 回归：lut loader + presenter + browser + catalog 44 passed；project IO/
  归档 39 passed（含修正 `archived_path` 相对归档根的前轮测试断言缺陷——
  该失败在 R02 之前的干净 HEAD 上即存在）。
- 原生验证：passed（50 效果 + 5 LUT 目录、UI 草稿不改工程/engine 保持）。

## 尚未支持（后续轮次）

- LUT 应用到画面（R03 命令 + R05 节点语义）；栈载荷 `lut_resource` 字段随
  R05 命令落写；旧 `active_lut_path` 的保存期批量迁移在 R05 一并处理。
- LUT 缩略图后台任务（R07）；收藏/目录树用户偏好（R10）。
