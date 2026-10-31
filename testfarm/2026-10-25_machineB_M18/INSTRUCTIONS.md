# M18 机器 B 复验运行指令（Mac 侧 2026-10-25 投递；N02 后更新版）

Mac 侧已推送 `fix-ui` 至 origin（HEAD `91446d42b`，含 M18.4b 接入包
+ N02 严格复验裁决重写 + W09 schema2 假绿修复 + W01-W10 全量）。

机器 B 执行：

1. `git fetch && git checkout fix-ui && git pull`
2. `bash testfarm/2026-10-25_machineB_M18/run_m18_recheck.sh [Windows打包目录]`
   - 无打包产物可省目录参数——PE ABI 段按 `not_run` 如实记录
     （不冒称安装包门通过；有产物时顶层唯一 .exe 自动作主入口，
     多个/零个需 `--main-exe <相对路径>` 显式指定）。
   - 脚本（`scripts/m18_machine_b_recheck.py`，N02 重写版）行为要点：
     * 仅 Windows 宿主可跑（其他宿主全门 not_run 快速失败）；
     * 报告含 git HEAD + dirty 指纹（受测代码可对齐）；
     * 单门超时 1800s 为独立 `timeout` 状态；探针缺失/无 `--dist`
       为 `not_run` 分级，不伪装成通过；
     * 默认只跑 golden chain3——`three_chains_verified` 恒 False
       如实声明（chain1/2 无独立复验入口）；
     * `--dist` 存在时走严格 ABI 裁决（strict_verdict：空/畸形/截断/
       架构不匹配/缺主入口全部 FAIL，不再只看 consistent 字段）。
3. 产物回传：`git add -A && git commit -m "[machine_B] M18复验回传" && git push`
   （报告 JSON 写入 docs/validation/upgrade_2026-10-24/M18/machine_B/report/）

可选加跑（N23 修复后的 W09 基准，schema 2）：

    .venv/Scripts/python.exe scripts/bench_ab_routes_w09.py \
        --out docs/validation/upgrade_2026-10-24/W09/_work/ab_results_v2.json

失败（md5 mismatch/路由退化/零样本）现在非零退出且报告保留——如实
回传即可，不要为绿改 skip/容差。

Mac 侧收到回传后按 `host_check`+`overall`+各门分级状态闭环 Windows
复验与 Windows 侧安装包 ABI 两门（部分门 not_run 时按 N17 条件项
登记，不整体冒称通过）。
