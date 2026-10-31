# 交接记录 · 基线 guardrail 失败（M 线所有权）

> 记录时间：2026-10-24，分支 `codex/project-insights`，base `dd1a28fdd`。
> 这是 T 线开工时发现的**存量**问题，属 M 线文件所有权，T 线不改，仅登记。

## 现象

```
.venv/bin/python -m pytest tests/architecture/test_boundary_guardrails.py::TestAppLayerForbiddenImports -q
→ 2 failed
  test_app_layer_has_no_forbidden_feature_imports
  test_shared_layer_has_no_forbidden_feature_imports
```

失败样本（stash 掉 T 线全部新增后在干净基线上复现，退出码一致）：

- `src/app/controllers/edit_controller.py:4125` `from src.features.edit.core.commands import assert_track_editable`
- `src/app/controllers/edit_controller.py:4237` 同上
- `src/app/controllers/edit_controller.py:9046` `from src.features.edit.core.render_cache import build_render_cache_fingerprint`

即基线指纹（FORBIDDEN_DEEP_PATTERNS 的已知违例 hash 白名单）未随这三处
新增深导入更新。属 M 线（edit_controller/commands 所有权）。

## 建议（供 M 线集成者）

三选一：函数提升到 app 可见的浅层；经既有 facade 转发；或更新基线指纹并
注明理由。T 线无倾向。
