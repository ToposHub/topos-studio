# CPU oracle 约定（references/）

> specs/spec_schema.md §4 的执行细则。所有 L 线组件的独立数学参考遵守本约定。

1. **数值**：参考实现内部 float64 计算，接口 float32 进出；比较在同一工作域的
   浮点像素上进行（显示缩略图不能证明数值正确）。
2. **域**：scene-linear RGB；负值/HDR 透传；除明确定义的权重/mask 域外不得
   clamp 到 [0,1]（.clinerules GLSL/HDR 红线的 CPU 侧同构）。
3. **alpha**：默认 straight；premult 语义由被测算法显式声明并在 oracle 中同构。
4. **边界**：邻域采样的 oracle 按被测组件 manifest 的 `image.sampling_border`
   同构处理（默认 clamp-half-texel：索引钳到 [0, n-1]）。
5. **禁自证**：oracle 不得 import 宿主渲染/GPU 代码生成自己的预期；
   手算向量（小尺寸、可笔算复核）优先钉住公式结构，oracle 提供全图输出。
6. **确定性**：不使用随机流；需要噪声/纹理的算法用与本库 fixtures 相同的
   整数格点散列（`fixtures/generate_fixtures.py::_lattice_hash`）或组件内
   自带的确定性哈希，并在规格中写明 seed 语义（L01 默认 static/固定 seed）。
7. **命名**：`references/<slug>_ref.py` 暴露 `evaluate(image, params) -> np.ndarray`
   （转场为 `evaluate_mask(width, height, progress, params)` / 双源
   `evaluate(a, b, progress, params)`）；参数用各自组件 manifest 的默认值补全。
8. **对拍口径**：GPU 实测（L02 起）与 oracle 的容差按效果类别声明
   （点处理 1e-6 相对、邻域采样 2e-4 相对起步，超差必须解释而非放宽）。
