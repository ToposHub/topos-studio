# Post Validation Remaining Cleanup Plan (2026-05-27)

> 目的：记录在最新一轮复核后仍未完全收口的问题，避免后续继续围绕已经完成的 Phase A-G 重复工作。
>
> 当前总体判断：主线问题已经基本解决。`media_core` 不再挂起，`tests/visual` 不再 segfault，marker gate 已经真实可用，`main_window.py` 已从 9216 行降到 931 行。剩余问题集中在 feature flag 优先级、旧 cache 残留、文档数字校准、deep import 预算收紧这几项。

---

## 1. 当前已确认通过的状态

### 1.1 架构和边界

- `src/app/main_window.py` 当前约 931 行，已低于原计划 3000 行目标。
- app 层 deep feature import 当前约 5 条，已从上一轮复核的 26 条继续下降。
- `tests/test_src_directory_boundaries.py`、feature flags、page context、acceptance markers 组合测试通过：

```bash
.venv/bin/python -m pytest \
  tests/test_src_directory_boundaries.py \
  tests/test_feature_flags.py \
  tests/test_feature_flags_registry.py \
  tests/test_page_context.py \
  tests/test_acceptance_markers.py \
  -q
```

结果：

```text
79 passed, 1 warning
```

### 1.2 Marker gate

以下 marker 已确认不是 `0 selected`：

```text
color_core: 341 collected
composite_core: 1325 collected
media_core: 1421 collected
modular: 82 collected
visual: 18 collected
```

### 1.3 关键测试

```bash
.venv/bin/python -m compileall -q src tests
```

结果：通过。

```bash
.venv/bin/python -m pytest \
  tests/media/test_decode_worker.py \
  tests/media/test_video_frame_provider_service.py \
  -q -m media_core
```

结果：

```text
70 passed
```

```bash
.venv/bin/python -m pytest tests/visual -q
```

结果：

```text
3 passed, 15 skipped
```

```bash
.venv/bin/python -m pytest \
  tests/test_color_session.py \
  tests/test_alpha_system.py \
  tests/test_cache_fix.py \
  -q -m color_core
```

结果：

```text
57 passed
```

```bash
.venv/bin/python -m pytest \
  tests/test_composite_graph.py \
  tests/test_composite_preset.py \
  tests/test_expression_parser.py \
  -q -m composite_core
```

结果：

```text
140 passed, 85 warnings
```

---

## 2. 剩余问题清单

| 优先级 | 问题 | 当前状态 | 影响 |
|---|---|---|---|
| P0 | Feature flag 环境变量优先级与原计划不一致 | `TOPOS_DISABLE_FEATURES` 仍会覆盖 `TOPOS_FEATURE_*` | 隔离测试和 CI 配置容易产生误解 |
| P0 | 旧 cache 目录仍残留在源码树附近 | `.images_cache/`、`src/.images_cache/` 仍是 untracked | `git status` 不干净，且 `src/.images_cache` 容易被误认为代码仍在写入 src |
| P1 | validation 文档数字过时 | `main_window.py`、deep import、modular collect 数字不准 | 后续 agent 会基于旧数字误判进度 |
| P1 | deep import budget 太宽 | 实际约 5 条，但测试预算仍是 100 | 回归约束不足，后续可能重新引入大量 deep imports |
| P2 | 文档措辞过满 | `All Issues Resolved` 与剩余尾巴不完全一致 | 容易产生“已经完全结束”的假结论 |
| P2 | 旧长跑 pytest 进程可能仍存在 | 之前发现两个旧 pytest 进程挂了很久 | 会干扰本机性能和后续测试判断 |

---

## 3. P0：修正 Feature Flag 优先级

### 当前现象

当前实现中，`src/shared/feature_flags.py` 先读取 `TOPOS_FEATURE_<NAME>`，再读取 `TOPOS_DISABLE_FEATURES`。因此：

```bash
TOPOS_FEATURE_COMPOSITE=true TOPOS_DISABLE_FEATURES=composite \
.venv/bin/python - <<'PY'
from src.shared.feature_flags import load_feature_flags
print(load_feature_flags(None).composite)
PY
```

当前输出：

```text
False
```

这说明 bulk disable 覆盖了显式 feature env。

### 目标行为

按原计划建议，优先级应为：

1. 显式 `TOPOS_FEATURE_<NAME>` 优先。
2. `TOPOS_DISABLE_FEATURES` 次之。
3. config 默认值再次之。
4. 默认全部开启。
5. `color` 永远保持 enabled。

### 建议修改

目标文件：

```text
src/shared/feature_flags.py
tests/test_feature_flags_registry.py
tests/test_feature_flags.py
```

建议顺序：

1. 先读取 config。
2. 再应用 `TOPOS_DISABLE_FEATURES`。
3. 最后应用每个 `TOPOS_FEATURE_<NAME>`，让显式 env 覆盖 bulk disable。
4. 最后强制 `color=True`。
5. 增加测试覆盖冲突优先级。

### 建议测试

新增或补充用例：

```python
def test_explicit_feature_env_overrides_bulk_disable(monkeypatch):
    monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "composite")
    monkeypatch.setenv("TOPOS_FEATURE_COMPOSITE", "true")
    flags = load_feature_flags(None)
    assert flags.composite is True
```

同时保留：

```python
def test_bulk_disable_still_works_without_explicit_override(monkeypatch):
    monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "composite")
    monkeypatch.delenv("TOPOS_FEATURE_COMPOSITE", raising=False)
    flags = load_feature_flags(None)
    assert flags.composite is False
```

### 验收命令

```bash
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_feature_flags_registry.py -q
```

并手动验证：

```bash
TOPOS_FEATURE_COMPOSITE=true TOPOS_DISABLE_FEATURES=composite \
.venv/bin/python - <<'PY'
from src.shared.feature_flags import load_feature_flags
assert load_feature_flags(None).composite is True
print("ok")
PY
```

---

## 4. P0：清理旧 cache 残留并防止回归

### 当前现象

当前 `git status --short` 仍显示：

```text
?? .images_cache/
?? src/.images_cache/
```

当前本地仍存在旧缓存文件：

```text
.images_cache/*.exr
.images_cache/*.jpg
src/.images_cache/*.exr
```

代码新路径已改为：

```text
.cache/topos-color/images_cache
.cache/topos-color/baked_layers
```

因此旧目录大概率是历史残留，但仍会污染工作区。

### 建议修改

1. 删除本地旧残留目录：

```bash
rm -rf .images_cache src/.images_cache
```

2. 在 `.gitignore` 中增加防御性忽略：

```gitignore
.images_cache/
src/.images_cache/
```

3. 增加或强化测试，确保核心测试不会重新创建 `src/.images_cache`。

可以在 `tests/test_cache_fix.py` 或边界测试中加入：

```python
def test_cache_dir_not_under_src():
    from pathlib import Path
    from src.shared.io.file_io import FileIO

    cache_dir = Path(FileIO.CACHE_DIR).resolve()
    src_dir = Path(__file__).resolve().parents[1] / "src"
    assert src_dir.resolve() not in cache_dir.parents
```

### 验收命令

```bash
rm -rf .images_cache src/.images_cache
.venv/bin/python -m pytest tests/test_cache_fix.py tests/test_image_engine_state.py -q
test ! -d .images_cache
test ! -d src/.images_cache
git status --short
```

预期：

- 不再出现 `.images_cache/` 或 `src/.images_cache/`。
- 新缓存只出现在 `.cache/topos-color/`。

---

## 5. P1：收紧 deep import budget

### 当前现象

实际 app 层 deep feature imports 约 5 条：

```bash
rg -n "^(from|import) src\\.features\\.[^.]+\\.(core|ui|nodes|render|evaluators|adapters)" src/app -g '*.py'
```

当前剩余主要是：

```text
src/app/main_window.py: ImageEngine
src/app/controllers/color_controller.py: layer/plugin_manager
src/app/controllers/file_controller.py: layer
src/app/controllers/layer_controller.py: layer
```

但 `tests/test_src_directory_boundaries.py` 里的预算仍是：

```python
MAX_APP_DEEP_FEATURE_IMPORTS = 100
```

文档也仍写着 actual count 约 94。

### 建议修改

1. 将预算从 100 下调到 5 或 8。
2. 推荐先用 8，给少量维护空间；如果确认 5 条都是刻意保留，则直接设为 5。
3. 更新注释，写明当前剩余 deep imports 是 controller bridge 层和 `ImageEngine` 初始化依赖。

示例：

```python
MAX_APP_DEEP_FEATURE_IMPORTS = 8  # Current count: 5; keep this budget tight.
```

### 后续优化方向

最终目标仍是 0，但不要为了数字把依赖藏到无意义的动态 import 里。推荐下一步用 public facade 或 adapter 解决：

- `src.features.color.facade`
- `src.features.color.adapters`
- `src.shared.color`

### 验收命令

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

---

## 6. P1：更新 validation 文档

### 当前过时内容

目标文件：

```text
docs/validation_status_2026-05-27.md
```

需要修正：

| 字段 | 当前文档 | 当前复核结果 |
|---|---:|---:|
| `main_window.py` 行数 | 958 | 931 |
| deep import actual count | ~94 | ~5 |
| `modular` collected | 81 | 82 |

### 建议修改

1. 将 “All Issues Resolved” 改为更精确的 “Core Issues Resolved; Cleanup Tail Remaining”。
2. 增加一个 “Remaining Cleanup Tail” 小节，引用本文档。
3. 更新所有数字。
4. 将 Phase E 描述从 “budget 100 actual 94” 改为 “budget should now be tightened from 100 to 5/8”。

### 验收

文档类变更无强制测试，但建议同时跑：

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_feature_flags.py -q
```

---

## 7. P2：处理旧长跑 pytest 进程

### 当前观察

之前复核时发现有旧的 pytest 进程运行了很久，命令形态类似：

```text
python -m pytest tests --ignore=tests/visual --ignore=tests/edit ...
```

这些进程看起来不是本轮复核新启动的，可能来自之前的 agent 或 shell snapshot。

### 建议操作

先检查：

```bash
ps -axo pid,ppid,stat,etime,command | rg 'python -m pytest|pytest tests'
```

如果确认不是当前正在使用的测试，再终止：

```bash
kill <pid>
```

不要使用 `kill -9`，除非普通 `kill` 无效。

### 验收

```bash
ps -axo pid,ppid,stat,etime,command | rg 'python -m pytest|pytest tests' || true
```

预期：没有长期挂起的旧 pytest。

---

## 8. 建议提交顺序

### Commit 1：Feature flag precedence

包含：

- `src/shared/feature_flags.py`
- `tests/test_feature_flags.py`
- `tests/test_feature_flags_registry.py`

验证：

```bash
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_feature_flags_registry.py -q
```

### Commit 2：Cache residue cleanup

包含：

- `.gitignore`
- `tests/test_cache_fix.py` 或边界测试
- 删除本地 `.images_cache/` 和 `src/.images_cache/`

验证：

```bash
rm -rf .images_cache src/.images_cache
.venv/bin/python -m pytest tests/test_cache_fix.py tests/test_image_engine_state.py -q
test ! -d .images_cache
test ! -d src/.images_cache
```

### Commit 3：Tighten import budget and docs

包含：

- `tests/test_src_directory_boundaries.py`
- `docs/validation_status_2026-05-27.md`
- `docs/post_validation_remaining_cleanup_plan_2026-05-27.md`

验证：

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_page_context.py -q
```

---

## 9. 最终完成标准

剩余清理完成后，应满足：

- `TOPOS_FEATURE_COMPOSITE=true` 能覆盖 `TOPOS_DISABLE_FEATURES=composite`。
- `git status --short` 不再出现 `.images_cache/` 或 `src/.images_cache/`。
- 核心测试不会重新生成旧 cache 目录。
- app deep feature import 预算从 100 收紧到 5 或 8，并测试通过。
- `docs/validation_status_2026-05-27.md` 中的数字与当前复核一致。
- 没有长期挂起的旧 pytest 进程干扰后续验证。

