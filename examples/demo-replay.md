# 端到端运行回放（真实输出，已脱敏）

> 生成方式：bash tests/run_e2e.sh（合成数据，全虚构）。

```text
== 1. 脚手架：把 app/ 复制成临时项目 ==
== 2. init ==
数据库已初始化：<skill>/tests/.demo-project/data/career_profile.sqlite3
标准目录已就绪：materials/ staging/ review/ exports/（材料请放进 materials/）
== 3. 放入合成材料 ==
== 4. 生成候选与决定文件 ==
== 5. validate-candidates（应通过） ==
候选文件校验通过：1 个来源，15 个候选
== 6. 负向测试：坏 excerpt 必须被拒 ==
OK: 坏 excerpt 被正确拒绝
== 7. apply-decisions ==
已应用 15 个决定；profile_revision = 1，备份：career_profile.20260917T154445Z.86b01b82b05be968295fd19eb07829a7544a9de599e9acd919e8c52d07648449.db
== 8. export + validate-export ==
已导出 Snapshot：exports/career-profile.snapshot.json（profileRevision=1）
Snapshot 校验通过（符合共享契约）
== 9. 断言快照内容 ==
OK: 快照含 person/education/award 且值正确，profileRevision = 1
== 10. 查看器 API 烟测 ==
OK: 查看器 entities=5 facts=15 sources=1

全部通过。演示项目留在：<skill>/tests/.demo-project
```

退出码：0
