# Video Performance Acceptance Test Framework
视频性能验收测试框架 - P5-3 验收指标测试

## 目录结构

```
tests/benchmark/
├── __init__.py                    # 模块初始化
├── acceptance_criteria.py         # 验收指标定义
├── benchmark_base.py              # 基准测试基类
├── test_playback_performance.py   # 播放帧率测试
├── test_ui_tick.py                # UI tick 性能测试
├── test_renderer_queue.py         # 渲染队列深度测试
├── test_latency.py                # 延迟和 Fallback 测试
├── run_acceptance.py              # 一键运行脚本
├── report_generator.py            # 报告生成器
└── reports/                       # 报告输出目录
```

## 验收指标

| 指标 ID | 指标名称 | 最低目标 | 专业目标 |
|---------|----------|----------|----------|
| playback_1080p_h264_fps | 1080p H.264 Proxy Playback FPS | 24fps | 60fps |
| playback_4k_h264_fps | 4K H.264/H.265 Proxy Playback FPS | 24fps | 60fps |
| playback_4k_prores_fps | 4K ProRes Proxy Playback FPS | 24fps | 60fps |
| ui_tick_p95_ms | UI Tick P95 Latency | < 5ms | < 2ms |
| ui_tick_max_ms | UI Tick Max Latency | < 20ms | < 10ms |
| renderer_queue_depth_p95 | Renderer Queue Depth P95 | <= 3 | <= 2 |
| renderer_queue_max_depth | Renderer Queue Max Depth | <= 5 | <= 3 |
| playback_latency_growth_rate | Playback Latency Growth Rate | <= 1ms/s | 0ms/s |
| seek_latency_p95_ms | Seek Latency P95 | <= 100ms | <= 50ms |
| zoom_pan_render_storm_count | Zoom/Pan Render Storm Count | 0 | 0 |
| zoom_pan_response_ms | Zoom/Pan Response Time | < 50ms | < 16ms |
| fallback_v2_count | Fallback V2 Count | <= 1 | 0 |
| fallback_rate | Fallback Rate | <= 5% | 0% |

## 使用方法

### 1. 一键运行所有验收测试

```bash
# 运行完整测试（约 60-90 秒）
python tests/benchmark/run_acceptance.py

# 快速模式（约 20-30 秒）
python tests/benchmark/run_acceptance.py --quick

# 详细输出
python tests/benchmark/run_acceptance.py --verbose

# 只输出 JSON 报告
python tests/benchmark/run_acceptance.py --format json

# 不生成报告文件
python tests/benchmark/run_acceptance.py --no-report
```

### 2. 使用 pytest 运行

```bash
# 运行所有验收测试
pytest tests/benchmark/ -m acceptance -v

# 运行特定类别测试
pytest tests/benchmark/test_playback_performance.py -m acceptance -v
pytest tests/benchmark/test_ui_tick.py -m acceptance -v
pytest tests/benchmark/test_renderer_queue.py -m acceptance -v
pytest tests/benchmark/test_latency.py -m acceptance -v

# 排除验收测试
pytest tests/benchmark/ -m "not acceptance"
```

### 3. 运行单个基准测试

```python
from tests.benchmark.test_playback_performance import PlaybackFPSBenchmark

# 1080p H.264 播放测试
benchmark = PlaybackFPSBenchmark(
    resolution=(1920, 1080),
    codec="h264",
    target_fps=30.0,
)
result = benchmark.run_benchmark()

print(f"FPS: {result.measured_value:.1f}")
print(f"Pass: {result.passed}")
```

## 报告格式

### JSON 报告

结构化数据，用于自动化处理和 CI/CD 集成。

```json
{
  "results": {
    "playback_1080p_h264_fps": {
      "measured_value": 45.2,
      "level": "minimum",
      "passed": true,
      "details": {...}
    }
  },
  "summary": {
    "total_metrics": 13,
    "passed": 10,
    "failed": 3,
    "pass_rate": 0.769
  },
  "overall_passed": false
}
```

### Markdown 报告

文本报告，用于版本控制和文档。

```markdown
# Video Performance Acceptance Report

**整体结果**: Minimum Pass
**通过率**: 76.9%

## PLAYBACK
| 指标 | 测量值 | 最低目标 | 专业目标 | 结果 |
|------|--------|----------|----------|------|
| 1080p H.264 Playback FPS | 45.2 fps | 24 fps | 60 fps | Minimum Pass |
```

### HTML 报告

可视化报告，用于展示和分享。

包含：
- 整体结果卡片
- 分类表格
- 颜色标记（绿色通过，红色失败）
- 优化建议

## 测试环境

验收测试使用 `MockPlaybackEnvironment` 模拟播放环境：

- 模拟视频解码器（不同分辨率、帧率）
- 模拟渲染器队列状态
- 使用真实的 `VideoPerformanceMonitor` 收集数据
- 支持压力测试和稳定性测试

## CI/CD 集成

### GitHub Actions

```yaml
- name: Run Video Performance Acceptance Tests
  run: python tests/benchmark/run_acceptance.py --quick --format json

- name: Upload Acceptance Report
  uses: actions/upload-artifact@v3
  with:
    name: acceptance-report
    path: tests/benchmark/reports/acceptance_*.json
```

### Jenkins

```groovy
stage('Acceptance Tests') {
    steps {
        sh 'python tests/benchmark/run_acceptance.py --quick'
    }
    post {
        always {
            archiveArtifacts artifacts: 'tests/benchmark/reports/*'
        }
    }
}
```

## 自定义验收指标

可以通过修改 `acceptance_criteria.py` 添加新的验收指标：

```python
METRICS.add_metric(AcceptanceMetric(
    id="custom_metric_id",
    name="Custom Metric Name",
    category=MetricCategory.PLAYBACK,
    description="Custom metric description",
    unit="ms",
    minimum_target=10.0,
    professional_target=5.0,
    measurement_func="测量方法描述",
))
```

## 注意事项

1. **反向指标**: UI tick、队列深度等指标是反向指标（测量值越低越好）
2. **快速模式**: 用于 CI/CD，缩短测试时间但结果仍可靠
3. **真实环境**: Mock 环境不能完全模拟真实硬件性能，真实测试需要实际播放
4. **稳定性**: 多次运行结果可能有波动，建议取平均值

## 扩展开发

### 添加新的基准测试

1. 创建新的测试文件 `test_xxx.py`
2. 继承 `VideoPerformanceBenchmark`
3. 实现 `setup_mock_environment()` 和 `run_benchmark()`
4. 在 `run_acceptance.py` 中添加调用

### 自定义报告格式

修改 `report_generator.py`：

- 添加新的输出格式（如 CSV、PDF）
- 自定义报告样式和内容
- 添加图表生成

## 相关文档

- [P5-3 视频优化方案](../../P5.3_VIDEO_OPTIMIZATION.md)
- [VideoPerformanceMonitor 模块](../../src/core/media/performance_monitor.py)
- [VideoPlaybackEngine 模块](../../src/core/video_playback.py)