# 计划01 全库审查：文件清单与批次表（P0 阶段产出）

- 生成方式：`find src -name '*.py'` 全量扫描（排除 `__pycache__`），排除空文件统计
- 总文件数：**1483**，总行数：**835,840**
- P0 优先级（渲染心脏）：**62 文件 / 34,146 行**
- P1 优先级（用户可感知功能路径）：**725 文件 / 514,994 行**
- P2 优先级（其余）：**696 文件 / 286,700 行**
- 总批次数：**398**（每批 ≤8 文件且 ≤3000 行，按目录聚类不跨目录混批）

优先级执行顺序：P0 批次（阶段 P1 执行）→ 汇总台账（阶段 P2）→ P1 批次（阶段 P3）→ P2 批次（阶段 P4）。

## 批次表

| 批次 | 优先级 | 主题（目录） | 文件数 | 行数 |
|------|--------|--------------|--------|------|
| batch-001 | P0 | `src/features/color/core/` | 1 | 0 |
| batch-002 | P0 | `src/features/color/core/` | 1 | 6,669 |
| batch-003 | P0 | `src/features/color/core/` | 4 | 2,498 |
| batch-004 | P0 | `src/features/color/core/` | 6 | 2,884 |
| batch-005 | P0 | `src/features/color/core/` | 3 | 709 |
| batch-006 | P0 | `src/features/color/core/` | 1 | 4,730 |
| batch-007 | P0 | `src/features/color/core/` | 4 | 2,769 |
| batch-008 | P0 | `src/features/color/core/` | 6 | 2,629 |
| batch-009 | P0 | `src/features/color/core/` | 6 | 2,617 |
| batch-010 | P0 | `src/features/color/core/` | 2 | 817 |
| batch-011 | P0 | `src/shared/effects/` | 2 | 615 |
| batch-012 | P0 | `src/shared/gpu/` | 8 | 1,562 |
| batch-013 | P0 | `src/shared/gpu/` | 4 | 1,027 |
| batch-014 | P0 | `src/shared/gpu/metal/` | 8 | 2,607 |
| batch-015 | P0 | `src/shared/gpu/metal/` | 3 | 1,508 |
| batch-016 | P0 | `src/shared/gpu/` | 1 | 464 |
| batch-017 | P0 | `src/shared/render/` | 2 | 41 |
| batch-018 | P1 | `src/app/` | 2 | 44 |
| batch-019 | P1 | `src/app/controllers/` | 2 | 101 |
| batch-020 | P1 | `src/app/controllers/` | 1 | 6,424 |
| batch-021 | P1 | `src/app/controllers/` | 2 | 2,209 |
| batch-022 | P1 | `src/app/controllers/` | 2 | 2,261 |
| batch-023 | P1 | `src/app/controllers/` | 2 | 2,098 |
| batch-024 | P1 | `src/app/controllers/` | 1 | 1,352 |
| batch-025 | P1 | `src/app/controllers/` | 1 | 1,901 |
| batch-026 | P1 | `src/app/controllers/` | 1 | 3,092 |
| batch-027 | P1 | `src/app/controllers/` | 1 | 9,861 |
| batch-028 | P1 | `src/app/controllers/` | 1 | 7,313 |
| batch-029 | P1 | `src/app/controllers/` | 3 | 2,489 |
| batch-030 | P1 | `src/app/controllers/` | 1 | 1,883 |
| batch-031 | P1 | `src/app/controllers/` | 1 | 3,600 |
| batch-032 | P1 | `src/app/` | 2 | 242 |
| batch-033 | P1 | `src/app/integrations/` | 1 | 0 |
| batch-034 | P1 | `src/app/integrations/file_explorer/` | 3 | 745 |
| batch-035 | P1 | `src/app/` | 1 | 2,008 |
| batch-036 | P1 | `src/app/menu/` | 2 | 267 |
| batch-037 | P1 | `src/app/` | 2 | 284 |
| batch-038 | P1 | `src/app/ui/` | 6 | 1,244 |
| batch-039 | P1 | `src/app/ui/` | 1 | 2,685 |
| batch-040 | P1 | `src/app/ui/` | 3 | 2,770 |
| batch-041 | P1 | `src/app/ui/` | 2 | 1,318 |
| batch-042 | P1 | `src/app/` | 1 | 94 |
| batch-043 | P1 | `src/features/composite/` | 1 | 1 |
| batch-044 | P1 | `src/features/composite/adapters/` | 3 | 774 |
| batch-045 | P1 | `src/features/composite/ai/` | 2 | 348 |
| batch-046 | P1 | `src/features/composite/` | 1 | 268 |
| batch-047 | P1 | `src/features/composite/core/` | 8 | 2,397 |
| batch-048 | P1 | `src/features/composite/core/` | 5 | 2,686 |
| batch-049 | P1 | `src/features/composite/core/` | 2 | 2,678 |
| batch-050 | P1 | `src/features/composite/core/` | 3 | 2,204 |
| batch-051 | P1 | `src/features/composite/core/` | 1 | 2,810 |
| batch-052 | P1 | `src/features/composite/core/` | 5 | 1,115 |
| batch-053 | P1 | `src/features/composite/core/` | 1 | 7,113 |
| batch-054 | P1 | `src/features/composite/core/` | 4 | 2,184 |
| batch-055 | P1 | `src/features/composite/core/` | 1 | 1,105 |
| batch-056 | P1 | `src/features/composite/` | 1 | 1,020 |
| batch-057 | P1 | `src/features/composite/evaluators/` | 2 | 108 |
| batch-058 | P1 | `src/features/composite/evaluators/` | 1 | 3,444 |
| batch-059 | P1 | `src/features/composite/evaluators/` | 5 | 1,424 |
| batch-060 | P1 | `src/features/composite/evaluators/` | 4 | 2,676 |
| batch-061 | P1 | `src/features/composite/evaluators/` | 6 | 1,831 |
| batch-062 | P1 | `src/features/composite/` | 5 | 1,375 |
| batch-063 | P1 | `src/features/composite/nodes/` | 8 | 614 |
| batch-064 | P1 | `src/features/composite/nodes/` | 8 | 2,945 |
| batch-065 | P1 | `src/features/composite/nodes/` | 8 | 1,996 |
| batch-066 | P1 | `src/features/composite/nodes/` | 8 | 1,930 |
| batch-067 | P1 | `src/features/composite/nodes/` | 8 | 1,382 |
| batch-068 | P1 | `src/features/composite/nodes/` | 8 | 1,660 |
| batch-069 | P1 | `src/features/composite/nodes/` | 8 | 1,978 |
| batch-070 | P1 | `src/features/composite/nodes/` | 8 | 2,072 |
| batch-071 | P1 | `src/features/composite/nodes/` | 8 | 2,370 |
| batch-072 | P1 | `src/features/composite/nodes/` | 8 | 1,364 |
| batch-073 | P1 | `src/features/composite/nodes/` | 1 | 5,253 |
| batch-074 | P1 | `src/features/composite/nodes/` | 8 | 2,524 |
| batch-075 | P1 | `src/features/composite/nodes/` | 4 | 721 |
| batch-076 | P1 | `src/features/composite/nodes/` | 2 | 2,915 |
| batch-077 | P1 | `src/features/composite/nodes/` | 3 | 605 |
| batch-078 | P1 | `src/features/composite/` | 1 | 397 |
| batch-079 | P1 | `src/features/composite/render/` | 4 | 2,415 |
| batch-080 | P1 | `src/features/composite/render/` | 3 | 2,559 |
| batch-081 | P1 | `src/features/composite/render/` | 2 | 1,526 |
| batch-082 | P1 | `src/features/composite/render/` | 2 | 2,879 |
| batch-083 | P1 | `src/features/composite/render/` | 5 | 2,999 |
| batch-084 | P1 | `src/features/composite/render/` | 5 | 2,882 |
| batch-085 | P1 | `src/features/composite/render/` | 2 | 454 |
| batch-086 | P1 | `src/features/composite/render3d/` | 5 | 1,055 |
| batch-087 | P1 | `src/features/composite/render3d/deformers/` | 3 | 513 |
| batch-088 | P1 | `src/features/composite/render3d/` | 7 | 2,974 |
| batch-089 | P1 | `src/features/composite/render3d/` | 8 | 2,843 |
| batch-090 | P1 | `src/features/composite/render3d/` | 5 | 2,826 |
| batch-091 | P1 | `src/features/composite/render3d/` | 8 | 2,976 |
| batch-092 | P1 | `src/features/composite/render3d/` | 7 | 1,986 |
| batch-093 | P1 | `src/features/composite/` | 1 | 240 |
| batch-094 | P1 | `src/features/composite/ui/` | 3 | 1,031 |
| batch-095 | P1 | `src/features/composite/ui/` | 1 | 10,083 |
| batch-096 | P1 | `src/features/composite/ui/` | 2 | 608 |
| batch-097 | P1 | `src/features/composite/ui/` | 1 | 11,512 |
| batch-098 | P1 | `src/features/composite/ui/` | 2 | 2,849 |
| batch-099 | P1 | `src/features/composite/ui/` | 7 | 2,953 |
| batch-100 | P1 | `src/features/composite/ui/` | 3 | 2,349 |
| batch-101 | P1 | `src/features/composite/ui/` | 1 | 3,041 |
| batch-102 | P1 | `src/features/composite/ui/` | 1 | 1,880 |
| batch-103 | P1 | `src/features/composite/ui/` | 3 | 2,437 |
| batch-104 | P1 | `src/features/composite/ui/` | 1 | 1,508 |
| batch-105 | P1 | `src/features/composite/ui/` | 1 | 5,128 |
| batch-106 | P1 | `src/features/composite/ui/` | 2 | 1,675 |
| batch-107 | P1 | `src/features/composite/ui/` | 1 | 7,683 |
| batch-108 | P1 | `src/features/composite/ui/` | 8 | 2,719 |
| batch-109 | P1 | `src/features/composite/ui/` | 4 | 2,861 |
| batch-110 | P1 | `src/features/composite/ui/` | 5 | 2,823 |
| batch-111 | P1 | `src/features/composite/ui/` | 5 | 2,129 |
| batch-112 | P1 | `src/features/composite/ui/` | 2 | 1,253 |
| batch-113 | P1 | `src/features/composite/ui/` | 1 | 2,087 |
| batch-114 | P1 | `src/features/composite/ui/` | 4 | 2,387 |
| batch-115 | P1 | `src/features/composite/utils/` | 7 | 2,770 |
| batch-116 | P1 | `src/features/composite/utils/` | 1 | 545 |
| batch-117 | P1 | `src/features/deliver/` | 1 | 3 |
| batch-118 | P1 | `src/features/deliver/adapters/` | 1 | 7 |
| batch-119 | P1 | `src/features/deliver/core/` | 8 | 2,977 |
| batch-120 | P1 | `src/features/deliver/core/` | 2 | 731 |
| batch-121 | P1 | `src/features/deliver/` | 4 | 749 |
| batch-122 | P1 | `src/features/deliver/ui/` | 4 | 2,013 |
| batch-123 | P1 | `src/features/deliver/ui/` | 1 | 1,134 |
| batch-124 | P1 | `src/features/deliver/ui/` | 1 | 2,954 |
| batch-125 | P1 | `src/features/edit/` | 1 | 3 |
| batch-126 | P1 | `src/features/edit/adapters/` | 5 | 1,139 |
| batch-127 | P1 | `src/features/edit/ai_editing/` | 8 | 2,779 |
| batch-128 | P1 | `src/features/edit/ai_editing/` | 6 | 2,734 |
| batch-129 | P1 | `src/features/edit/ai_editing/` | 6 | 2,596 |
| batch-130 | P1 | `src/features/edit/ai_editing/` | 7 | 2,955 |
| batch-131 | P1 | `src/features/edit/ai_editing/` | 8 | 2,600 |
| batch-132 | P1 | `src/features/edit/ai_editing/` | 8 | 2,712 |
| batch-133 | P1 | `src/features/edit/ai_editing/` | 8 | 2,371 |
| batch-134 | P1 | `src/features/edit/ai_editing/` | 8 | 2,880 |
| batch-135 | P1 | `src/features/edit/ai_editing/` | 8 | 2,922 |
| batch-136 | P1 | `src/features/edit/ai_editing/` | 8 | 2,903 |
| batch-137 | P1 | `src/features/edit/ai_editing/` | 5 | 2,797 |
| batch-138 | P1 | `src/features/edit/ai_editing/` | 6 | 2,539 |
| batch-139 | P1 | `src/features/edit/ai_editing/` | 8 | 2,067 |
| batch-140 | P1 | `src/features/edit/ai_editing/` | 6 | 2,250 |
| batch-141 | P1 | `src/features/edit/ai_editing/` | 8 | 2,974 |
| batch-142 | P1 | `src/features/edit/ai_editing/` | 8 | 1,463 |
| batch-143 | P1 | `src/features/edit/ai_editing/` | 8 | 2,139 |
| batch-144 | P1 | `src/features/edit/ai_editing/` | 8 | 2,582 |
| batch-145 | P1 | `src/features/edit/ai_editing/` | 6 | 1,571 |
| batch-146 | P1 | `src/features/edit/ai_editing/tests/` | 1 | 213 |
| batch-147 | P1 | `src/features/edit/ai_editing/` | 6 | 2,880 |
| batch-148 | P1 | `src/features/edit/ai_editing/` | 5 | 1,730 |
| batch-149 | P1 | `src/features/edit/ai_editing/` | 6 | 2,708 |
| batch-150 | P1 | `src/features/edit/ai_editing/` | 6 | 2,048 |
| batch-151 | P1 | `src/features/edit/` | 1 | 249 |
| batch-152 | P1 | `src/features/edit/core/` | 4 | 651 |
| batch-153 | P1 | `src/features/edit/core/` | 1 | 2,505 |
| batch-154 | P1 | `src/features/edit/core/` | 3 | 1,302 |
| batch-155 | P1 | `src/features/edit/core/` | 1 | 4,855 |
| batch-156 | P1 | `src/features/edit/core/` | 7 | 2,584 |
| batch-157 | P1 | `src/features/edit/core/` | 1 | 443 |
| batch-158 | P1 | `src/features/edit/core/captions/` | 6 | 2,305 |
| batch-159 | P1 | `src/features/edit/core/captions/` | 7 | 2,542 |
| batch-160 | P1 | `src/features/edit/core/captions/` | 2 | 747 |
| batch-161 | P1 | `src/features/edit/core/` | 3 | 1,548 |
| batch-162 | P1 | `src/features/edit/core/` | 1 | 16,251 |
| batch-163 | P1 | `src/features/edit/core/` | 7 | 2,856 |
| batch-164 | P1 | `src/features/edit/core/` | 2 | 1,249 |
| batch-165 | P1 | `src/features/edit/core/` | 1 | 3,037 |
| batch-166 | P1 | `src/features/edit/core/` | 1 | 5,297 |
| batch-167 | P1 | `src/features/edit/core/` | 2 | 2,839 |
| batch-168 | P1 | `src/features/edit/core/` | 6 | 2,835 |
| batch-169 | P1 | `src/features/edit/core/` | 8 | 2,737 |
| batch-170 | P1 | `src/features/edit/core/` | 2 | 272 |
| batch-171 | P1 | `src/features/edit/core/` | 1 | 5,392 |
| batch-172 | P1 | `src/features/edit/core/` | 5 | 1,842 |
| batch-173 | P1 | `src/features/edit/core/realtime_playback/` | 8 | 2,786 |
| batch-174 | P1 | `src/features/edit/core/realtime_playback/` | 8 | 2,754 |
| batch-175 | P1 | `src/features/edit/core/realtime_playback/` | 2 | 713 |
| batch-176 | P1 | `src/features/edit/core/` | 5 | 2,419 |
| batch-177 | P1 | `src/features/edit/core/` | 1 | 873 |
| batch-178 | P1 | `src/features/edit/core/scene_cut/` | 3 | 2,413 |
| batch-179 | P1 | `src/features/edit/core/scene_cut/` | 3 | 2,647 |
| batch-180 | P1 | `src/features/edit/core/scene_cut/` | 2 | 2,269 |
| batch-181 | P1 | `src/features/edit/core/scene_cut/` | 3 | 2,741 |
| batch-182 | P1 | `src/features/edit/core/` | 5 | 2,970 |
| batch-183 | P1 | `src/features/edit/core/` | 3 | 2,959 |
| batch-184 | P1 | `src/features/edit/core/` | 1 | 11,028 |
| batch-185 | P1 | `src/features/edit/core/` | 2 | 2,622 |
| batch-186 | P1 | `src/features/edit/core/` | 1 | 5,412 |
| batch-187 | P1 | `src/features/edit/core/` | 1 | 869 |
| batch-188 | P1 | `src/features/edit/core/` | 1 | 3,346 |
| batch-189 | P1 | `src/features/edit/core/` | 1 | 11,621 |
| batch-190 | P1 | `src/features/edit/core/` | 2 | 1,959 |
| batch-191 | P1 | `src/features/edit/core/` | 1 | 3,549 |
| batch-192 | P1 | `src/features/edit/core/` | 7 | 1,637 |
| batch-193 | P1 | `src/features/edit/` | 3 | 1,281 |
| batch-194 | P1 | `src/features/edit/ui/` | 6 | 2,668 |
| batch-195 | P1 | `src/features/edit/ui/` | 1 | 3,784 |
| batch-196 | P1 | `src/features/edit/ui/` | 7 | 2,678 |
| batch-197 | P1 | `src/features/edit/ui/` | 5 | 1,926 |
| batch-198 | P1 | `src/features/edit/ui/` | 2 | 2,647 |
| batch-199 | P1 | `src/features/edit/ui/` | 1 | 3,302 |
| batch-200 | P1 | `src/features/edit/ui/` | 1 | 4,896 |
| batch-201 | P1 | `src/features/edit/ui/` | 2 | 978 |
| batch-202 | P1 | `src/features/edit/ui/` | 1 | 10,671 |
| batch-203 | P1 | `src/features/edit/ui/` | 7 | 1,922 |
| batch-204 | P1 | `src/features/edit/ui/` | 1 | 3,414 |
| batch-205 | P1 | `src/features/edit/ui/` | 2 | 1,216 |
| batch-206 | P1 | `src/features/edit/ui/` | 1 | 2,740 |
| batch-207 | P1 | `src/features/edit/ui/` | 2 | 1,946 |
| batch-208 | P1 | `src/features/edit/ui/` | 3 | 2,105 |
| batch-209 | P1 | `src/features/edit/ui/` | 3 | 2,895 |
| batch-210 | P1 | `src/features/edit/ui/` | 4 | 1,493 |
| batch-211 | P1 | `src/features/edit/ui/` | 1 | 11,028 |
| batch-212 | P1 | `src/features/edit/ui/` | 1 | 872 |
| batch-213 | P1 | `src/features/edit/ui/` | 2 | 2,817 |
| batch-214 | P1 | `src/features/edit/ui/` | 1 | 15,708 |
| batch-215 | P1 | `src/features/edit/ui/` | 2 | 523 |
| batch-216 | P1 | `src/products/` | 6 | 426 |
| batch-217 | P2 | `src/` | 1 | 0 |
| batch-218 | P2 | `src/features/` | 1 | 3 |
| batch-219 | P2 | `src/features/ai_harness/` | 6 | 2,577 |
| batch-220 | P2 | `src/features/ai_harness/` | 6 | 2,813 |
| batch-221 | P2 | `src/features/ai_harness/` | 6 | 2,861 |
| batch-222 | P2 | `src/features/ai_harness/` | 8 | 2,679 |
| batch-223 | P2 | `src/features/ai_harness/` | 8 | 2,916 |
| batch-224 | P2 | `src/features/ai_harness/` | 6 | 2,826 |
| batch-225 | P2 | `src/features/ai_harness/tests/` | 8 | 1,861 |
| batch-226 | P2 | `src/features/ai_harness/tests/` | 8 | 2,016 |
| batch-227 | P2 | `src/features/ai_harness/tests/` | 8 | 2,118 |
| batch-228 | P2 | `src/features/ai_harness/tests/` | 8 | 2,146 |
| batch-229 | P2 | `src/features/ai_workflow/` | 1 | 0 |
| batch-230 | P2 | `src/features/ai_workflow/adapters/` | 6 | 2,286 |
| batch-231 | P2 | `src/features/ai_workflow/adapters/` | 5 | 1,969 |
| batch-232 | P2 | `src/features/ai_workflow/core/` | 8 | 2,793 |
| batch-233 | P2 | `src/features/ai_workflow/` | 1 | 111 |
| batch-234 | P2 | `src/features/ai_workflow/runtime/` | 6 | 1,682 |
| batch-235 | P2 | `src/features/ai_workflow/tests/` | 8 | 2,981 |
| batch-236 | P2 | `src/features/ai_workflow/tests/` | 8 | 2,265 |
| batch-237 | P2 | `src/features/ai_workflow/tests/` | 8 | 1,837 |
| batch-238 | P2 | `src/features/ai_workflow/tests/` | 2 | 413 |
| batch-239 | P2 | `src/features/ai_workflow/ui/` | 8 | 1,843 |
| batch-240 | P2 | `src/features/ai_workflow/ui/` | 2 | 774 |
| batch-241 | P2 | `src/features/color/` | 1 | 4 |
| batch-242 | P2 | `src/features/color/adapters/` | 2 | 429 |
| batch-243 | P2 | `src/features/color/ai/` | 2 | 638 |
| batch-244 | P2 | `src/features/color/` | 1 | 255 |
| batch-245 | P2 | `src/features/color/integrations/` | 2 | 1,023 |
| batch-246 | P2 | `src/features/color/` | 2 | 1,469 |
| batch-247 | P2 | `src/features/color/shaders/` | 1 | 669 |
| batch-248 | P2 | `src/features/color/tracking/` | 8 | 124 |
| batch-249 | P2 | `src/features/color/tracking/` | 8 | 917 |
| batch-250 | P2 | `src/features/color/tracking/` | 1 | 14 |
| batch-251 | P2 | `src/features/color/ui/` | 4 | 833 |
| batch-252 | P2 | `src/features/color/ui/` | 1 | 6,123 |
| batch-253 | P2 | `src/features/color/ui/` | 6 | 2,503 |
| batch-254 | P2 | `src/features/color/ui/` | 8 | 2,210 |
| batch-255 | P2 | `src/features/color/ui/` | 4 | 2,710 |
| batch-256 | P2 | `src/features/color/ui/` | 4 | 2,745 |
| batch-257 | P2 | `src/features/color/ui/` | 3 | 2,514 |
| batch-258 | P2 | `src/features/color/ui/` | 6 | 2,671 |
| batch-259 | P2 | `src/features/scripting/` | 4 | 2,075 |
| batch-260 | P2 | `src/features/scripting/examples/` | 5 | 165 |
| batch-261 | P2 | `src/features/scripting/` | 1 | 14 |
| batch-262 | P2 | `src/features/shared/` | 1 | 1 |
| batch-263 | P2 | `src/features/shared/effects/` | 4 | 1,577 |
| batch-264 | P2 | `src/features/shared/effects/` | 5 | 2,855 |
| batch-265 | P2 | `src/features/teaching_animation/` | 1 | 11 |
| batch-266 | P2 | `src/features/teaching_animation/ai/` | 7 | 2,739 |
| batch-267 | P2 | `src/features/teaching_animation/ai/` | 7 | 2,729 |
| batch-268 | P2 | `src/features/teaching_animation/ai/` | 7 | 2,912 |
| batch-269 | P2 | `src/features/teaching_animation/ai/` | 8 | 2,760 |
| batch-270 | P2 | `src/features/teaching_animation/ai/` | 8 | 2,848 |
| batch-271 | P2 | `src/features/teaching_animation/ai/` | 4 | 907 |
| batch-272 | P2 | `src/features/teaching_animation/` | 2 | 191 |
| batch-273 | P2 | `src/features/teaching_animation/core/` | 4 | 1,912 |
| batch-274 | P2 | `src/features/teaching_animation/core/` | 7 | 2,810 |
| batch-275 | P2 | `src/features/teaching_animation/core/` | 8 | 2,240 |
| batch-276 | P2 | `src/features/teaching_animation/core/` | 6 | 2,919 |
| batch-277 | P2 | `src/features/teaching_animation/core/` | 8 | 1,483 |
| batch-278 | P2 | `src/features/teaching_animation/core/` | 3 | 1,093 |
| batch-279 | P2 | `src/features/teaching_animation/` | 3 | 1,436 |
| batch-280 | P2 | `src/features/teaching_animation/` | 1 | 5,895 |
| batch-281 | P2 | `src/features/teaching_animation/motion/` | 8 | 1,640 |
| batch-282 | P2 | `src/features/teaching_animation/motion/` | 3 | 454 |
| batch-283 | P2 | `src/features/teaching_animation/presets/` | 5 | 868 |
| batch-284 | P2 | `src/features/teaching_animation/presets/library/backgrounds/aurora_glow_bg/` | 1 | 48 |
| batch-285 | P2 | `src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/` | 1 | 48 |
| batch-286 | P2 | `src/features/teaching_animation/presets/library/charts/animated_histogram/` | 1 | 50 |
| batch-287 | P2 | `src/features/teaching_animation/presets/library/charts/line_curve_chart/` | 1 | 66 |
| batch-288 | P2 | `src/features/teaching_animation/presets/library/charts/pie_donut_chart/` | 1 | 73 |
| batch-289 | P2 | `src/features/teaching_animation/presets/library/light/lens_flare_stack/` | 1 | 60 |
| batch-290 | P2 | `src/features/teaching_animation/presets/library/light/light_sweep_banner/` | 1 | 41 |
| batch-291 | P2 | `src/features/teaching_animation/presets/library/particles/confetti_burst/` | 1 | 41 |
| batch-292 | P2 | `src/features/teaching_animation/presets/library/particles/floating_orbs/` | 1 | 44 |
| batch-293 | P2 | `src/features/teaching_animation/presets/library/shapes3d/isometric_cube/` | 1 | 50 |
| batch-294 | P2 | `src/features/teaching_animation/presets/library/shapes3d/wireframe_globe/` | 1 | 49 |
| batch-295 | P2 | `src/features/teaching_animation/presets/library/text/kinetic_pop_title/` | 1 | 45 |
| batch-296 | P2 | `src/features/teaching_animation/presets/library/text/neon_glow_title/` | 1 | 41 |
| batch-297 | P2 | `src/features/teaching_animation/presets/` | 4 | 411 |
| batch-298 | P2 | `src/features/teaching_animation/render/` | 8 | 1,653 |
| batch-299 | P2 | `src/features/teaching_animation/render/` | 3 | 715 |
| batch-300 | P2 | `src/features/teaching_animation/render/html_layer/` | 7 | 2,133 |
| batch-301 | P2 | `src/features/teaching_animation/render/` | 4 | 1,575 |
| batch-302 | P2 | `src/features/teaching_animation/render/html_renderer_spike/` | 8 | 2,781 |
| batch-303 | P2 | `src/features/teaching_animation/render/` | 8 | 1,672 |
| batch-304 | P2 | `src/features/teaching_animation/render/` | 2 | 883 |
| batch-305 | P2 | `src/features/teaching_animation/` | 2 | 171 |
| batch-306 | P2 | `src/features/teaching_animation/ui/` | 8 | 2,497 |
| batch-307 | P2 | `src/features/teaching_animation/ui/` | 4 | 1,393 |
| batch-308 | P2 | `src/features/teaching_animation/ui/` | 2 | 2,049 |
| batch-309 | P2 | `src/features/teaching_animation/ui/` | 1 | 3,017 |
| batch-310 | P2 | `src/features/teaching_animation/ui/` | 7 | 2,952 |
| batch-311 | P2 | `src/features/teaching_animation/ui/` | 1 | 86 |
| batch-312 | P2 | `src/` | 1 | 312 |
| batch-313 | P2 | `src/shared/` | 1 | 3 |
| batch-314 | P2 | `src/shared/actions/` | 1 | 92 |
| batch-315 | P2 | `src/shared/ai_models/` | 4 | 1,580 |
| batch-316 | P2 | `src/shared/ai_models/generation/` | 7 | 2,510 |
| batch-317 | P2 | `src/shared/ai_models/generation/` | 1 | 564 |
| batch-318 | P2 | `src/shared/ai_models/` | 6 | 2,518 |
| batch-319 | P2 | `src/shared/` | 1 | 47 |
| batch-320 | P2 | `src/shared/assets/` | 2 | 646 |
| batch-321 | P2 | `src/shared/codec/` | 4 | 559 |
| batch-322 | P2 | `src/shared/codec/` | 1 | 4,449 |
| batch-323 | P2 | `src/shared/codec/` | 4 | 2,093 |
| batch-324 | P2 | `src/shared/color/` | 3 | 2,881 |
| batch-325 | P2 | `src/shared/color/` | 4 | 1,915 |
| batch-326 | P2 | `src/shared/config/` | 2 | 665 |
| batch-327 | P2 | `src/shared/control_surfaces/` | 1 | 1 |
| batch-328 | P2 | `src/shared/control_surfaces/tangent/` | 4 | 725 |
| batch-329 | P2 | `src/shared/db/` | 2 | 242 |
| batch-330 | P2 | `src/shared/diagnostics/` | 4 | 498 |
| batch-331 | P2 | `src/shared/export/` | 6 | 2,760 |
| batch-332 | P2 | `src/shared/export/` | 7 | 2,532 |
| batch-333 | P2 | `src/shared/export/` | 3 | 2,450 |
| batch-334 | P2 | `src/shared/export/` | 1 | 3,537 |
| batch-335 | P2 | `src/shared/export/` | 2 | 2,666 |
| batch-336 | P2 | `src/shared/export/` | 1 | 365 |
| batch-337 | P2 | `src/shared/` | 2 | 437 |
| batch-338 | P2 | `src/shared/i18n/` | 2 | 425 |
| batch-339 | P2 | `src/shared/io/` | 2 | 984 |
| batch-340 | P2 | `src/shared/` | 1 | 1,146 |
| batch-341 | P2 | `src/shared/layout/` | 3 | 744 |
| batch-342 | P2 | `src/shared/logging/` | 2 | 122 |
| batch-343 | P2 | `src/shared/media/` | 4 | 2,145 |
| batch-344 | P2 | `src/shared/media/` | 3 | 2,434 |
| batch-345 | P2 | `src/shared/media/` | 4 | 2,545 |
| batch-346 | P2 | `src/shared/media/` | 1 | 1,296 |
| batch-347 | P2 | `src/shared/media/` | 1 | 3,020 |
| batch-348 | P2 | `src/shared/media/` | 7 | 2,801 |
| batch-349 | P2 | `src/shared/media/` | 1 | 1,009 |
| batch-350 | P2 | `src/shared/media/macos_vt/` | 7 | 1,971 |
| batch-351 | P2 | `src/shared/media/macos_vt/` | 4 | 2,951 |
| batch-352 | P2 | `src/shared/media/macos_vt/` | 2 | 1,138 |
| batch-353 | P2 | `src/shared/media/macos_vt/` | 2 | 2,717 |
| batch-354 | P2 | `src/shared/media/` | 3 | 2,477 |
| batch-355 | P2 | `src/shared/media/` | 3 | 2,382 |
| batch-356 | P2 | `src/shared/media/` | 3 | 2,698 |
| batch-357 | P2 | `src/shared/media/` | 3 | 2,797 |
| batch-358 | P2 | `src/shared/media/` | 5 | 2,520 |
| batch-359 | P2 | `src/shared/media/` | 2 | 2,467 |
| batch-360 | P2 | `src/shared/media/windows_hw/` | 8 | 1,448 |
| batch-361 | P2 | `src/shared/media/windows_hw/` | 8 | 1,765 |
| batch-362 | P2 | `src/shared/media/windows_hw/` | 6 | 1,116 |
| batch-363 | P2 | `src/shared/node_canvas/` | 2 | 440 |
| batch-364 | P2 | `src/shared/node_help/` | 2 | 282 |
| batch-365 | P2 | `src/shared/panels/` | 2 | 277 |
| batch-366 | P2 | `src/shared/paths/` | 2 | 121 |
| batch-367 | P2 | `src/shared/preferences/` | 1 | 158 |
| batch-368 | P2 | `src/shared/project/` | 8 | 2,125 |
| batch-369 | P2 | `src/shared/project/` | 3 | 2,467 |
| batch-370 | P2 | `src/shared/project/` | 3 | 2,424 |
| batch-371 | P2 | `src/shared/project/` | 2 | 1,390 |
| batch-372 | P2 | `src/shared/` | 1 | 265 |
| batch-373 | P2 | `src/shared/tracking/` | 8 | 1,454 |
| batch-374 | P2 | `src/shared/tracking/` | 6 | 2,126 |
| batch-375 | P2 | `src/shared/ui/` | 1 | 2 |
| batch-376 | P2 | `src/shared/ui/audio_mixer/` | 4 | 2,627 |
| batch-377 | P2 | `src/shared/ui/audio_mixer/` | 8 | 2,028 |
| batch-378 | P2 | `src/shared/ui/audio_mixer/` | 8 | 2,283 |
| batch-379 | P2 | `src/shared/ui/audio_mixer/` | 1 | 1,919 |
| batch-380 | P2 | `src/shared/ui/` | 8 | 2,858 |
| batch-381 | P2 | `src/shared/ui/` | 3 | 2,130 |
| batch-382 | P2 | `src/shared/ui/widgets/` | 8 | 2,300 |
| batch-383 | P2 | `src/shared/ui/widgets/` | 8 | 2,642 |
| batch-384 | P2 | `src/shared/ui/widgets/` | 4 | 2,223 |
| batch-385 | P2 | `src/shared/utils/` | 2 | 102 |
| batch-386 | P2 | `src/shared/vault/` | 3 | 392 |
| batch-387 | P2 | `src/shared/video/` | 5 | 1,787 |
| batch-388 | P2 | `src/shared/video/` | 1 | 1,526 |
| batch-389 | P2 | `src/shared/video/denoise/` | 4 | 829 |
| batch-390 | P2 | `src/shared/video/` | 3 | 1,110 |
| batch-391 | P2 | `src/shared/video/` | 1 | 2,376 |
| batch-392 | P2 | `src/shared/video/` | 5 | 2,757 |
| batch-393 | P2 | `src/shared/video/` | 4 | 2,739 |
| batch-394 | P2 | `src/shared/video/` | 4 | 2,639 |
| batch-395 | P2 | `src/shared/video/` | 1 | 1,052 |
| batch-396 | P2 | `src/shared/video/` | 1 | 4,982 |
| batch-397 | P2 | `src/shared/video/` | 3 | 1,610 |
| batch-398 | P2 | `src/shared/video/` | 1 | 3,294 |

## 批次明细

<details><summary>batch-001 [P0] <code>src/features/color/core/</code>（1 文件，0 行）</summary>

- `src/features/color/core/__init__.py` (0)

</details>

<details><summary>batch-002 [P0] <code>src/features/color/core/</code>（1 文件，6,669 行）</summary>

- `src/features/color/core/async_renderer.py` (6,669)

</details>

<details><summary>batch-003 [P0] <code>src/features/color/core/</code>（4 文件，2,498 行）</summary>

- `src/features/color/core/auto_color.py` (261)
- `src/features/color/core/cache_manager.py` (1,653)
- `src/features/color/core/color_manager.py` (497)
- `src/features/color/core/color_output.py` (87)

</details>

<details><summary>batch-004 [P0] <code>src/features/color/core/</code>（6 文件，2,884 行）</summary>

- `src/features/color/core/color_session.py` (735)
- `src/features/color/core/color_stack_render_service.py` (150)
- `src/features/color/core/composition_uniforms.py` (495)
- `src/features/color/core/delivery_matrix.py` (665)
- `src/features/color/core/denoise_engine.py` (662)
- `src/features/color/core/export_denoise_bridge.py` (177)

</details>

<details><summary>batch-005 [P0] <code>src/features/color/core/</code>（3 文件，709 行）</summary>

- `src/features/color/core/grade_clipboard.py` (217)
- `src/features/color/core/grade_keyframes.py` (337)
- `src/features/color/core/history.py` (155)

</details>

<details><summary>batch-006 [P0] <code>src/features/color/core/</code>（1 文件，4,730 行）</summary>

- `src/features/color/core/image_engine.py` (4,730)

</details>

<details><summary>batch-007 [P0] <code>src/features/color/core/</code>（4 文件，2,769 行）</summary>

- `src/features/color/core/layer.py` (1,209)
- `src/features/color/core/lut_exporter.py` (1,111)
- `src/features/color/core/msl_composition.py` (249)
- `src/features/color/core/opengl_render_backend.py` (200)

</details>

<details><summary>batch-008 [P0] <code>src/features/color/core/</code>（6 文件，2,629 行）</summary>

- `src/features/color/core/plugin_manager.py` (567)
- `src/features/color/core/preset_meta.py` (222)
- `src/features/color/core/project_dependencies.py` (300)
- `src/features/color/core/scope_types.py` (206)
- `src/features/color/core/shader_compiler.py` (430)
- `src/features/color/core/shader_nodes.py` (904)

</details>

<details><summary>batch-009 [P0] <code>src/features/color/core/</code>（6 文件，2,617 行）</summary>

- `src/features/color/core/shader_nodes_msl.py` (917)
- `src/features/color/core/the_decompiler.py` (541)
- `src/features/color/core/video_denoise_gl.py` (145)
- `src/features/color/core/video_denoise_pipeline.py` (218)
- `src/features/color/core/video_denoise_shaders.py` (559)
- `src/features/color/core/video_spatial_denoise.py` (237)

</details>

<details><summary>batch-010 [P0] <code>src/features/color/core/</code>（2 文件，817 行）</summary>

- `src/features/color/core/video_temporal_denoise.py` (520)
- `src/features/color/core/workspace_presets.py` (297)

</details>

<details><summary>batch-011 [P0] <code>src/shared/effects/</code>（2 文件，615 行）</summary>

- `src/shared/effects/__init__.py` (28)
- `src/shared/effects/preview_quality.py` (587)

</details>

<details><summary>batch-012 [P0] <code>src/shared/gpu/</code>（8 文件，1,562 行）</summary>

- `src/shared/gpu/__init__.py` (43)
- `src/shared/gpu/backend_selector.py` (231)
- `src/shared/gpu/backend_types.py` (179)
- `src/shared/gpu/capabilities.py` (464)
- `src/shared/gpu/composite_gpu_frame_handle.py` (215)
- `src/shared/gpu/gl_backend.py` (132)
- `src/shared/gpu/gl_context_hub.py` (120)
- `src/shared/gpu/gl_pass_timer.py` (178)

</details>

<details><summary>batch-013 [P0] <code>src/shared/gpu/</code>（4 文件，1,027 行）</summary>

- `src/shared/gpu/gl_resource_registry.py` (156)
- `src/shared/gpu/gpu_scope_engine.py` (662)
- `src/shared/gpu/graphics_backend.py` (120)
- `src/shared/gpu/host_gpu_info.py` (89)

</details>

<details><summary>batch-014 [P0] <code>src/shared/gpu/metal/</code>（8 文件，2,607 行）</summary>

- `src/shared/gpu/metal/__init__.py` (17)
- `src/shared/gpu/metal/context.py` (541)
- `src/shared/gpu/metal/kernels.py` (355)
- `src/shared/gpu/metal/kernels_color.py` (584)
- `src/shared/gpu/metal/kernels_dct.py` (309)
- `src/shared/gpu/metal/kernels_dec.py` (550)
- `src/shared/gpu/metal/kernels_enc.py` (92)
- `src/shared/gpu/metal/kernels_histogram.py` (159)

</details>

<details><summary>batch-015 [P0] <code>src/shared/gpu/metal/</code>（3 文件，1,508 行）</summary>

- `src/shared/gpu/metal/kernels_idct.py` (1,195)
- `src/shared/gpu/metal/kernels_video.py` (174)
- `src/shared/gpu/metal/submission_scheduler.py` (139)

</details>

<details><summary>batch-016 [P0] <code>src/shared/gpu/</code>（1 文件，464 行）</summary>

- `src/shared/gpu/present_frame.py` (464)

</details>

<details><summary>batch-017 [P0] <code>src/shared/render/</code>（2 文件，41 行）</summary>

- `src/shared/render/__init__.py` (10)
- `src/shared/render/source.py` (31)

</details>

<details><summary>batch-018 [P1] <code>src/app/</code>（2 文件，44 行）</summary>

- `src/app/__init__.py` (5)
- `src/app/app_context.py` (39)

</details>

<details><summary>batch-019 [P1] <code>src/app/controllers/</code>（2 文件，101 行）</summary>

- `src/app/controllers/__init__.py` (1)
- `src/app/controllers/agent_v2_chat_worker.py` (100)

</details>

<details><summary>batch-020 [P1] <code>src/app/controllers/</code>（1 文件，6,424 行）</summary>

- `src/app/controllers/ai_edit_controller.py` (6,424)

</details>

<details><summary>batch-021 [P1] <code>src/app/controllers/</code>（2 文件，2,209 行）</summary>

- `src/app/controllers/ai_edit_host_bridge.py` (445)
- `src/app/controllers/ai_edit_host_bridge_impl.py` (1,764)

</details>

<details><summary>batch-022 [P1] <code>src/app/controllers/</code>（2 文件，2,261 行）</summary>

- `src/app/controllers/ai_editing_mixin.py` (1,902)
- `src/app/controllers/ai_workflow_composite_render.py` (359)

</details>

<details><summary>batch-023 [P1] <code>src/app/controllers/</code>（2 文件，2,098 行）</summary>

- `src/app/controllers/color_controller.py` (1,841)
- `src/app/controllers/color_workspace_controller.py` (257)

</details>

<details><summary>batch-024 [P1] <code>src/app/controllers/</code>（1 文件，1,352 行）</summary>

- `src/app/controllers/composite_controller.py` (1,352)

</details>

<details><summary>batch-025 [P1] <code>src/app/controllers/</code>（1 文件，1,901 行）</summary>

- `src/app/controllers/deliver_controller.py` (1,901)

</details>

<details><summary>batch-026 [P1] <code>src/app/controllers/</code>（1 文件，3,092 行）</summary>

- `src/app/controllers/dock_layout_controller.py` (3,092)

</details>

<details><summary>batch-027 [P1] <code>src/app/controllers/</code>（1 文件，9,861 行）</summary>

- `src/app/controllers/edit_controller.py` (9,861)

</details>

<details><summary>batch-028 [P1] <code>src/app/controllers/</code>（1 文件，7,313 行）</summary>

- `src/app/controllers/file_controller.py` (7,313)

</details>

<details><summary>batch-029 [P1] <code>src/app/controllers/</code>（3 文件，2,489 行）</summary>

- `src/app/controllers/host_help_sources.py` (304)
- `src/app/controllers/layer_controller.py` (1,910)
- `src/app/controllers/media_analysis_controller.py` (275)

</details>

<details><summary>batch-030 [P1] <code>src/app/controllers/</code>（1 文件，1,883 行）</summary>

- `src/app/controllers/menu_toolbar_controller.py` (1,883)

</details>

<details><summary>batch-031 [P1] <code>src/app/controllers/</code>（1 文件，3,600 行）</summary>

- `src/app/controllers/page_controller.py` (3,600)

</details>

<details><summary>batch-032 [P1] <code>src/app/</code>（2 文件，242 行）</summary>

- `src/app/feature_flags.py` (21)
- `src/app/feature_registry.py` (221)

</details>

<details><summary>batch-033 [P1] <code>src/app/integrations/</code>（1 文件，0 行）</summary>

- `src/app/integrations/__init__.py` (0)

</details>

<details><summary>batch-034 [P1] <code>src/app/integrations/file_explorer/</code>（3 文件，745 行）</summary>

- `src/app/integrations/file_explorer/__init__.py` (0)
- `src/app/integrations/file_explorer/adapter.py` (280)
- `src/app/integrations/file_explorer/edit_benchmark_service.py` (465)

</details>

<details><summary>batch-035 [P1] <code>src/app/</code>（1 文件，2,008 行）</summary>

- `src/app/main_window.py` (2,008)

</details>

<details><summary>batch-036 [P1] <code>src/app/menu/</code>（2 文件，267 行）</summary>

- `src/app/menu/__init__.py` (2)
- `src/app/menu/menu_builder.py` (265)

</details>

<details><summary>batch-037 [P1] <code>src/app/</code>（2 文件，284 行）</summary>

- `src/app/module_loader.py` (181)
- `src/app/page_contract.py` (103)

</details>

<details><summary>batch-038 [P1] <code>src/app/ui/</code>（6 文件，1,244 行）</summary>

- `src/app/ui/__init__.py` (27)
- `src/app/ui/audio_mixer.py` (5)
- `src/app/ui/files_panel.py` (409)
- `src/app/ui/info_panel.py` (176)
- `src/app/ui/library_adapter.py` (15)
- `src/app/ui/library_panel.py` (612)

</details>

<details><summary>batch-039 [P1] <code>src/app/ui/</code>（1 文件，2,685 行）</summary>

- `src/app/ui/preferences_dialog.py` (2,685)

</details>

<details><summary>batch-040 [P1] <code>src/app/ui/</code>（3 文件，2,770 行）</summary>

- `src/app/ui/project_manager_dialog.py` (1,107)
- `src/app/ui/resize_dialog.py` (69)
- `src/app/ui/script_panel.py` (1,594)

</details>

<details><summary>batch-041 [P1] <code>src/app/ui/</code>（2 文件，1,318 行）</summary>

- `src/app/ui/shortcut_dialog.py` (865)
- `src/app/ui/welcome_dialog.py` (453)

</details>

<details><summary>batch-042 [P1] <code>src/app/</code>（1 文件，94 行）</summary>

- `src/app/update_checker.py` (94)

</details>

<details><summary>batch-043 [P1] <code>src/features/composite/</code>（1 文件，1 行）</summary>

- `src/features/composite/__init__.py` (1)

</details>

<details><summary>batch-044 [P1] <code>src/features/composite/adapters/</code>（3 文件，774 行）</summary>

- `src/features/composite/adapters/__init__.py` (5)
- `src/features/composite/adapters/caption_input.py` (35)
- `src/features/composite/adapters/main_window_adapter.py` (734)

</details>

<details><summary>batch-045 [P1] <code>src/features/composite/ai/</code>（2 文件，348 行）</summary>

- `src/features/composite/ai/__init__.py` (0)
- `src/features/composite/ai/composite_plan.py` (348)

</details>

<details><summary>batch-046 [P1] <code>src/features/composite/</code>（1 文件，268 行）</summary>

- `src/features/composite/bake_service.py` (268)

</details>

<details><summary>batch-047 [P1] <code>src/features/composite/core/</code>（8 文件，2,397 行）</summary>

- `src/features/composite/core/__init__.py` (1)
- `src/features/composite/core/asset_dependency.py` (860)
- `src/features/composite/core/color_space.py` (70)
- `src/features/composite/core/eval_context.py` (157)
- `src/features/composite/core/format.py` (330)
- `src/features/composite/core/frame_sequence.py` (39)
- `src/features/composite/core/gl_bind.py` (57)
- `src/features/composite/core/graph_migration.py` (883)

</details>

<details><summary>batch-048 [P1] <code>src/features/composite/core/</code>（5 文件，2,686 行）</summary>

- `src/features/composite/core/graph_preflight.py` (1,199)
- `src/features/composite/core/graph_snapshot.py` (184)
- `src/features/composite/core/image_loader.py` (753)
- `src/features/composite/core/layer_model.py` (321)
- `src/features/composite/core/layer_provenance.py` (229)

</details>

<details><summary>batch-049 [P1] <code>src/features/composite/core/</code>（2 文件，2,678 行）</summary>

- `src/features/composite/core/layer_stack_compiler.py` (702)
- `src/features/composite/core/layer_stack_controller.py` (1,976)

</details>

<details><summary>batch-050 [P1] <code>src/features/composite/core/</code>（3 文件，2,204 行）</summary>

- `src/features/composite/core/layer_stack_validator.py` (338)
- `src/features/composite/core/media_drop_payload.py` (346)
- `src/features/composite/core/node_capabilities.py` (1,520)

</details>

<details><summary>batch-051 [P1] <code>src/features/composite/core/</code>（1 文件，2,810 行）</summary>

- `src/features/composite/core/node_engine.py` (2,810)

</details>

<details><summary>batch-052 [P1] <code>src/features/composite/core/</code>（5 文件，1,115 行）</summary>

- `src/features/composite/core/node_plugin.py` (565)
- `src/features/composite/core/output_targets.py` (164)
- `src/features/composite/core/quality_profile.py` (61)
- `src/features/composite/core/runtime_context.py` (139)
- `src/features/composite/core/runtime_diagnostics.py` (186)

</details>

<details><summary>batch-053 [P1] <code>src/features/composite/core/</code>（1 文件，7,113 行）</summary>

- `src/features/composite/core/shaders.py` (7,113)

</details>

<details><summary>batch-054 [P1] <code>src/features/composite/core/</code>（4 文件，2,184 行）</summary>

- `src/features/composite/core/tile_padding_computer.py` (494)
- `src/features/composite/core/tile_scheduler.py` (116)
- `src/features/composite/core/time_remap.py` (385)
- `src/features/composite/core/undo_stack.py` (1,189)

</details>

<details><summary>batch-055 [P1] <code>src/features/composite/core/</code>（1 文件，1,105 行）</summary>

- `src/features/composite/core/video_context.py` (1,105)

</details>

<details><summary>batch-056 [P1] <code>src/features/composite/</code>（1 文件，1,020 行）</summary>

- `src/features/composite/edit_service.py` (1,020)

</details>

<details><summary>batch-057 [P1] <code>src/features/composite/evaluators/</code>（2 文件，108 行）</summary>

- `src/features/composite/evaluators/__init__.py` (67)
- `src/features/composite/evaluators/ai_evaluator.py` (41)

</details>

<details><summary>batch-058 [P1] <code>src/features/composite/evaluators/</code>（1 文件，3,444 行）</summary>

- `src/features/composite/evaluators/base_evaluator.py` (3,444)

</details>

<details><summary>batch-059 [P1] <code>src/features/composite/evaluators/</code>（5 文件，1,424 行）</summary>

- `src/features/composite/evaluators/blur_evaluator.py` (420)
- `src/features/composite/evaluators/cache_evaluator.py` (192)
- `src/features/composite/evaluators/channel_evaluator.py` (252)
- `src/features/composite/evaluators/color_evaluator.py` (531)
- `src/features/composite/evaluators/expression_evaluator.py` (29)

</details>

<details><summary>batch-060 [P1] <code>src/features/composite/evaluators/</code>（4 文件，2,676 行）</summary>

- `src/features/composite/evaluators/filter_evaluator.py` (1,717)
- `src/features/composite/evaluators/graph_evaluator_mixin.py` (98)
- `src/features/composite/evaluators/histogram_evaluator.py` (29)
- `src/features/composite/evaluators/histogram_manager.py` (832)

</details>

<details><summary>batch-061 [P1] <code>src/features/composite/evaluators/</code>（6 文件，1,831 行）</summary>

- `src/features/composite/evaluators/keyer_evaluator.py` (544)
- `src/features/composite/evaluators/keyframe_evaluator.py` (31)
- `src/features/composite/evaluators/merge_evaluator.py` (311)
- `src/features/composite/evaluators/shader_library.py` (345)
- `src/features/composite/evaluators/tile_evaluator.py` (160)
- `src/features/composite/evaluators/transform_evaluator.py` (440)

</details>

<details><summary>batch-062 [P1] <code>src/features/composite/</code>（5 文件，1,375 行）</summary>

- `src/features/composite/export_provider.py` (161)
- `src/features/composite/facade.py` (218)
- `src/features/composite/face_workflow.py` (144)
- `src/features/composite/ml_roto.py` (319)
- `src/features/composite/module.py` (533)

</details>

<details><summary>batch-063 [P1] <code>src/features/composite/nodes/</code>（8 文件，614 行）</summary>

- `src/features/composite/nodes/__init__.py` (97)
- `src/features/composite/nodes/_gpu_readback.py` (71)
- `src/features/composite/nodes/ai_denoise_node.py` (24)
- `src/features/composite/nodes/ai_expand_node.py` (26)
- `src/features/composite/nodes/ai_gen_expand_node.py` (248)
- `src/features/composite/nodes/ai_generate_node.py` (31)
- `src/features/composite/nodes/ai_inpaint_node.py` (29)
- `src/features/composite/nodes/ai_remove_node.py` (88)

</details>

<details><summary>batch-064 [P1] <code>src/features/composite/nodes/</code>（8 文件，2,945 行）</summary>

- `src/features/composite/nodes/base_node.py` (1,325)
- `src/features/composite/nodes/blend_node.py` (313)
- `src/features/composite/nodes/blur_node.py` (382)
- `src/features/composite/nodes/camera3d_node.py` (351)
- `src/features/composite/nodes/channel_merge_node.py` (221)
- `src/features/composite/nodes/chroma_keyer_node.py` (127)
- `src/features/composite/nodes/chromatic_aberration_node.py` (121)
- `src/features/composite/nodes/clamp_node.py` (105)

</details>

<details><summary>batch-065 [P1] <code>src/features/composite/nodes/</code>（8 文件，1,996 行）</summary>

- `src/features/composite/nodes/color_correct_node.py` (165)
- `src/features/composite/nodes/color_node.py` (116)
- `src/features/composite/nodes/compare_node.py` (133)
- `src/features/composite/nodes/constant_node.py` (680)
- `src/features/composite/nodes/copy_channel_node.py` (96)
- `src/features/composite/nodes/cornerpin_node.py` (343)
- `src/features/composite/nodes/crop_node.py` (175)
- `src/features/composite/nodes/cryptomatte_select_node.py` (288)

</details>

<details><summary>batch-066 [P1] <code>src/features/composite/nodes/</code>（8 文件，1,930 行）</summary>

- `src/features/composite/nodes/cs_transform_node.py` (91)
- `src/features/composite/nodes/deep_merge_node.py` (223)
- `src/features/composite/nodes/deflicker_node.py` (601)
- `src/features/composite/nodes/defocus_node.py` (170)
- `src/features/composite/nodes/deformer3d_node.py` (490)
- `src/features/composite/nodes/denoise_node.py` (161)
- `src/features/composite/nodes/depth_common.py` (79)
- `src/features/composite/nodes/depth_matte_node.py` (115)

</details>

<details><summary>batch-067 [P1] <code>src/features/composite/nodes/</code>（8 文件，1,382 行）</summary>

- `src/features/composite/nodes/difference_keyer_node.py` (151)
- `src/features/composite/nodes/dissolve_node.py` (154)
- `src/features/composite/nodes/dot_node.py` (61)
- `src/features/composite/nodes/edge_detect_node.py` (156)
- `src/features/composite/nodes/erode_dilate_node.py` (134)
- `src/features/composite/nodes/face_smooth_node.py` (114)
- `src/features/composite/nodes/face_track_node.py` (130)
- `src/features/composite/nodes/file_output_node.py` (482)

</details>

<details><summary>batch-068 [P1] <code>src/features/composite/nodes/</code>（8 文件，1,660 行）</summary>

- `src/features/composite/nodes/fog_node.py` (119)
- `src/features/composite/nodes/frame_hold_node.py` (92)
- `src/features/composite/nodes/frame_interpolate_node.py` (264)
- `src/features/composite/nodes/geo3d_node.py` (326)
- `src/features/composite/nodes/glow_node.py` (148)
- `src/features/composite/nodes/godrays_node.py` (151)
- `src/features/composite/nodes/grade_node.py` (155)
- `src/features/composite/nodes/group_node.py` (405)

</details>

<details><summary>batch-069 [P1] <code>src/features/composite/nodes/</code>（8 文件，1,978 行）</summary>

- `src/features/composite/nodes/huecorrect_node.py` (129)
- `src/features/composite/nodes/image_input_node.py` (617)
- `src/features/composite/nodes/invert_node.py` (86)
- `src/features/composite/nodes/keyer_node.py` (278)
- `src/features/composite/nodes/lens_distortion_node.py` (201)
- `src/features/composite/nodes/lens_flare_node.py` (169)
- `src/features/composite/nodes/light3d_node.py` (372)
- `src/features/composite/nodes/light_wrap_node.py` (126)

</details>

<details><summary>batch-070 [P1] <code>src/features/composite/nodes/</code>（8 文件，2,072 行）</summary>

- `src/features/composite/nodes/luma_keyer_node.py` (109)
- `src/features/composite/nodes/lut_node.py` (180)
- `src/features/composite/nodes/mask_merge_node.py` (141)
- `src/features/composite/nodes/mask_node.py` (186)
- `src/features/composite/nodes/matte_cleanup_node.py` (133)
- `src/features/composite/nodes/matte_node.py` (139)
- `src/features/composite/nodes/model3d_node.py` (1,162)
- `src/features/composite/nodes/node_state.py` (22)

</details>

<details><summary>batch-071 [P1] <code>src/features/composite/nodes/</code>（8 文件，2,370 行）</summary>

- `src/features/composite/nodes/noise_node.py` (127)
- `src/features/composite/nodes/noop_node.py` (67)
- `src/features/composite/nodes/normalize_node.py` (160)
- `src/features/composite/nodes/output_node.py` (797)
- `src/features/composite/nodes/paint_node.py` (250)
- `src/features/composite/nodes/particle_node.py` (760)
- `src/features/composite/nodes/placeholder_node.py` (28)
- `src/features/composite/nodes/pointcloud3d_node.py` (181)

</details>

<details><summary>batch-072 [P1] <code>src/features/composite/nodes/</code>（8 文件，1,364 行）</summary>

- `src/features/composite/nodes/precomp_node.py` (59)
- `src/features/composite/nodes/premult_node.py` (58)
- `src/features/composite/nodes/ramp_node.py` (237)
- `src/features/composite/nodes/reformat_node.py` (299)
- `src/features/composite/nodes/relight_node.py` (189)
- `src/features/composite/nodes/remove_channels_node.py` (106)
- `src/features/composite/nodes/retime_node.py` (124)
- `src/features/composite/nodes/roto_shape_node.py` (292)

</details>

<details><summary>batch-073 [P1] <code>src/features/composite/nodes/</code>（1 文件，5,253 行）</summary>

- `src/features/composite/nodes/scene3d_node.py` (5,253)

</details>

<details><summary>batch-074 [P1] <code>src/features/composite/nodes/</code>（8 文件，2,524 行）</summary>

- `src/features/composite/nodes/set_alpha_node.py` (105)
- `src/features/composite/nodes/sharpen_node.py` (119)
- `src/features/composite/nodes/shuffle_node.py` (97)
- `src/features/composite/nodes/switch_node.py` (145)
- `src/features/composite/nodes/text_node.py` (1,709)
- `src/features/composite/nodes/time_offset_node.py` (93)
- `src/features/composite/nodes/timeline_clip_input_node.py` (163)
- `src/features/composite/nodes/topos_color_node.py` (93)

</details>

<details><summary>batch-075 [P1] <code>src/features/composite/nodes/</code>（4 文件，721 行）</summary>

- `src/features/composite/nodes/tracker_node.py` (151)
- `src/features/composite/nodes/transform_node.py` (392)
- `src/features/composite/nodes/unknown_placeholder_node.py` (120)
- `src/features/composite/nodes/unpremult_node.py` (58)

</details>

<details><summary>batch-076 [P1] <code>src/features/composite/nodes/</code>（2 文件，2,915 行）</summary>

- `src/features/composite/nodes/video_frame_node.py` (2,805)
- `src/features/composite/nodes/vignette_node.py` (110)

</details>

<details><summary>batch-077 [P1] <code>src/features/composite/nodes/</code>（3 文件，605 行）</summary>

- `src/features/composite/nodes/volume3d_node.py` (201)
- `src/features/composite/nodes/warp_node.py` (263)
- `src/features/composite/nodes/zblur_node.py` (141)

</details>

<details><summary>batch-078 [P1] <code>src/features/composite/</code>（1 文件，397 行）</summary>

- `src/features/composite/preferences.py` (397)

</details>

<details><summary>batch-079 [P1] <code>src/features/composite/render/</code>（4 文件，2,415 行）</summary>

- `src/features/composite/render/__init__.py` (1)
- `src/features/composite/render/ai_cache_manager.py` (268)
- `src/features/composite/render/ai_compute_manager.py` (1,275)
- `src/features/composite/render/cache_manager.py` (871)

</details>

<details><summary>batch-080 [P1] <code>src/features/composite/render/</code>（3 文件，2,559 行）</summary>

- `src/features/composite/render/composite_export_renderer.py` (1,521)
- `src/features/composite/render/composite_frame_renderer.py` (777)
- `src/features/composite/render/composite_graph_render_service.py` (261)

</details>

<details><summary>batch-081 [P1] <code>src/features/composite/render/</code>（2 文件，1,526 行）</summary>

- `src/features/composite/render/composite_render_output.py` (1,240)
- `src/features/composite/render/composite_render_worker.py` (286)

</details>

<details><summary>batch-082 [P1] <code>src/features/composite/render/</code>（2 文件，2,879 行）</summary>

- `src/features/composite/render/composite_renderer.py` (2,200)
- `src/features/composite/render/compute_shader_manager.py` (679)

</details>

<details><summary>batch-083 [P1] <code>src/features/composite/render/</code>（5 文件，2,999 行）</summary>

- `src/features/composite/render/disk_cache_store.py` (479)
- `src/features/composite/render/frame_trace.py` (163)
- `src/features/composite/render/gl_utils.py` (292)
- `src/features/composite/render/gpu_resource_pool.py` (1,869)
- `src/features/composite/render/metal_compute_bridge.py` (196)

</details>

<details><summary>batch-084 [P1] <code>src/features/composite/render/</code>（5 文件，2,882 行）</summary>

- `src/features/composite/render/parallel_frame_render.py` (262)
- `src/features/composite/render/pbo_readback.py` (583)
- `src/features/composite/render/prefetch_scheduler.py` (531)
- `src/features/composite/render/render_scheduler.py` (1,372)
- `src/features/composite/render/thumbnail_policy.py` (134)

</details>

<details><summary>batch-085 [P1] <code>src/features/composite/render/</code>（2 文件，454 行）</summary>

- `src/features/composite/render/topos_cache_codec.py` (285)
- `src/features/composite/render/tracker_analyze_worker.py` (169)

</details>

<details><summary>batch-086 [P1] <code>src/features/composite/render3d/</code>（5 文件，1,055 行）</summary>

- `src/features/composite/render3d/__init__.py` (10)
- `src/features/composite/render3d/animation.py` (327)
- `src/features/composite/render3d/aov.py` (461)
- `src/features/composite/render3d/camera_model.py` (101)
- `src/features/composite/render3d/cryptomatte.py` (156)

</details>

<details><summary>batch-087 [P1] <code>src/features/composite/render3d/deformers/</code>（3 文件，513 行）</summary>

- `src/features/composite/render3d/deformers/__init__.py` (42)
- `src/features/composite/render3d/deformers/base.py` (149)
- `src/features/composite/render3d/deformers/cpu.py` (322)

</details>

<details><summary>batch-088 [P1] <code>src/features/composite/render3d/</code>（7 文件，2,974 行）</summary>

- `src/features/composite/render3d/fields.py` (165)
- `src/features/composite/render3d/format_capabilities.py` (798)
- `src/features/composite/render3d/frustum.py` (183)
- `src/features/composite/render3d/gizmo_overlay.py` (656)
- `src/features/composite/render3d/gizmo_renderer.py` (365)
- `src/features/composite/render3d/hdri.py` (705)
- `src/features/composite/render3d/ies_profile.py` (102)

</details>

<details><summary>batch-089 [P1] <code>src/features/composite/render3d/</code>（8 文件，2,843 行）</summary>

- `src/features/composite/render3d/lod_builder.py` (261)
- `src/features/composite/render3d/material_model.py` (668)
- `src/features/composite/render3d/material_state.py` (156)
- `src/features/composite/render3d/material_texture_loader.py` (281)
- `src/features/composite/render3d/math_utils.py` (439)
- `src/features/composite/render3d/mesh_diagnostics.py` (247)
- `src/features/composite/render3d/mesh_picking.py` (650)
- `src/features/composite/render3d/mesh_vbo_pool.py` (141)

</details>

<details><summary>batch-090 [P1] <code>src/features/composite/render3d/</code>（5 文件，2,826 行）</summary>

- `src/features/composite/render3d/model_import_cache.py` (390)
- `src/features/composite/render3d/model_import_report.py` (152)
- `src/features/composite/render3d/model_import_service.py` (426)
- `src/features/composite/render3d/model_loader.py` (1,741)
- `src/features/composite/render3d/morph.py` (117)

</details>

<details><summary>batch-091 [P1] <code>src/features/composite/render3d/</code>（8 文件，2,976 行）</summary>

- `src/features/composite/render3d/node_icon_overlay.py` (726)
- `src/features/composite/render3d/perf_stats.py` (264)
- `src/features/composite/render3d/point_cloud.py` (191)
- `src/features/composite/render3d/primitives.py` (645)
- `src/features/composite/render3d/procedural_hdri.py` (79)
- `src/features/composite/render3d/raycast.py` (193)
- `src/features/composite/render3d/scene_graph.py` (599)
- `src/features/composite/render3d/scene_hierarchy.py` (279)

</details>

<details><summary>batch-092 [P1] <code>src/features/composite/render3d/</code>（7 文件，1,986 行）</summary>

- `src/features/composite/render3d/scene_payload.py` (421)
- `src/features/composite/render3d/selection.py` (335)
- `src/features/composite/render3d/skeleton.py` (413)
- `src/features/composite/render3d/skeleton_renderer.py` (239)
- `src/features/composite/render3d/skinning.py` (211)
- `src/features/composite/render3d/viewport_overlays.py` (267)
- `src/features/composite/render3d/volume_grid.py` (100)

</details>

<details><summary>batch-093 [P1] <code>src/features/composite/</code>（1 文件，240 行）</summary>

- `src/features/composite/runtime.py` (240)

</details>

<details><summary>batch-094 [P1] <code>src/features/composite/ui/</code>（3 文件，1,031 行）</summary>

- `src/features/composite/ui/__init__.py` (38)
- `src/features/composite/ui/asset_dependency_panel.py` (199)
- `src/features/composite/ui/backdrop_item.py` (794)

</details>

<details><summary>batch-095 [P1] <code>src/features/composite/ui/</code>（1 文件，10,083 行）</summary>

- `src/features/composite/ui/composite_page.py` (10,083)

</details>

<details><summary>batch-096 [P1] <code>src/features/composite/ui/</code>（2 文件，608 行）</summary>

- `src/features/composite/ui/composite_render_dialog.py` (552)
- `src/features/composite/ui/composite_scope_widgets.py` (56)

</details>

<details><summary>batch-097 [P1] <code>src/features/composite/ui/</code>（1 文件，11,512 行）</summary>

- `src/features/composite/ui/composite_viewport.py` (11,512)

</details>

<details><summary>batch-098 [P1] <code>src/features/composite/ui/</code>（2 文件，2,849 行）</summary>

- `src/features/composite/ui/connection_item.py` (1,004)
- `src/features/composite/ui/graph_editor_view.py` (1,845)

</details>

<details><summary>batch-099 [P1] <code>src/features/composite/ui/</code>（7 文件，2,953 行）</summary>

- `src/features/composite/ui/graph_expression_editor.py` (322)
- `src/features/composite/ui/graph_layout.py` (472)
- `src/features/composite/ui/graph_model.py` (958)
- `src/features/composite/ui/graph_navigation.py` (213)
- `src/features/composite/ui/graph_node_search_popup.py` (476)
- `src/features/composite/ui/graph_view.py` (198)
- `src/features/composite/ui/group_commands.py` (314)

</details>

<details><summary>batch-100 [P1] <code>src/features/composite/ui/</code>（3 文件，2,349 行）</summary>

- `src/features/composite/ui/import_report_panel.py` (438)
- `src/features/composite/ui/keyframe_editor.py` (911)
- `src/features/composite/ui/keyframes_model.py` (1,000)

</details>

<details><summary>batch-101 [P1] <code>src/features/composite/ui/</code>（1 文件，3,041 行）</summary>

- `src/features/composite/ui/keyframes_panel.py` (3,041)

</details>

<details><summary>batch-102 [P1] <code>src/features/composite/ui/</code>（1 文件，1,880 行）</summary>

- `src/features/composite/ui/keyframes_timeline_view.py` (1,880)

</details>

<details><summary>batch-103 [P1] <code>src/features/composite/ui/</code>（3 文件，2,437 行）</summary>

- `src/features/composite/ui/keyframes_tree_view.py` (1,683)
- `src/features/composite/ui/material_panel.py` (387)
- `src/features/composite/ui/minimap_widget.py` (367)

</details>

<details><summary>batch-104 [P1] <code>src/features/composite/ui/</code>（1 文件，1,508 行）</summary>

- `src/features/composite/ui/node_editor_actions.py` (1,508)

</details>

<details><summary>batch-105 [P1] <code>src/features/composite/ui/</code>（1 文件，5,128 行）</summary>

- `src/features/composite/ui/node_editor_widget.py` (5,128)

</details>

<details><summary>batch-106 [P1] <code>src/features/composite/ui/</code>（2 文件，1,675 行）</summary>

- `src/features/composite/ui/node_graph_commands.py` (843)
- `src/features/composite/ui/node_item.py` (832)

</details>

<details><summary>batch-107 [P1] <code>src/features/composite/ui/</code>（1 文件，7,683 行）</summary>

- `src/features/composite/ui/node_properties_panel.py` (7,683)

</details>

<details><summary>batch-108 [P1] <code>src/features/composite/ui/</code>（8 文件，2,719 行）</summary>

- `src/features/composite/ui/node_search_popup.py` (676)
- `src/features/composite/ui/node_thumbnail.py` (543)
- `src/features/composite/ui/perf_panel.py` (230)
- `src/features/composite/ui/port_item.py` (200)
- `src/features/composite/ui/preview_connection_item.py` (158)
- `src/features/composite/ui/project_settings_section.py` (183)
- `src/features/composite/ui/project_settings_widget.py` (402)
- `src/features/composite/ui/scene_outliner_panel.py` (327)

</details>

<details><summary>batch-109 [P1] <code>src/features/composite/ui/</code>（4 文件，2,861 行）</summary>

- `src/features/composite/ui/spline_commands.py` (85)
- `src/features/composite/ui/spline_model.py` (492)
- `src/features/composite/ui/spline_panel.py` (741)
- `src/features/composite/ui/spline_view.py` (1,543)

</details>

<details><summary>batch-110 [P1] <code>src/features/composite/ui/</code>（5 文件，2,823 行）</summary>

- `src/features/composite/ui/sticky_note_item.py` (667)
- `src/features/composite/ui/timeline_commands.py` (951)
- `src/features/composite/ui/timeline_expression.py` (360)
- `src/features/composite/ui/timeline_expression_editor.py` (300)
- `src/features/composite/ui/timeline_icons.py` (545)

</details>

<details><summary>batch-111 [P1] <code>src/features/composite/ui/</code>（5 文件，2,129 行）</summary>

- `src/features/composite/ui/timeline_layout.py` (407)
- `src/features/composite/ui/timeline_session.py` (348)
- `src/features/composite/ui/timeline_shortcuts.py` (438)
- `src/features/composite/ui/timeline_time_display.py` (344)
- `src/features/composite/ui/timeline_view_model.py` (592)

</details>

<details><summary>batch-112 [P1] <code>src/features/composite/ui/</code>（2 文件，1,253 行）</summary>

- `src/features/composite/ui/viewer3d_settings_section.py` (1,014)
- `src/features/composite/ui/viewer_handle_drawing.py` (239)

</details>

<details><summary>batch-113 [P1] <code>src/features/composite/ui/</code>（1 文件，2,087 行）</summary>

- `src/features/composite/ui/viewer_handles.py` (2,087)

</details>

<details><summary>batch-114 [P1] <code>src/features/composite/ui/</code>（4 文件，2,387 行）</summary>

- `src/features/composite/ui/viewer_tool_system.py` (1,683)
- `src/features/composite/ui/viewport3d_controller.py` (152)
- `src/features/composite/ui/viewport3d_state.py` (382)
- `src/features/composite/ui/viewport3d_toolbar.py` (170)

</details>

<details><summary>batch-115 [P1] <code>src/features/composite/utils/</code>（7 文件，2,770 行）</summary>

- `src/features/composite/utils/__init__.py` (1)
- `src/features/composite/utils/expression_parser.py` (1,290)
- `src/features/composite/utils/keyframe.py` (45)
- `src/features/composite/utils/pass_fusion.py` (529)
- `src/features/composite/utils/path_versioning.py` (100)
- `src/features/composite/utils/perf_monitor.py` (422)
- `src/features/composite/utils/preset_manager.py` (383)

</details>

<details><summary>batch-116 [P1] <code>src/features/composite/utils/</code>（1 文件，545 行）</summary>

- `src/features/composite/utils/serialization_formats.py` (545)

</details>

<details><summary>batch-117 [P1] <code>src/features/deliver/</code>（1 文件，3 行）</summary>

- `src/features/deliver/__init__.py` (3)

</details>

<details><summary>batch-118 [P1] <code>src/features/deliver/adapters/</code>（1 文件，7 行）</summary>

- `src/features/deliver/adapters/__init__.py` (7)

</details>

<details><summary>batch-119 [P1] <code>src/features/deliver/core/</code>（8 文件，2,977 行）</summary>

- `src/features/deliver/core/__init__.py` (0)
- `src/features/deliver/core/custom_export_presets.py` (275)
- `src/features/deliver/core/delivery_capability.py` (139)
- `src/features/deliver/core/export_queue.py` (434)
- `src/features/deliver/core/video_batch_export_controller.py` (295)
- `src/features/deliver/core/video_export_formats.py` (95)
- `src/features/deliver/core/video_export_job.py` (976)
- `src/features/deliver/core/video_export_preflight.py` (763)

</details>

<details><summary>batch-120 [P1] <code>src/features/deliver/core/</code>（2 文件，731 行）</summary>

- `src/features/deliver/core/video_export_presets.py` (368)
- `src/features/deliver/core/video_job_runner.py` (363)

</details>

<details><summary>batch-121 [P1] <code>src/features/deliver/</code>（4 文件，749 行）</summary>

- `src/features/deliver/facade.py` (38)
- `src/features/deliver/module.py` (332)
- `src/features/deliver/preferences.py` (278)
- `src/features/deliver/srt_exporter.py` (101)

</details>

<details><summary>batch-122 [P1] <code>src/features/deliver/ui/</code>（4 文件，2,013 行）</summary>

- `src/features/deliver/ui/__init__.py` (0)
- `src/features/deliver/ui/deliver_page.py` (1,173)
- `src/features/deliver/ui/deliver_workspace.py` (572)
- `src/features/deliver/ui/detachable_panel_host.py` (268)

</details>

<details><summary>batch-123 [P1] <code>src/features/deliver/ui/</code>（1 文件，1,134 行）</summary>

- `src/features/deliver/ui/video_batch_export_panel.py` (1,134)

</details>

<details><summary>batch-124 [P1] <code>src/features/deliver/ui/</code>（1 文件，2,954 行）</summary>

- `src/features/deliver/ui/video_export_settings_panel.py` (2,954)

</details>

<details><summary>batch-125 [P1] <code>src/features/edit/</code>（1 文件，3 行）</summary>

- `src/features/edit/__init__.py` (3)

</details>

<details><summary>batch-126 [P1] <code>src/features/edit/adapters/</code>（5 文件，1,139 行）</summary>

- `src/features/edit/adapters/__init__.py` (7)
- `src/features/edit/adapters/fcpxml_adapter.py` (443)
- `src/features/edit/adapters/import_builder.py` (289)
- `src/features/edit/adapters/interchange_types.py` (157)
- `src/features/edit/adapters/otio_adapter.py` (243)

</details>

<details><summary>batch-127 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,779 行）</summary>

- `src/features/edit/ai_editing/__init__.py` (142)
- `src/features/edit/ai_editing/action_contract.py` (237)
- `src/features/edit/ai_editing/agent_feature_flags.py` (93)
- `src/features/edit/ai_editing/agent_inbox.py` (257)
- `src/features/edit/ai_editing/agent_metrics.py` (238)
- `src/features/edit/ai_editing/agent_models.py` (897)
- `src/features/edit/ai_editing/agent_orchestrator.py` (697)
- `src/features/edit/ai_editing/agent_phase_presenter.py` (218)

</details>

<details><summary>batch-128 [P1] <code>src/features/edit/ai_editing/</code>（6 文件，2,734 行）</summary>

- `src/features/edit/ai_editing/agent_presets.py` (119)
- `src/features/edit/ai_editing/agent_task_executor.py` (938)
- `src/features/edit/ai_editing/agent_task_prompts.py` (201)
- `src/features/edit/ai_editing/agent_tool_loop.py` (563)
- `src/features/edit/ai_editing/ai_provider_catalog.py` (332)
- `src/features/edit/ai_editing/analysis_models.py` (581)

</details>

<details><summary>batch-129 [P1] <code>src/features/edit/ai_editing/</code>（6 文件，2,596 行）</summary>

- `src/features/edit/ai_editing/analysis_request.py` (352)
- `src/features/edit/ai_editing/analysis_scheduler.py` (425)
- `src/features/edit/ai_editing/analysis_service.py` (871)
- `src/features/edit/ai_editing/approval_service.py` (358)
- `src/features/edit/ai_editing/artifact_models.py` (402)
- `src/features/edit/ai_editing/artifact_store.py` (188)

</details>

<details><summary>batch-130 [P1] <code>src/features/edit/ai_editing/</code>（7 文件，2,955 行）</summary>

- `src/features/edit/ai_editing/assistant_capability_router.py` (415)
- `src/features/edit/ai_editing/assistant_events.py` (189)
- `src/features/edit/ai_editing/assistant_modes.py` (106)
- `src/features/edit/ai_editing/assistant_plugins.py` (457)
- `src/features/edit/ai_editing/assistant_service.py` (1,043)
- `src/features/edit/ai_editing/assistant_session_store.py` (443)
- `src/features/edit/ai_editing/audio_extractor.py` (302)

</details>

<details><summary>batch-131 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,600 行）</summary>

- `src/features/edit/ai_editing/audit_store.py` (176)
- `src/features/edit/ai_editing/autonomous_agent.py` (415)
- `src/features/edit/ai_editing/autonomous_mission.py` (307)
- `src/features/edit/ai_editing/autonomous_recovery.py` (193)
- `src/features/edit/ai_editing/brief_service.py` (349)
- `src/features/edit/ai_editing/capability_catalog.py` (367)
- `src/features/edit/ai_editing/capability_manifest.py` (414)
- `src/features/edit/ai_editing/capability_status.py` (379)

</details>

<details><summary>batch-132 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,712 行）</summary>

- `src/features/edit/ai_editing/chat_run_driver.py` (739)
- `src/features/edit/ai_editing/commercial_acceptance.py` (359)
- `src/features/edit/ai_editing/commercial_gate.py` (77)
- `src/features/edit/ai_editing/composition_plugin.py` (290)
- `src/features/edit/ai_editing/context_budget.py` (284)
- `src/features/edit/ai_editing/context_bundle.py` (299)
- `src/features/edit/ai_editing/context_service.py` (500)
- `src/features/edit/ai_editing/context_spill.py` (164)

</details>

<details><summary>batch-133 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,371 行）</summary>

- `src/features/edit/ai_editing/creative_assets.py` (151)
- `src/features/edit/ai_editing/credential_store.py` (12)
- `src/features/edit/ai_editing/cross_domain.py` (344)
- `src/features/edit/ai_editing/cross_domain_cost.py` (308)
- `src/features/edit/ai_editing/decision_service.py` (115)
- `src/features/edit/ai_editing/delivery_analyzer.py` (385)
- `src/features/edit/ai_editing/delivery_contracts.py` (866)
- `src/features/edit/ai_editing/delivery_quality_gate.py` (190)

</details>

<details><summary>batch-134 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,880 行）</summary>

- `src/features/edit/ai_editing/diagnostics.py` (278)
- `src/features/edit/ai_editing/edit_api.py` (412)
- `src/features/edit/ai_editing/edit_capability_plugin.py` (235)
- `src/features/edit/ai_editing/edit_project_service.py` (350)
- `src/features/edit/ai_editing/editorial_advanced.py` (391)
- `src/features/edit/ai_editing/editorial_models.py` (665)
- `src/features/edit/ai_editing/editorial_quality.py` (341)
- `src/features/edit/ai_editing/eval_runner.py` (208)

</details>

<details><summary>batch-135 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,922 行）</summary>

- `src/features/edit/ai_editing/eval_trace.py` (318)
- `src/features/edit/ai_editing/evidence_kinds.py` (239)
- `src/features/edit/ai_editing/evidence_policy.py` (67)
- `src/features/edit/ai_editing/execution_policy.py` (444)
- `src/features/edit/ai_editing/export_service.py` (882)
- `src/features/edit/ai_editing/feedback_store.py` (290)
- `src/features/edit/ai_editing/generated_media_materializer.py` (329)
- `src/features/edit/ai_editing/generation_bin.py` (353)

</details>

<details><summary>batch-136 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,903 行）</summary>

- `src/features/edit/ai_editing/generation_models.py` (12)
- `src/features/edit/ai_editing/generation_provider.py` (255)
- `src/features/edit/ai_editing/generation_provider_pack.py` (564)
- `src/features/edit/ai_editing/goal_round_driver.py` (162)
- `src/features/edit/ai_editing/goal_supervisor.py` (659)
- `src/features/edit/ai_editing/help_index.py` (409)
- `src/features/edit/ai_editing/http_generation_provider.py` (567)
- `src/features/edit/ai_editing/image_attachment.py` (275)

</details>

<details><summary>batch-137 [P1] <code>src/features/edit/ai_editing/</code>（5 文件，2,797 行）</summary>

- `src/features/edit/ai_editing/jobs.py` (543)
- `src/features/edit/ai_editing/keyframe_sampler.py` (267)
- `src/features/edit/ai_editing/kill_switch.py` (286)
- `src/features/edit/ai_editing/llm_client.py` (1,615)
- `src/features/edit/ai_editing/llm_cost.py` (86)

</details>

<details><summary>batch-138 [P1] <code>src/features/edit/ai_editing/</code>（6 文件，2,539 行）</summary>

- `src/features/edit/ai_editing/llm_variant_planner.py` (560)
- `src/features/edit/ai_editing/mcp_gateway.py` (504)
- `src/features/edit/ai_editing/mcp_local_ipc.py` (368)
- `src/features/edit/ai_editing/mcp_plugin.py` (216)
- `src/features/edit/ai_editing/mcp_stdio.py` (177)
- `src/features/edit/ai_editing/mcp_vision_gateway.py` (714)

</details>

<details><summary>batch-139 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,067 行）</summary>

- `src/features/edit/ai_editing/media_generation_service.py` (497)
- `src/features/edit/ai_editing/media_materialization_pipeline.py` (218)
- `src/features/edit/ai_editing/media_probe.py` (545)
- `src/features/edit/ai_editing/message_record.py` (204)
- `src/features/edit/ai_editing/mission_workspace.py` (154)
- `src/features/edit/ai_editing/missions.py` (357)
- `src/features/edit/ai_editing/model_pack_manager.py` (12)
- `src/features/edit/ai_editing/model_router.py` (80)

</details>

<details><summary>batch-140 [P1] <code>src/features/edit/ai_editing/</code>（6 文件，2,250 行）</summary>

- `src/features/edit/ai_editing/models.py` (560)
- `src/features/edit/ai_editing/motion_graphic_artifacts.py` (260)
- `src/features/edit/ai_editing/motion_graphic_run.py` (256)
- `src/features/edit/ai_editing/music_policy.py` (165)
- `src/features/edit/ai_editing/ocr_provider.py` (522)
- `src/features/edit/ai_editing/operation_protocol.py` (487)

</details>

<details><summary>batch-141 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,974 行）</summary>

- `src/features/edit/ai_editing/plan_compiler.py` (756)
- `src/features/edit/ai_editing/plan_repository.py` (268)
- `src/features/edit/ai_editing/plan_validator.py` (569)
- `src/features/edit/ai_editing/postcondition_checker.py` (270)
- `src/features/edit/ai_editing/privacy_guard.py` (75)
- `src/features/edit/ai_editing/professional_skills.py` (376)
- `src/features/edit/ai_editing/profile_tts_provider.py` (229)
- `src/features/edit/ai_editing/provider_capabilities.py` (431)

</details>

<details><summary>batch-142 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，1,463 行）</summary>

- `src/features/edit/ai_editing/provider_plugin.py` (241)
- `src/features/edit/ai_editing/provider_profiles.py` (12)
- `src/features/edit/ai_editing/provider_resume.py` (271)
- `src/features/edit/ai_editing/quality_gate.py` (247)
- `src/features/edit/ai_editing/release_evidence.py` (240)
- `src/features/edit/ai_editing/release_gate.py` (168)
- `src/features/edit/ai_editing/release_manifest.py` (137)
- `src/features/edit/ai_editing/release_test_matrix.py` (147)

</details>

<details><summary>batch-143 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,139 行）</summary>

- `src/features/edit/ai_editing/remote_transcript_provider.py` (356)
- `src/features/edit/ai_editing/replan_policy.py` (298)
- `src/features/edit/ai_editing/request_recovery.py` (132)
- `src/features/edit/ai_editing/rollout_metrics.py` (92)
- `src/features/edit/ai_editing/run_context.py` (239)
- `src/features/edit/ai_editing/runtime_capability_snapshot.py` (509)
- `src/features/edit/ai_editing/safe_path_policy.py` (153)
- `src/features/edit/ai_editing/schema.py` (360)

</details>

<details><summary>batch-144 [P1] <code>src/features/edit/ai_editing/</code>（8 文件，2,582 行）</summary>

- `src/features/edit/ai_editing/semantic_index.py` (303)
- `src/features/edit/ai_editing/session_models.py` (410)
- `src/features/edit/ai_editing/skill_manifest.py` (484)
- `src/features/edit/ai_editing/skill_registry.py` (287)
- `src/features/edit/ai_editing/skill_runtime.py` (659)
- `src/features/edit/ai_editing/snapshot_service.py` (183)
- `src/features/edit/ai_editing/speech_preprocessor.py` (135)
- `src/features/edit/ai_editing/startup_diagnostics.py` (121)

</details>

<details><summary>batch-145 [P1] <code>src/features/edit/ai_editing/</code>（6 文件，1,571 行）</summary>

- `src/features/edit/ai_editing/structured_output.py` (87)
- `src/features/edit/ai_editing/subtitle_ai_planner.py` (170)
- `src/features/edit/ai_editing/subtitle_draft.py` (167)
- `src/features/edit/ai_editing/subtitle_placement.py` (192)
- `src/features/edit/ai_editing/task_artifacts.py` (354)
- `src/features/edit/ai_editing/task_graph.py` (601)

</details>

<details><summary>batch-146 [P1] <code>src/features/edit/ai_editing/tests/</code>（1 文件，213 行）</summary>

- `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py` (213)

</details>

<details><summary>batch-147 [P1] <code>src/features/edit/ai_editing/</code>（6 文件，2,880 行）</summary>

- `src/features/edit/ai_editing/timebase.py` (331)
- `src/features/edit/ai_editing/token_estimator.py` (131)
- `src/features/edit/ai_editing/tool_call_log.py` (300)
- `src/features/edit/ai_editing/tool_calling_client.py` (361)
- `src/features/edit/ai_editing/tool_facade.py` (1,066)
- `src/features/edit/ai_editing/tool_invocation.py` (691)

</details>

<details><summary>batch-148 [P1] <code>src/features/edit/ai_editing/</code>（5 文件，1,730 行）</summary>

- `src/features/edit/ai_editing/tool_operation_ledger.py` (340)
- `src/features/edit/ai_editing/tool_result.py` (252)
- `src/features/edit/ai_editing/tool_result_pruner.py` (82)
- `src/features/edit/ai_editing/toposctl.py` (455)
- `src/features/edit/ai_editing/transaction_service.py` (601)

</details>

<details><summary>batch-149 [P1] <code>src/features/edit/ai_editing/</code>（6 文件，2,708 行）</summary>

- `src/features/edit/ai_editing/transcript_provider.py` (1,446)
- `src/features/edit/ai_editing/transcript_router.py` (302)
- `src/features/edit/ai_editing/tts_provider.py` (435)
- `src/features/edit/ai_editing/tts_settings.py` (118)
- `src/features/edit/ai_editing/user_ai_config.py` (12)
- `src/features/edit/ai_editing/variant_planner.py` (395)

</details>

<details><summary>batch-150 [P1] <code>src/features/edit/ai_editing/</code>（6 文件，2,048 行）</summary>

- `src/features/edit/ai_editing/visual_semantic_provider.py` (437)
- `src/features/edit/ai_editing/visual_semantics.py` (420)
- `src/features/edit/ai_editing/volcengine_generation_provider.py` (12)
- `src/features/edit/ai_editing/whisperx_provider.py` (197)
- `src/features/edit/ai_editing/workflow.py` (696)
- `src/features/edit/ai_editing/workflow_transaction.py` (286)

</details>

<details><summary>batch-151 [P1] <code>src/features/edit/</code>（1 文件，249 行）</summary>

- `src/features/edit/commands_facade.py` (249)

</details>

<details><summary>batch-152 [P1] <code>src/features/edit/core/</code>（4 文件，651 行）</summary>

- `src/features/edit/core/__init__.py` (162)
- `src/features/edit/core/adjustment_composite.py` (130)
- `src/features/edit/core/asset_usage_index.py` (137)
- `src/features/edit/core/audio_capabilities.py` (222)

</details>

<details><summary>batch-153 [P1] <code>src/features/edit/core/</code>（1 文件，2,505 行）</summary>

- `src/features/edit/core/audio_dsp.py` (2,505)

</details>

<details><summary>batch-154 [P1] <code>src/features/edit/core/</code>（3 文件，1,302 行）</summary>

- `src/features/edit/core/audio_graph_renderer.py` (621)
- `src/features/edit/core/audio_graph_snapshot.py` (359)
- `src/features/edit/core/audio_health.py` (322)

</details>

<details><summary>batch-155 [P1] <code>src/features/edit/core/</code>（1 文件，4,855 行）</summary>

- `src/features/edit/core/audio_playback.py` (4,855)

</details>

<details><summary>batch-156 [P1] <code>src/features/edit/core/</code>（7 文件，2,584 行）</summary>

- `src/features/edit/core/audio_timestretch.py` (153)
- `src/features/edit/core/audio_transition_catalog.py` (81)
- `src/features/edit/core/audio_waveform.py` (1,560)
- `src/features/edit/core/automation_recorder.py` (259)
- `src/features/edit/core/blend_modes.py` (256)
- `src/features/edit/core/blend_shader.py` (65)
- `src/features/edit/core/bwf.py` (210)

</details>

<details><summary>batch-157 [P1] <code>src/features/edit/core/</code>（1 文件，443 行）</summary>

- `src/features/edit/core/capability_registry.py` (443)

</details>

<details><summary>batch-158 [P1] <code>src/features/edit/core/captions/</code>（6 文件，2,305 行）</summary>

- `src/features/edit/core/captions/__init__.py` (17)
- `src/features/edit/core/captions/commands.py` (1,049)
- `src/features/edit/core/captions/delivery.py` (281)
- `src/features/edit/core/captions/edit_policy.py` (232)
- `src/features/edit/core/captions/export.py` (300)
- `src/features/edit/core/captions/font_resolver.py` (426)

</details>

<details><summary>batch-159 [P1] <code>src/features/edit/core/captions/</code>（7 文件，2,542 行）</summary>

- `src/features/edit/core/captions/graphics.py` (860)
- `src/features/edit/core/captions/importer.py` (342)
- `src/features/edit/core/captions/model.py` (711)
- `src/features/edit/core/captions/mux.py` (42)
- `src/features/edit/core/captions/quality.py` (177)
- `src/features/edit/core/captions/render_plan.py` (147)
- `src/features/edit/core/captions/schema.py` (263)

</details>

<details><summary>batch-160 [P1] <code>src/features/edit/core/captions/</code>（2 文件，747 行）</summary>

- `src/features/edit/core/captions/style_preset.py` (486)
- `src/features/edit/core/captions/translation.py` (261)

</details>

<details><summary>batch-161 [P1] <code>src/features/edit/core/</code>（3 文件，1,548 行）</summary>

- `src/features/edit/core/channel_layout.py` (386)
- `src/features/edit/core/chapter_export.py` (128)
- `src/features/edit/core/clip_params.py` (1,034)

</details>

<details><summary>batch-162 [P1] <code>src/features/edit/core/</code>（1 文件，16,251 行）</summary>

- `src/features/edit/core/commands.py` (16,251)

</details>

<details><summary>batch-163 [P1] <code>src/features/edit/core/</code>（7 文件，2,856 行）</summary>

- `src/features/edit/core/comp_clip_data.py` (331)
- `src/features/edit/core/composite_failure_policy.py` (53)
- `src/features/edit/core/composite_graph_data.py` (976)
- `src/features/edit/core/consolidate.py` (797)
- `src/features/edit/core/crop_cpu.py` (141)
- `src/features/edit/core/crop_render.py` (372)
- `src/features/edit/core/custom_transitions.py` (186)

</details>

<details><summary>batch-164 [P1] <code>src/features/edit/core/</code>（2 文件，1,249 行）</summary>

- `src/features/edit/core/delivery_qc.py` (393)
- `src/features/edit/core/edit_frame_coordinator.py` (856)

</details>

<details><summary>batch-165 [P1] <code>src/features/edit/core/</code>（1 文件，3,037 行）</summary>

- `src/features/edit/core/edit_gpu_decode_manager.py` (3,037)

</details>

<details><summary>batch-166 [P1] <code>src/features/edit/core/</code>（1 文件，5,297 行）</summary>

- `src/features/edit/core/edit_gpu_preview_renderer.py` (5,297)

</details>

<details><summary>batch-167 [P1] <code>src/features/edit/core/</code>（2 文件，2,839 行）</summary>

- `src/features/edit/core/edit_playback_controller.py` (1,238)
- `src/features/edit/core/edit_project_io.py` (1,601)

</details>

<details><summary>batch-168 [P1] <code>src/features/edit/core/</code>（6 文件，2,835 行）</summary>

- `src/features/edit/core/effect_instance.py` (803)
- `src/features/edit/core/effect_registry.py` (886)
- `src/features/edit/core/effect_render_operations.py` (177)
- `src/features/edit/core/export_manifest.py` (435)
- `src/features/edit/core/generator_clip_transaction.py` (171)
- `src/features/edit/core/generator_transaction_guard.py` (363)

</details>

<details><summary>batch-169 [P1] <code>src/features/edit/core/</code>（8 文件，2,737 行）</summary>

- `src/features/edit/core/gpu_transition_shaders.py` (279)
- `src/features/edit/core/interchange.py` (426)
- `src/features/edit/core/loudness_analyzer.py` (186)
- `src/features/edit/core/loudness_meter.py` (324)
- `src/features/edit/core/mixer_preset.py` (261)
- `src/features/edit/core/multicam.py` (300)
- `src/features/edit/core/multicam_sync.py` (304)
- `src/features/edit/core/nested_sequence.py` (657)

</details>

<details><summary>batch-170 [P1] <code>src/features/edit/core/</code>（2 文件，272 行）</summary>

- `src/features/edit/core/placeholder_frame.py` (93)
- `src/features/edit/core/playback_debug.py` (179)

</details>

<details><summary>batch-171 [P1] <code>src/features/edit/core/</code>（1 文件，5,392 行）</summary>

- `src/features/edit/core/project.py` (5,392)

</details>

<details><summary>batch-172 [P1] <code>src/features/edit/core/</code>（5 文件，1,842 行）</summary>

- `src/features/edit/core/project_archive.py` (249)
- `src/features/edit/core/project_migration.py` (216)
- `src/features/edit/core/project_serializer.py` (425)
- `src/features/edit/core/proxy_generator.py` (763)
- `src/features/edit/core/proxy_policy.py` (189)

</details>

<details><summary>batch-173 [P1] <code>src/features/edit/core/realtime_playback/</code>（8 文件，2,786 行）</summary>

- `src/features/edit/core/realtime_playback/__init__.py` (147)
- `src/features/edit/core/realtime_playback/adaptive_quality.py` (271)
- `src/features/edit/core/realtime_playback/audio_ring_buffer.py` (262)
- `src/features/edit/core/realtime_playback/avsync_controller.py` (213)
- `src/features/edit/core/realtime_playback/clip_decode_stream.py` (506)
- `src/features/edit/core/realtime_playback/decode_manager_source.py` (64)
- `src/features/edit/core/realtime_playback/engine.py` (1,112)
- `src/features/edit/core/realtime_playback/frame_ring_buffer.py` (211)

</details>

<details><summary>batch-174 [P1] <code>src/features/edit/core/realtime_playback/</code>（8 文件，2,754 行）</summary>

- `src/features/edit/core/realtime_playback/gpu_frame_pool.py` (276)
- `src/features/edit/core/realtime_playback/playback_budget.py` (244)
- `src/features/edit/core/realtime_playback/policy.py` (341)
- `src/features/edit/core/realtime_playback/render_cache_adapter.py` (435)
- `src/features/edit/core/realtime_playback/source_resolver.py` (349)
- `src/features/edit/core/realtime_playback/task_priority.py` (124)
- `src/features/edit/core/realtime_playback/telemetry.py` (289)
- `src/features/edit/core/realtime_playback/timeline_decode_scheduler.py` (696)

</details>

<details><summary>batch-175 [P1] <code>src/features/edit/core/realtime_playback/</code>（2 文件，713 行）</summary>

- `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py` (399)
- `src/features/edit/core/realtime_playback/types.py` (314)

</details>

<details><summary>batch-176 [P1] <code>src/features/edit/core/</code>（5 文件，2,419 行）</summary>

- `src/features/edit/core/recording_manager.py` (1,356)
- `src/features/edit/core/render_cache.py` (348)
- `src/features/edit/core/render_cache_encoder.py` (249)
- `src/features/edit/core/render_cache_evaluator.py` (120)
- `src/features/edit/core/render_cache_status.py` (346)

</details>

<details><summary>batch-177 [P1] <code>src/features/edit/core/</code>（1 文件，873 行）</summary>

- `src/features/edit/core/render_cache_worker.py` (873)

</details>

<details><summary>batch-178 [P1] <code>src/features/edit/core/scene_cut/</code>（3 文件，2,413 行）</summary>

- `src/features/edit/core/scene_cut/__init__.py` (342)
- `src/features/edit/core/scene_cut/apply_ops.py` (907)
- `src/features/edit/core/scene_cut/cache.py` (1,164)

</details>

<details><summary>batch-179 [P1] <code>src/features/edit/core/scene_cut/</code>（3 文件，2,647 行）</summary>

- `src/features/edit/core/scene_cut/detector.py` (684)
- `src/features/edit/core/scene_cut/filters.py` (993)
- `src/features/edit/core/scene_cut/frame_sampler.py` (970)

</details>

<details><summary>batch-180 [P1] <code>src/features/edit/core/scene_cut/</code>（2 文件，2,269 行）</summary>

- `src/features/edit/core/scene_cut/metrics.py` (957)
- `src/features/edit/core/scene_cut/models.py` (1,312)

</details>

<details><summary>batch-181 [P1] <code>src/features/edit/core/scene_cut/</code>（3 文件，2,741 行）</summary>

- `src/features/edit/core/scene_cut/subclips.py` (765)
- `src/features/edit/core/scene_cut/timeline_mapping.py` (997)
- `src/features/edit/core/scene_cut/worker.py` (979)

</details>

<details><summary>batch-182 [P1] <code>src/features/edit/core/</code>（5 文件，2,970 行）</summary>

- `src/features/edit/core/scene_cut_controller.py` (654)
- `src/features/edit/core/selection.py` (517)
- `src/features/edit/core/serialization.py` (891)
- `src/features/edit/core/sidechain_ducker.py` (269)
- `src/features/edit/core/stem_export.py` (639)

</details>

<details><summary>batch-183 [P1] <code>src/features/edit/core/</code>（3 文件，2,959 行）</summary>

- `src/features/edit/core/subtitle_renderer.py` (582)
- `src/features/edit/core/take_sheet.py` (269)
- `src/features/edit/core/timeline_clip_frame_provider.py` (2,108)

</details>

<details><summary>batch-184 [P1] <code>src/features/edit/core/</code>（1 文件，11,028 行）</summary>

- `src/features/edit/core/timeline_export.py` (11,028)

</details>

<details><summary>batch-185 [P1] <code>src/features/edit/core/</code>（2 文件，2,622 行）</summary>

- `src/features/edit/core/timeline_frame_mapper.py` (398)
- `src/features/edit/core/timeline_gpu_export_renderer.py` (2,224)

</details>

<details><summary>batch-186 [P1] <code>src/features/edit/core/</code>（1 文件，5,412 行）</summary>

- `src/features/edit/core/timeline_metal_export_renderer.py` (5,412)

</details>

<details><summary>batch-187 [P1] <code>src/features/edit/core/</code>（1 文件，869 行）</summary>

- `src/features/edit/core/timeline_occlusion.py` (869)

</details>

<details><summary>batch-188 [P1] <code>src/features/edit/core/</code>（1 文件，3,346 行）</summary>

- `src/features/edit/core/timeline_ops.py` (3,346)

</details>

<details><summary>batch-189 [P1] <code>src/features/edit/core/</code>（1 文件，11,621 行）</summary>

- `src/features/edit/core/timeline_preview_renderer.py` (11,621)

</details>

<details><summary>batch-190 [P1] <code>src/features/edit/core/</code>（2 文件，1,959 行）</summary>

- `src/features/edit/core/timeline_render_plan.py` (1,593)
- `src/features/edit/core/timeline_thumbnail_scheduler.py` (366)

</details>

<details><summary>batch-191 [P1] <code>src/features/edit/core/</code>（1 文件，3,549 行）</summary>

- `src/features/edit/core/timeline_v2.py` (3,549)

</details>

<details><summary>batch-192 [P1] <code>src/features/edit/core/</code>（7 文件，1,637 行）</summary>

- `src/features/edit/core/title_templates.py` (163)
- `src/features/edit/core/transition_catalog.py` (173)
- `src/features/edit/core/transition_effects.py` (525)
- `src/features/edit/core/transition_mask.py` (154)
- `src/features/edit/core/transition_mask_gpu.py` (192)
- `src/features/edit/core/transition_wipe.py` (168)
- `src/features/edit/core/workspace_presets.py` (262)

</details>

<details><summary>batch-193 [P1] <code>src/features/edit/</code>（3 文件，1,281 行）</summary>

- `src/features/edit/facade.py` (191)
- `src/features/edit/module.py` (657)
- `src/features/edit/preferences.py` (433)

</details>

<details><summary>batch-194 [P1] <code>src/features/edit/ui/</code>（6 文件，2,668 行）</summary>

- `src/features/edit/ui/__init__.py` (163)
- `src/features/edit/ui/adr_take_sheet_panel.py` (563)
- `src/features/edit/ui/ai_assistant_composer.py` (1,508)
- `src/features/edit/ui/ai_brief_editor.py` (208)
- `src/features/edit/ui/ai_context_summary.py` (84)
- `src/features/edit/ui/ai_decision_inspector.py` (142)

</details>

<details><summary>batch-195 [P1] <code>src/features/edit/ui/</code>（1 文件，3,784 行）</summary>

- `src/features/edit/ui/ai_edit_assistant_panel.py` (3,784)

</details>

<details><summary>batch-196 [P1] <code>src/features/edit/ui/</code>（7 文件，2,678 行）</summary>

- `src/features/edit/ui/ai_help_answer.py` (185)
- `src/features/edit/ui/ai_job_panel.py` (126)
- `src/features/edit/ui/ai_preferences_page.py` (1,402)
- `src/features/edit/ui/ai_result_card.py` (191)
- `src/features/edit/ui/ai_skill_card.py` (360)
- `src/features/edit/ui/ai_timeline_diff.py` (250)
- `src/features/edit/ui/ai_variant_list.py` (164)

</details>

<details><summary>batch-197 [P1] <code>src/features/edit/ui/</code>（5 文件，1,926 行）</summary>

- `src/features/edit/ui/caption_workspace.py` (567)
- `src/features/edit/ui/chapter_export_dialog.py` (182)
- `src/features/edit/ui/clip_audio_effect_dialog_adapter.py` (200)
- `src/features/edit/ui/clip_transform_handle_controller.py` (533)
- `src/features/edit/ui/comp_clip_settings_dialog.py` (444)

</details>

<details><summary>batch-198 [P1] <code>src/features/edit/ui/</code>（2 文件，2,647 行）</summary>

- `src/features/edit/ui/edit_effects_panel.py` (2,270)
- `src/features/edit/ui/edit_info_panel.py` (377)

</details>

<details><summary>batch-199 [P1] <code>src/features/edit/ui/</code>（1 文件，3,302 行）</summary>

- `src/features/edit/ui/edit_page.py` (3,302)

</details>

<details><summary>batch-200 [P1] <code>src/features/edit/ui/</code>（1 文件，4,896 行）</summary>

- `src/features/edit/ui/edit_properties_panel.py` (4,896)

</details>

<details><summary>batch-201 [P1] <code>src/features/edit/ui/</code>（2 文件，978 行）</summary>

- `src/features/edit/ui/edit_style.py` (806)
- `src/features/edit/ui/interchange_dialog.py` (172)

</details>

<details><summary>batch-202 [P1] <code>src/features/edit/ui/</code>（1 文件，10,671 行）</summary>

- `src/features/edit/ui/media_pool_panel.py` (10,671)

</details>

<details><summary>batch-203 [P1] <code>src/features/edit/ui/</code>（7 文件，1,922 行）</summary>

- `src/features/edit/ui/multicam_panel.py` (379)
- `src/features/edit/ui/multicam_sync_result_dialog.py` (128)
- `src/features/edit/ui/new_project_dialog.py` (352)
- `src/features/edit/ui/new_timeline_dialog.py` (677)
- `src/features/edit/ui/placeholder_dialog.py` (84)
- `src/features/edit/ui/playback_hud.py` (213)
- `src/features/edit/ui/preview_view_transform.py` (89)

</details>

<details><summary>batch-204 [P1] <code>src/features/edit/ui/</code>（1 文件，3,414 行）</summary>

- `src/features/edit/ui/program_viewer.py` (3,414)

</details>

<details><summary>batch-205 [P1] <code>src/features/edit/ui/</code>（2 文件，1,216 行）</summary>

- `src/features/edit/ui/quick_project_dialog.py` (662)
- `src/features/edit/ui/relink_media_dialog.py` (554)

</details>

<details><summary>batch-206 [P1] <code>src/features/edit/ui/</code>（1 文件，2,740 行）</summary>

- `src/features/edit/ui/scene_cut_dialog.py` (2,740)

</details>

<details><summary>batch-207 [P1] <code>src/features/edit/ui/</code>（2 文件，1,946 行）</summary>

- `src/features/edit/ui/scene_cut_graph_widget.py` (1,120)
- `src/features/edit/ui/scene_cut_preview_widget.py` (826)

</details>

<details><summary>batch-208 [P1] <code>src/features/edit/ui/</code>（3 文件，2,105 行）</summary>

- `src/features/edit/ui/scene_cut_result_model.py` (1,110)
- `src/features/edit/ui/scene_cut_shortcuts.py` (98)
- `src/features/edit/ui/scene_cut_timeline_overlay.py` (897)

</details>

<details><summary>batch-209 [P1] <code>src/features/edit/ui/</code>（3 文件，2,895 行）</summary>

- `src/features/edit/ui/source_viewer.py` (2,332)
- `src/features/edit/ui/speech_to_text_dialog.py` (146)
- `src/features/edit/ui/stem_export_dialog.py` (417)

</details>

<details><summary>batch-210 [P1] <code>src/features/edit/ui/</code>（4 文件，1,493 行）</summary>

- `src/features/edit/ui/subtitle_clip_item.py` (540)
- `src/features/edit/ui/subtitle_delivery_dialog.py` (114)
- `src/features/edit/ui/subtitle_handle_controller.py` (607)
- `src/features/edit/ui/timeline_interaction.py` (232)

</details>

<details><summary>batch-211 [P1] <code>src/features/edit/ui/</code>（1 文件，11,028 行）</summary>

- `src/features/edit/ui/timeline_items.py` (11,028)

</details>

<details><summary>batch-212 [P1] <code>src/features/edit/ui/</code>（1 文件，872 行）</summary>

- `src/features/edit/ui/timeline_layout.py` (872)

</details>

<details><summary>batch-213 [P1] <code>src/features/edit/ui/</code>（2 文件，2,817 行）</summary>

- `src/features/edit/ui/timeline_panel.py` (2,679)
- `src/features/edit/ui/timeline_shortcuts.py` (138)

</details>

<details><summary>batch-214 [P1] <code>src/features/edit/ui/</code>（1 文件，15,708 行）</summary>

- `src/features/edit/ui/timeline_view.py` (15,708)

</details>

<details><summary>batch-215 [P1] <code>src/features/edit/ui/</code>（2 文件，523 行）</summary>

- `src/features/edit/ui/trim_numeric_dialog.py` (202)
- `src/features/edit/ui/tts_settings_dialog.py` (321)

</details>

<details><summary>batch-216 [P1] <code>src/products/</code>（6 文件，426 行）</summary>

- `src/products/__init__.py` (167)
- `src/products/packaging.py` (104)
- `src/products/topos_color.py` (39)
- `src/products/topos_composite.py` (40)
- `src/products/topos_edit.py` (40)
- `src/products/topos_studio.py` (36)

</details>

<details><summary>batch-217 [P2] <code>src/</code>（1 文件，0 行）</summary>

- `src/__init__.py` (0)

</details>

<details><summary>batch-218 [P2] <code>src/features/</code>（1 文件，3 行）</summary>

- `src/features/__init__.py` (3)

</details>

<details><summary>batch-219 [P2] <code>src/features/ai_harness/</code>（6 文件，2,577 行）</summary>

- `src/features/ai_harness/__init__.py` (237)
- `src/features/ai_harness/analysis_skills.py` (430)
- `src/features/ai_harness/editorial_skills.py` (611)
- `src/features/ai_harness/generation_adapter.py` (389)
- `src/features/ai_harness/help_assistant.py` (189)
- `src/features/ai_harness/invocation_models.py` (721)

</details>

<details><summary>batch-220 [P2] <code>src/features/ai_harness/</code>（6 文件，2,813 行）</summary>

- `src/features/ai_harness/invocation_router.py` (499)
- `src/features/ai_harness/legacy_adapters.py` (734)
- `src/features/ai_harness/llm_role_executor.py` (504)
- `src/features/ai_harness/llm_role_model_client.py` (78)
- `src/features/ai_harness/mcp_http_transport.py` (395)
- `src/features/ai_harness/mcp_outbound.py` (603)

</details>

<details><summary>batch-221 [P2] <code>src/features/ai_harness/</code>（6 文件，2,861 行）</summary>

- `src/features/ai_harness/mcp_protocol.py` (291)
- `src/features/ai_harness/mcp_stdio_transport.py` (342)
- `src/features/ai_harness/plugin_broker.py` (497)
- `src/features/ai_harness/plugin_discovery.py` (314)
- `src/features/ai_harness/plugin_isolation.py` (761)
- `src/features/ai_harness/plugin_persistence.py` (656)

</details>

<details><summary>batch-222 [P2] <code>src/features/ai_harness/</code>（8 文件，2,679 行）</summary>

- `src/features/ai_harness/plugin_rollout.py` (286)
- `src/features/ai_harness/plugin_runtime.py` (1,078)
- `src/features/ai_harness/plugin_sandbox.py` (419)
- `src/features/ai_harness/plugin_sandbox_worker.py` (36)
- `src/features/ai_harness/plugin_status.py` (371)
- `src/features/ai_harness/plugin_trace.py` (87)
- `src/features/ai_harness/plugin_trust.py` (82)
- `src/features/ai_harness/result_card.py` (320)

</details>

<details><summary>batch-223 [P2] <code>src/features/ai_harness/</code>（8 文件，2,916 行）</summary>

- `src/features/ai_harness/result_card_builder.py` (351)
- `src/features/ai_harness/rollout_metrics.py` (151)
- `src/features/ai_harness/run_trace.py` (695)
- `src/features/ai_harness/runtime.py` (786)
- `src/features/ai_harness/runtime_invariants.py` (139)
- `src/features/ai_harness/scheduler_adapter.py` (191)
- `src/features/ai_harness/session_fork.py` (138)
- `src/features/ai_harness/skill_catalog.py` (465)

</details>

<details><summary>batch-224 [P2] <code>src/features/ai_harness/</code>（6 文件，2,826 行）</summary>

- `src/features/ai_harness/skill_compiler.py` (291)
- `src/features/ai_harness/skill_dry_run.py` (818)
- `src/features/ai_harness/skill_eval_suite.py` (416)
- `src/features/ai_harness/skill_release.py` (333)
- `src/features/ai_harness/skill_studio_drafts.py` (692)
- `src/features/ai_harness/subagent_runtime.py` (276)

</details>

<details><summary>batch-225 [P2] <code>src/features/ai_harness/tests/</code>（8 文件，1,861 行）</summary>

- `src/features/ai_harness/tests/__init__.py` (0)
- `src/features/ai_harness/tests/test_analysis_skills.py` (299)
- `src/features/ai_harness/tests/test_composite_render_skill.py` (161)
- `src/features/ai_harness/tests/test_editorial_skills.py` (303)
- `src/features/ai_harness/tests/test_generation_adapter.py` (246)
- `src/features/ai_harness/tests/test_help_assistant.py` (209)
- `src/features/ai_harness/tests/test_invocation_models.py` (287)
- `src/features/ai_harness/tests/test_invocation_router.py` (356)

</details>

<details><summary>batch-226 [P2] <code>src/features/ai_harness/tests/</code>（8 文件，2,016 行）</summary>

- `src/features/ai_harness/tests/test_legacy_adapters.py` (401)
- `src/features/ai_harness/tests/test_llm_role_executor.py` (273)
- `src/features/ai_harness/tests/test_llm_role_model_client.py` (54)
- `src/features/ai_harness/tests/test_mcp_outbound.py` (345)
- `src/features/ai_harness/tests/test_p1_editorial_provider.py` (199)
- `src/features/ai_harness/tests/test_p1_host_wiring.py` (211)
- `src/features/ai_harness/tests/test_p2_acceptance.py` (210)
- `src/features/ai_harness/tests/test_p2_wiring.py` (323)

</details>

<details><summary>batch-227 [P2] <code>src/features/ai_harness/tests/</code>（8 文件，2,118 行）</summary>

- `src/features/ai_harness/tests/test_p3_red_lines.py` (242)
- `src/features/ai_harness/tests/test_p4_acceptance.py` (356)
- `src/features/ai_harness/tests/test_p4_security.py` (289)
- `src/features/ai_harness/tests/test_result_card.py` (193)
- `src/features/ai_harness/tests/test_result_card_builder.py` (342)
- `src/features/ai_harness/tests/test_rollout_metrics.py` (71)
- `src/features/ai_harness/tests/test_run_trace.py` (182)
- `src/features/ai_harness/tests/test_runtime.py` (443)

</details>

<details><summary>batch-228 [P2] <code>src/features/ai_harness/tests/</code>（8 文件，2,146 行）</summary>

- `src/features/ai_harness/tests/test_runtime_node_events.py` (125)
- `src/features/ai_harness/tests/test_scheduler_adapter.py` (176)
- `src/features/ai_harness/tests/test_skill_catalog.py` (270)
- `src/features/ai_harness/tests/test_skill_compiler.py` (383)
- `src/features/ai_harness/tests/test_skill_dry_run.py` (287)
- `src/features/ai_harness/tests/test_skill_eval_suite.py` (317)
- `src/features/ai_harness/tests/test_skill_release.py` (266)
- `src/features/ai_harness/tests/test_skill_studio_drafts.py` (322)

</details>

<details><summary>batch-229 [P2] <code>src/features/ai_workflow/</code>（1 文件，0 行）</summary>

- `src/features/ai_workflow/__init__.py` (0)

</details>

<details><summary>batch-230 [P2] <code>src/features/ai_workflow/adapters/</code>（6 文件，2,286 行）</summary>

- `src/features/ai_workflow/adapters/__init__.py` (0)
- `src/features/ai_workflow/adapters/comfyui_client.py` (495)
- `src/features/ai_workflow/adapters/comfyui_converter.py` (344)
- `src/features/ai_workflow/adapters/comfyui_executor.py` (725)
- `src/features/ai_workflow/adapters/comfyui_export.py` (326)
- `src/features/ai_workflow/adapters/comfyui_importer.py` (396)

</details>

<details><summary>batch-231 [P2] <code>src/features/ai_workflow/adapters/</code>（5 文件，1,969 行）</summary>

- `src/features/ai_workflow/adapters/comfyui_models.py` (808)
- `src/features/ai_workflow/adapters/comfyui_node_specs.py` (107)
- `src/features/ai_workflow/adapters/comfyui_profiles.py` (92)
- `src/features/ai_workflow/adapters/composite_adapter.py` (599)
- `src/features/ai_workflow/adapters/generation_node_specs.py` (363)

</details>

<details><summary>batch-232 [P2] <code>src/features/ai_workflow/core/</code>（8 文件，2,793 行）</summary>

- `src/features/ai_workflow/core/__init__.py` (0)
- `src/features/ai_workflow/core/node_specs.py` (495)
- `src/features/ai_workflow/core/publish_as_skill.py` (235)
- `src/features/ai_workflow/core/template_registry.py` (603)
- `src/features/ai_workflow/core/workflow_compiler.py` (407)
- `src/features/ai_workflow/core/workflow_graph_edit.py` (188)
- `src/features/ai_workflow/core/workflow_models.py` (447)
- `src/features/ai_workflow/core/workflow_validator.py` (418)

</details>

<details><summary>batch-233 [P2] <code>src/features/ai_workflow/</code>（1 文件，111 行）</summary>

- `src/features/ai_workflow/module.py` (111)

</details>

<details><summary>batch-234 [P2] <code>src/features/ai_workflow/runtime/</code>（6 文件，1,682 行）</summary>

- `src/features/ai_workflow/runtime/__init__.py` (14)
- `src/features/ai_workflow/runtime/fake_workflow_service.py` (173)
- `src/features/ai_workflow/runtime/workflow_action_service.py` (273)
- `src/features/ai_workflow/runtime/workflow_context.py` (193)
- `src/features/ai_workflow/runtime/workflow_run_bridge.py` (59)
- `src/features/ai_workflow/runtime/workflow_run_controller.py` (970)

</details>

<details><summary>batch-235 [P2] <code>src/features/ai_workflow/tests/</code>（8 文件，2,981 行）</summary>

- `src/features/ai_workflow/tests/__init__.py` (0)
- `src/features/ai_workflow/tests/test_comfyui_client.py` (322)
- `src/features/ai_workflow/tests/test_comfyui_converter.py` (299)
- `src/features/ai_workflow/tests/test_comfyui_executor.py` (680)
- `src/features/ai_workflow/tests/test_comfyui_export.py` (298)
- `src/features/ai_workflow/tests/test_comfyui_importer.py` (382)
- `src/features/ai_workflow/tests/test_comfyui_models.py` (538)
- `src/features/ai_workflow/tests/test_composite_adapter.py` (462)

</details>

<details><summary>batch-236 [P2] <code>src/features/ai_workflow/tests/</code>（8 文件，2,265 行）</summary>

- `src/features/ai_workflow/tests/test_fake_workflow_service.py` (231)
- `src/features/ai_workflow/tests/test_generation_node_specs.py` (285)
- `src/features/ai_workflow/tests/test_node_canvas_protocol.py` (128)
- `src/features/ai_workflow/tests/test_node_specs.py` (185)
- `src/features/ai_workflow/tests/test_p10_3_templates.py` (184)
- `src/features/ai_workflow/tests/test_p5_acceptance.py` (286)
- `src/features/ai_workflow/tests/test_p6_acceptance.py` (572)
- `src/features/ai_workflow/tests/test_p7_acceptance.py` (394)

</details>

<details><summary>batch-237 [P2] <code>src/features/ai_workflow/tests/</code>（8 文件，1,837 行）</summary>

- `src/features/ai_workflow/tests/test_publish_as_skill.py` (215)
- `src/features/ai_workflow/tests/test_template_registry.py` (202)
- `src/features/ai_workflow/tests/test_template_runnability.py` (223)
- `src/features/ai_workflow/tests/test_workflow_action_service.py` (184)
- `src/features/ai_workflow/tests/test_workflow_compiler.py` (280)
- `src/features/ai_workflow/tests/test_workflow_context.py` (133)
- `src/features/ai_workflow/tests/test_workflow_graph_edit.py` (231)
- `src/features/ai_workflow/tests/test_workflow_models.py` (369)

</details>

<details><summary>batch-238 [P2] <code>src/features/ai_workflow/tests/</code>（2 文件，413 行）</summary>

- `src/features/ai_workflow/tests/test_workflow_run_bridge.py` (94)
- `src/features/ai_workflow/tests/test_workflow_validator.py` (319)

</details>

<details><summary>batch-239 [P2] <code>src/features/ai_workflow/ui/</code>（8 文件，1,843 行）</summary>

- `src/features/ai_workflow/ui/__init__.py` (0)
- `src/features/ai_workflow/ui/workflow_canvas_adapter.py` (197)
- `src/features/ai_workflow/ui/workflow_canvas_view.py` (637)
- `src/features/ai_workflow/ui/workflow_connection_item.py` (181)
- `src/features/ai_workflow/ui/workflow_form_page.py` (147)
- `src/features/ai_workflow/ui/workflow_gallery_page.py` (111)
- `src/features/ai_workflow/ui/workflow_inspector.py` (179)
- `src/features/ai_workflow/ui/workflow_node_item.py` (391)

</details>

<details><summary>batch-240 [P2] <code>src/features/ai_workflow/ui/</code>（2 文件，774 行）</summary>

- `src/features/ai_workflow/ui/workflow_node_search.py` (116)
- `src/features/ai_workflow/ui/workflow_panel.py` (658)

</details>

<details><summary>batch-241 [P2] <code>src/features/color/</code>（1 文件，4 行）</summary>

- `src/features/color/__init__.py` (4)

</details>

<details><summary>batch-242 [P2] <code>src/features/color/adapters/</code>（2 文件，429 行）</summary>

- `src/features/color/adapters/__init__.py` (7)
- `src/features/color/adapters/color_stack_hydrator.py` (422)

</details>

<details><summary>batch-243 [P2] <code>src/features/color/ai/</code>（2 文件，638 行）</summary>

- `src/features/color/ai/__init__.py` (0)
- `src/features/color/ai/color_context.py` (638)

</details>

<details><summary>batch-244 [P2] <code>src/features/color/</code>（1 文件，255 行）</summary>

- `src/features/color/facade.py` (255)

</details>

<details><summary>batch-245 [P2] <code>src/features/color/integrations/</code>（2 文件，1,023 行）</summary>

- `src/features/color/integrations/__init__.py` (1)
- `src/features/color/integrations/tangent_bridge.py` (1,022)

</details>

<details><summary>batch-246 [P2] <code>src/features/color/</code>（2 文件，1,469 行）</summary>

- `src/features/color/module.py` (560)
- `src/features/color/preferences.py` (909)

</details>

<details><summary>batch-247 [P2] <code>src/features/color/shaders/</code>（1 文件，669 行）</summary>

- `src/features/color/shaders/shaders.py` (669)

</details>

<details><summary>batch-248 [P2] <code>src/features/color/tracking/</code>（8 文件，124 行）</summary>

- `src/features/color/tracking/__init__.py` (26)
- `src/features/color/tracking/coordinate_map.py` (14)
- `src/features/color/tracking/corner_pin.py` (14)
- `src/features/color/tracking/feature_detector.py` (14)
- `src/features/color/tracking/frame_sampler.py` (14)
- `src/features/color/tracking/homography_stability.py` (14)
- `src/features/color/tracking/merge.py` (14)
- `src/features/color/tracking/point_manager.py` (14)

</details>

<details><summary>batch-249 [P2] <code>src/features/color/tracking/</code>（8 文件，917 行）</summary>

- `src/features/color/tracking/shape_mask.py` (14)
- `src/features/color/tracking/track_data.py` (14)
- `src/features/color/tracking/tracker_2d.py` (14)
- `src/features/color/tracking/tracking_cache.py` (14)
- `src/features/color/tracking/tracking_evaluator.py` (274)
- `src/features/color/tracking/tracking_service.py` (373)
- `src/features/color/tracking/tracking_store.py` (118)
- `src/features/color/tracking/tracking_worker.py` (96)

</details>

<details><summary>batch-250 [P2] <code>src/features/color/tracking/</code>（1 文件，14 行）</summary>

- `src/features/color/tracking/types.py` (14)

</details>

<details><summary>batch-251 [P2] <code>src/features/color/ui/</code>（4 文件，833 行）</summary>

- `src/features/color/ui/__init__.py` (31)
- `src/features/color/ui/adjustment_panel.py` (104)
- `src/features/color/ui/auto_color_dialog.py` (129)
- `src/features/color/ui/blur_sharpen_panel.py` (569)

</details>

<details><summary>batch-252 [P2] <code>src/features/color/ui/</code>（1 文件，6,123 行）</summary>

- `src/features/color/ui/canvas.py` (6,123)

</details>

<details><summary>batch-253 [P2] <code>src/features/color/ui/</code>（6 文件，2,503 行）</summary>

- `src/features/color/ui/color_mapping_panel.py` (69)
- `src/features/color/ui/color_status_bar.py` (248)
- `src/features/color/ui/color_wheel.py` (1,259)
- `src/features/color/ui/crop_panel.py` (555)
- `src/features/color/ui/deblur_panel.py` (231)
- `src/features/color/ui/delivery_matrix_dialog.py` (141)

</details>

<details><summary>batch-254 [P2] <code>src/features/color/ui/</code>（8 文件，2,210 行）</summary>

- `src/features/color/ui/denoise_panel.py` (576)
- `src/features/color/ui/dependency_report_dialog.py` (110)
- `src/features/color/ui/film_grain_panel.py` (261)
- `src/features/color/ui/gl_viewport.py` (190)
- `src/features/color/ui/grade_paste_dialog.py` (81)
- `src/features/color/ui/hsl_curves_panel.py` (113)
- `src/features/color/ui/layer_panel.py` (820)
- `src/features/color/ui/lut_panel.py` (59)

</details>

<details><summary>batch-255 [P2] <code>src/features/color/ui/</code>（4 文件，2,710 行）</summary>

- `src/features/color/ui/mask_panel.py` (980)
- `src/features/color/ui/mixer_panel.py` (288)
- `src/features/color/ui/plugin_panel.py` (758)
- `src/features/color/ui/presets_panel.py` (684)

</details>

<details><summary>batch-256 [P2] <code>src/features/color/ui/</code>（4 文件，2,745 行）</summary>

- `src/features/color/ui/qualifier_panel.py` (769)
- `src/features/color/ui/raw_panel.py` (477)
- `src/features/color/ui/scope_panel.py` (1,017)
- `src/features/color/ui/soft_clip_panel.py` (482)

</details>

<details><summary>batch-257 [P2] <code>src/features/color/ui/</code>（3 文件，2,514 行）</summary>

- `src/features/color/ui/timeline_clip_strip.py` (1,802)
- `src/features/color/ui/tools_panel.py` (239)
- `src/features/color/ui/tracker_graph.py` (473)

</details>

<details><summary>batch-258 [P2] <code>src/features/color/ui/</code>（6 文件，2,671 行）</summary>

- `src/features/color/ui/tracker_overlay.py` (711)
- `src/features/color/ui/tracker_panel.py` (1,058)
- `src/features/color/ui/tracker_status.py` (67)
- `src/features/color/ui/tracker_toolbar.py` (178)
- `src/features/color/ui/viewer_guides.py` (540)
- `src/features/color/ui/warper_panel.py` (117)

</details>

<details><summary>batch-259 [P2] <code>src/features/scripting/</code>（4 文件，2,075 行）</summary>

- `src/features/scripting/__init__.py` (1)
- `src/features/scripting/api.py` (1,516)
- `src/features/scripting/composite_api.py` (177)
- `src/features/scripting/engine.py` (381)

</details>

<details><summary>batch-260 [P2] <code>src/features/scripting/examples/</code>（5 文件，165 行）</summary>

- `src/features/scripting/examples/advanced_workflow.py` (51)
- `src/features/scripting/examples/basic_grade.py` (23)
- `src/features/scripting/examples/batch_grade.py` (30)
- `src/features/scripting/examples/composite_batch_params.py` (29)
- `src/features/scripting/examples/composite_build_graph.py` (32)

</details>

<details><summary>batch-261 [P2] <code>src/features/scripting/</code>（1 文件，14 行）</summary>

- `src/features/scripting/module.py` (14)

</details>

<details><summary>batch-262 [P2] <code>src/features/shared/</code>（1 文件，1 行）</summary>

- `src/features/shared/__init__.py` (1)

</details>

<details><summary>batch-263 [P2] <code>src/features/shared/effects/</code>（4 文件，1,577 行）</summary>

- `src/features/shared/effects/__init__.py` (95)
- `src/features/shared/effects/dctl_wrapper.py` (165)
- `src/features/shared/effects/effect_capability.py` (574)
- `src/features/shared/effects/effect_catalog.py` (743)

</details>

<details><summary>batch-264 [P2] <code>src/features/shared/effects/</code>（5 文件，2,855 行）</summary>

- `src/features/shared/effects/effect_pass_runner.py` (1,602)
- `src/features/shared/effects/effect_spec.py` (294)
- `src/features/shared/effects/effect_tiers.py` (44)
- `src/features/shared/effects/plugin_identity.py` (524)
- `src/features/shared/effects/preview_policy.py` (391)

</details>

<details><summary>batch-265 [P2] <code>src/features/teaching_animation/</code>（1 文件，11 行）</summary>

- `src/features/teaching_animation/__init__.py` (11)

</details>

<details><summary>batch-266 [P2] <code>src/features/teaching_animation/ai/</code>（7 文件，2,739 行）</summary>

- `src/features/teaching_animation/ai/__init__.py` (610)
- `src/features/teaching_animation/ai/action_matrix.py` (566)
- `src/features/teaching_animation/ai/advanced_capabilities.py` (390)
- `src/features/teaching_animation/ai/approval_scope.py` (410)
- `src/features/teaching_animation/ai/artifact_links.py` (283)
- `src/features/teaching_animation/ai/asset_provenance.py` (283)
- `src/features/teaching_animation/ai/audio_plan.py` (197)

</details>

<details><summary>batch-267 [P2] <code>src/features/teaching_animation/ai/</code>（7 文件，2,729 行）</summary>

- `src/features/teaching_animation/ai/brief.py` (499)
- `src/features/teaching_animation/ai/capability_context.py` (342)
- `src/features/teaching_animation/ai/command_catalog.py` (960)
- `src/features/teaching_animation/ai/commands.py` (123)
- `src/features/teaching_animation/ai/context_collector.py` (319)
- `src/features/teaching_animation/ai/context_provider.py` (131)
- `src/features/teaching_animation/ai/creative_rubric.py` (355)

</details>

<details><summary>batch-268 [P2] <code>src/features/teaching_animation/ai/</code>（7 文件，2,912 行）</summary>

- `src/features/teaching_animation/ai/creative_templates.py` (409)
- `src/features/teaching_animation/ai/delivery_profile.py` (355)
- `src/features/teaching_animation/ai/design_validator.py` (777)
- `src/features/teaching_animation/ai/error_codes.py` (422)
- `src/features/teaching_animation/ai/eval_rubric.py` (337)
- `src/features/teaching_animation/ai/export_job.py` (329)
- `src/features/teaching_animation/ai/fallback.py` (283)

</details>

<details><summary>batch-269 [P2] <code>src/features/teaching_animation/ai/</code>（8 文件，2,760 行）</summary>

- `src/features/teaching_animation/ai/html_design_quality.py` (411)
- `src/features/teaching_animation/ai/html_edit_audit.py` (116)
- `src/features/teaching_animation/ai/html_edit_autonomy.py` (630)
- `src/features/teaching_animation/ai/html_generation_protocol.py` (582)
- `src/features/teaching_animation/ai/html_llm_generator.py` (316)
- `src/features/teaching_animation/ai/html_quality_gate.py` (251)
- `src/features/teaching_animation/ai/motion_graphic_eval.py` (201)
- `src/features/teaching_animation/ai/motion_quality_gate.py` (253)

</details>

<details><summary>batch-270 [P2] <code>src/features/teaching_animation/ai/</code>（8 文件，2,848 行）</summary>

- `src/features/teaching_animation/ai/preview.py` (436)
- `src/features/teaching_animation/ai/proposal.py` (548)
- `src/features/teaching_animation/ai/proposal_card.py` (364)
- `src/features/teaching_animation/ai/provider_registry.py` (37)
- `src/features/teaching_animation/ai/quality_gate.py` (320)
- `src/features/teaching_animation/ai/revision.py` (520)
- `src/features/teaching_animation/ai/rollout.py` (345)
- `src/features/teaching_animation/ai/service.py` (278)

</details>

<details><summary>batch-271 [P2] <code>src/features/teaching_animation/ai/</code>（4 文件，907 行）</summary>

- `src/features/teaching_animation/ai/session.py` (54)
- `src/features/teaching_animation/ai/session_store.py` (382)
- `src/features/teaching_animation/ai/slo_benchmark.py` (213)
- `src/features/teaching_animation/ai/test_matrix.py` (258)

</details>

<details><summary>batch-272 [P2] <code>src/features/teaching_animation/</code>（2 文件，191 行）</summary>

- `src/features/teaching_animation/cache_quality.py` (34)
- `src/features/teaching_animation/composite_node.py` (157)

</details>

<details><summary>batch-273 [P2] <code>src/features/teaching_animation/core/</code>（4 文件，1,912 行）</summary>

- `src/features/teaching_animation/core/__init__.py` (253)
- `src/features/teaching_animation/core/asset_provider.py` (353)
- `src/features/teaching_animation/core/audio_plan.py` (247)
- `src/features/teaching_animation/core/builtin_templates.py` (1,059)

</details>

<details><summary>batch-274 [P2] <code>src/features/teaching_animation/core/</code>（7 文件，2,810 行）</summary>

- `src/features/teaching_animation/core/commands.py` (1,242)
- `src/features/teaching_animation/core/components.py` (257)
- `src/features/teaching_animation/core/composition_schema.py` (186)
- `src/features/teaching_animation/core/constraints.py` (239)
- `src/features/teaching_animation/core/content_localization.py` (67)
- `src/features/teaching_animation/core/contracts.py` (526)
- `src/features/teaching_animation/core/design_tokens.py` (293)

</details>

<details><summary>batch-275 [P2] <code>src/features/teaching_animation/core/</code>（8 文件，2,240 行）</summary>

- `src/features/teaching_animation/core/devtools_property_mapping.py` (274)
- `src/features/teaching_animation/core/executable_template.py` (266)
- `src/features/teaching_animation/core/font_registry.py` (178)
- `src/features/teaching_animation/core/graphics.py` (332)
- `src/features/teaching_animation/core/hierarchy.py` (234)
- `src/features/teaching_animation/core/html_fonts.py` (183)
- `src/features/teaching_animation/core/html_layer.py` (247)
- `src/features/teaching_animation/core/html_layer_commands.py` (526)

</details>

<details><summary>batch-276 [P2] <code>src/features/teaching_animation/core/</code>（6 文件，2,919 行）</summary>

- `src/features/teaching_animation/core/html_page_deck.py` (909)
- `src/features/teaching_animation/core/html_template.py` (248)
- `src/features/teaching_animation/core/layout.py` (433)
- `src/features/teaching_animation/core/mg_package_registry.py` (208)
- `src/features/teaching_animation/core/migration.py` (187)
- `src/features/teaching_animation/core/model.py` (934)

</details>

<details><summary>batch-277 [P2] <code>src/features/teaching_animation/core/</code>（8 文件，1,483 行）</summary>

- `src/features/teaching_animation/core/motion_graphic_generator_registry.py` (149)
- `src/features/teaching_animation/core/motion_graphic_template_library.py` (198)
- `src/features/teaching_animation/core/motion_graphic_template_store.py` (266)
- `src/features/teaching_animation/core/pages.py` (221)
- `src/features/teaching_animation/core/professional.py` (161)
- `src/features/teaching_animation/core/style_schema.py` (233)
- `src/features/teaching_animation/core/template_commands.py` (156)
- `src/features/teaching_animation/core/template_registry.py` (99)

</details>

<details><summary>batch-278 [P2] <code>src/features/teaching_animation/core/</code>（3 文件，1,093 行）</summary>

- `src/features/teaching_animation/core/text_layout.py` (339)
- `src/features/teaching_animation/core/validator.py` (596)
- `src/features/teaching_animation/core/vector_shapes.py` (158)

</details>

<details><summary>batch-279 [P2] <code>src/features/teaching_animation/</code>（3 文件，1,436 行）</summary>

- `src/features/teaching_animation/export_service.py` (566)
- `src/features/teaching_animation/facade.py` (302)
- `src/features/teaching_animation/host_adapters.py` (568)

</details>

<details><summary>batch-280 [P2] <code>src/features/teaching_animation/</code>（1 文件，5,895 行）</summary>

- `src/features/teaching_animation/module.py` (5,895)

</details>

<details><summary>batch-281 [P2] <code>src/features/teaching_animation/motion/</code>（8 文件，1,640 行）</summary>

- `src/features/teaching_animation/motion/__init__.py` (108)
- `src/features/teaching_animation/motion/beat.py` (114)
- `src/features/teaching_animation/motion/easing.py` (233)
- `src/features/teaching_animation/motion/expression.py` (177)
- `src/features/teaching_animation/motion/interpolate.py` (159)
- `src/features/teaching_animation/motion/motion_binding.py` (77)
- `src/features/teaching_animation/motion/motion_commands.py` (461)
- `src/features/teaching_animation/motion/presets.py` (311)

</details>

<details><summary>batch-282 [P2] <code>src/features/teaching_animation/motion/</code>（3 文件，454 行）</summary>

- `src/features/teaching_animation/motion/sequence.py` (206)
- `src/features/teaching_animation/motion/spring.py` (138)
- `src/features/teaching_animation/motion/time_range.py` (110)

</details>

<details><summary>batch-283 [P2] <code>src/features/teaching_animation/presets/</code>（5 文件，868 行）</summary>

- `src/features/teaching_animation/presets/__init__.py` (42)
- `src/features/teaching_animation/presets/builders.py` (209)
- `src/features/teaching_animation/presets/fragment.py` (226)
- `src/features/teaching_animation/presets/i18n_fields.py` (19)
- `src/features/teaching_animation/presets/library.py` (372)

</details>

<details><summary>batch-284 [P2] <code>src/features/teaching_animation/presets/library/backgrounds/aurora_glow_bg/</code>（1 文件，48 行）</summary>

- `src/features/teaching_animation/presets/library/backgrounds/aurora_glow_bg/build.py` (48)

</details>

<details><summary>batch-285 [P2] <code>src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/</code>（1 文件，48 行）</summary>

- `src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/build.py` (48)

</details>

<details><summary>batch-286 [P2] <code>src/features/teaching_animation/presets/library/charts/animated_histogram/</code>（1 文件，50 行）</summary>

- `src/features/teaching_animation/presets/library/charts/animated_histogram/build.py` (50)

</details>

<details><summary>batch-287 [P2] <code>src/features/teaching_animation/presets/library/charts/line_curve_chart/</code>（1 文件，66 行）</summary>

- `src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py` (66)

</details>

<details><summary>batch-288 [P2] <code>src/features/teaching_animation/presets/library/charts/pie_donut_chart/</code>（1 文件，73 行）</summary>

- `src/features/teaching_animation/presets/library/charts/pie_donut_chart/build.py` (73)

</details>

<details><summary>batch-289 [P2] <code>src/features/teaching_animation/presets/library/light/lens_flare_stack/</code>（1 文件，60 行）</summary>

- `src/features/teaching_animation/presets/library/light/lens_flare_stack/build.py` (60)

</details>

<details><summary>batch-290 [P2] <code>src/features/teaching_animation/presets/library/light/light_sweep_banner/</code>（1 文件，41 行）</summary>

- `src/features/teaching_animation/presets/library/light/light_sweep_banner/build.py` (41)

</details>

<details><summary>batch-291 [P2] <code>src/features/teaching_animation/presets/library/particles/confetti_burst/</code>（1 文件，41 行）</summary>

- `src/features/teaching_animation/presets/library/particles/confetti_burst/build.py` (41)

</details>

<details><summary>batch-292 [P2] <code>src/features/teaching_animation/presets/library/particles/floating_orbs/</code>（1 文件，44 行）</summary>

- `src/features/teaching_animation/presets/library/particles/floating_orbs/build.py` (44)

</details>

<details><summary>batch-293 [P2] <code>src/features/teaching_animation/presets/library/shapes3d/isometric_cube/</code>（1 文件，50 行）</summary>

- `src/features/teaching_animation/presets/library/shapes3d/isometric_cube/build.py` (50)

</details>

<details><summary>batch-294 [P2] <code>src/features/teaching_animation/presets/library/shapes3d/wireframe_globe/</code>（1 文件，49 行）</summary>

- `src/features/teaching_animation/presets/library/shapes3d/wireframe_globe/build.py` (49)

</details>

<details><summary>batch-295 [P2] <code>src/features/teaching_animation/presets/library/text/kinetic_pop_title/</code>（1 文件，45 行）</summary>

- `src/features/teaching_animation/presets/library/text/kinetic_pop_title/build.py` (45)

</details>

<details><summary>batch-296 [P2] <code>src/features/teaching_animation/presets/library/text/neon_glow_title/</code>（1 文件，41 行）</summary>

- `src/features/teaching_animation/presets/library/text/neon_glow_title/build.py` (41)

</details>

<details><summary>batch-297 [P2] <code>src/features/teaching_animation/presets/</code>（4 文件，411 行）</summary>

- `src/features/teaching_animation/presets/loader.py` (102)
- `src/features/teaching_animation/presets/manifest.py` (116)
- `src/features/teaching_animation/presets/paths.py` (101)
- `src/features/teaching_animation/presets/preview.py` (92)

</details>

<details><summary>batch-298 [P2] <code>src/features/teaching_animation/render/</code>（8 文件，1,653 行）</summary>

- `src/features/teaching_animation/render/__init__.py` (85)
- `src/features/teaching_animation/render/advanced.py` (128)
- `src/features/teaching_animation/render/asset_resolver.py` (108)
- `src/features/teaching_animation/render/benchmark.py` (621)
- `src/features/teaching_animation/render/bitrate_probe.py` (146)
- `src/features/teaching_animation/render/cache.py` (272)
- `src/features/teaching_animation/render/composite_effect_bridge.py` (124)
- `src/features/teaching_animation/render/consistency.py` (169)

</details>

<details><summary>batch-299 [P2] <code>src/features/teaching_animation/render/</code>（3 文件，715 行）</summary>

- `src/features/teaching_animation/render/effect_capabilities.py` (357)
- `src/features/teaching_animation/render/evaluator.py` (290)
- `src/features/teaching_animation/render/frame_adapter.py` (68)

</details>

<details><summary>batch-300 [P2] <code>src/features/teaching_animation/render/html_layer/</code>（7 文件，2,133 行）</summary>

- `src/features/teaching_animation/render/html_layer/__init__.py` (58)
- `src/features/teaching_animation/render/html_layer/budget.py` (150)
- `src/features/teaching_animation/render/html_layer/html_patch.py` (200)
- `src/features/teaching_animation/render/html_layer/html_patch_apply.py` (352)
- `src/features/teaching_animation/render/html_layer/node_identity.py` (89)
- `src/features/teaching_animation/render/html_layer/sanitizer.py` (980)
- `src/features/teaching_animation/render/html_layer/template_promote.py` (304)

</details>

<details><summary>batch-301 [P2] <code>src/features/teaching_animation/render/</code>（4 文件，1,575 行）</summary>

- `src/features/teaching_animation/render/html_native_overlay.py` (618)
- `src/features/teaching_animation/render/html_preview_renderer.py` (37)
- `src/features/teaching_animation/render/html_realtime_recorder.py` (650)
- `src/features/teaching_animation/render/html_renderer_service.py` (270)

</details>

<details><summary>batch-302 [P2] <code>src/features/teaching_animation/render/html_renderer_spike/</code>（8 文件，2,781 行）</summary>

- `src/features/teaching_animation/render/html_renderer_spike/__init__.py` (106)
- `src/features/teaching_animation/render/html_renderer_spike/capture_backend.py` (244)
- `src/features/teaching_animation/render/html_renderer_spike/determinism.py` (92)
- `src/features/teaching_animation/render/html_renderer_spike/fixtures.py` (319)
- `src/features/teaching_animation/render/html_renderer_spike/html_spike.py` (280)
- `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py` (1,061)
- `src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py` (260)
- `src/features/teaching_animation/render/html_renderer_spike/security.py` (419)

</details>

<details><summary>batch-303 [P2] <code>src/features/teaching_animation/render/</code>（8 文件，1,672 行）</summary>

- `src/features/teaching_animation/render/html_scene_renderer_v2.py` (378)
- `src/features/teaching_animation/render/html_validation.py` (259)
- `src/features/teaching_animation/render/perf_slo.py` (164)
- `src/features/teaching_animation/render/release_fixtures.py` (166)
- `src/features/teaching_animation/render/release_manifest.py` (129)
- `src/features/teaching_animation/render/render_worker.py` (123)
- `src/features/teaching_animation/render/scene_renderer.py` (25)
- `src/features/teaching_animation/render/scene_renderer_router.py` (428)

</details>

<details><summary>batch-304 [P2] <code>src/features/teaching_animation/render/</code>（2 文件，883 行）</summary>

- `src/features/teaching_animation/render/svg_renderer.py` (629)
- `src/features/teaching_animation/render/v2_adapter.py` (254)

</details>

<details><summary>batch-305 [P2] <code>src/features/teaching_animation/</code>（2 文件，171 行）</summary>

- `src/features/teaching_animation/scene_cache.py` (74)
- `src/features/teaching_animation/scene_store.py` (97)

</details>

<details><summary>batch-306 [P2] <code>src/features/teaching_animation/ui/</code>（8 文件，2,497 行）</summary>

- `src/features/teaching_animation/ui/__init__.py` (42)
- `src/features/teaching_animation/ui/align.py` (115)
- `src/features/teaching_animation/ui/asset_library_panel.py` (744)
- `src/features/teaching_animation/ui/editor_controller.py` (346)
- `src/features/teaching_animation/ui/editor_window_registry.py` (57)
- `src/features/teaching_animation/ui/html_browser_preview.py` (712)
- `src/features/teaching_animation/ui/html_devtools_panel.py` (135)
- `src/features/teaching_animation/ui/html_inspector.py` (346)

</details>

<details><summary>batch-307 [P2] <code>src/features/teaching_animation/ui/</code>（4 文件，1,393 行）</summary>

- `src/features/teaching_animation/ui/html_source_editor.py` (172)
- `src/features/teaching_animation/ui/layer_tree_model.py` (183)
- `src/features/teaching_animation/ui/layer_tree_panel.py` (612)
- `src/features/teaching_animation/ui/mg_keyframes_model.py` (426)

</details>

<details><summary>batch-308 [P2] <code>src/features/teaching_animation/ui/</code>（2 文件，2,049 行）</summary>

- `src/features/teaching_animation/ui/mg_keyframes_panel.py` (1,955)
- `src/features/teaching_animation/ui/mg_shortcuts.py` (94)

</details>

<details><summary>batch-309 [P2] <code>src/features/teaching_animation/ui/</code>（1 文件，3,017 行）</summary>

- `src/features/teaching_animation/ui/motion_graphic_editor_window.py` (3,017)

</details>

<details><summary>batch-310 [P2] <code>src/features/teaching_animation/ui/</code>（7 文件，2,952 行）</summary>

- `src/features/teaching_animation/ui/motion_graphic_style.py` (461)
- `src/features/teaching_animation/ui/page_navigation.py` (140)
- `src/features/teaching_animation/ui/page_timing_panel.py` (433)
- `src/features/teaching_animation/ui/preview_bridge.py` (151)
- `src/features/teaching_animation/ui/scene_inspector.py` (958)
- `src/features/teaching_animation/ui/scene_timeline.py` (291)
- `src/features/teaching_animation/ui/scene_view.py` (518)

</details>

<details><summary>batch-311 [P2] <code>src/features/teaching_animation/ui/</code>（1 文件，86 行）</summary>

- `src/features/teaching_animation/ui/storyboard_panel.py` (86)

</details>

<details><summary>batch-312 [P2] <code>src/</code>（1 文件，312 行）</summary>

- `src/main.py` (312)

</details>

<details><summary>batch-313 [P2] <code>src/shared/</code>（1 文件，3 行）</summary>

- `src/shared/__init__.py` (3)

</details>

<details><summary>batch-314 [P2] <code>src/shared/actions/</code>（1 文件，92 行）</summary>

- `src/shared/actions/__init__.py` (92)

</details>

<details><summary>batch-315 [P2] <code>src/shared/ai_models/</code>（4 文件，1,580 行）</summary>

- `src/shared/ai_models/__init__.py` (75)
- `src/shared/ai_models/ai_mask_engine.py` (454)
- `src/shared/ai_models/ai_plugin_engine.py` (492)
- `src/shared/ai_models/denoise_worker.py` (559)

</details>

<details><summary>batch-316 [P2] <code>src/shared/ai_models/generation/</code>（7 文件，2,510 行）</summary>

- `src/shared/ai_models/generation/__init__.py` (8)
- `src/shared/ai_models/generation/credential_store.py` (342)
- `src/shared/ai_models/generation/generation_models.py` (618)
- `src/shared/ai_models/generation/image_gen_job.py` (251)
- `src/shared/ai_models/generation/model_pack_manager.py` (872)
- `src/shared/ai_models/generation/provider_profiles.py` (351)
- `src/shared/ai_models/generation/user_ai_config.py` (68)

</details>

<details><summary>batch-317 [P2] <code>src/shared/ai_models/generation/</code>（1 文件，564 行）</summary>

- `src/shared/ai_models/generation/volcengine_generation_provider.py` (564)

</details>

<details><summary>batch-318 [P2] <code>src/shared/ai_models/</code>（6 文件，2,518 行）</summary>

- `src/shared/ai_models/inference_engine.py` (996)
- `src/shared/ai_models/model_manager.py` (543)
- `src/shared/ai_models/nafnet.py` (149)
- `src/shared/ai_models/onnx_env.py` (353)
- `src/shared/ai_models/rife_interpolator.py` (141)
- `src/shared/ai_models/swinir.py` (336)

</details>

<details><summary>batch-319 [P2] <code>src/shared/</code>（1 文件，47 行）</summary>

- `src/shared/app_restart.py` (47)

</details>

<details><summary>batch-320 [P2] <code>src/shared/assets/</code>（2 文件，646 行）</summary>

- `src/shared/assets/__init__.py` (1)
- `src/shared/assets/icon_manager.py` (645)

</details>

<details><summary>batch-321 [P2] <code>src/shared/codec/</code>（4 文件，559 行）</summary>

- `src/shared/codec/__init__.py` (24)
- `src/shared/codec/color_metadata_config.py` (151)
- `src/shared/codec/decode_worker_budget.py` (222)
- `src/shared/codec/slice_threads_policy.py` (162)

</details>

<details><summary>batch-322 [P2] <code>src/shared/codec/</code>（1 文件，4,449 行）</summary>

- `src/shared/codec/topos_binding.py` (4,449)

</details>

<details><summary>batch-323 [P2] <code>src/shared/codec/</code>（4 文件，2,093 行）</summary>

- `src/shared/codec/topos_image_binding.py` (1,045)
- `src/shared/codec/topos_meta.py` (127)
- `src/shared/codec/topos_profiles.py` (805)
- `src/shared/codec/topos_trwm.py` (116)

</details>

<details><summary>batch-324 [P2] <code>src/shared/color/</code>（3 文件，2,881 行）</summary>

- `src/shared/color/__init__.py` (53)
- `src/shared/color/color_conversion.py` (39)
- `src/shared/color/color_picker.py` (2,789)

</details>

<details><summary>batch-325 [P2] <code>src/shared/color/</code>（4 文件，1,915 行）</summary>

- `src/shared/color/color_space_descriptor.py` (395)
- `src/shared/color/color_transform_provider.py` (864)
- `src/shared/color/lut_loader.py` (347)
- `src/shared/color/types.py` (309)

</details>

<details><summary>batch-326 [P2] <code>src/shared/config/</code>（2 文件，665 行）</summary>

- `src/shared/config/__init__.py` (3)
- `src/shared/config/config_manager.py` (662)

</details>

<details><summary>batch-327 [P2] <code>src/shared/control_surfaces/</code>（1 文件，1 行）</summary>

- `src/shared/control_surfaces/__init__.py` (1)

</details>

<details><summary>batch-328 [P2] <code>src/shared/control_surfaces/tangent/</code>（4 文件，725 行）</summary>

- `src/shared/control_surfaces/tangent/__init__.py` (45)
- `src/shared/control_surfaces/tangent/hub_client.py` (365)
- `src/shared/control_surfaces/tangent/protocol.py` (211)
- `src/shared/control_surfaces/tangent/settings.py` (104)

</details>

<details><summary>batch-329 [P2] <code>src/shared/db/</code>（2 文件，242 行）</summary>

- `src/shared/db/__init__.py` (3)
- `src/shared/db/database.py` (239)

</details>

<details><summary>batch-330 [P2] <code>src/shared/diagnostics/</code>（4 文件，498 行）</summary>

- `src/shared/diagnostics/__init__.py` (0)
- `src/shared/diagnostics/flash_diag.py` (47)
- `src/shared/diagnostics/performance_bundle.py` (171)
- `src/shared/diagnostics/resource_snapshot.py` (280)

</details>

<details><summary>batch-331 [P2] <code>src/shared/export/</code>（6 文件，2,760 行）</summary>

- `src/shared/export/__init__.py` (271)
- `src/shared/export/batch_queue_store.py` (195)
- `src/shared/export/encoder.py` (440)
- `src/shared/export/export_dialog.py` (280)
- `src/shared/export/export_panel.py` (1,541)
- `src/shared/export/export_source.py` (33)

</details>

<details><summary>batch-332 [P2] <code>src/shared/export/</code>（7 文件，2,532 行）</summary>

- `src/shared/export/export_stage_telemetry.py` (415)
- `src/shared/export/frame_provider.py` (499)
- `src/shared/export/gif_encoder.py` (212)
- `src/shared/export/gpu_benchmark_report.py` (219)
- `src/shared/export/gpu_encode_surface.py` (173)
- `src/shared/export/gpu_trail.py` (369)
- `src/shared/export/presets.py` (645)

</details>

<details><summary>batch-333 [P2] <code>src/shared/export/</code>（3 文件，2,450 行）</summary>

- `src/shared/export/quick_export_dialog.py` (1,326)
- `src/shared/export/render_cache.py` (264)
- `src/shared/export/sequence_exporter.py` (860)

</details>

<details><summary>batch-334 [P2] <code>src/shared/export/</code>（1 文件，3,537 行）</summary>

- `src/shared/export/topos_encoder.py` (3,537)

</details>

<details><summary>batch-335 [P2] <code>src/shared/export/</code>（2 文件，2,666 行）</summary>

- `src/shared/export/topos_rate_control.py` (227)
- `src/shared/export/video_encoder.py` (2,439)

</details>

<details><summary>batch-336 [P2] <code>src/shared/export/</code>（1 文件，365 行）</summary>

- `src/shared/export/video_export_worker.py` (365)

</details>

<details><summary>batch-337 [P2] <code>src/shared/</code>（2 文件，437 行）</summary>

- `src/shared/feature_flags.py` (310)
- `src/shared/ffmpeg_locator.py` (127)

</details>

<details><summary>batch-338 [P2] <code>src/shared/i18n/</code>（2 文件，425 行）</summary>

- `src/shared/i18n/__init__.py` (3)
- `src/shared/i18n/i18n.py` (422)

</details>

<details><summary>batch-339 [P2] <code>src/shared/io/</code>（2 文件，984 行）</summary>

- `src/shared/io/__init__.py` (3)
- `src/shared/io/file_io.py` (981)

</details>

<details><summary>batch-340 [P2] <code>src/shared/</code>（1 文件，1,146 行）</summary>

- `src/shared/keyframe.py` (1,146)

</details>

<details><summary>batch-341 [P2] <code>src/shared/layout/</code>（3 文件，744 行）</summary>

- `src/shared/layout/__init__.py` (18)
- `src/shared/layout/profile.py` (133)
- `src/shared/layout/store.py` (593)

</details>

<details><summary>batch-342 [P2] <code>src/shared/logging/</code>（2 文件，122 行）</summary>

- `src/shared/logging/__init__.py` (3)
- `src/shared/logging/log_manager.py` (119)

</details>

<details><summary>batch-343 [P2] <code>src/shared/media/</code>（4 文件，2,145 行）</summary>

- `src/shared/media/__init__.py` (478)
- `src/shared/media/adapter.py` (733)
- `src/shared/media/asset_proxy_manager.py` (413)
- `src/shared/media/cache.py` (521)

</details>

<details><summary>batch-344 [P2] <code>src/shared/media/</code>（3 文件，2,434 行）</summary>

- `src/shared/media/color_decode.py` (975)
- `src/shared/media/color_metadata.py` (888)
- `src/shared/media/decode_policy.py` (571)

</details>

<details><summary>batch-345 [P2] <code>src/shared/media/</code>（4 文件，2,545 行）</summary>

- `src/shared/media/decode_queue.py` (621)
- `src/shared/media/disk_proxy_service.py` (887)
- `src/shared/media/error_handling.py` (940)
- `src/shared/media/errors.py` (97)

</details>

<details><summary>batch-346 [P2] <code>src/shared/media/</code>（1 文件，1,296 行）</summary>

- `src/shared/media/factory.py` (1,296)

</details>

<details><summary>batch-347 [P2] <code>src/shared/media/</code>（1 文件，3,020 行）</summary>

- `src/shared/media/ffmpeg_source.py` (3,020)

</details>

<details><summary>batch-348 [P2] <code>src/shared/media/</code>（7 文件，2,801 行）</summary>

- `src/shared/media/format_registry.py` (222)
- `src/shared/media/frame.py` (481)
- `src/shared/media/frame_cache.py` (614)
- `src/shared/media/frame_path.py` (516)
- `src/shared/media/gop_index.py` (407)
- `src/shared/media/gpu_frame.py` (533)
- `src/shared/media/gpu_frame_handle.py` (28)

</details>

<details><summary>batch-349 [P2] <code>src/shared/media/</code>（1 文件，1,009 行）</summary>

- `src/shared/media/hwaccel.py` (1,009)

</details>

<details><summary>batch-350 [P2] <code>src/shared/media/macos_vt/</code>（7 文件，1,971 行）</summary>

- `src/shared/media/macos_vt/__init__.py` (7)
- `src/shared/media/macos_vt/capability.py` (621)
- `src/shared/media/macos_vt/cv_metal_bridge.py` (672)
- `src/shared/media/macos_vt/external_texture.py` (36)
- `src/shared/media/macos_vt/gpu_encode_surface.py` (154)
- `src/shared/media/macos_vt/gpu_export_bridge.py` (143)
- `src/shared/media/macos_vt/hardware_capability.py` (338)

</details>

<details><summary>batch-351 [P2] <code>src/shared/media/macos_vt/</code>（4 文件，2,951 行）</summary>

- `src/shared/media/macos_vt/iosurface_gl_bridge.py` (1,198)
- `src/shared/media/macos_vt/metal_capability_report.py` (151)
- `src/shared/media/macos_vt/packet_demux.py` (324)
- `src/shared/media/macos_vt/source.py` (1,278)

</details>

<details><summary>batch-352 [P2] <code>src/shared/media/macos_vt/</code>（2 文件，1,138 行）</summary>

- `src/shared/media/macos_vt/topos_surface.py` (203)
- `src/shared/media/macos_vt/vt_compression.py` (935)

</details>

<details><summary>batch-353 [P2] <code>src/shared/media/macos_vt/</code>（2 文件，2,717 行）</summary>

- `src/shared/media/macos_vt/vt_session.py` (1,891)
- `src/shared/media/macos_vt/vt_zero_copy_probe.py` (826)

</details>

<details><summary>batch-354 [P2] <code>src/shared/media/</code>（3 文件，2,477 行）</summary>

- `src/shared/media/metadata.py` (571)
- `src/shared/media/motion_graphic_delivery.py` (124)
- `src/shared/media/performance_monitor.py` (1,782)

</details>

<details><summary>batch-355 [P2] <code>src/shared/media/</code>（3 文件，2,382 行）</summary>

- `src/shared/media/pixel_format.py` (930)
- `src/shared/media/probe.py` (219)
- `src/shared/media/proxy.py` (1,233)

</details>

<details><summary>batch-356 [P2] <code>src/shared/media/</code>（3 文件，2,698 行）</summary>

- `src/shared/media/proxy_generator.py` (1,666)
- `src/shared/media/proxy_manager.py` (698)
- `src/shared/media/proxy_manifest.py` (334)

</details>

<details><summary>batch-357 [P2] <code>src/shared/media/</code>（3 文件，2,797 行）</summary>

- `src/shared/media/seek.py` (465)
- `src/shared/media/source.py` (360)
- `src/shared/media/thumbnail_provider.py` (1,972)

</details>

<details><summary>batch-358 [P2] <code>src/shared/media/</code>（5 文件，2,520 行）</summary>

- `src/shared/media/timecode.py` (527)
- `src/shared/media/timeline.py` (815)
- `src/shared/media/topos_frame_extract.py` (219)
- `src/shared/media/topos_image_sequence.py` (640)
- `src/shared/media/topos_image_source.py` (319)

</details>

<details><summary>batch-359 [P2] <code>src/shared/media/</code>（2 文件，2,467 行）</summary>

- `src/shared/media/topos_source.py` (2,240)
- `src/shared/media/topos_surface_capability.py` (227)

</details>

<details><summary>batch-360 [P2] <code>src/shared/media/windows_hw/</code>（8 文件，1,448 行）</summary>

- `src/shared/media/windows_hw/__init__.py` (87)
- `src/shared/media/windows_hw/capability.py` (178)
- `src/shared/media/windows_hw/d3d11_decoder.py` (274)
- `src/shared/media/windows_hw/d3d11_device.py` (396)
- `src/shared/media/windows_hw/d3d11_effects.py` (60)
- `src/shared/media/windows_hw/d3d11_export_capability.py` (197)
- `src/shared/media/windows_hw/d3d11_format_contract.py` (141)
- `src/shared/media/windows_hw/d3d11_frame.py` (115)

</details>

<details><summary>batch-361 [P2] <code>src/shared/media/windows_hw/</code>（8 文件，1,765 行）</summary>

- `src/shared/media/windows_hw/d3d11_gl_bridge.py` (127)
- `src/shared/media/windows_hw/d3d11_passthrough_chain.py` (204)
- `src/shared/media/windows_hw/d3d11_pipeline.py` (240)
- `src/shared/media/windows_hw/d3d11_render_graph.py` (328)
- `src/shared/media/windows_hw/d3d11_shaders.py` (46)
- `src/shared/media/windows_hw/d3d11_sync.py` (51)
- `src/shared/media/windows_hw/d3d11_texture_pool.py` (467)
- `src/shared/media/windows_hw/gpu_capability.py` (302)

</details>

<details><summary>batch-362 [P2] <code>src/shared/media/windows_hw/</code>（6 文件，1,116 行）</summary>

- `src/shared/media/windows_hw/gpu_encode_surface.py` (298)
- `src/shared/media/windows_hw/hw_planar_source.py` (292)
- `src/shared/media/windows_hw/native_bridge.py` (200)
- `src/shared/media/windows_hw/reasons.py` (71)
- `src/shared/media/windows_hw/vendor_policy.py` (105)
- `src/shared/media/windows_hw/windows_zero_copy_probe.py` (150)

</details>

<details><summary>batch-363 [P2] <code>src/shared/node_canvas/</code>（2 文件，440 行）</summary>

- `src/shared/node_canvas/graph_ui_protocol.py` (312)
- `src/shared/node_canvas/undo_stack.py` (128)

</details>

<details><summary>batch-364 [P2] <code>src/shared/node_help/</code>（2 文件，282 行）</summary>

- `src/shared/node_help/__init__.py` (123)
- `src/shared/node_help/dialog.py` (159)

</details>

<details><summary>batch-365 [P2] <code>src/shared/panels/</code>（2 文件，277 行）</summary>

- `src/shared/panels/__init__.py` (10)
- `src/shared/panels/registry.py` (267)

</details>

<details><summary>batch-366 [P2] <code>src/shared/paths/</code>（2 文件，121 行）</summary>

- `src/shared/paths/__init__.py` (3)
- `src/shared/paths/paths.py` (118)

</details>

<details><summary>batch-367 [P2] <code>src/shared/preferences/</code>（1 文件，158 行）</summary>

- `src/shared/preferences/__init__.py` (158)

</details>

<details><summary>batch-368 [P2] <code>src/shared/project/</code>（8 文件，2,125 行）</summary>

- `src/shared/project/__init__.py` (38)
- `src/shared/project/color_stack.py` (340)
- `src/shared/project/document.py` (32)
- `src/shared/project/edit_project_adapter.py` (140)
- `src/shared/project/layer_adapter.py` (43)
- `src/shared/project/library.py` (648)
- `src/shared/project/manifest.py` (248)
- `src/shared/project/offline_asset_manager.py` (636)

</details>

<details><summary>batch-369 [P2] <code>src/shared/project/</code>（3 文件，2,467 行）</summary>

- `src/shared/project/payload_cache.py` (228)
- `src/shared/project/project_manager.py` (710)
- `src/shared/project/save_router.py` (1,529)

</details>

<details><summary>batch-370 [P2] <code>src/shared/project/</code>（3 文件，2,424 行）</summary>

- `src/shared/project/schema_v1_spec.py` (876)
- `src/shared/project/topos_project.py` (926)
- `src/shared/project/topos_project_controller.py` (622)

</details>

<details><summary>batch-371 [P2] <code>src/shared/project/</code>（2 文件，1,390 行）</summary>

- `src/shared/project/topos_project_io.py` (1,174)
- `src/shared/project/topos_project_read_service.py` (216)

</details>

<details><summary>batch-372 [P2] <code>src/shared/</code>（1 文件，265 行）</summary>

- `src/shared/render_diagnostics.py` (265)

</details>

<details><summary>batch-373 [P2] <code>src/shared/tracking/</code>（8 文件，1,454 行）</summary>

- `src/shared/tracking/__init__.py` (18)
- `src/shared/tracking/coordinate_map.py` (141)
- `src/shared/tracking/corner_pin.py` (354)
- `src/shared/tracking/feature_detector.py` (125)
- `src/shared/tracking/frame_sampler.py` (269)
- `src/shared/tracking/homography_stability.py` (193)
- `src/shared/tracking/merge.py` (204)
- `src/shared/tracking/point_manager.py` (150)

</details>

<details><summary>batch-374 [P2] <code>src/shared/tracking/</code>（6 文件，2,126 行）</summary>

- `src/shared/tracking/shape_mask.py` (135)
- `src/shared/tracking/stabilize.py` (186)
- `src/shared/tracking/track_data.py` (282)
- `src/shared/tracking/tracker_2d.py` (1,151)
- `src/shared/tracking/tracking_cache.py` (131)
- `src/shared/tracking/types.py` (241)

</details>

<details><summary>batch-375 [P2] <code>src/shared/ui/</code>（1 文件，2 行）</summary>

- `src/shared/ui/__init__.py` (2)

</details>

<details><summary>batch-376 [P2] <code>src/shared/ui/audio_mixer/</code>（4 文件，2,627 行）</summary>

- `src/shared/ui/audio_mixer/__init__.py` (17)
- `src/shared/ui/audio_mixer/audio_fader.py` (676)
- `src/shared/ui/audio_mixer/audio_level_meter.py` (1,218)
- `src/shared/ui/audio_mixer/audio_mixer_panel.py` (716)

</details>

<details><summary>batch-377 [P2] <code>src/shared/ui/audio_mixer/</code>（8 文件，2,028 行）</summary>

- `src/shared/ui/audio_mixer/automation_lane.py` (396)
- `src/shared/ui/audio_mixer/compressor_dialog.py` (298)
- `src/shared/ui/audio_mixer/delay_dialog.py` (294)
- `src/shared/ui/audio_mixer/dynamics_dialog.py` (480)
- `src/shared/ui/audio_mixer/eq_presets.py` (66)
- `src/shared/ui/audio_mixer/imager_dialog.py` (255)
- `src/shared/ui/audio_mixer/limiter_dialog.py` (206)
- `src/shared/ui/audio_mixer/mixer_feature_flags.py` (33)

</details>

<details><summary>batch-378 [P2] <code>src/shared/ui/audio_mixer/</code>（8 文件，2,283 行）</summary>

- `src/shared/ui/audio_mixer/mixer_separator.py` (31)
- `src/shared/ui/audio_mixer/noise_gate_dialog.py` (265)
- `src/shared/ui/audio_mixer/preset_dialog.py` (224)
- `src/shared/ui/audio_mixer/reverb_dialog.py` (272)
- `src/shared/ui/audio_mixer/routing_dialog.py` (349)
- `src/shared/ui/audio_mixer/routing_matrix_dialog.py` (279)
- `src/shared/ui/audio_mixer/saturation_dialog.py` (245)
- `src/shared/ui/audio_mixer/strip_eq_dialog.py` (618)

</details>

<details><summary>batch-379 [P2] <code>src/shared/ui/audio_mixer/</code>（1 文件，1,919 行）</summary>

- `src/shared/ui/audio_mixer/track_mixer_widget.py` (1,919)

</details>

<details><summary>batch-380 [P2] <code>src/shared/ui/</code>（8 文件，2,858 行）</summary>

- `src/shared/ui/column_grid.py` (222)
- `src/shared/ui/dock_context_menu.py` (37)
- `src/shared/ui/dock_tab_style.py` (78)
- `src/shared/ui/effect_degradation_indicator.py` (309)
- `src/shared/ui/panel_divider.py` (58)
- `src/shared/ui/playback_hud.py` (1,180)
- `src/shared/ui/playback_hud_integration.py` (464)
- `src/shared/ui/shortcut_manager.py` (510)

</details>

<details><summary>batch-381 [P2] <code>src/shared/ui/</code>（3 文件，2,130 行）</summary>

- `src/shared/ui/shortcut_scope.py` (83)
- `src/shared/ui/style_sheet.py` (1,826)
- `src/shared/ui/video_control_panel.py` (221)

</details>

<details><summary>batch-382 [P2] <code>src/shared/ui/widgets/</code>（8 文件，2,300 行）</summary>

- `src/shared/ui/widgets/__init__.py` (14)
- `src/shared/ui/widgets/chevron_combo.py` (119)
- `src/shared/ui/widgets/cie_scope_widget.py` (555)
- `src/shared/ui/widgets/clickable_slider.py` (32)
- `src/shared/ui/widgets/color_picker_control.py` (170)
- `src/shared/ui/widgets/color_warper.py` (266)
- `src/shared/ui/widgets/curves_widget.py` (749)
- `src/shared/ui/widgets/drag_value.py` (395)

</details>

<details><summary>batch-383 [P2] <code>src/shared/ui/widgets/</code>（8 文件，2,642 行）</summary>

- `src/shared/ui/widgets/gpu_scope_widget.py` (509)
- `src/shared/ui/widgets/hsl_curves_widget.py` (955)
- `src/shared/ui/widgets/lut_gallery.py` (327)
- `src/shared/ui/widgets/param_slider_row.py` (246)
- `src/shared/ui/widgets/pref_drag_value.py` (56)
- `src/shared/ui/widgets/pref_form_layout.py` (90)
- `src/shared/ui/widgets/qualifier_range_bar.py` (283)
- `src/shared/ui/widgets/realtime_fps_indicator.py` (176)

</details>

<details><summary>batch-384 [P2] <code>src/shared/ui/widgets/</code>（4 文件，2,223 行）</summary>

- `src/shared/ui/widgets/toggle_switch.py` (80)
- `src/shared/ui/widgets/topos_controls.py` (293)
- `src/shared/ui/widgets/vectorscope_widget.py` (1,513)
- `src/shared/ui/widgets/viewport_toolbar_base.py` (337)

</details>

<details><summary>batch-385 [P2] <code>src/shared/utils/</code>（2 文件，102 行）</summary>

- `src/shared/utils/__init__.py` (3)
- `src/shared/utils/geometry_math.py` (99)

</details>

<details><summary>batch-386 [P2] <code>src/shared/vault/</code>（3 文件，392 行）</summary>

- `src/shared/vault/__init__.py` (25)
- `src/shared/vault/bridge.py` (232)
- `src/shared/vault/client.py` (135)

</details>

<details><summary>batch-387 [P2] <code>src/shared/video/</code>（5 文件，1,787 行）</summary>

- `src/shared/video/__init__.py` (263)
- `src/shared/video/camera_gamut_matrices.py` (280)
- `src/shared/video/cfa_cpu_debayer.py` (149)
- `src/shared/video/cfa_gpu_debayer.py` (433)
- `src/shared/video/color_frame_provider_adapter.py` (662)

</details>

<details><summary>batch-388 [P2] <code>src/shared/video/</code>（1 文件，1,526 行）</summary>

- `src/shared/video/decode_worker.py` (1,526)

</details>

<details><summary>batch-389 [P2] <code>src/shared/video/denoise/</code>（4 文件，829 行）</summary>

- `src/shared/video/denoise/__init__.py` (26)
- `src/shared/video/denoise/frame_types.py` (216)
- `src/shared/video/denoise/params.py` (264)
- `src/shared/video/denoise/window_provider.py` (323)

</details>

<details><summary>batch-390 [P2] <code>src/shared/video/</code>（3 文件，1,110 行）</summary>

- `src/shared/video/external_yuv_renderer.py` (269)
- `src/shared/video/fallback_types.py` (626)
- `src/shared/video/frame_provider_protocol.py` (215)

</details>

<details><summary>batch-391 [P2] <code>src/shared/video/</code>（1 文件，2,376 行）</summary>

- `src/shared/video/frame_provider_service.py` (2,376)

</details>

<details><summary>batch-392 [P2] <code>src/shared/video/</code>（5 文件，2,757 行）</summary>

- `src/shared/video/frame_queue.py` (829)
- `src/shared/video/frame_request.py` (268)
- `src/shared/video/frame_trace_schema.py` (159)
- `src/shared/video/monitor_output.py` (1,240)
- `src/shared/video/performance_budgets.py` (261)

</details>

<details><summary>batch-393 [P2] <code>src/shared/video/</code>（4 文件，2,739 行）</summary>

- `src/shared/video/pipeline_decision.py` (622)
- `src/shared/video/pixel_copy_counters.py` (88)
- `src/shared/video/playback_clock.py` (1,439)
- `src/shared/video/playback_frame_trace.py` (590)

</details>

<details><summary>batch-394 [P2] <code>src/shared/video/</code>（4 文件，2,639 行）</summary>

- `src/shared/video/playback_state.py` (1,023)
- `src/shared/video/render_commands.py` (45)
- `src/shared/video/scrub_stride.py` (64)
- `src/shared/video/session.py` (1,507)

</details>

<details><summary>batch-395 [P2] <code>src/shared/video/</code>（1 文件，1,052 行）</summary>

- `src/shared/video/video_async_renderer.py` (1,052)

</details>

<details><summary>batch-396 [P2] <code>src/shared/video/</code>（1 文件，4,982 行）</summary>

- `src/shared/video/video_playback.py` (4,982)

</details>

<details><summary>batch-397 [P2] <code>src/shared/video/</code>（3 文件，1,610 行）</summary>

- `src/shared/video/video_texture_pool.py` (378)
- `src/shared/video/video_upload.py` (900)
- `src/shared/video/yuv_gpu_self_test.py` (332)

</details>

<details><summary>batch-398 [P2] <code>src/shared/video/</code>（1 文件，3,294 行）</summary>

- `src/shared/video/yuv_gpu_upload.py` (3,294)

</details>
