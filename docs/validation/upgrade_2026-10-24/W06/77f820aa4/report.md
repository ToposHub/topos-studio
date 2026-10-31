# W06 · 可选 CUDA 安装组合、冻结包与能力诊断 —— 验证报告

- 基线提交：77f820aa4（W05 收口）
- 机器：B（Windows 11 Pro 26200 / RTX 4090 / Python 3.13.12）
- 对应计划：W06（W-I06），硬依赖 W00 ✅

## 交付内容

### 1. `cuda-windows` extra 声明（pyproject.toml）

- `[project.optional-dependencies].cuda-windows = ["cuda-bindings>=13.0.0,<14"]`；
- **核心 dependencies 不含 CUDA**（单元测试断言锁定）——可选能力缺席
  必须可安装、可运行；
- 实测矩阵记录在案：dev venv av 18.1.0（FFmpeg 8.1.2）× cuda-bindings
  13.4.2（driver 13050）；干净 venv av 19.0.2 × cuda-bindings 13.4.4。

### 2. 能力诊断 `resident_capability_report()`（cuda_resident.py）

分层探测链（JSON 可序列化，安装包/打包记录用）：平台门 → nvcuda.dll →
cuda-bindings（含版本）→ opt-in 开关 → 硬解 kill-switch → 全局门闩 →
设备身份（ordinal/名称/驱动版本，cuInit 后轻量枚举，不建上下文）。
每层 ok/reason 如实报告——"实际后端/缺依赖原因"可见，不冒充。真机输出：

```json
{"nvcuda_ok": true, "cuda_bindings_ok": true,
 "cuda_bindings_version": "13.4.2", "route_available": true,
 "device": {"count": 1,
   "items": [{"ordinal": 0, "name": "NVIDIA GeForce RTX 4090"}],
   "driver_version": 13050}}
```

### 3. 打包收集 `_collect_cuda_bindings()`（base_spec.py）

- 构建机装了 extra：显式登记 `cuda.bindings.driver/runtime` 延迟导入
  （函数内 import，静态分析可能漏）+ 收集 wheel 全部本地扩展
  （`cuda/**/*.pyd|.so` 按 `cuda/` 原层级放置）；
- 未装 extra：什么都不打包，冻结产物探针如实报缺、驻留路径
  fail-closed 回退（可选缺席不阻塞构建）；
- `nvcuda.dll` 永不打包（目标机 NVIDIA 驱动提供）；
- **PyInstaller 本机缺失 + AppLocker 拦截新 exe（W00/W01 已登记）：
  冻结包实build未验**——收集逻辑经单元测试覆盖（base_spec import
  安全，无需 PyInstaller），补装 PyInstaller 的机器可直接
  `pyinstaller packaging/pyinstaller/topos_color.spec` 验证。

### 4. 干净 venv 两态验收（计划验收项，真机执行 ✅）

`.w06-venv`（miniconda 基础 Python 3.13.12 全新 venv，非开发 venv 克隆）：

| 步骤 | 结果 |
|---|---|
| `pip install -e .`（无 extra） | ✅ 全量核心依赖安装成功（PyQt6 6.11.0 / **av 19.0.2** / numpy 2.5.4 / moderngl 5.13.0 等，**无 torch 间接包**） |
| `pip check`（无 extra） | ✅ 无冲突 |
| 探针 + 下载路径（`--expect-no-cuda`） | ✅ 探针如实报 `cuda_bindings_unavailable:ModuleNotFoundError`；下载 planar 解码自洽校验通过；**无 extra → 下载/CPU fail-closed** |
| `pip install cuda-bindings`（extra） | ✅ cuda-bindings 13.4.4 + cuda-pathfinder 1.8.4 |
| `pip check`（有 extra） | ✅ 无冲突 |
| 驻留路径（`--expect-cuda`） | ✅ probe available；WindowsCudaResidentSource open→read→GL interop→**md5 与同解码器下载参考逐字节一致**；**有 extra → 真实驻留** |

验收脚本固化为 `tools/w06_capability_check.py`（退出码语义供打包/安装
流水线消费；能力报告全文打印归档）。

### 5. 单元测试（tests/media/test_cuda_extras_w06.py，5 绿）

- extra 声明 + 核心依赖无 CUDA 锁定；
- 报告结构完整 + JSON 可序列化 + fail-closed 链（nvcuda 缺席短路 /
  bindings 缺席如实带原因——monkeypatch 注入）；
- 打包收集：cuda 在场 → driver hiddenimport + 二进制按 `cuda/` 层级；
  缺席 → 空。

## 已知限制与如实登记

- **冻结 exe 未验**：本机无 PyInstaller 且 AppLocker 拦新 exe（结构性，
  W01 已登记）——收集逻辑已就绪并有单测，待有 PyInstaller 的机器
  实build补验；这不是"只更新声明"——两态 venv 验收为真机实测。
- av 19.0.2 在干净 venv 实测通过（驻留 md5 对拍一致）；dev 声明矩阵
  以 18.1.0 为准，19.x 作为附加证据记录。
