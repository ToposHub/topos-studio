# 第三轮导出 LUT 复查

HEAD `63c0bdeaeb5edcee59363479cd8c10b947c3ddb1`，对照`833dcd382`。本报告两个问题为 CPU 真实 exporter 函数执行（M），不新增脚本 GPU 操作。实际既有 OpenGL/Metal 测试177项已由pytest执行通过，不能以其通过覆盖这里未测的缓存连续帧与网格外输入。

## RG01：LUT第二次命中缓存抛ValueError（P1，新回归）

位置：[timeline_export.py:9366](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_export.py:9366>)，新增`cached = cache.get(cache_key, "missing")`后用`cached != "missing"`判断。缓存实际是形状(256,1,3)的numpy.ndarray，与字符串比较得到数组，if求真值失败。NumPy 1.26.4。

直接helper：identity和真实合法Gain参数各首次生成LUT成功，第二/三次均`The truth value of an array with more than one element is ambiguous`。

真实`_apply_color_grade`：Gain wheel `(49.360623248919055,0.375193068518081,0)`，BGR像素[59,140,112]，第一帧返回[43,153,131]，第二/三帧均抛`color grade failed for clip clip: ... ambiguous ...`。上层是fail-fast，CPU该路径的连续帧导出可失败；没有说无调色或所有GPU路径都必然失败。

修复：用`cache_key in cache`或唯一object sentinel并按identity比较，正确区分缺失、None（不可分离）和数组；不得为了避异常退回未调色帧。

验收：identity/合法非空Gain/拒绝快路的Lift，同一exporter连续至少3帧；确认缓存对象复用、None结论复用、无异常；实际CPU有调色短片成功，颜色与参考一致。现有test每个params只调用一次，需覆盖第二次命中。

## RG02：7³安全网格不能证明可分离，合法参数仍输出错色（P1，R30部分修复）

位置：[timeline_export.py:9389](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_export.py:9389>)，只检查levels=[0,1,2,3,7,64,255]的343组合，最大偏差≤1就接受独立通道LUT；跨通道保护条件在其他RGB值仍会触发。原测试wheel已被正确拒绝，但不能因此把R30全部关闭。

固定seed7713，30组合法wheel（hue[0,360]、sat[0,1]、brightness[-1,1]）中20组被接受，已发现3组网格外反例，最大偏差8/2/4 code。第一组参数：

```json
{"lift":[271.6186690578128,0.19411276782870046,-0.5892569835578423],"gamma":[71.73376380027234,0.316113852565303,0.12122455594011261],"gain":[275.51972102495455,0.8444802110074772,0.47807158906166936]}
```

RGB[112,140,59]，真实`_apply_color_grade`输出[255,86,244]，GradingMath正确[255,86,252]，差8，超过新安全门声明的≤1。此为实际路径验证，不只是理论怀疑或超UI范围fixture。

修复：可分离必须由公式/参数条件证明；不能靠有限网格“认证”。短期对可能触发跨RGB保护的参数走float；长期LUT只做可分离前缀、RGB保护单独stage。可以扩大grid用于找回归，但不能以抽样无失败替代安全条件。

验收：上述3个固定反例、正常/边界合法wheel、近黑/中间值/全色域，fast/float按明确数值预算一致；保持原exactness契约，拒绝快路不缓存错误表；与RG01一起验证连续帧。

证据：`evidence/render_probe.py`、`render_probe.json`（前段JSON+真实异常日志）、`render_real_path.json`、`lut_safety_probe.py/json`。探针只用于复现，未修改生产或原测试。
