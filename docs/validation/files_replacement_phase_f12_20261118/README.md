# F 阶段第十二批（F12）：安装目标抵抗并发路径替换 + 可信 root 证明

日期：2026-11-18 · 分支：fix-ui · 范围：`installer.py` 的 fd 锚定层与新
TOCTOU 测试。PR-01 已有"检查→创建→复查"，但关键变更（rename 就位/
指针切换/provenance 写入）仍是**路径版**——核验与使用之间的路径分量可
被并发替换（"不能只做 Path 检查后再使用"）。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `installer.py` | ① **`_DirAnchor`**：`O_NOFOLLOW\|O_DIRECTORY` 打开的 fd 锚——子项操作（mkdir/rename_into/symlink/replace_within/unlink/open(O_EXCL\|O_NOFOLLOW)/lstat）**相对 fd 解析**，检查后的使用之间没有可被替换的路径分量。② **插件目录核验后立即锚定**：versions 的创建/复核经锚 fd（并发换成符号链接 → `is_real_dir` 拒绝或锚定 open 失败 → `PATH_NOT_OWNED`）；版本目录 rename 就位、provenance 写入、`.current.tmp` 创建与原子切换全部锚定。③ **`verify_trusted_root(root, boundary)`**：从边界向下逐级 `lstat` 证明 root 及全部祖先是真实目录（**词法路径**——`resolve()` 会吞链接，证明即失效）；链上任何一级为符号链接/非目录/root 越界 → `PATH_NOT_OWNED`。④ 平台不支持 `dir_fd` 时回退路径版（`_HAS_DIRFD` 如实判定，不冒充已锚定）；`replace_within` 对注入式 `os.replace` 替身回退路径版（仍同锚目录内原子）。 |

## TOCTOU 抵抗证明（tests/plugins/test_installer_toctou_f12.py，8 项）

- **versions 换链攻击**：核验插件目录后、使用 versions 前并发把 versions
  换成指向攻击者目录的符号链接 → 锚定拒绝（`PATH_NOT_OWNED`），**攻击者
  目录零写入**；
- **plugin_root 换链攻击**：同型——立即锚定使 open 失败，攻击者目录
  零写入；
- 可信 root 证明：逐级链返回/链上符号链接拒绝/文件阻塞拒绝/root 越界
  拒绝；
- 锚定后的正常安装/更新端到端仍满足既有契约（current 指针语义、旧版本
  保留回滚）；
- 源侧所有权（包符号链接/安装根在包内）拒绝不受影响。

回归：tests/plugins 全量（installer 55 + lifecycle/provider/sandbox 等）
+ tests/files —— 通过（一次 event_bus 顺序抖动为仓库既有类，隔离绿）。

## 诚实边界

- **Windows 无 `dir_fd`**：回退路径版（创建后复查保留，核验-使用窗口
  仍在）——F13 安装锁批次给 Windows 等价实现与如实标注。
- 锚定覆盖安装关键变更面；`_read_pointer` 等读路径仍路径版（strict 校验
  已拒绝越界链接，读侧风险是可见性不是写入逃逸）。
- 攻击模拟为确定性注入（ensure 钩子换链）；真实并发竞态的模糊测试未做。

## 隐私

合成包/临时目录；无隐私路径。
