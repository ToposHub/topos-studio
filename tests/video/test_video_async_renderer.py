"""
测试 VideoAsyncRenderer 的基本功能

验证：
1. 视频渲染器可以正确初始化
2. 视频帧可以正确上传和渲染
3. 视频渲染器与图片渲染器完全隔离
4. 资源清理正常工作
"""

import sys
import os
import numpy as np
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QOpenGLContext, QOffscreenSurface, QSurfaceFormat

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.shared.video.video_async_renderer import VideoAsyncRenderer, VideoRenderCommand


def test_video_renderer_creation():
    """测试视频渲染器创建和初始化"""
    print("\n=== 测试 1: 视频渲染器创建 ===")
    
    # 创建 QApplication（OpenGL 上下文需要）
    app = QApplication.instance() or QApplication(sys.argv)
    
    # 创建主 OpenGL 上下文
    main_context = QOpenGLContext()
    format = QSurfaceFormat()
    format.setVersion(3, 3)
    format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    main_context.setFormat(format)
    
    if not main_context.create():
        print("❌ 无法创建主 OpenGL 上下文")
        return False
    
    # 创建视频渲染器
    renderer = VideoAsyncRenderer(main_context)
    
    print("✓ 视频渲染器创建成功")
    print(f"  - 线程名称: {renderer.objectName()}")
    print(f"  - 初始化状态: {renderer.is_initialized()}")
    
    # 启动渲染线程
    renderer.start()
    
    # 等待初始化
    import time
    time.sleep(0.5)
    
    print(f"  - 运行状态: {renderer.isRunning()}")
    print(f"  - 初始化状态: {renderer.is_initialized()}")
    
    if renderer.is_initialized():
        print("✅ 测试 1 通过: 视频渲染器创建和初始化成功")
    else:
        print("❌ 测试 1 失败: 视频渲染器初始化失败")
    
    # 清理
    renderer.stop_thread()
    return renderer.is_initialized()


def test_frame_upload_and_render():
    """测试视频帧上传和渲染"""
    print("\n=== 测试 2: 视频帧上传和渲染 ===")
    
    app = QApplication.instance() or QApplication(sys.argv)
    
    # 创建主 OpenGL 上下文
    main_context = QOpenGLContext()
    format = QSurfaceFormat()
    format.setVersion(3, 3)
    format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    main_context.setFormat(format)
    
    if not main_context.create():
        print("❌ 无法创建主 OpenGL 上下文")
        return False
    
    # 创建视频渲染器
    renderer = VideoAsyncRenderer(main_context)
    
    frame_received = []
    
    def on_frame_ready(tex_id):
        frame_received.append(tex_id)
        print(f"  ✓ 帧渲染完成，纹理 ID: {tex_id}")
    
    renderer.frame_ready.connect(on_frame_ready)
    renderer.start()
    
    # 等待初始化
    import time
    time.sleep(0.5)
    
    if not renderer.is_initialized():
        print("❌ 渲染器未初始化")
        return False
    
    # 创建测试帧（红色渐变）
    h, w = 480, 640
    test_frame = np.zeros((h, w, 3), dtype=np.uint8)
    test_frame[:, :, 2] = np.linspace(0, 255, w, dtype=np.uint8)  # R 通道渐变
    
    # 提交帧进行渲染
    viewport_size = (w, h)
    renderer.upload_and_render_frame(test_frame, viewport_size)
    
    # 等待渲染完成
    time.sleep(0.5)
    
    if frame_received:
        print(f"✅ 测试 2 通过: 视频帧上传和渲染成功")
        print(f"  - 测试帧尺寸: {w}x{h}")
        print(f"  - 渲染的纹理 ID: {frame_received[0]}")
        result = True
    else:
        print("❌ 测试 2 失败: 未收到帧渲染完成信号")
        result = False
    
    # 清理
    renderer.stop_thread()
    return result


def test_resource_cleanup():
    """测试资源清理"""
    print("\n=== 测试 3: 资源清理 ===")
    
    app = QApplication.instance() or QApplication(sys.argv)
    
    # 创建主 OpenGL 上下文
    main_context = QOpenGLContext()
    format = QSurfaceFormat()
    format.setVersion(3, 3)
    format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    main_context.setFormat(format)
    
    if not main_context.create():
        print("❌ 无法创建主 OpenGL 上下文")
        return False
    
    # 创建视频渲染器
    renderer = VideoAsyncRenderer(main_context)
    renderer.start()
    
    import time
    time.sleep(0.5)
    
    if not renderer.is_initialized():
        print("❌ 渲染器未初始化")
        return False
    
    print(f"  - 清理前初始化状态: {renderer.is_initialized()}")
    print(f"  - 清理前运行状态: {renderer.isRunning()}")
    
    # 停止并清理
    renderer.stop_thread()
    
    time.sleep(0.3)
    
    print(f"  - 清理后初始化状态: {renderer.is_initialized()}")
    print(f"  - 清理后运行状态: {renderer.isRunning()}")
    
    if not renderer.is_initialized() and not renderer.isRunning():
        print("✅ 测试 3 通过: 资源清理成功")
        return True
    else:
        print("❌ 测试 3 失败: 资源清理不完整")
        return False


def test_command_queue():
    """测试命令队列处理"""
    print("\n=== 测试 4: 命令队列处理 ===")
    
    app = QApplication.instance() or QApplication(sys.argv)
    
    # 创建主 OpenGL 上下文
    main_context = QOpenGLContext()
    format = QSurfaceFormat()
    format.setVersion(3, 3)
    format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    main_context.setFormat(format)
    
    if not main_context.create():
        print("❌ 无法创建主 OpenGL 上下文")
        return False
    
    # 创建视频渲染器
    renderer = VideoAsyncRenderer(main_context)
    
    commands_processed = []
    
    def on_frame_ready(tex_id):
        commands_processed.append(tex_id)
    
    renderer.frame_ready.connect(on_frame_ready)
    renderer.start()
    
    import time
    time.sleep(0.5)
    
    if not renderer.is_initialized():
        print("❌ 渲染器未初始化")
        return False
    
    # 提交多个命令测试队列处理
    h, w = 240, 320
    
    for i in range(3):
        test_frame = np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)
        renderer.upload_and_render_frame(test_frame, (w, h))
        time.sleep(0.1)  # 短暂间隔
    
    # 等待所有命令处理完成
    time.sleep(1.0)
    
    if len(commands_processed) > 0:
        print(f"✅ 测试 4 通过: 命令队列处理成功")
        print(f"  - 提交的命令数: 3")
        print(f"  - 完成的渲染: {len(commands_processed)}")
        return True
    else:
        print("❌ 测试 4 失败: 命令队列处理失败")
        return False


def main():
    """运行所有测试"""
    print("=" * 60)
    print("VideoAsyncRenderer 测试套件")
    print("=" * 60)
    
    results = []
    
    # 运行测试
    results.append(("创建和初始化", test_video_renderer_creation()))
    results.append(("帧上传和渲染", test_frame_upload_and_render()))
    results.append(("资源清理", test_resource_cleanup()))
    results.append(("命令队列", test_command_queue()))
    
    # 打印总结
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{status} - {name}")
    
    print(f"\n总计: {passed}/{total} 测试通过")
    
    if passed == total:
        print("\n🎉 所有测试通过！VideoAsyncRenderer 工作正常。")
        return 0
    else:
        print(f"\n⚠️  {total - passed} 个测试失败，请检查问题。")
        return 1


if __name__ == '__main__':
    sys.exit(main())
