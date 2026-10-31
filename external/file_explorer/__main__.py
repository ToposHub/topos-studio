"""[ARCH-1] 独立模式入口：在 external/ 目录下执行 ``python -m file_explorer``。

（``python external/file_explorer/main.py`` 直跑也受支持：main.py 的
``__main__`` 守卫调用同一 run_standalone()。）"""
import os
import sys

# 平铺模块（main/const/utils...）所在目录加入 path，使 -m 方式与直跑
# main.py 的导入环境一致。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402

main.run_standalone()
