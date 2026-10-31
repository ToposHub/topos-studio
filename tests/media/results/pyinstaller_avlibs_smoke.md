# PyInstaller av.libs 打包冒烟（W7-03 本机半边）

- 日期：2026-09-12 ｜ PyInstaller（venv 安装）｜ onedir 布局
- 工件：`packaging/pyinstaller/smoke_av.spec` + `smoke_av_main.py`
  （复用 `base_spec._collect_pyav_dlls()` 真实收集逻辑——被验证的正是
  W7-03 已提交的 DLL 收集代码，不是平行实现）。
- 构建：`_collect_pyav_dlls()` 收集 **25 个 av 相关 DLL**（av/*.dll →
  `av`、av.libs/*.dll → `av.libs`），产物布局 `_internal/av.libs/` 与
  delvewheel 重定位约定一致。

## 正向（完整产物）

```
[smoke] step 1: import av
[smoke] av 18.1.0 imported
[smoke] step 2: encode/decode roundtrip
[smoke] roundtrip frames=4
[smoke] PASS        （exit 0）
```

FFmpeg DLL 经 av.libs/ 布局在打包环境真实解析，libx264 编 4 帧 + 回解
4 帧全链路通过。

## 负向（移除 av.libs）

```
[smoke] step 1: import av
[smoke] FAIL import: ImportError: DLL load failed while importing _core: 找不到指定的模块。
（exit 2）
```

缺失时显式诊断 + 非零退出，无静默半可用状态——上游 §9.1「扩展缺失时
程序仍可启动并回退」的 app 级实现可直接复用该入口模式。

## 对 W7-03 的状态更新

- ✅ 本机半边完成：DLL 收集 → 真实构建 → 产物运行 → 正/负两向冒烟。
- ⬜ 留白：signtool 签名与安装器实测（需代码签名证书，采购后补）。
