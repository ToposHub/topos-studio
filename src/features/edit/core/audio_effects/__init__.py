"""新复合音频 FX 包（NF/VC/SM，2026-11-15 计划 §6–§8）。

复杂 DSP 不再继续堆入 audio_dsp.py 大文件；本包持有 Voice Changer 与
Spectral Mangler 的处理器实现。``base`` 提供无环共享原语（禁止子模块
反向导入正在初始化的 audio_dsp 造成循环——audio_dsp 惰性工厂
``_ensure_effect_registry`` 注册本包的类）。
"""
