# 出厂默认布局（Factory Default Layout）

## 背景

历史上 "Reset to Default" 的恢复来源是运行时可写的 DB 记录（`layouts` 表 `is_default=1`）
与 `.topos/default_layout_seed.json` 种子文件。它们都能被用户的 "Set as Default"
覆盖——一旦保存了坏布局，默认布局就被永久污染，用户再也回不到干净的默认状态。

## 机制（2026-08 起）

默认布局的唯一来源是**随仓库提交**的 `default_layout.json`（项目根目录）：

```
{
  "schema": 1,
  "modes": {
    "video": { "geometry": "<hex>", "state": "<hex>", "panel_states": { "<页面>": "<hex>" } },
    "image": { ... }
  }
}
```

要点：

1. **按工作模式分桶**：视频模式与图片模式布局不同，各存一份。模式无自己的捕获时
   回退 `all` 桶，再回退旧版扁平结构；都没有则 `Reset to Default` 走代码内
   `apply_default_layout()` 硬编码兜底。
2. **只能通过提交变更**：该文件是受版本控制的源码文件。运行时没有任何用户操作能
   改写它（旧 "Set as Default" 菜单已移除），拖乱布局不影响默认布局本身。
3. **首次启动**：无任何已保存布局时，用出厂文件播种 `page_layouts`，新用户看到的
   就是调优后的默认布局。
4. **打包构建**：文件随可执行文件分发（`sys.executable` 同目录）。

## 工作区面板

顶部工具栏以居中的橙色标签切换工作区。应用菜单在左侧，当前页的面板入口按功能放在两侧，用灰白图标和文字显示开启状态。停靠标签组居中显示。混音面板停靠时省去额外的名称和关闭横条，可从顶部快捷入口或面板菜单收起和召回。

切页时在内存中立即保存停靠面板的显隐与尺寸，短间隔内的布局变更合并写盘。关闭一组面板时，另外记录打开的标签及前台标签，召回时恢复。

Studio 保存时分开写入 `studio_image` 与 `studio_video` 布局。切换工作模式的设置从下次启动生效，当前窗口始终保存到它实际运行的模式。缺失的页面从对应出厂布局或代码默认布局补齐。交付渲染队列由交付页内部管理，重启后恢复内嵌状态、显隐和宽度。

混音页左上是混音器，以及共享标签组的媒体池与音频效果，右上是视频监看。下方时间线顶部用一行摆放播放、录音、编辑工具和立体声主输出表，时间码位于标尺左侧。音频时间线与视频监看共用同一工程的命令历史和播放控制。升级时，旧默认摆位中的媒体池与音频效果会移到混音器旁，旧主输出停靠区由时间线填满。可在混音页使用“窗口 → 重置为默认”应用出厂摆位。

## 维护流程（更新出厂默认布局）

"设为默认"入口只在**开发者模式**下出现，开发者模式需在偏好设置中开启并重启生效：

1. 偏好设置（Topos Color > Preferences）→ Advanced → Developer → 勾选
   "启用开发者模式" → Save → 按提示重启应用。
2. 把界面调整成想要的默认布局，然后 Window > Set as Default
   （写入当前工作模式的分桶，其它模式的捕获保留）。
3. 视频与图片模式各设置一次（在偏好设置里切换工作模式并重启后再次设置）。
4. 审查 diff 并提交：

```bash
git diff default_layout.json
git add default_layout.json && git commit -m "chore: update factory default layout"
```

关闭开发者模式可再次隐藏该入口，普通用户始终无法改动默认布局。

测试：`tests/ui/test_factory_default_layout.py` 守护已提交文件的结构合法性。

相关代码：`src/app/controllers/dock_layout_controller.py`
（`_load_factory_layout` / `reset_layout_to_default` / `set_current_layout_as_default`）。
