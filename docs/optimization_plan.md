# 节点体系扩展 - 审计报告与优化大纲

> 审计日期: 2026-05-14
> 审计范围: 对照需求文档 Section 0-19 逐项检查实现状态

---

## 总体评估

| 指标 | 数值 |
|------|------|
| NODE_REGISTRY 总条目 | 52 |
| 已实现节点（有真实 class） | 41 |
| 注册为 None 的占位符 | 11 |
| 代码文件存在但未注册的节点 | 6 |
| 测试总数 | 100 (全部通过) |
| compileall | 通过 |
| 序列化测试覆盖节点数 | 6/41 (15%) |
| bbox/cache key/GPU 测试 | 0 |

**核心结论**: 节点骨架和基础功能完成度较高，但存在三类系统性问题：
1. **注册不一致** — 多个已实现的节点被标为 placeholder
2. **测试严重不足** — 只有 registration/instantiation 级别测试，无行为测试
3. **分类混乱** — 同一节点出现在多个类别，Effect 和 Filter 重复

---

## 一、注册不一致（高优先级）

以下节点**有完整实现文件但 NODE_REGISTRY 中标记为 None**，应立即修复：

| 节点 | 实现文件 | 注册状态 | 备注 |
|------|----------|----------|------|
| `roto_shape` | `roto_shape_node.py` | `None` (placeholder) | 有完整 bbox/ROI/evaluate |
| `mask_merge` | `mask_merge_node.py` | `None` (placeholder) | 有完整 compute 逻辑 |
| `matte_cleanup` | `matte_cleanup_node.py` | `None` (placeholder) | 有完整 PARAM_DEFS 和 evaluate |
| `time_offset` | `time_offset_node.py` | `None` (placeholder) | 结构完整，effect marker 模式 |
| `frame_hold` | `frame_hold_node.py` | `None` (placeholder) | 结构完整 |
| `retime` | `retime_node.py` | `None` (placeholder) | 结构完整 |

**修复操作**:
- 在 `node_engine.py` 顶部 import 这些节点类
- 将 `NODE_REGISTRY` 和 `NODE_CATEGORIES` 中的 `None` 替换为实际类引用
- 验证右键菜单和搜索 popup 能找到这些节点

> 注意：Time 节点（time_offset/frame_hold/retime）虽然有代码但本质是框架占位 —— evaluate 只返回 effect marker，GPU 侧没有对应 shader。注册后应标注为 "limited" 或在 UI 中标记。

---

## 二、测试覆盖不足（高优先级）

### 2.1 当前测试状态

100 个测试全部是 **registration + instantiation** 级别，仅 6 个节点有序列化测试：

| 测试文件 | 测试数 | 覆盖节点 | 有序列化测试 | 有行为测试 |
|----------|--------|----------|-------------|-----------|
| `test_composite_nodes_ai.py` | 4 | ai_inpaint, ai_remove, ai_expand | 无 | 无 |
| `test_composite_nodes_channel.py` | 6 | shuffle, copy_channel, set_alpha | shuffle | 无 |
| `test_composite_nodes_color.py` | 7 | grade, color_correct, invert, clamp, cs_transform | grade | 无 |
| `test_composite_nodes_filter.py` | 9 | blur, defocus, glow, sharpen, erode_dilate, denoise | blur | 无 |
| `test_composite_nodes_input.py` | 8 | image_input, constant, text, output, file_output | image_input, constant | 无 |
| `test_composite_nodes_keyer.py` | 4 | luma_keyer, chroma_keyer, keyer, matte_cleanup | 无 | 无 |
| `test_composite_nodes_mask.py` | 2 | roto_shape, mask_merge | 无 | 无 |
| `test_composite_nodes_merge.py` | 10 | merge, premult, unpremult, dissolve, switch, mask, matte | merge | 无 |
| `test_composite_nodes_registry.py` | 5 | 全局 | N/A | N/A |
| `test_composite_nodes_time.py` | 3 | time_offset, frame_hold, retime | 无 | 无 |
| `test_composite_nodes_transform.py` | 6 | transform, reformat, crop | transform | 无 |
| `test_composite_nodes_utility.py` | 4 | dot, noop | 无 | 无 |

### 2.2 需要补充的测试（按优先级）

#### P0 — 每个节点必须有的基础测试

对**所有 41 个已实现节点**，补充以下测试：

```python
# 1. 序列化往返测试
def test_to_dict_from_dict(self):
    node = XxxNode()
    node.parameters["key"] = new_value
    d = node.to_dict()
    node2 = XxxNode()
    node2.from_dict(d)
    assert node2.parameters["key"] == new_value

# 2. 默认参数完整性测试
def test_default_params_complete(self):
    node = XxxNode()
    for pdef in node.PARAM_DEFS:
        assert pdef.name in node.parameters

# 3. PARAM_DEFS 与 parameters 一致性测试
def test_param_defs_match_parameters(self):
    node = XxxNode()
    param_names = {p.name for p in node.PARAM_DEFS}
    default_names = set(node.parameters.keys())
    assert param_names == default_names
```

#### P1 — 关键行为测试

```python
# 4. bbox 传播测试（有输入的节点）
def test_bbox_propagation(self):
    node = XxxNode()
    # 设置输入 bbox，验证输出 bbox 是否正确

# 5. ROI 反向传播测试（空间类节点）
def test_roi_backward_propagation(self):
    node = XxxNode()
    # 验证 compute_input_roi 返回正确的 ROI

# 6. cache key 参数包含测试
def test_cache_key_includes_critical_params(self):
    # 验证影响输出的参数都参与 cache key 计算
```

#### P2 — 可选 GPU 测试

```python
# 7. 小图像 GPU 正确性测试（标记 @pytest.mark.gpu）
@pytest.mark.gpu
def test_gpu_output_small_image(self):
    # 2x2 或 4x4 图像，验证 GPU 输出像素值
```

### 2.3 建议测试文件组织

按需求文档的建议，保持现有文件拆分，但在每个文件中补充完整测试：

| 文件 | 需补充节点 | 预估新增测试数 |
|------|-----------|---------------|
| `test_composite_nodes_input.py` | text, output, file_output 的序列化；Constant pattern 行为 | ~15 |
| `test_composite_nodes_merge.py` | premult, unpremult, dissolve, switch, mask, matte 的序列化+bbox | ~25 |
| `test_composite_nodes_transform.py` | reformat, crop 的序列化+bbox+ROI | ~12 |
| `test_composite_nodes_channel.py` | copy_channel, set_alpha 的序列化；Shuffle channel mapping | ~10 |
| `test_composite_nodes_color.py` | color_correct, invert, clamp, cs_transform 的序列化 | ~15 |
| `test_composite_nodes_filter.py` | defocus, glow, sharpen, erode_dilate 的序列化；Blur ROI padding | ~20 |
| `test_composite_nodes_keyer.py` | 全部节点的序列化 | ~12 |
| `test_composite_nodes_mask.py` | roto_shape, mask_merge 的序列化+bbox | ~8 |
| `test_composite_nodes_time.py` | 全部节点的序列化 | ~6 |
| **总计** | | **~123** |

---

## 三、节点分类问题（中优先级）

### 3.1 重复注册问题

以下节点在 `NODE_CATEGORIES` 中出现**两次**，造成右键菜单重复：

| 节点 | 出现的类别 |
|------|-----------|
| `transform` | Effect, Transform |
| `reformat` | Effect, Transform |
| `crop` | Effect, Transform |
| `blur` | Effect, Filter |
| `glow` | Effect, Filter |
| `denoise` | Effect, Filter |
| `sharpen` | Effect, Filter |
| `erode_dilate` | Effect, Filter |
| `defocus` | Effect, Filter |
| `grade` | Effect, Color |
| `invert` | Effect, Utility |
| `keyer` | Keyer, Merge |
| `mask` | Merge, Mask |

### 3.2 建议

- **删除 "Effect" 类别** — 它是 Effect/Filter/Transform/Color 的混合，拆分后不需要
- 将 Effect 中的节点分别归入对应专业类别
- `keyer` 不应出现在 Merge 类别中
- `mask` 和 `matte` 应明确区分：mask 在 Mask 类别，matte 可保留在 Merge

### 3.3 建议的最终分类

```
Input:    color_input, image_input, constant, video_frame, text
Output:   output, file_output
Merge:    merge, blend, premult, unpremult, dissolve, switch, matte, mask
Transform: transform, reformat, crop, cornerpin (placeholder)
Channel:  shuffle, copy_channel, set_alpha, remove_channels (placeholder)
Color:    grade, color_correct, cs_transform, huecorrect (placeholder), lut (placeholder)
Filter:   blur, glow, denoise, sharpen, erode_dilate, defocus
Keyer:    keyer, luma_keyer, chroma_keyer, difference_keyer (placeholder)
Mask:     roto_shape, mask_merge, matte_cleanup
Utility:  dot, noop, invert, clamp, normalize (placeholder), compare (placeholder)
Time:     time_offset, frame_hold, retime
AI:       ai_generate, ai_inpaint, ai_expand, ai_remove
```

---

## 四、各需求章节完成状态

### Section 0: 阶段前置条件

| 前置条件 | 状态 | 备注 |
|----------|------|------|
| Format/BBox/ROI/Alpha/ColorSpace 语义稳定 | ✅ 完成 | BBox, Format 类健全 |
| 基础节点可靠 (Merge/Premult/Reformat/Crop/Transform/Channel) | ✅ 完成 | 全部有实现 |
| RenderScheduler/Cache/ROI/Tile/GPUResourcePool | ✅ 完成 | 架构完整 |
| 参数序列化和版本迁移 | ✅ 完成 | 支持 v1→v4 迁移 |
| NodePropertiesPanel 稳定显示参数 | ⚠️ 部分 | 缺少 color picker 控件 |
| Viewer 支持 RGB/R/G/B/A 查看 | 未验证 | 需实际运行确认 |
| 统一测试路径 | ❌ 不足 | 只有注册级别测试 |

### Section 1: 节点开发规范

| 规范要求 | 状态 | 备注 |
|----------|------|------|
| node_type/title/header_color | ✅ 所有节点 | |
| input/output ports | ✅ 所有节点 | |
| PARAM_DEFS | ✅ 所有节点 | |
| to_dict/from_dict | ⚠️ 继承自 base | 多数节点未覆盖，依赖基类 |
| bbox 计算逻辑 | ✅ 大部分节点 | |
| ROI 反向传播 | ⚠️ 部分节点 | 一些空间节点缺少 ROI padding |
| cache key 参数 | 未验证 | 需检查 graph_evaluator_mixin |
| GPU shader 实现 | ⚠️ 部分节点 | 有 marker 系统，但部分 shader 可能缺失 |
| tile 支持 | 未验证 | |
| preview 降质 | 未验证 | |

### Section 2: 节点分类体系

| 要求 | 状态 | 备注 |
|------|------|------|
| 14 个分类 | ✅ 有分类 | 但 Effect 重复，需清理 |
| NODE_REGISTRY 更新 | ✅ | |
| NODE_CATEGORIES 更新 | ⚠️ | 有重复 |
| 右键菜单分类 | ⚠️ | Effect 类别造成混乱 |
| 节点搜索菜单 | ✅ | 搜索 popup 功能完整 |
| placeholder 标记 | ❌ | 未注册的已实现节点没有标记 |

### Section 3: Input 节点扩展

#### 3.1 ImageInput

| 功能 | 状态 | 备注 |
|------|------|------|
| PNG/JPEG/TIFF/EXR | ✅ | 完整格式支持 |
| alpha 支持 | ✅ | |
| 16bit/float | ✅ | |
| color_space 参数 | ✅ | auto/sRGB/linear/ACES |
| missing file 状态 | ✅ | error/black/checker |
| async decode | ✅ | QThread worker |
| reload | ✅ | mtime-based |
| frame sequence | ⚠️ | 参数存在但未实现 |

#### 3.2 Constant

| 功能 | 状态 | 备注 |
|------|------|------|
| solid color | ✅ | |
| checker | ✅ | |
| gradient | ✅ | horizontal/vertical/radial |
| noise | ❌ | 未实现 |
| HDR color | ✅ | 不强制 0-1 |

#### 3.3 Text

| 功能 | 状态 | 备注 |
|------|------|------|
| 文字图层 | ✅ | QPainter 实现 |
| 字体/大小/颜色/对齐 | ✅ | |
| 描边 | ✅ | outline_enabled/width/color |
| 阴影 | ❌ | 未实现 |
| 中文支持 | 未验证 | 依赖 QFont |
| alpha 边缘 | 未验证 | |

### Section 4: Output 节点扩展

#### 4.1 Output 节点

| 功能 | 状态 | 备注 |
|------|------|------|
| 明确输出 format | ✅ | |
| output color space | ✅ | |
| alpha export policy | ⚠️ | 需确认具体策略参数 |
| viewer/output 分离 | 未验证 | |

#### 4.2 FileOutput / Write

| 功能 | 状态 | 备注 |
|------|------|------|
| 多输出节点 | ✅ | |
| 文件格式支持 | ✅ | PNG/JPEG/TIFF/EXR |
| bit depth | ✅ | 8/16/32 |
| color space | ✅ | |
| compression | ✅ | |

### Section 5: Merge / Composite 节点扩展

#### 5.1 Merge

| 操作 | 状态 |
|------|------|
| over, under, plus, minus, multiply, screen, overlay | ✅ |
| difference, max, min, copy | ✅ |
| mask, stencil, atop | ⚠️ stencil/atop 未确认 |
| xor | ❌ |
| mix 参数 | ✅ |
| alpha_mode | ✅ |
| format/bbox policy | ✅ |
| clamp_output | ⚠️ 需确认 |

#### 5.2 Dissolve

| 功能 | 状态 |
|------|------|
| mix 混合 | ✅ |
| alpha_mode | ✅ |

#### 5.3 Switch

| 功能 | 状态 |
|------|------|
| 多输入选择 | ✅ |
| input_count 动态端口 | ✅ |
| 未选输入不强制渲染 | ⚠️ 需验证 |

### Section 6: Transform / Format 节点扩展

#### 6.1 Transform2D

| 功能 | 状态 |
|------|------|
| translate/rotate/scale | ✅ |
| pivot | ✅ |
| filter | ✅ (linear/nearest) |
| skew | ❌ |
| bbox 更新 | ✅ |
| ROI 反向传播 | ✅ |

#### 6.2 Reformat

| 功能 | 状态 |
|------|------|
| fit/fill/distort | ✅ |
| 预设格式 | ✅ |
| filter | ✅ |
| preserve_bbox | ⚠️ 需确认 |

#### 6.3 Crop

| 功能 | 状态 |
|------|------|
| crop bbox | ✅ |
| crop format | ✅ |
| outside_policy | ✅ |

#### 6.4 CornerPin

| 功能 | 状态 |
|------|------|
| placeholder | ✅ (None) |

### Section 7: Channel 节点扩展

| 节点 | 状态 | 备注 |
|------|------|------|
| Shuffle | ✅ | 支持 zero/one/luma |
| CopyChannel | ✅ | |
| SetAlpha | ✅ | constant/luma/r/g/b |
| Remove/KeepChannels | ❌ | placeholder |

### Section 8: Color 节点扩展

| 节点 | 状态 | 备注 |
|------|------|------|
| Grade | ✅ | lift/gamma/gain/offset/blackpoint/whitepoint/multiply/add/channels/mix |
| ColorCorrect | ✅ | shadows/midtones/highlights/saturation/contrast/hue/exposure/temperature |
| HueCorrect | ❌ | placeholder |
| LUT/CSTransform | ⚠️ | CSTransform 已实现, LUT 为 placeholder |

### Section 9: Filter 节点扩展

| 节点 | 状态 | 备注 |
|------|------|------|
| Blur | ✅ | gaussian/box/directional, x/y 独立, edge policy |
| Defocus | ✅ | |
| Sharpen | ✅ | |
| Glow | ✅ | |
| Erode/Dilate | ✅ | |

### Section 10: Keyer / Matte 节点扩展

| 节点 | 状态 | 备注 |
|------|------|------|
| LumaKeyer | ✅ | black/white point + softness |
| ChromaKeyer | ✅ | screen_color/tolerance/softness/despill/edge |
| DifferenceKeyer | ❌ | placeholder |
| MatteCleanup | ⚠️ | 代码存在但未注册 |

### Section 11: Mask / Roto 节点扩展

| 节点 | 状态 | 备注 |
|------|------|------|
| RotoShape | ⚠️ | 代码存在但未注册；rectangle/ellipse/polygon |
| RotoPaint | ❌ | 未规划 |
| MaskMerge | ⚠️ | 代码存在但未注册 |

### Section 12: Utility / Debug 节点

| 节点 | 状态 | 备注 |
|------|------|------|
| Dot | ✅ | |
| NoOp | ✅ | |
| Clamp | ✅ | |
| Normalize | ❌ | placeholder |
| Compare | ❌ | placeholder |
| Probe/Viewer | 未验证 | |

### Section 13: Time 节点预留

| 节点 | 状态 | 备注 |
|------|------|------|
| TimeOffset | ⚠️ | 代码存在但未注册，effect marker 模式 |
| FrameHold | ⚠️ | 同上 |
| Retime | ⚠️ | 同上 |

### Section 14: AI 节点扩展

| 节点 | 状态 | 备注 |
|------|------|------|
| ai_generate | ✅ | |
| ai_inpaint | ✅ | |
| ai_expand | ✅ | |
| ai_remove | ✅ | |
| ai_denoise | ❌ | 未在 NODE_REGISTRY 注册 |
| ai_upscale | ❌ | 未在 NODE_REGISTRY 注册 |

> 注意：`ai_denoise_node.py` 和 `ai_upscale_node` (如果存在) 未被 import 到 node_engine.py

### Section 15: 节点搜索与创建体验

| 功能 | 状态 | 备注 |
|------|------|------|
| Tab 搜索 | ✅ | |
| 关键词过滤 | ✅ | |
| 别名支持 | ✅ | 60+ 别名 |
| 最近使用 | ✅ | 最近 20 个 |
| 收藏节点 | ✅ | Ctrl+D |
| placeholder 标记 | ❌ | UI 中无明确标识 |

### Section 16: 节点文档与帮助

| 要求 | 状态 | 备注 |
|------|------|------|
| docs/composite_nodes.md | ✅ | 存在，34 个节点已文档化 |
| HELP_TEXT | ⚠️ | 搜索 popup 用了 tooltip，但不确定每个节点都有 |
| 文档与实现一致性 | ⚠️ | 有 5+ 节点实现但未文档化，placeholder 状态标注不准确 |

### Section 17: 测试计划

| 测试文件 | 状态 | 备注 |
|----------|------|------|
| test_composite_nodes_input.py | ⚠️ 存在 | 只有注册测试 |
| test_composite_nodes_merge.py | ⚠️ 存在 | 只有注册 + merge 序列化 |
| test_composite_nodes_transform.py | ⚠️ 存在 | 只有注册 + transform 序列化 |
| test_composite_nodes_channel.py | ⚠️ 存在 | 只有注册 + shuffle 序列化 |
| test_composite_nodes_color.py | ⚠️ 存在 | 只有注册 + grade 序列化 |
| test_composite_nodes_filter.py | ⚠️ 存在 | 只有注册 + blur 序列化 |
| test_composite_nodes_keyer.py | ⚠️ 存在 | 只有注册 |
| test_composite_nodes_serialization.py | ❌ 缺失 | 需要专项序列化测试 |

### Section 18-19: 实施顺序与完成标准

根据各批次的完成情况：

| 批次 | 内容 | 完成度 |
|------|------|--------|
| 第一批: 基础生产节点 | Read/Constant/Output/Write/Merge/Dissolve/Switch | **90%** |
| 第二批: 格式与变换节点 | Reformat/Crop/Transform/CornerPin | **90%** (CornerPin 除外) |
| 第三批: 通道与 alpha 节点 | Shuffle/CopyChannel/SetAlpha/RemoveChannels | **75%** (RemoveChannels 除外) |
| 第四批: 颜色节点 | Grade/ColorCorrect/HueShift/LUT | **60%** (HueCorrect/LUT 除外) |
| 第五批: 滤镜节点 | Blur/Sharpen/Glow/ErodeDilate/Defocus | **100%** |
| 第六批: Key/Matte/Mask | LumaKeyer/ChromaKeyer/MatteCleanup/RotoShape/MaskMerge | **70%** (代码在但 3 个未注册) |
| 第七批: 体验节点与工具 | Dot/NoOp/Probe/Compare/Clamp/Normalize | **50%** |
| 第八批: AI 和 Time 预留 | Time/AI placeholders | **50%** |

---

## 五、具体修复任务清单

### 优先级 1 — 立即修复（阻塞其他工作）

#### 1.1 注册缺失节点

文件: `src/composite/node_engine.py`

```python
# 添加 import (文件顶部)
from .nodes.roto_shape_node import RotoShapeNode
from .nodes.mask_merge_node import MaskMergeNode
from .nodes.matte_cleanup_node import MatteCleanupNode
from .nodes.time_offset_node import TimeOffsetNode
from .nodes.frame_hold_node import FrameHoldNode
from .nodes.retime_node import RetimeNode

# NODE_REGISTRY 修改
"roto_shape": RotoShapeNode,      # 替换 None
"mask_merge": MaskMergeNode,       # 替换 None
"matte_cleanup": MatteCleanupNode, # 替换 None
"time_offset": TimeOffsetNode,     # 替换 None
"frame_hold": FrameHoldNode,       # 替换 None
"retime": RetimeNode,              # 替换 None

# NODE_CATEGORIES 同步修改所有对应条目
```

#### 1.2 清理分类重复

- 删除 "Effect" 类别，将其中节点归入正确类别
- 从 Merge 中移除 keyer
- 从 Merge 中移除 mask（mask 属于 Mask 类别）

#### 1.3 检查 ai_denoise 注册

`ai_denoise_node.py` 存在但可能在 registry 中缺少。确认 `DenoiseNode` (filter) 和 `AIDenoiseNode` (AI) 的区分是否正确。

### 优先级 2 — 短期修复（1-2 周）

#### 2.1 补全序列化测试

为所有 41 个已实现节点添加 `test_to_dict_from_dict` 测试。可使用模板：

```python
def test_to_dict_from_dict(self):
    """序列化/反序列化往返测试"""
    node = XxxNode()
    # 修改一些参数
    node.parameters["some_param"] = non_default_value
    d = node.to_dict()
    restored = XxxNode()
    restored.from_dict(d)
    assert restored.parameters == node.parameters
    assert restored.node_type == "xxx"
```

#### 2.2 补全 PARAM_DEFS 一致性测试

```python
def test_param_defs_match_defaults(self):
    """PARAM_DEFS 与 parameters 默认值一致"""
    node = XxxNode()
    for pdef in node.PARAM_DEFS:
        assert pdef.name in node.parameters, f"Missing default: {pdef.name}"
```

#### 2.3 补全 bbox/ROI 行为测试

对以下节点添加 bbox 和 ROI 传播测试：
- Merge (各种 bbox_policy)
- Transform (位移/缩放后 bbox 变化)
- Crop (裁剪后 bbox)
- Reformat (尺寸变化)
- Blur (ROI padding)

#### 2.4 更新文档

- 将 roto_shape, mask_merge, matte_cleanup, time 系列节点添加到 `docs/composite_nodes.md`
- 标注 placeholder 节点的准确状态
- 确认所有文档参数与 PARAM_DEFS 一致

### 优先级 3 — 中期优化（1 个月）

#### 3.1 ROI padding 修复

检查所有空间效果节点的 `compute_input_roi`：
- Blur: 需要按 size 扩展 ROI
- ErodeDilate: 需要按 size 扩展 ROI
- Glow: 需要按 blur_size 扩展 ROI
- Defocus: 需要 ROI padding
- MatteCleanup: 已有 `requires_roi_expansion` 但 `compute_input_roi` 没有实际扩展

#### 3.2 GPU shader 补全

确认 GPU renderer 中是否有以下 effect_type 的处理逻辑：
- `mask_merge`
- `matte_cleanup`
- `roto_shape`
- `time_offset` / `frame_hold` / `retime`

如果 GPU 侧没有对应实现，这些节点注册后运行时会只返回空结果。

#### 3.3 Color picker 控件

NodePropertiesPanel 目前支持 option/float/int/string/bool，但缺少：
- **color picker** — Grade/Constant/Text/ChromaKeyer 等节点需要
- **slider 联动** — 验证 float 参数的 slider 是否正常工作

#### 3.4 未实现占位节点

以下 placeholder 需要决定是否在当前阶段实现：
- `cornerpin` — Transform 类别，需求中标记 "先做 placeholder"
- `remove_channels` — Channel 类别
- `huecorrect` — Color 类别
- `lut` — Color 类别
- `difference_keyer` — Keyer 类别
- `normalize` — Utility 类别
- `compare` — Utility 类别

### 优先级 4 — 长期完善

#### 4.1 GPU 端到端测试

添加小图像 GPU 正确性测试（标记 `@pytest.mark.gpu`，CI 中可选跳过）。

#### 4.2 Tile 渲染测试

验证空间效果节点在 tile 模式下无接缝。

#### 4.3 旧项目迁移测试

创建 v1/v2/v3 格式的测试数据，验证迁移到 v4 是否正确补齐新参数默认值。

#### 4.4 Frame Sequence 实现

ImageInput 的 frame sequence 功能（参数已预留，逻辑未实现）。

---

## 六、验收标准差距

对照需求 Section 19 的完成标准：

| 标准 | 达标 | 差距 |
|------|------|------|
| 节点库覆盖基础合成工作流 | ⚠️ | RotoShape/MaskMerge/MatteCleanup 未注册 |
| 每类节点有清晰分类和搜索入口 | ⚠️ | Effect 重复，需清理 |
| 遵守 Format/BBox/Alpha/ColorSpace/ROI | ⚠️ | ROI padding 可能不完整 |
| 关键节点支持 tile 和 cache | 未验证 | |
| 新节点保存/加载兼容 | ⚠️ | 序列化测试不足 |
| Viewer 可查看任意节点输出 | 未验证 | |
| 用户可完成基础抠像/叠加/调色/通道处理/mask cleanup/导出 | ⚠️ | MatteCleanup 未注册 |
| compileall 通过 | ✅ | |
| 纯逻辑测试通过 | ⚠️ | 测试太浅，只有注册级别 |

---

## 七、建议工作顺序

1. **注册修复** (1-2h) — 修复 6 个已实现但未注册的节点
2. **分类清理** (1h) — 删除 Effect 重复，统一分类
3. **序列化测试** (4-6h) — 为所有 41 个节点补充序列化测试
4. **bbox/ROI 测试** (4-6h) — 为关键节点补充行为测试
5. **ROI padding 审查** (2-3h) — 检查并修复空间节点 ROI padding
6. **GPU shader 审查** (2-3h) — 确认新注册节点的 GPU 端支持
7. **文档更新** (2-3h) — 同步文档与实现
8. **UI 完善** (后续) — color picker、placeholder 标识等
