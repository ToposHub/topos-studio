# 音频效果器后端实施交付记录

2026-11-21 计划（`docs/plans/2026-11-21-audio-effect-panels-backend-handoff.md`）
逐项交付记录。分支 `bugfix`；每项一个提交批次；测试音频与夹具均以
固定 seed 合成生成（计划 A 阶段口径），实机项标注待办。

## 已完成项

| 记录项 | FX-11 卷积混响（P0） |
| --- | --- |
| 提交 | 1414562c |
| 实例与作用域 | `convolution_reverb`；Clip/Track/Bus/Master 经 `ConvolutionReverbAdapter`；任意声道布局 |
| 工程兼容 | 参数键不变（ir_path/wet/dry/true_stereo/enabled）；加载键含内容指纹（mtime+size），同路径替换重验哈希；清空/确认失败卸载 kernel（is_active=False、尾音 0） |
| 核心算法 | 头分区滑动 overlap-save + 尾部 UPC 频域延迟线；与直接卷积差 ≤1.5e-6；分块 1/127/513/1024/2048/4001 全部位精确；`latency == 实测 onset == 0`（旧报 2048 与实测不符——修复） |
| 资源与生命周期 | 4 路 WAV 显式 LL/LR/RL/RR（第一字母=输出）；2 路对角退化策略；失败键不逐块重试；模块级 IR 缓存（容量 8） |
| 时间与性能 | 2 s IR 每块 0.29 ms（预算 10.7 ms 的 37 倍余量）；FFT 恒 2P 不随 IR 膨胀 |
| 面板能力 | 本批未动 UI；worker 装载/取消（计划 P1 项）待后续批次 |

| 记录项 | FX-23 真峰限制器（P0） |
| --- | --- |
| 提交 | 2c0e1b1 系列（true peak limiter 重写） |
| 实例与作用域 | `true_peak_limiter`；遥测新增 output_peak_dbfs |
| 核心算法 | Link = 逐声道 4× 过采样峰值取上界（旧 mono 平均反相抵消——修复）；独立模式每声道 release 状态数组（旧折叠最小值——修复）；前视 = 原始目标流 O(N) 后向最小窗 + L+D 输出延迟；分块与整段**逐位一致**（流式 FIR + 2D overlap-save 历史） |
| 延迟合同 | `latency = lookahead + D(=10)`——计划明示允许更新；上限合规经 `analyze_loudness` 独立复核 |
| 交付 | `delivery_dither` 模块：TPDF@1 LSB（2^-(depth-1)）、half-away-from-zero 舍入、饱和 [-1, 1-LSB]、固定 seed、静音空白策略、浮点路径关闭；接入 stem WAV 写入器（默认关闭） |
| 待办 | LUFS Match 异步分析（UI 层）；噪声塑形 dither（须先校验滤波状态） |

| 记录项 | FX-18 合唱/镶边/移相（P1） |
| --- | --- |
| 提交 | 5edcfdeb |
| 核心算法 | 读头 = 写游标 − 期望延时（旧首跳 2160 → 240 ✓ ±1）；每声道环形缓冲；反馈符号保留且跨块持久（移相 501 切块差 0.154 → 0 ✓）；级数放开 1–12 |
| 尾音 | −60 dB 目标修正（旧 −6 与注释不一致）；fb=0.6 末次回声在预算内 ≤−60 dB |
| 兼容 | 旧正弦/旧参数路径行为等价（分块位精确）；梳状凹口 1/(2D) 间距 1/D 验证 |

| 记录项 | FX-14 饱和（P1） |
| --- | --- |
| 提交 | 2609ac1f 之前（saturation multiband） |
| 版本化 | `model_profile` legacy（缺键，逐样本位精确，drive 0/20/80 验证）\| multiband；未知字段保留 |
| 新模型 | 三段减法互补分频（重构 ≤5e-3 带限）+ tape/tube/transistor（斜率归一、偶次、有界）+ DC 阻断 + RBJ Tone + 2×/4× 流式过采样（声明延迟 20） |
| 抗混叠 | 10 kHz 三次谐波折叠降低 57 dB（≥20 门槛）；分块 1×/2×/4× 位精确 |

| 记录项 | FX-04 多段压缩（P1） |
| --- | --- |
| 提交 | ad02ed73 |
| 合同 | 段数 2/3/4（crossovers 严格升序，非法报错）；通道自适应 SOS 状态（旧写死 2ch）；旋钮提交复用包络（旧每块重建）；`makeup_db→makeup_gain` 别名集中参数入口；mute 归零、disabled 参与重构 |
| 重构 | 1/2/6 ch × 2/3/4 段全平段重构 ≤1e-6 |

| 记录项 | FX-02 动态均衡（P1） |
| --- | --- |
| 提交 | dynamic eq multiband |
| 版本化 | `bands[]`（空 = v0–v4 冻结单段）；稳定段 ID = 序号 |
| 新支路 | 逐声道功率聚合检源（反相不抵消）；每段独立 detector/包络/输出滤波；增益网格按绝对样本时钟 + 格首样本值 + 格增益跨块携带 |
| 分块 | 1/97/513/1024/4001 全部位精确（修复三处分块依赖：RMS zi 形状元数、格续块增益、attack/release 运行态判定） |
| 待办 | 外检源（依赖 FX-03 侧链路由）；近线/线性相位模式 |

| 记录项 | FX-13 M/S 成像（P1） |
| --- | --- |
| 提交 | stereo-imager ms |
| 新字段 | mid/side/output_gain_db + mono_maker（hz + 6/12 斜率枚举，校验拒绝其它） |
| 兼容 | 新字段全中性 → 旧快速路径位精确（手算 M/S+linear pan 对拍） |
| Mono Maker | 低频 Side −49 dB（低频 mono）、高频保留；分块位精确；seek 复位 |

| 记录项 | FX-17 瞬态塑形（P1） |
| --- | --- |
| 提交 | transient pct |
| 映射 | `db = 0.12 × pct`（±100% → ±12 dB；计划推荐曲线）；pct 键存在 → 新支路 |
| 新支路 | 逐声道快/慢包络 + 各声道绝对包络取最大（反相不抵消——旧声道平均语义冻结） |
| 分块 | 位精确；mix 0 直通；legacy dB 路径与旧实现逐样本一致 |

| 记录项 | FX-19 颤音/自动声像（P1） |
| --- | --- |
| 提交 | tremolo waveforms |
| 波形 | triangle/square/saw_up/saw_down/random（seed 确定性 S&H，按绝对周期索引）；未知回退正弦；空/"sine" → 冻结路径 |
| 相位 | 绝对样本公式 `base + (s−pos)·rate/fs`——任意分块位精确；rate 变化点重置基准（相位连续、切块不变） |
| 保留 | auto_pan depth 0 → √0.5 中心增益（旧语义）；square/saw 平滑与占空比标注后续批次 |

| 记录项 | FX-05 总线压缩（P1） |
| --- | --- |
| 提交 | 5753f996 |
| 合同 | 连续字段（threshold/ratio/attack/release/knee/mix/makeup_gain）显式覆盖模板；模板 `makeup_db→makeup_gain` 别名修复（旧静默失效）；包络状态跨参数提交保留 |
| 传递曲线 | 硬膝 GR = over·(1−1/ratio) ±0.2 dB（12 kHz 纹波最小夹具）；SC HPF 只作用检测支路（输出低频保留） |

| 记录项 | FX-06 齿音消除（P1） |
| --- | --- |
| 提交 | de-esser v2 |
| 新支路 | `detector_shape`（bandpass/high_shelf）+ `lookahead_ms`（0/2/5 → 0/96/240 样本，脉冲 onset == 声明）+ `output_gain_db` |
| 检测 | 逐声道独立检测带 + 能量联动（反相不抵消）；Split 带外仅受输出增益影响（−2.95 dB vs −3 设定）；Wide 两段同降 |
| 兼容 | legacy 参数组合走旧路径（自身分块一致性冻结验证） |

| 记录项 | FX-07 去点击/去嗡声（P1） |
| --- | --- |
| 提交 | 2047771b |
| Hum | `reduction_db`（0..48）衰减受限：目标频点保留 10^(−red/20)（20 dB cap 实测保留 0.108）；50 Hz 降幅 36 dB、220 Hz 损失 0.32 dB；`region` 半开区间写回（选区外逐样本一致） |
| Click | MAD 信号尺度下限（数字净音阈值塌缩修复）；6/6 命中 ≤2 样本、修复误差 <0.05；贴边窗合法；鼓瞬态 veto 验证 |
| 待办 | 片段命令接线（`audio_edit.PROCESSORS` 注册、差分试听）——计划第 4 步 |

| 记录项 | FX-10 算法混响分析（P2） |
| --- | --- |
| 提交 | reverb analysis |
| 函数 | `measure_rt60`（Schroeder 逆积分 −5/−35 拟合，理想 IR 精确）；`measure_t30_band_limited`（RV-03 口径）；`analyze_reverb_snapshot`（全新实例、立体声脉冲、生产状态不参与） |
| 精度 | 目标 0.5/1/2/4 s 偏差 7.0/1.1/1.7/2.6%（≤10%）；干信号明确报原因 |

| 记录项 | FX-12 空间声像（P2） |
| --- | --- |
| 提交 | e5c0f550 + 2609ac1f（布局修复） |
| 函数 | 5.1/7.1 扬声器方位角表（ITU BS.775：C=0/L·R=±30/Ls·Rs=±110；LFE 不投影）+ VBAP 扇区对增益（平方和 1、±180 回绕、扇区外报错）；立体声 2×2 基线不动 |
| 布局修复 | `with_bus_layout` 限制 MX-15 允许集（stereo/5.1/7.1——quad/高度布局显式拒绝）；存量 QUAD 支持与过时断言和解（quad 导出 → 4 声道；4ch 交付走标准下混） |

## 全量验证

- `tests/edit/` 全目录：**14351 passed / 15 failed / 43 skipped**（41 分钟）。
- 15 个失败全部为 `test_timeline_pointer_and_chrome`（UI hover 指针）——
  **单独运行该文件 27/27 全过**：长跑套件中的测试隔离伪影（存量），
  与音频 DSP 改动无关。
- 每批次附带按计划验收标准编写的新测试（累计新增 10 个测试文件、
  ~110 个用例）。

## 实机（E 阶段）待办

以下条目需要真机监听验证（macOS/Windows 设备、块长、采样率记录）：
FX-11 卷积混响 IR 试听、FX-23 限制器 A/B 与 Dither 交付对拍、
FX-18 调制扫频听感、FX-14 三段饱和等响 A/B、FX-06 齿音对白实测、
FX-13 Mono Maker 低频监听。合成夹具验收已全部通过。
