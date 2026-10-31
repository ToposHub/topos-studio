"""
AssetDeck Pro Audio Player Plugin
专业音频播放器插件 - 基于 WaveSurfer.js
"""

# 插件元数据
NAME = 'Audio Pro'
VERSION = '1.0.0'
AUTHOR = 'AssetDeck Team'
DESCRIPTION = '专业音频播放器，支持波形图和截辑'

class AudioProPlugin:
    """音频插件类：处理音频文件打开和 JS 注册"""
    def __init__(self, app, context):
        self.app, self.context = app, context
        self.exts = ['.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac']
        # 注册音频文件处理器
        for ext in self.exts: context.register_file_handler(ext, 'AudioProPlugin.openProAudio')

def setup(context):
    """插件初始化入口"""
    plugin = AudioProPlugin(context.app, context)
    context.register_js('/static_plugin/audio_pro/audio_player.js')

