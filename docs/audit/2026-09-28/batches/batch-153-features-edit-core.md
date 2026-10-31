# 批次报告：batch-153 — src/features/edit/core（audio_dsp.py）

- 批次：batch-153 [P1]
- 编号区间：AUD-15300–AUD-15399
- 审查文件：`src/features/edit/core/audio_dsp.py`（2,505 行，全文通读）
- 交叉核对（只读）：`audio_capabilities.py`、`timeline_v2.py`（参数 dataclass / TrackEffectInstance / to_dsp_entries）、`audio_playback.py`（EffectChain 调用点、声道适配）

---

## src/features/edit/core/audio_dsp.py (行数: 2505)

### 发现

- [AUD-15300] [Major] ReverbEffect.set_params：predelay/采样率变化分支用旧 decay/damping 重建滤波器，新值永不应用到 comb
  - 位置: src/features/edit/core/audio_dsp.py:1569-1580
  - 证据:
    ```python
            predelay_changed = abs(new_predelay - self._pre_delay_ms) > 0.1
            if sr_changed or predelay_changed:
                self._pre_delay_ms = new_predelay
                self._rebuild_filters()
            elif abs(new_decay - self._decay) > 1e-6 or abs(new_damping - self._damping_hz) > 1e-6:
                self._decay = new_decay
                self._damping_hz = new_damping
                self._apply_comb_params()

            self._mix = new_mix
            self._decay = new_decay
    ```
  - 影响: `_rebuild_filters()` → `_apply_comb_params()` 读取的是**尚未更新**的 `self._decay`/`self._damping_hz`（旧值），随后属性才在第 1580-1582 行被覆盖为新值，但 `elif` 分支（唯一调用 `_apply_comb_params` 应用新值的地方）已被跳过。后续同值 set_params 时差值判据均为 0，也不再重算。典型触发路径：工程加载/预设应用/undo 恢复时一次性下发全量参数（新实例默认 pre_delay=0.0ms、decay=2.0s、damping=5000Hz；只要保存值同时改了 predelay 与 decay/damping）——comb 的反馈系数与阻尼停留在默认值，实际混响 RT60/高频衰减与用户设置不符，且会无限期保持错误。
  - 建议: 把 `self._decay = new_decay` / `self._damping_hz = new_damping` 移到 `_rebuild_filters()` 之前（或在 rebuild 分支末尾补一次 `_apply_comb_params()`），使重建总是用本次新值。
  - 置信度: 高

- [AUD-15301] [Major] DelayEffect.set_params：time/采样率变化分支用旧 low_cut_hz 重算 highpass 系数，新 lowcut 值被吞
  - 位置: src/features/edit/core/audio_dsp.py:1771-1783
  - 证据:
    ```python
            time_changed = abs(new_time - self._time_ms) > 0.1
            if sr_changed or time_changed:
                self._time_ms = new_time
                self._rebuild_delay_line(len(self._delay_bufs) if self._delay_bufs else 2)
            elif abs(new_lowcut - self._low_cut_hz) > 1e-6:
                self._low_cut_hz = new_lowcut
                self._recompute_hp_coeff()
            ...
            self._low_cut_hz = new_lowcut
    ```
  - 影响: 与 AUD-15300 同构：`_rebuild_delay_line()` → `_recompute_hp_coeff()` 使用旧 `self._low_cut_hz`；随后属性更新为新值但 elif 已跳过，之后同值调用差值为 0 不再重算。工程加载/预设恢复时只要 time 与 low_cut 同时偏离默认值（300ms / 100Hz），反馈回路低切就停留在旧截止频率，回声音色与参数面板显示不一致。
  - 建议: 先更新 `self._low_cut_hz = new_lowcut` 再判断/重建，或去掉 elif 依赖：任何分支结束后统一按当前属性重算一次 HP 系数。
  - 置信度: 高

- [AUD-15302] [Major] StripEQFilter.set_params：mode 切换的状态保留过滤条件恒为空集，Low-Cut/High-Cut 状态也被误清空
  - 位置: src/features/edit/core/audio_dsp.py:820-823
  - 证据:
    ```python
            if old_mode != new_mode:
                # mode 变化 → 频段身份变化 → 丢弃频段状态（与旧实现一致）
                self._zi_by_key = {k: v for k, v in self._zi_by_key.items()
                                   if k not in ("lc", "hc") and not k.startswith("b")}
    ```
  - 影响: `_zi_by_key` 的键只可能是 `"lc"`、`"hc"`、`"b{i}"`（见 `_recompute_coefficients` 的 keys.append），而条件保留的是"既不是 lc/hc 也不以 b 开头"的键——结果恒为空 dict。注释意图是"丢弃频段状态、保留 lc/hc"（正确写法应为 `if k in ("lc", "hc")`），实际连 Low-Cut/High-Cut 的 IIR 状态也一并清零。播放中切换 EQ 模式（3/7/10/15-band）且启用 Low-Cut 时，滤波器状态跳变会产生可闻的瞬态咔哒声。
  - 建议: 条件改为 `if k in ("lc", "hc")`，并补一条"mode 切换保留 lc/hc 状态"的单测防回归。
  - 置信度: 高

- [AUD-15303] [Major] DelayEffect.process：ping-pong 模式在奇数声道（≥3）时 swap 索引越界，渲染线程 IndexError
  - 位置: src/features/edit/core/audio_dsp.py:1829-1852
  - 证据:
    ```python
            ping_pong = self._ping_pong and channels >= 2
            # ping-pong 声道交换索引：ch -> ch^1（与逐样本 other = ch ^ 1 一致）
            swap = np.arange(channels) ^ 1
            ...
                src = seg[swap] if ping_pong else seg
    ```
  - 影响: `channels=3` 时 `swap = [1,0,3]`，`seg[swap]` 对 shape (3,m) 的数组取下标 3 → IndexError: index 3 out of bounds；任意奇数声道（3/5/7…）同理（末声道 `ch^1` 越界一）。播放/导出线程直接崩溃。当前管线 `mix_samples` 会把块适配到 `output_channels`（通常 2），偶数多声道（4/6）也恰好安全，但该函数按契约接受任意 `(channels, n)`，同文件 `ImagerEffect` 对 >2 声道仅处理前两声道的防御做法在此缺失。
  - 建议: `swap = np.arange(channels) ^ 1; swap = np.minimum(swap, channels - 1)`，或对奇数声道把末声道映射到自身；并加 shape 断言单测。
  - 置信度: 中（代码路径确凿；实际触发依赖奇数声道块到达此效果器，常规立体声管线不触发）

- [AUD-15304] [Major] FX 效果器 dict 参数无范围校验，CompressorEffect ratio=0 时渲染线程 ZeroDivisionError
  - 位置: src/features/edit/core/audio_dsp.py:1193-1199, 1222
  - 证据:
    ```python
            self._threshold_db = float(params.get("threshold_db", _COMP_DEFAULT_THRESHOLD_DB))
            self._ratio = float(params.get("ratio", _COMP_DEFAULT_RATIO))
    ```
    ```python
            gain_db[above] = (input_db[above] - threshold) * (1.0 / ratio - 1.0)
    ```
  - 影响: 各效果器 set_params 直接信任 dict 内容，未按文档声明的范围（ratio ∈ [1,20] 等）钳位/校验。上游 `timeline_v2.TrackEffectInstance.params` 是未校验的裸 dict（`from_dict` 对 params 只做 `dict(...)`），即工程文件反序列化内容可原样进入 DSP。`ratio=0.0` 时 `1.0 / ratio` 抛 ZeroDivisionError，且发生在包络超过 knee 下界后的 `process()` 热路径——播放/导出中途崩溃；负 ratio/NaN 也会产生无声或异常结果。违反 .clinerules"外部数据须验证"。正常 UI 路径经 `CompressorParams.__post_init__` 校验后 `to_dict()`，故仅损坏/手改工程文件触发。
  - 建议: 在各 set_params 入口对数值参数做范围 clamp（或至少 `max(ratio, 1.0)` 类防御），与 `MasterLimiter.threshold.setter` 的钳位风格统一。
  - 置信度: 中（崩溃路径真实，但需上游数据异常触发）

- [AUD-15305] [Suggestion] _biquad_lowshelf 中 alpha 的首次赋值是死代码，易误导维护者
  - 位置: src/features/edit/core/audio_dsp.py:71-74
  - 证据:
    ```python
        # shelf slope factor S=1（标准 12dB/oct）
        alpha = sin_w0 / 2.0 * np.sqrt((A + 1.0 / A) * (1.0 / 1.0 - 1.0) + 2.0)
        # 上式 sqrt 内 (1/S - 1) + 2，S=1 时简化为 sqrt(2)
        alpha = sin_w0 / 2.0 * np.sqrt(2.0)
    ```
  - 影响: 第一行立即被第二行覆盖；`(1.0 / 1.0 - 1.0)` 恒为 0，虽然数值与简化式一致，但保留两行易让读者误以为存在两套公式，也掩盖了"若要支持可调 S 需恢复通式"的事实。
  - 建议: 删除首行，或以注释保留通式 `sqrt((A + 1/A) * (1/S - 1) + 2)` 而非可执行死代码。
  - 置信度: 高

- [AUD-15306] [Suggestion] pan 系列函数返回值为裸 tuple 注解，缺少元素类型
  - 位置: src/features/edit/core/audio_dsp.py:256, 280, 289, 311
  - 证据:
    ```python
    def pan_gains(pan: float) -> tuple:
    ```
    （`linear_pan_gains` / `balance_gains` / `pan_gains_for_channels` 同样为 `-> tuple`）
  - 影响: 文件其余部分已用 `Tuple[np.ndarray, np.ndarray]`（如 `crossfade_envelopes`）等精确注解；这四个函数的裸 `tuple` 使调用方与类型检查器无法得知元素类型与个数。
  - 建议: 改为 `Tuple[float, float]`。
  - 置信度: 高

- [AUD-15307] [Suggestion] StripEQFilter.process 只判 _scipy_available()，未防模块级 sosfilt 为 None，与文件其余处的防护不一致
  - 位置: src/features/edit/core/audio_dsp.py:881-905
  - 证据:
    ```python
            if p is None or not p.enabled or not _scipy_available():
                return samples
            ...
            out, zf = sosfilt(sos, out, axis=-1, zi=zi)
    ```
  - 影响: `_scipy_available()` 查询的是 audio_capabilities 的独立探测结果，而本模块顶部 `try: from scipy.signal import lfilter, sosfilt except ImportError:` 是另一套判据。文件内其它路径（`_CombFilter.process_block`、`DelayEffect.process`、`_smooth_attack_release`）均额外判 `lfilter is None`；此处一旦两套判据不一致（如测试 monkeypatch capabilities 为 True 而环境无 scipy——模块注释明确说明这是支持的测试手法），`sosfilt(None, ...)` 将 TypeError。
  - 建议: 条件加 `or sosfilt is None`，或统一改为依赖模块级导入结果。
  - 置信度: 中（需两套判据失配才触发，日常一致）

- [AUD-15308] [Suggestion] MasterLimiter 输入验证与其余效果器不一致：构造器不 clamp threshold、不防 0 声道块
  - 位置: src/features/edit/core/audio_dsp.py:2412-2424, 2485
  - 证据:
    ```python
        def __init__(self, sample_rate: float = 48000.0, threshold: float = 1.0, ...) -> None:
            self._sample_rate = float(sample_rate)
            self._threshold = float(threshold)
    ```
    （`threshold.setter` 有 `max(0.01, float(value))` 钳位，但构造器直接赋值绕过；`process` 亦无 `samples.shape[0] < 1` 防护）
    ```python
            abs_max = np.max(np.abs(samples), axis=0) if samples.ndim > 1 else np.abs(samples)
    ```
  - 影响: 构造器传入 threshold<=0 时行为异常（一切信号被视为超阈、增益趋 0 → 静音）而不像 setter 那样钳到 0.01；`(0, n)` 空声道块在 `self._gain < 1.0`（恢复中）时会走 `np.max(..., axis=0)` 对空轴归约抛 ValueError。同文件其它效果器均有 `shape[0] < 1` 早退。现网调用（`audio_graph_renderer.py:121`、`audio_playback.py:1437`，均 threshold=1.0）不触发。
  - 建议: 构造器经 setter 或同款钳位赋值；process 开头补 `samples.ndim != 2 or samples.shape[0] < 1` 早退。
  - 置信度: 高（不一致确凿；实际触发条件苛刻）

### 通过项

- 线程安全：模块级共享可变状态仅 crossfade 包络缓存（`_CROSSFADE_ENV_CACHE`），加锁/锁外生成/上限驱逐实现正确，返回数组只读共享（调用方仅花式索引取值不修改）；效果器实例按轨/每渲染器隔离并在 AudioMixer `_mutex` 下访问；`_ensure_effect_registry` 惰性填充幂等且写入为同值赋值，无实际竞争
- GPU 资源生命周期：不适用（纯 CPU/NumPy DSP，无 ModernGL/OpenGL 资源创建与释放）
- GLSL·HDR 正确性：不适用（无 GLSL 代码）；Python 侧浮点字面量均显式 `.0`；DSP 全程 float64 内部计算，仅按契约在块边界转回输入 dtype，无不当 clamp(0,1)（Limiter/MasterLimiter 的阈值钳制属音频限幅语义而非 HDR 数据破坏）
- 信号防递归：不适用（模块无 Qt 控件/信号，纯算法层）
- 状态提交一致性：不适用（模块不承担 undo/redo/revision/持久化；参数由调用方经 set_params 提交，实例复用策略见 EffectChain，未发现状态错乱）
- 异常与取消路径：无宽泛 except；模块顶部与 `compute_eq_response` 的 ImportError 防护窄化且降级路径明确（EQ 静默旁路 + `is_backend_available` 供 UI 提示）；`EffectChain.set_params` 对未知 effect_type 记录可定位警告而非静默丢弃
- 性能模式：整体优秀——RBJ 系数预算缓存、SOS 一次 sosfilt 级联、`_smooth_attack_release` 常系数 lfilter 快路径（数学等价性核对通过）、NoiseGate 分段闭式几何级数、Comb/Allpass/Delay 块级先读后写向量化、crossfade 包络 LRU 缓存、clip fade 中段 O(1) 早退；逐样本回退路径均有 scipy 缺失兜底
- RBJ Audio EQ Cookbook 五种 biquad（lowshelf/peaking/highshelf/highpass/lowpass）公式逐项核对与 cookbook 一致；lowshelf/highshelf 的 S=1 alpha 化简数值正确
- pan 家族（等功率 pan / 线性 pan / balance / `pan_gains_for_channels` 按声道数选择）语义核对通过；等功率恒功率性质（cos²+sin²）成立
- `apply_clip_fade` / `apply_crossfade_block` 的绝对采样坐标跨块连续性、mask 语义与早退判据一致性核对通过；fade 重叠取较安静包络的处理正确
- `crossfade_envelopes` 四种曲线（constant_power/equal_power/linear/fade）端点与功率性质核对通过；n<=0 抛 ValueError 有文档
- `_CombFilter`/`_AllpassFilter`/`DelayEffect` 环形缓冲段内先读后写语义（含块内绕回读到本块先前写入值）与逐样本实现等价性核对通过；lfilter zi/zf 状态交接（DF2T 约定）核对正确，`_filt = zf[0]/damp1` 回推正确
- Limiter/Compressor/MasterLimiter/NoiseGate 的声道联动（link channels）增益应用、包络状态跨块连续、快路径等价性（含 rise_c=0 瞬时跟随、c=0 直通、等值样本分支结果相同）核对通过
- MasterLimiter attack/release 与 `_smooth_attack_release` rise/fall 的方向映射及末端硬钳制逻辑核对通过
- `EffectChain` 实例按类型复用保持 DSP 状态连续、`_last_entries` 供采样率变化重放的设计核对通过
