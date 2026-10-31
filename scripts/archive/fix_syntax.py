#!/usr/bin/env python3
"""修复 benchmark_video_pipeline.py 的语法错误"""

file_path = 'tests/media/benchmark_video_pipeline.py'

with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 找到问题行并修复
for i, line in enumerate(lines):
    # 修复缺少括号的问题
    if "help='启用 GPU 上传测试'" in line and not line.strip().endswith(')'):
        # 这行缺少右括号，需要修复
        lines[i] = "        help='启用 GPU 上传测试'\n    )\n"
        # 移除后面插入的重复内容
        # 查找并删除错误的 p10a 定义块（那些导致语法错误的）
        j = i + 1
        while j < len(lines):
            if lines[j].strip().startswith('parser.add_argument'):
                if "'--p10a'" in lines[j] or "'--quiet'" in lines[j]:
                    # 删除错误插入的内容直到找到正确的位置
                    # 找到下一个正确的结构
                    if "    )" in lines[j-1] or ")" in lines[j-1]:
                        break
                    lines[j] = ""  # 暂时清空
                    j += 1
                else:
                    break
            else:
                j += 1

# 清理空行和重复内容
new_lines = []
skip_until_quiet = False
for line in lines:
    if "parser.add_argument(" in line and "'--gpu-upload'" in line:
        new_lines.append(line)
        continue
    if "action='store_true'," in line and len(new_lines) > 0 and "'--gpu-upload'" in new_lines[-2]:
        new_lines.append(line)
        continue
    if "help='启用 GPU 上传测试'" in line:
        new_lines.append("        help='启用 GPU 上传测试'\n")
        new_lines.append("    )\n")
        # 添加正确的 p10a 参数块
        new_lines.append("\n")
        new_lines.append("    parser.add_argument(\n")
        new_lines.append("        '--p10a',\n")
        new_lines.append("        action='store_true',\n")
        new_lines.append("        help='运行 P10.A 验收基准测试'\n")
        new_lines.append("    )\n")
        skip_until_quiet = True
        continue
    if skip_until_quiet:
        if "'--quiet'" in line:
            skip_until_quiet = False
            new_lines.append("\n")
            new_lines.append("    parser.add_argument(\n")
            new_lines.append("        '--quiet', '-q',\n")
            new_lines.append("        action='store_true',\n")
            new_lines.append("        help='安静模式，只输出结果'\n")
            new_lines.append("    )\n")
            continue
        else:
            continue
    new_lines.append(line)

with open(file_path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print('语法错误已修复')