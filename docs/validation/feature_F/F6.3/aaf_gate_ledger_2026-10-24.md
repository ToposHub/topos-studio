# F6.3 · AAF 声音后期交接 — 条件延期台账

- 日期：2026-10-24；计划条目：§F6.3
- 测试：tests/edit/test_interchange.py::TestAafConditionalGate（2）＋套件 45 全绿

## 1. 延期判定（计划原文的验收门）

计划验收："**真实接收软件导入能重建剪辑和声道/时间关系**……接收端未
验收保持未完成"。两个门均未开：
1. **依赖门**：aaf2 不在工程依赖清单（新增第三方依赖属显式决策，
   本仓不擅自引入）；自研 AAF 二进制格式不可行。
2. **接收端门**：Pro Tools/Media Composer 不在本仓环境——"真实接收
   软件导入"验收无法执行。

## 2. 交付（延期不等于空白）

| 组件 | 内容 |
|---|---|
| 可执行交接建议 | interchange AAF 条目升级：当前音频后期交接 = **Stems（F3.4 routing.json 分轨重建声明）+ CSV Cue Sheet**——明确"不以 Stem/CSV 冒充可重建时间线"（计划原文边界）；需 AAF 时间线重建时经 FCPXML（DaVinci Resolve 可转出 AAF）或外部工具 |
| 不冒充锁定 | 测试断言：AAF 不在 available_formats；notes 必含 Stems/routing.json/FCPXML 路径与边界声明 |
| OMF | 维持既有明确不可用声明（计划原文"OMF 不自动扩项"） |

## 3. 复开条件

1. 用户批准引入 aaf2（BSD-3）→ 实现 adapter（轨道/源时码/声道/
   handles/位置采样时间/声明音量自动化；复杂总线/插件/retime 烘焙
   或报告）。
2. 真实接收软件样本工程回传 → 接收端验收。
