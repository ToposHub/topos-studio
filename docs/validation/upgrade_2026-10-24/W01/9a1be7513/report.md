# W01 · Windows codec 重建与红基线清理报告（机器 B · 2026-10-24）

> 责任包：W01（`docs/upgrade_plan_W_windows_2026-10-24.md` §3）。
> base `9a1be7513`（W00 提交后）；构建+修复全在本机完成。

## 1. DLL 重建（W-I01 主项）

- 工具链（venv 内，无系统改动）：cmake 4.4.3（`python -m cmake`）、
  ninja 1.13.2、zig 0.16.0（PyPI ziglang 包内 zig.exe）。
- 构建方式：`zig cc`/`zig ar` 传递包装器（`build/zig-cc.cmd`、`zig-ar.cmd`）
  + `CMAKE_C_COMPILER_TARGET=x86_64-windows-gnu` + Ninja + Release，
  产物目录 `build/win-release-zig9/`，250/250 编译零告警失败。
- 产物：`topos_codec.dll` 801280 B，
  SHA256 `95d08b8915dec4e7a687e050391996e985672d72712c5826462301ca778e55bd`。
  ABI 校验（含 `tc_mux_add_audio`）通过；`resolve_topos_codec_library()=ok`。

### 1.1 构建基建修复（CMakeLists）

zig 识别正则 `^zig(\.exe)?$` 不匹配 `zig-cc.cmd` 包装器 → 误入 GCC/MinGW
分支（`-static` 模式写出 `!<arch>` 归档伪 DLL = **zig4 WinError 193 的
成因**；GCC 专属告警也让 zig 0.16 的 clang 前端直接报错）。放宽为
`^zig[.-]?` 前缀匹配，注释记录 zig4 实案。

## 2. 加载器加固（"只验文件存在"→平台/架构/ABI 三重预检）

`src/shared/codec/topos_binding.py`：

1. 候选目录过滤：Windows 扫描只认 `win-release*`（字母序曾使
   `win-half-tests` 半途构建先于正式构建被选中）；
2. PE 架构校验：`pe_machine_type()`/`dll_matches_current_arch()` 在 CDLL
   之前读 PE 头 machine 字段对拍解释器架构，伪 DLL/错架构带原因拒绝
   （zig4 实案 → `not_a_pe_image`）；
3. 必选导出门槛：`_REQUIRED_EXPORTS`（12 符号）——可加载但缺符号的
   **陈旧构建不再被悄然选中**（zig7/zig8 实案 → `missing_exports`）；
4. `first_resolved_library()`：测试/工具统一入口（三态解析）。

回归：`tests/media/test_topos_codec_loader_w01.py` 10 用例
（PE 解析/伪 DLL/错架构/目录过滤/门槛跳过，全平台可跑 + 本机实案）。
5 个直接消费 `candidate_library_paths()` 的测试夹具迁移到统一入口。

## 3. 红基线清理结果（旧 C0.1 基线 1023 error + 416 fail）

| 组 | 旧基线 | W01 后 |
|---|---|---|
| G1 逻辑 CI 对齐（13 文件） | 228 绿 | **228 绿** |
| G2 `tests/media` | 192 failed + 945 errors | **3371 passed / 0 failed / 213 skipped** |
| G3 `tests/shared tests/export tests/native` | 23 failed（DLL 修复后首测） | **2589 passed / 0 failed / 130 skipped** |

注：G3 的 23 个失败为 DLL 修复后**首次真正执行**暴露的 Windows 适配债
（非新回归）；本包全部归因并处理（见 §4/§5）。

## 4. 修复的真实产品缺陷（DLL 修复后首跑发现）

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| 1 | `topos_image_binding.write_packet` 的 `os.open` 缺 `O_BINARY` | **数据损坏**：Windows 文本模式把载荷 0x0A 翻译 CRLF，.toos 帧提取实测 650≠647 字节，读回即坏 | 逐 fd 强制 O_BINARY（帧提取 16 失败→0） |
| 2 | `config_manager.load_config` 在读句柄未关时保存 | **Windows 配置迁移保存全程静默失败**（os.replace 恒 WinError 5） | 保存挪出读上下文 |
| 3 | `topos_encoder._staging_owner_alive` 把 WinError 87（已释放 PID）当存活 | Windows 上 journal 恢复永不触发 | 87 显式按死亡 |
| 4 | 序列暂存活性锁（AUD-33401）仅 POSIX flock | Windows 永远回退 PID 判活（AUD 要解决的脆弱路径） | msvcrt.locking 跨平台原语（`_lock_nonblocking`/`_unlock`），35 用例全绿 |
| 5 | `topos_image_binding` 目录 fsync 在 Windows PermissionError 噪声 | 每次写包刷 WARNING+traceback | win32 跳过（NTFS 日志语义），降为 debug |
| 6 | `_dead_pid` 测试助手句柄未释放 | PID 判活测试自身污染判活 | del+gc 释放句柄（测试侧） |

## 5. 测试侧归因与平台适配（非产品缺陷）

- macOS 专属缺平台门 13 例（VT/Metal/CV 桥）→ 精准 `skipif`（类/函数级，
  保留跨平台可跑用例）；
- POSIX 路径断言 5 例（restart 命令/ffmpeg 定位）→ `os.path.abspath` 同源
  期望；
- `os.geteuid` 2 例、chmod 000 读语义 3 例（Windows 只读属性不阻读）→
  getattr 守卫 / 目录占位等价构造；
- NV12 支持位断言把 `D3D11_BIND_SHADER_RESOURCE=0x8` 的 bind 枚举与
  format-support 枚举混用（0x80=TEXTURECUBE）→ 改 TEXTURE2D|SHADER_SAMPLE
  （4090 实测 bits=0xfa82c320）；
- HEVC 10bit 软件拒绝防线在 NVENC 机器解析为 hevc_nvenc 被正确放行 →
  测试固定 `hwaccel="off"`；
- VT 挂载断言（ProRes 预览）Windows 为 CPU planar 设计路径 → 单用例 skip；
- hwaccel 端到端：伪造 `detect_available_decoders` 使 Windows 走到被测
  回退分支；
- vtable 合成测试 proto 缺 `c_void_p` 入参（win64 指针溢出）；
- 异步容器关闭（Q01 恒异步入队）时序断言 → 有界等待；
- `GpuEncodeSurface` 二次提交测试回调返回 falsy 触发 AUD-33208 先行
  release 守卫 → 回调返回真值；
- v8 inspector CLI 候选：字母序选到旧构建 CLI（缺 H1 sRGB tag=13）→
  跟随 `resolve_topos_codec_library()` 同目录，保证 CLI 与 DLL 同代。

## 6. 结构性环境限制（如实登记，未伪造通过）

- **AppLocker/WDAC 拦截新构建未签名 exe**：C 层 ctest 套件（92 项，51
  BAD_COMMAND）与 topos_inspect/toos CLI 直接运行被策略拒绝（WinError
  4551/「策略阻止」）；策略对旧构建（zig8 前产物）放行，故此前未被发
  现。进程内 DLL 加载不受影响。处置：v8 CLI 用例在 CreateProcess 拒绝时
  结构化 skip（带原因）；C ctest 完整执行需签名或策略豁免，登记为待
  环境项。Python 层一致性测试（含 V8 编码解码往返）不受影响。
- pip check：`qwen-tts 0.1.1` vs transformers 4.57.6（开发工具，非核心，
  沿 W00 登记）。

## 7. 交付物清单

- `native/topos_codec/build/win-release-zig9/`（DLL + CLI，未入版本控制，
  hash 见上）
- `native/topos_codec/CMakeLists.txt`（zig 识别修复）
- `src/shared/codec/topos_binding.py`（加载器加固）
- `src/shared/codec/topos_image_binding.py`（O_BINARY + dir fsync）
- `src/shared/export/topos_encoder.py`（判活 87 + msvcrt 锁）
- `src/shared/config/config_manager.py`（迁移保存句柄修复）
- 测试：`tests/media/test_topos_codec_loader_w01.py`（新）+ 14 个测试
  文件的平台适配（详见 §5）
