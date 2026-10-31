# AssetDeck Pro Audio Player Plugin

## 🎵 功能介绍

专业音频播放器插件,基于 WaveSurfer.js 实现,提供以下功能:

### 卡片预览功能
- **悬停即播**: 鼠标悬停在音频文件卡片上,延迟50ms自动加载波形并播放
- **可视化波形**: 实时显示音频波形图,支持进度拖拽
- **时间显示**: 显示当前播放时间/总时长
- **性能优化**: 列表视图下自动隐藏波形,提升渲染性能

### 专业播放器
双击音频文件打开专业播放器,包含:
- **主波形编辑器**: 高精度波形显示
- **迷你地图**: 全曲overview,支持快速定位
- **时间轴**: 精确时间标记
- **频谱图**: 可选的频谱分析视图
- **波形缩放**: 支持10-1000倍缩放

## 📁 文件结构

```
plugins/audio_pro/
├── __init__.py                 # 插件入口,注册文件处理器
├── README.md                   # 本文档
└── static/
    ├── audio_player.js         # 播放器核心逻辑
    └── audio_player.css        # 播放器样式
```

## 🚀 使用方法

### 1. 插件已自动加载
本插件已被 PluginManager 自动检测加载,无需手动启用。

### 2. 支持的音频格式
- MP3 (.mp3)
- WAV (.wav)
- OGG (.ogg)
- FLAC (.flac)
- M4A (.m4a)
- AAC (.aac)

### 3. 交互方式
- **悬停预览**: 鼠标移到音频卡片上自动播放
- **拖拽跳转**: 在卡片上按住鼠标左右拖动可跳转进度
- **双击打开**: 双击卡片打开专业播放器

## 🔧 技术实现

### 依赖库
- **WaveSurfer.js v7**: 核心音频波形库
- **WaveSurfer.Timeline**: 时间轴插件
- **WaveSurfer.Spectrogram**: 频谱图插件
- **WaveSurfer.Hover**: 悬停提示插件

### 代码架构
```javascript
window.AudioProPlugin = {
    state: { /* 状态管理 */ },
    startAudioPreview(),      // 卡片预览
    stopAudioPreview(),       // 停止预览
    openProAudio(),           // 打开专业播放器
    createAudioCardContent(), // 创建卡片内容
    setupAudioCardEvents()    // 绑定交互事件
}
```

## 📝 开发说明

### 插件接口
- `get_static_js()`: 返回JS文件路径
- `get_head_html()`: 返回需要插入HTML head的内容
- `get_body_html()`: 返回播放器UI模板

### 文件处理器注册
```python
for ext in ['.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac']:
    plugin_manager.register_file_handler(ext, 'AudioProPlugin.openProAudio')
```

## ⚠️ 注意事项

1. **浏览器兼容性**: 需要支持Web Audio API的现代浏览器
2. **自动播放策略**: 部分浏览器可能阻止自动播放,已做防御性处理
3. **性能优化**: 列表视图下波形容器被隐藏以提升性能

## 📄 License

MIT License - 随AssetDeck主项目
