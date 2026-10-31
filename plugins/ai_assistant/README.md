# AI Assistant 模式插件目录

这里是 AI Assistant 的模式插件目录。每个一级子文件夹都会在应用启动时被扫描，并在打开模式菜单时自动刷新。

最小插件结构：

```text
my_mode/
├── plugin.json
└── system_prompt.md
```

最小 `plugin.json`：

```json
{
  "api_version": "1",
  "kind": "assistant_mode",
  "id": "my_mode",
  "version": "1.0.0",
  "label": "我的模式",
  "description": "这个模式做什么",
  "handler": "generic_chat",
  "capabilities": ["language"],
  "prompts": {"system": "system_prompt.md"},
  "ui": {"order": 60, "label": "我的模式"}
}
```

规则：

- `id` 必须与文件夹名称一致，只能使用小写字母、数字、`_` 和 `-`。
- `handler: generic_chat` 必须声明 `language` 能力，并通过宿主现有聊天、运行状态和权限流程执行。
- 提示词文件是用户可编辑的 Markdown；提示词不能自行授予文件、Shell、网络或时间线写入权限。
- 目前内置处理器包括 `builtin:teaching_animation` 和 `builtin:media_generation`；自定义插件不要伪造这两个处理器。
- 新增或删除文件夹后，重新打开模式菜单即可刷新；重启应用同样生效。
- 非法 JSON、路径越界、未知 API 版本和不合法的插件声明会被拒绝，并保留诊断信息。

Motion Graphic Clip 的默认提示词在 `motion_graphic/` 中：

- `html_build_prompt.md`：网页设计模型提示词
- `creative_brief_prompt.md`：创意计划提示词
- `proposal_prompt.md`：多方案提案提示词
- `legacy_scene_prompt.md`：旧 JSON 场景路径提示词

插件扫描器不会执行此目录里的 Python 文件。权限、审批、时间线事务、渲染和凭据仍由 Topos 宿主内核掌管。
