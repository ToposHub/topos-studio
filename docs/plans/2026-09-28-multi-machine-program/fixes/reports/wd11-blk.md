# WD11-BLK 修复报告：上波白名单阻塞项（wd10-g51 批 BLOCKED 3 条）

- 日期：2026-09-30
- 修复人：WD11-BLK（自动化修复会话）
- 计划表条目：REMEDIATION_PLAN.md L514–L516（AUD-14800 / AUD-14806 / AUD-14807）
- 处方来源：`fixes/reports/wd10-g51.md`（该批将 3 条标 BLOCKED，问题现场已核实并附行号证据，建议留给持有文件白名单的批次）
- 白名单（实际改动，以任务行内为准）：`src/features/edit/ai_editing/tool_operation_ledger.py`、`src/features/edit/ai_editing/toposctl.py`
- 新增测试：`tests/edit/test_fix_wd11blk_aud.py`（22 个用例，全数通过）
- 证据报告：`docs/audit/2026-09-28/batches/batch-148-features-edit-ai_editing.md`
- 约束遵守：无裸/宽 except 新增（数值解析收窄为 `except ValueError`，sink 写失败收窄为 `except OSError`，并移除了离线 capabilities 审计处原有的 `except Exception: pass` 吞错）；审计完整性增强（AUD-14807，含一处被吞掉的离线审计记录修复）；指纹无碰撞（AUD-14800，截断并入丢弃材料的 sha256）；新代码均带类型注解；未做任何 git 分支操作、无 commit/push；**未改动 REMEDIATION_PLAN.md（含勾选）**。

## 结论总览

| 编号 | 状态 | 修复方式 |
|------|------|---------|
| AUD-14800 | FIXED | `_canonical` 的每个截断界（str 2000 / 容器 64 / 深度 6）不再静默丢材料：保留部分不变，另并入 `dropped_count`/`full_length` 与被丢弃材料的 sha256（dict 溢出用转义 marker key 防覆盖用户键；深度上限由常量折叠改为整棵子树摘要）——共享前缀的不同参数不再碰撞出同一指纹，错误 REPLAY 消除，真重复的去重 REPLAY 保持 |
| AUD-14806 | FIXED | 新增 `_parse_int_option`（`except ValueError: raise _CliUsageError`，错误消息含选项名与坏值），三处 `int(args[i+1])`（snapshot `--asset-limit`、search-assets `--limit`、help `--limit`）统一走它；坏值命中 `run_cli` 既有 `_CliUsageError` 分支 → exit 2 + SCHEMA_INVALID 诊断 + usage 提示，不再 traceback |
| AUD-14807 | FIXED | `main()` 构造 `_PersistentToolCallLog`（`ToolCallLog` 子类，每条记录产生时即以一行 JSON 追加到 JSONL sink）传给 `run_cli(call_log=...)`；路径解析：显式 `audit_log_path` 参数 > `TOPOSCTL_AUDIT_LOG` 环境变量 > 默认 `~/.topos_cache/toposctl_audit_log.jsonl`（与 assistant_session_store 的 ~/.topos_cache 约定同源）。附带修复：`_capabilities_offline` 原以错误关键字调 `ToolCallLog.record`（TypeError 被 `except Exception: pass` 吞掉，离线 capabilities 审计从未落日志）→ 改为正确构造 `ToolCallRecord` 记录 |

**汇总：3 FIXED / 0 INVALID / 0 BLOCKED。**

---

## 验证记录（必须执行项）

### py_compile（2 个改动源文件 + 1 个新增测试，全部通过）

```
$ python3 -m py_compile src/features/edit/ai_editing/tool_operation_ledger.py \
    src/features/edit/ai_editing/toposctl.py tests/edit/test_fix_wd11blk_aud.py
PY_COMPILE_OK (exit 0)
```

### 新增测试

```
$ .venv/bin/python -m pytest tests/edit/test_fix_wd11blk_aud.py -q -p no:cacheprovider
22 passed in 1.68s
```

（首跑 21/22：`test_main_returns_2_for_bad_numeric_option` 初版未连宿主——`run_cli` 先判 NOT_CONNECTED（exit 1）再做参数解析，坏 `--limit` 到不了解析层。桩掉 `LocalMcpIpcClient` 走到解析后 22/22。这同时确认：无宿主时坏数值选项返回结构化 exit 1，同样不再是 traceback。）

### 邻接既有测试（grep toposctl / tool_operation_ledger / run_cli 消费方）

```
$ .venv/bin/python -m pytest tests/edit/ai_editing/test_toposctl.py \
    tests/edit/ai_editing/test_toposctl_offline.py \
    tests/edit/ai_editing/test_tool_operation_ledger.py \
    tests/edit/test_fix_wd10g51_aud.py -q -p no:cacheprovider
85 passed in 1.90s

$ .venv/bin/python -m pytest tests/edit/ai_editing -q -p no:cacheprovider
2009 passed, 4 skipped in 22.00s        # ai_editing 全量（含 ledger 消费方 tool_invocation / agent runner 等）

$ .venv/bin/python -m pytest tests/edit/test_scene_cut_clip_range.py \
    tests/edit/test_fix_wd11blk_aud.py tests/edit/test_fix_wd10g51_aud.py -q -p no:cacheprovider
82 passed in 2.82s                      # 其余 grep 命中的 toposctl/run_cli 消费方
```

**既有测试失败与锁定旧行为协议：无。** 全量 ai_editing 目录 2009 passed / 4 skipped（4 skip 为既有 GL/环境跳过），无任何翻正需求。

### 端到端行为证据（真实进程）

```
$ .venv/bin/python -c "... main(['--socket','/dev/null','snapshot','--asset-limit','abc'], audit_log_path=...)"
stdout（单一 JSON）: {"exit_code": 2, ..., "diagnostics": [{"code": "TAEP_SCHEMA_INVALID",
  "details": {"reason": "CLI usage error: --asset-limit expects an integer, got 'abc'"}}, ...], "usage_hint": "..."}
exit_code=2                                        # 修复前：ValueError traceback

$ .venv/bin/python -c "... main(['capabilities'], audit_log_path='/tmp/wd11blk-audit-demo.jsonl')"; cat <sink>
exit_code= 0
{"call_id": "tc-00000001", "timestamp": ..., "tool_name": "topos.get_capabilities",
 "client": "cli", ..., "succeeded": true, ...}     # 审计记录已持久化，进程退出后仍在
```

---

## 逐条详情

### AUD-14800 — FIXED

**位置（修复前）**：`tool_operation_ledger.py:49-54`（`_canonical`）。

**根因**：三个界都做**静默截断**——dict 排序后 `[:64]`、list/tuple `[:64]`、str 及非 JSON 标量 `str()` 后 `[:2000]`（另有 `depth>=6` 一律折叠为常量 `"<fingerprint-depth-limit>"`，同类问题）。只要两个参数在保留前缀上相同、差异落在丢弃部分，`fingerprint_args` 就产出同一 sha256——`begin()` 命中 `SUCCEEDED` 记录即错误 REPLAY 他人结果（tool_operation_ledger.py 原 :210-211），且无任何告警。

**修复**（按 wd10-g51 处方"截断时并入被丢弃部分长度或 hash"，采用 hash——长度仍可同长异容）：

- 保留行为不变（同样的 kept-prefix、同样的排序/深度/大小界限），但每个界在触发时并入一个 marker：`{"$topos.fingerprint.truncated": {"reason", "prefix"/"dropped_count"/"full_length", "dropped_sha256"}}`，其中摘要为被丢弃材料的 sha256（str 全文摘要；容器被丢 items 的分隔符分帧 `repr` 流摘要，`\x1e` 记录分隔符防相邻项合并）。
- dict 溢出的 marker key 若与用户键冲突则追加 `*` 转义，绝不覆盖用户材料（覆盖本身就再引入碰撞面）；序列溢出以追加元素表达，类型上不与普通 dict/str 混淆。
- 深度上限从"折叠为常量"改为整棵超界子树的 `str()` 摘要——深但不同的树不再互撞。
- 残余理论面（docstring 已声明）：只有蓄意伪造完全相同的 marker 形状（或 sha256 被破）才可能别名两棵不同的参数树；意外碰撞（长字符串共前缀、>64 容器共前缀、深嵌套）已不可能。指纹不是鉴权边界，参数本身不经认证。

**测试**（9 例）：共享 2000 前缀的长字符串、2000 vs 2001 边界、共享前 64 元素的 list、共享前 64 键的 71 键 dict、超深度嵌套、`str()` 溢出的非 JSON 标量——各自指纹互异；相同参数指纹稳定；小参数语义不变；端到端 ledger 判定：前缀碰撞参数在 A 成功后 B 必须 EXECUTE（不再错误 REPLAY），而真重复的 A 重放仍 REPLAY（去重保留）。

### AUD-14806 — FIXED

**位置（修复前）**：`toposctl.py:321`（另 337、373）——`out["asset_limit"] = int(args[i + 1])` 等三处。

**根因**：`int()` 对非整数值抛 ValueError，不在 `run_cli` 的 `except _CliUsageError` / `(OSError, ConnectionError, TimeoutError, json.JSONDecodeError)` 任一列表内，`main()` 也无兜底——`toposctl snapshot --asset-limit abc` 以 Python traceback 崩溃（uncaught exception 退出码 1 + 堆栈），违反 CLI 错误退出码规范（用户输入错误应 exit 2）。

**修复**（按 wd10-g51 处方）：新增 `_parse_int_option(flag, raw)`（`except ValueError as exc: raise _CliUsageError(f"{flag} expects an integer, got {raw!r}") from exc`），三处解析统一改走它。`_parse_snapshot/_parse_search/_parse_help` 在 `run_cli` 的 try 块内被调用，`_CliUsageError` 由既有分支（:187-188 语义）转为 `CliResult(exit_code=2, SCHEMA_INVALID 诊断, usage_hint)`。无新增宽 except（收窄为 ValueError），合法整数值（含负数）解析路径不变。

**测试**（6 例）：snapshot/search-assets/help 三命令的坏数值 → exit 2 + TAEP_SCHEMA_INVALID + 未触达 invoke；诊断 reason 点名选项与坏值；合法值（含 `-3`）照常解析；main() 层（连宿主）坏 `--limit` → 进程退出码 2 且 stdout 仍为单一可解析 JSON。

### AUD-14807 — FIXED

**位置（修复前）**：`toposctl.py:437`（配合 run_cli :130 的 `log = call_log or ToolCallLog()` 回退）。

**根因**：`main()` 调 `run_cli(filtered, invoke_fn)` 不传 `call_log`，run_cli 回退到纯内存临时 `ToolCallLog()`，进程退出即丢弃——模块红线自述"每次调用经 ToolCallLog 记录（client='cli'）"在 console 入口不可满足。**现场另核实一处连带缺陷**：`_capabilities_offline` 以 `log.record(tool_name=..., args=..., result=..., client=...)` 关键字调用只接受 `ToolCallRecord` 位置参数的 `ToolCallLog.record`（签名不符 → TypeError），且被既有 `except Exception: pass` 吞掉——离线 capabilities 的审计记录从未真正落日志，即使传了 call_log 也收不到。

**修复**：

1. `_PersistentToolCallLog(ToolCallLog)`（toposctl.py:431）：每条记录在 `record()` 内即时以一行 JSON 追加到落盘 sink（`json.dumps(rec.to_dict())` + `\n`，append 模式），进程后续崩溃也不丢已产生的记录；内存语义（锁、FIFO、call_id 唯一）继承 AUD-14710 修复后的基类；写失败（`except OSError`）warning 一次并停用 sink，不破坏 stdout 单一 JSON / 退出码契约。
2. 路径解析 `_resolve_audit_log_path`（:468）：显式 `main(..., audit_log_path=...)` 参数 > `TOPOSCTL_AUDIT_LOG` 环境变量 > 默认 `~/.topos_cache/toposctl_audit_log.jsonl`（与 `assistant_session_store` 的 ~/.topos_cache 用户本机轻量状态约定同源；记录本身经 `summarize_args/result` 脱敏，不含 secret/绝对路径）。目录不可创建时（`except (OSError, RuntimeError)`）降级为进程内日志并 warning 留痕。
3. `main()`（:544-545）构造该 sink 并 `run_cli(filtered, invoke_fn, call_log=audit_log)`。
4. `_capabilities_offline` 的审计调用改为正确构造 `ToolCallRecord`（call_id/timestamp/summarize_args/summarize_result/succeeded）后 `log.record(...)`，并删除原有的 `except Exception: pass`（它守护的正是那个坏调用；audit 失败的降级职责已由 sink 内部承接）。

**测试**（7 例）：main(capabilities) → sink JSONL 落盘且记录 client="cli"；同一路径跨调用 append-only（2 行）；`TOPOSCTL_AUDIT_LOG` 生效；version 无工具调用 → 无记录；父路径为普通文件时优雅降级（exit 0 + JSON 正常）；离线 capabilities 现在真正记录进传入的 call_log（修复前恒 0 条）；connected 路径注入 call_log 照常记录。

---

## 观察与移交（不属本批白名单）

1. **SCHEMA_INVALID 诊断消息模板占位符未替换**：端到端验证中 `TAEP_SCHEMA_INVALID` 的 `message` 渲染为原始 `计划不符合 TAEP JSON Schema：{detail}`（`{detail}` 未代入，完整信息只在 `details.reason`）。与 wd10 批已修复的 AUD-14410（REVISION_CONFLICT 同类模板 bug，commit 71a67888）同源，病灶在 `diagnostics.py` 的消息模板，不在本批白名单。建议持有该文件白名单的批次照 AUD-14410 方式处理。
2. **行为面变化声明**：`main()`（console 入口）现在默认向 `~/.topos_cache/toposctl_audit_log.jsonl` 追加审计记录（可用 `TOPOSCTL_AUDIT_LOG` 重定向或显式参数注入）。这是"审计不随进程丢弃"的直接后果，且与仓库既有 ~/.topos_cache 约定一致；`run_cli` 作为库函数的行为不变（仍默认临时日志，调用方可注入）。
3. **优先级说明**：`run_cli` 对"未连接宿主"（exit 1）的判定先于参数解析，因此 `toposctl snapshot --asset-limit abc`（无 `--socket`）返回结构化 NOT_CONNECTED exit 1 而非 exit 2。这是既有优先序（未知命令才优先判 exit 2），本批未改动；两种路径均不再 traceback。

## 变更足迹

源码（2 文件，+222/-24）：
`tool_operation_ledger.py`（指纹规范化重写：截断 marker + 摘要，+112/-? 于 :43-148 区域）、`toposctl.py`（+134：数值选项解析助手 :325-337、三处调用点、持久化审计 sink :421-492、main() 接线 :544-545、离线 capabilities 审计调用修复 :255-268）。

测试：新增 `tests/edit/test_fix_wd11blk_aud.py`（22 用例：AUD-14800 ×9、AUD-14806 ×6、AUD-14807 ×7）。无既有断言翻正（全量 ai_editing 2009 passed / 4 skipped 清洁）。
