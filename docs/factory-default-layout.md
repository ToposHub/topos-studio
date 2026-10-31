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
