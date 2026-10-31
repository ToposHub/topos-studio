# Topos Color - Agent 使用指南

本文档定义 Topos Color 项目中 Agent 的使用规范和最佳实践。

---

## 🎯 核心原则：少即是多

### ✅ 推荐使用的 Agent

| Agent | 类型 | 用途 | 使用频率 |
|-------|------|------|---------|
| **Explore** | 原生自带 | 架构调研、影响分析、代码搜索 | ⭐⭐⭐⭐⭐ |
| **general-purpose** | 原生自带 | 代码生成、问题调研、文档编写 | ⭐⭐⭐⭐ |
| **Review** | Skill | 代码审查、架构约束检查 | ⭐⭐⭐⭐ |

### ❌ 绝对不要安装的 Agent

| 类型 | 原因 | 典型症状 |
|-----|------|---------|
| 万能通用 Agent | 与 Explore 冲突，任务调度混乱 | 速度变慢、幻觉暴增 |
| 多功能混合 Agent | 与原生多 Agent 系统打架 | 工具调用失败、卡死 |
| 老旧第三方 Agent | 兼容性差 | 直接卡死、解析失败 |
| 小工具类 Agent | Claude 原生已内置 | 冗余消耗资源 |

---

## 📋 Agent 调用规范

### 前置条件

在调用任何 Agent 之前，必须：

1. **明确任务范围**：清楚定义要解决的问题
2. **提供充分上下文**：说明项目技术栈（PyQt6 + ModernGL + GLSL）
3. **引用相关文件**：列出涉及的文件路径（引用前先确认路径真实存在，
   渲染核心位于 `src/features/color/core/`，GPU 后端位于 `src/shared/gpu/`，
   而非 `src/core/`）
4. **提醒约束条件**：要求遵循 `.clinerules` 规范

### 工作流

```
开发新功能/修改核心模块：

1. Explore Agent（调研架构）
   ↓
   输出：架构图、数据流、影响范围

2. general-purpose Agent（设计方案）
   ↓
   输出：实现方案、代码草稿

3. 编码实现（遵循 .clinerules）
   ↓
   输出：代码变更

4. Review Agent（审查变更）
   ↓
   输出：问题清单、修复建议

5. 修复问题 → 再次 Review → 测试验证
```

---

## 🔍 Explore Agent 使用指南

### 何时使用

- 理解现有代码架构
- 分析修改的影响范围
- 定位问题根源
- 查找所有引用/调用点

### Prompt 模板

#### 架构分析
```
使用 Explore agent（thoroughness: very thorough）分析 Topos Color 的渲染管线。

任务：
从 src/features/color/core/image_engine.py → src/features/color/core/async_renderer.py → src/features/color/core/shader_compiler.py
梳理完整的数据流和参数传递路径。

输出：
1. 模块职责和接口
2. 数据流图（文本描述）
3. 关键函数调用链
4. 线程安全边界
```

#### 影响范围分析
```
使用 Explore agent（thoroughness: medium）分析 [修改目标] 的影响范围。

任务：
1. 找出所有引用 [函数/类/变量] 的文件
2. 识别直接和间接受影响的模块
3. 评估修改风险
4. 列出需要更新的相关代码
```

#### 问题定位
```
使用 Explore agent（thoroughness: medium）定位 [问题描述] 的根源。

症状：
[描述看到的现象]

任务：
1. 分析相关模块的实现
2. 检查数据流是否正确
3. 识别潜在的资源泄漏
4. 验证线程安全假设
```

### 注意事项

1. **指定 thoroughness 级别**：
   - `quick`：简单搜索（如查找文件）
   - `medium`：中等深度（如影响分析）
   - `very thorough`：全面分析（如架构理解）

2. **提供明确目标**：不要说"看看这个模块"，而要说"分析这个模块的输入/输出/依赖"

3. **分层次探索**：复杂系统先高层后细节，避免一次性给太多内容

---

## 💻 general-purpose Agent 使用指南

### 何时使用

- 生成新代码（插件、面板、工具函数）
- 调研技术方案
- 编写文档和注释
- 性能优化建议

### Prompt 模板

#### GLSL 插件开发
```
在 Topos Color 项目中开发一个 [功能] 插件。

项目背景：
- PyQt6 + ModernGL 图像/视频编辑器
- 动态 GLSL 着色器管线
- HDR 内部处理（float16/32）

技术要求：
1. 符合 .tglsl 规范（见 PLUGIN_DEV_GUIDE.md）
2. GLSL 严格类型（所有数字带 .0）
3. for 循环边界必须是常量
4. 使用 @param 定义 UI 参数
5. 性能优化（避免昂贵运算）

插件功能：[详细说明]
参数设计：[列出需要的参数]

输出：完整的 .tglsl 文件内容
```

#### UI 面板开发
```
在 Topos Color 项目中创建 [功能] 面板。

架构约束（严格遵循）：
1. 参考现有面板模式（面板按域分布：src/app/ui/ 应用级面板、src/features/<域>/ui/ 各域面板）
2. 信号防递归（使用状态锁）
3. 解耦渲染（通过 params_changed 信号）
4. 强类型（所有函数有类型注解）
5. 国际化支持（使用 i18n 工具）

功能需求：
[详细说明面板功能和交互逻辑]

参考文件：
- src/features/<对应域>/ui/[类似面板].py 或 src/app/ui/[类似面板].py（架构参考）
- src/shared/ui/widgets/（自定义控件）

输出：完整的 Python 文件内容
```

#### 性能优化
```
分析并优化 Topos Color 的 [模块/功能] 性能。

项目背景：
- 实时 4K 图像处理（60FPS 目标）
- 每像素执行 800 万次+操作
- GPU 渲染管线（AsyncRenderer）

当前问题：
[描述性能瓶颈]

优化方向：
1. 算法复杂度分析
2. 缓存策略优化
3. 减少不必要的计算
4. 并行化机会
5. 内存访问优化

输出：
1. 性能瓶颈分析
2. 优化方案（按优先级排序）
3. 预期收益估算
4. 实现建议
```

### 注意事项

1. **强调约束条件**：每次 prompt 都提醒 `.clinerules` 中的关键约束

2. **提供示例**：指出参考文件，如"参考 plugins/Vignette_Shapes.tglsl 的结构"

3. **分步骤任务**：复杂任务拆分为多个 prompt，每步确认后再继续

4. **自检要求**：要求 agent 在生成代码后自动检查关键问题

---

## 🔍 Review Agent 使用指南

### 何时使用

- 修改核心渲染模块后
- 提交 PR 前
- 发现可疑行为时
- 定期代码健康检查

### 调用方式

#### 本地文件审查
```bash
/review src/features/color/core/async_renderer.py
/review src/features/color/core/shader_compiler.py
/review src/app/main_window.py
```

#### PR 审查
```bash
/review 123                    # PR 编号
/review 123 --comment          # 提交 PR 内联评论
```

### 审查重点（Topos Color 特定）

Review Agent 会自动检查以下问题，但你应该了解其含义：

#### Critical（必须修复）

1. **GLSL 类型错误**
   - 数字缺少 `.0`（如 `2` 应为 `2.0`）
   - 循环边界非常量
   - 隐式类型转换

2. **线程安全问题**
   - UI 线程调用 OpenGL API
   - 缺少信号防递归锁
   - 数据竞争

3. **资源泄漏**
   - ModernGL 对象未释放
   - 纹理/缓冲区未释放
   - 内存泄漏

4. **构建/测试失败**
   - 编译错误
   - 测试不通过

#### Suggestion（建议修复）

1. **代码质量**
   - 缺少类型注解
   - 命名不一致
   - 代码重复

2. **性能优化**
   - 不必要的计算
   - 缺少缓存机会
   - 低效算法

#### Nice to have（可选）

1. **风格改进**
   - 注释优化
   - 代码组织
   - 小重构

### 审查工作流

```
1. 运行审查
   ↓
   /review <file>

2. 查看结果
   ↓
   按严重性分组的问题清单

3. 修复 Critical 和 Suggestion
   ↓
   使用 edit 工具或手动修改

4. 再次审查（如需要）
   ↓
   /review <file>

5. 提交/合并
```

---

## 🚫 常见错误与避免方法

### 错误 1：让 Agent 直接修改核心代码

❌ **错误做法**：
```
"帮我修改 async_renderer.py 的性能优化"
（直接执行，没有先分析和审查）
```

✅ **正确做法**：
```
1. Explore agent 分析当前实现
2. general-purpose agent 提出优化方案
3. 人工审核方案
4. 编码实现
5. Review agent 审查变更
```

### 错误 2：不提供足够上下文

❌ **错误做法**：
```
"这个函数有问题吗？"
（没有说明函数应该在什么上下文中工作）
```

✅ **正确做法**：
```
"这个函数在 Topos Color 的渲染管线中负责 [职责]。
它应该 [预期行为]，但现在观察到 [实际问题]。
请分析问题根源。"
```

### 错误 3：一次性给太多任务

❌ **错误做法**：
```
"帮我重构整个渲染系统、优化性能、添加测试、更新文档"
```

✅ **正确做法**：
```
分 4 个独立任务：
1. 重构渲染系统（先完成）
2. 优化性能（等 1 完成）
3. 添加测试（等 1-2 完成）
4. 更新文档（等 1-3 完成）
```

### 错误 4：忽略 Review 结果

❌ **错误做法**：
```
运行 /review 后直接忽略所有建议
```

✅ **正确做法**：
```
1. 阅读所有 Critical 问题
2. 逐一修复
3. 评估 Suggestion（选择性采纳）
4. 忽略 Nice to have（如不需要）
```

---

## 📊 任务分配决策树

```
需要做什么？
│
├─ 理解现有代码？
│  └─ YES → 使用 Explore agent
│
├─ 生成新代码？
│  └─ YES → 使用 general-purpose agent
│
├─ 审查已有代码？
│  └─ YES → 使用 Review skill (/review)
│
├─ 调研技术方案？
│  └─ YES → 使用 general-purpose agent
│
├─ 查找引用/调用者？
│  └─ YES → 使用 Explore agent
│
└─ 编写文档？
   └─ YES → 使用 general-purpose agent
```

---

## 🎓 进阶技巧

### 技巧 1：并行 Agent 调用

当任务独立时，可以同时调用多个 agent：

```
例如：同时分析 3 个模块的架构
- Agent 1：分析 src/features/color/core/async_renderer.py
- Agent 2：分析 src/features/color/core/shader_compiler.py
- Agent 3：分析 src/shared/gpu/gl_backend.py
```

### 技巧 2：利用 .clinerules

`.clinerules` 文件会自动被 Agent 加载，所以你不需要每次都重复约束条件。但第一次给新 agent 的 prompt 中可以简要提醒关键约束。

### 技巧 3：保存成功的 Prompt

当你找到一个高效的 prompt 模板时，添加到 `PROMPTS.md` 中，建立个人的 prompt 库。

### 技巧 4：使用绝对路径

引用文件时始终使用绝对路径，避免歧义：

❌ `src/core/async_renderer.py`（此路径不存在，渲染核心在 src/features/color/core/）
✅ `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/color/core/async_renderer.py`

（注：相对路径也支持，但绝对路径更明确；**引用任何文件前先 `ls` 确认路径存在**，
尤其是渲染/GPU 模块——它们不在 src/core/ 下，而在 src/features/color/core/ 与 src/shared/gpu/。）

---

## 📝 版本历史

- 2026-04-22：初始版本，定义 Agent 使用规范和最佳实践
- 2026-10-28：路径修正——`src/core/` 下并无渲染模块，async_renderer/
  shader_compiler/image_engine 实际在 `src/features/color/core/`，
  gl_backend 在 `src/shared/gpu/`，面板在 `src/app/ui/` 与
  `src/features/*/ui/`，自定义控件在 `src/shared/ui/widgets/`；
  插件示例更正为 `plugins/Vignette_Shapes.tglsl`（P0 质量还债批次）
