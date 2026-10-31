# Face Track 人脸跟踪

人脸关键点容器 + 直通输出。检测结果以 {帧号: [(x, y, 置信度)...]} JSON 形式**保存在工程里**——重开工程无需重跑检测。检测器经注入接入（ONNX 模型经 model_manager 注册，绝不静默下载；无检测器时节点可见 NEEDS_DETECTOR 状态）。

关键点供表达式/检查使用；皮肤 mask 由 face_workflow 的 build_skin_mask 从关键点几何生成（颊/额/鼻多边形 + 眼/眉/唇排除区 + 羽化），接给 Face Smooth 磨皮。

侧脸/遮挡退化由置信度兜底；多脸场景用"脸序号"选择。
