# T-P1-60 批次 C：绿幕/cryptomatte fixture 接线 + chroma shader 方向修复（W05 收尾）

- 执行日期：2026-09-18；环境：无头 GL 4.1 fragment fallback（AMD Radeon RX 6800，headless standalone context）
- 关联：W05（X-TEST-20 专用 fixture）、T-P1-60 步骤 2；批次 A/B 见 `T-P1-60_20260918-101559/`、`T-P1-60_batchB_20260918-113057/`

## 结果：93 节点矩阵 0 failed

| 状态 | 批次 A（此前） | 批次 C（本次） |
|---|---|---|
| passed_nonflat | 77 | **79**（+chroma_keyer +cryptomatte_select） |
| blocked_missing_model | 1 | 1 |
| blocked_missing_resource | 13 | 13 |
| **failed** | **2** | **0** |

`by_status={"blocked_missing_model": 1, "blocked_missing_resource": 13, "passed_nonflat": 79}`

两行不再是"碰巧非平"，输出均值与 fixture 几何精确吻合（preview u8 读回）：

- `chroma_keyer`：mean=63.75 = 255×25%（fixture 白色主体恰占画面 1/4，幕布预乘为黑）
- `cryptomatte_select`：mean=127.5 = 255×50%（选中左半 id=17 → matte 白左半、黑右半）

## 发现并修复的真实缺陷：两个 chroma shader 的 matte 方向反转

接线 fixture 时发现（这就是 fixture 接线先行的价值——不修语义，绿幕 fixture 只会"证书化"一个反的抠像器）：

1. `_CHROMA_KEYER_FRAGMENT`（chroma_keyer 节点 / keylight 兜底路径）：
   `matte = 1.0 - smoothstep(tol-s, tol+s, distance)` —— 屏幕色附近 matte=1（保留幕布），
   前景 alpha=0（抠掉主体）。与任何抠像器语义相反，也是批次 A 蓝幕参数在真实素材上
   平黑 failed 的根因（真实帧无幕布色 → 全帧 alpha≈0 → 预乘黑）。
2. `_KEYLIGHT_FRAGMENT`（keyer 节点默认 chroma 路径）：`matte = 1.0 - clamp(d, 0, 2)`
   —— 同类反转；且真实素材默认参数下大部分像素 d≥1 → matte≈0，即"默认开 keyer 图就没了"。

修复（`src/features/composite/core/shaders.py`）：

- chroma：`matte = smoothstep(...)`；硬边分支 `1.0 - step(u_tolerance, distance)`
- keylight：`matte = clamp(d, 0.0, 1.0)`（幕布 d≈0 → 透明；前景 d≥1 → 不透明；u_invert 仍可反转）

luma_keyer / difference_keyer 方向核对正确，未改。

## 回归与验证证据

1. **新增像素级 GL 回归** `tests/composite/test_chroma_keyer_gpu.py`（5 项，真实 standalone
   GL 上下文）：chroma/keylight 各自钉住方向语义——matte 模式幕布=0/前景=1；
   result 模式幕布预乘黑、前景不透明。全部通过。
2. **既有套件回归**：test_keyer_nodes + test_upgraded_nodes + test_composite_pixel_level +
   test_cryptomatte_select（271 passed）+ test_node_contract_classification +
   test_capability_registry（41 passed）。
3. **真实管线单节点验证**（CompositeFrameRenderer.render_comp_clip_frame，
   `w05_fixture_check.json`）：chroma_keyer mean=63.75/std=110.4 非平；
   cryptomatte_select（内联验证）左半=1.0 右半=0.0，8-bit ID 编码经
   image_input `color_space="linear"` 显式声明完整存活（不被 srgb→linear 破坏）。
4. **全量矩阵**：`real_topos_all_composite_nodes.json`（93 行六态可解释，退出码 0）。

## fixture 接线方式（可复用）

`scripts/bench_composite_nodes_gpu_coverage.py` 新增 `NODE_FIXTURES` +
`resolve_node_fixtures()`：节点定向源 fixture 注册表，返回 (替代源, 附加节点参数)；
源支持 `(path, 源节点参数)` 元组（image_input 的 color_space 等）。
两个矩阵入口（coverage main / bench_all_composite_nodes_real_topos）同一真相。

- 绿幕 fixture：`fixture_greenscreen.png` 纯绿底 (0,255,0) + 中央白主体；
  参数 screen_color=(0,1,0)、tolerance=0.15、softness=0.05
  （白主体距纯绿加权距离≈0.285，饱和到 matte=1）
- cryptomatte fixture：`fixture_cryptomatte_rgba24.png` 左半 id=17、右半 id=523
  （与 tests/composite/test_cryptomatte_select.py 同编码）；selected_ids="[17.0]"、
  output_mode="matte"、id_encoding="rgba24"、color_space="linear"

## 遗留（不变，批次 B 清单其余项）

- MODEL_UNAVAILABLE evaluator 级结构化返回
- 节点级 performance attribution 字段扩展
- 桌面 Metal/GL4.3 compute runner 验证（X-TEST-10，本环境 backend 字段显式标注 headless_gl_fragment）
