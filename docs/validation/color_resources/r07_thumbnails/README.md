# R07 留档：LUT 缩略图后台任务（2026-11-04）

## 实现

- `src/app/controllers/lut_thumbnail_service.py`：
  - `LutThumbnailRequest` DTO：LUT 内容身份（sha256，topos-res id 内嵌/
    路径现场哈希——**绝不只按文件名**）、源身份（session/stack/frame/
    revision）、显示变换 id（v1 = srgb-roundtrip）、尺寸、会话世代；
  - daemon 工作线程：源低分辨率帧（注入 provider）→ sRGB 解码 linear →
    候选 LUT 三线性采样（R05 规格、强度 1）→ sRGB 显示变换 → QImage，
    经排队信号 `thumbnail_ready` 在 GUI 线程投递（QImage→QPixmap 只在
    GUI 线程）；失败 `thumbnail_failed(entry_id, code)`（占位保持）；
  - 预算与守卫：在途上限（默认 8，超出拒收等下次滚动再请求）、有界 LRU
    （默认 256）、`set_source_identity` 切 clip/seek 世代 +1（在途与晚到
    全丢弃，不覆盖新 clip）、`cancel_all` 清在途含评估中（缓存保留）。
- 浏览器接收端：`set_thumbnail`（条目更新 + 图标替换）/`set_thumbnail_failed`
  （tooltip 标注，占位不冒充）/`visible_entry_ids`（只处理可见资源）。
- 控制器接线：`refresh_target` → 更新源身份（last_ack revision）→ 对可见
  LUT 条目（resource_uri/source_path）逐一请求；源 provider = 节点面板源
  缩略图（v1 近似，见下）。

## v1 近似说明（诚实边界）

源取显示域低分辨率源缩略图经 sRGB round-trip 进入 scene-linear 再套
LUT——非逐节点线性输入（R08 交付后切换，缓存键的 source_identity/
display_transform 自然分档不混用旧图）。与真实输出的像素对照在 R12
原生矩阵执行。

## 测试（tests/app/test_lut_thumbnail_service.py，6 项）

同内容不同文件名命中缓存（零重复评估）；世代切换丢弃晚到；损坏 LUT
失败码可见（无 ready）；在途预算拒收 + cancel_all 丢弃（含评估中）；
LRU 上界；投递图像尺寸/通道交换语义正确（R≠B 源 swap 后 R<B）。
回归：lut browser + 控制器 + 手势 27 passed；原生验证 passed。

## 尚未支持

Viewer 悬停预览（计划 R07.4 可选项：独立 transient token + 恢复权威输出，
随 R12 一并评估是否实做）；真实 viewer 显示变换接入。
