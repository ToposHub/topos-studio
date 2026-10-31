#!/usr/bin/env python3
"""重写 argparse 参数部分"""

import re

file_path = 'tests/media/benchmark_video_pipeline.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 使用正则表达式替换整个错误的参数块
pattern = r'''parser\.add_argument\(
        '--gpu-upload',
        action='store_true',
        help='启用 GPU 上传测试'.*?parser\.add_argument\(
        '--quiet'.*?\)'''

replacement = '''parser.add_argument(
        '--gpu-upload',
        action='store_true',
        help='启用 GPU 上传测试'
    )

    parser.add_argument(
        '--p10a',
        action='store_true',
        help='运行 P10.A 验收基准测试'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='安静模式，只输出结果'
    )'''

# 使用 DOTALL 标志匹配多行
content = re.sub(pattern, replacement, content, flags=re.DOTALL)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('参数块已重写')