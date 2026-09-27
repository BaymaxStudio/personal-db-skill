# 端到端运行回放（真实输出，已脱敏）

> 生成方式：bash tests/run_e2e.sh（合成数据，全虚构）。备份文件名里的时间和哈希已替换成占位符。

```text
== 0. 单元测试 ==
Ran 45 tests
OK
== 1. 脚手架：把 app/ 复制成临时项目 ==
== 2. init ==
数据库已初始化：<skill>/tests/.demo-project/data/career_profile.sqlite3
素材目录：<skill>/tests/.demo-project/materials
== 3. 放入合成材料（简历、成绩单、本人补充陈述） ==
== 4. 生成候选与决定文件 ==
== 5. validate-candidates（应通过） ==
候选文件校验通过：3 个来源，37 个候选
== 6. 负向测试：坏 excerpt 必须被拒 ==
OK: 坏 excerpt 被正确拒绝
== 7. apply-decisions ==
已应用 37 个决定；profile_revision = 1，备份：career_profile.<时间>.<sha256>.db
== 8. 补挂一手凭证：GPA 等事实原来只引用简历，补挂成绩单 ==
补挂来源文件校验通过：1 个来源，4 条补挂，0 条撤销
已补挂 4 条、撤销 0 条（否决 0 条）；profile_revision = 2，备份：career_profile.<时间>.<sha256>.db
== 9. 挪动材料后同步登记路径 ==
需要更新路径的来源 1 个，共登记 3 个
已更新 1 个来源路径，备份：career_profile.<时间>.<sha256>.db
== 10. export + validate-export ==
已导出 Snapshot：exports/career-profile.snapshot.json（profileRevision=2）
Snapshot 校验通过（符合共享契约）
== 11. 断言快照内容 ==
OK: 快照含 person/education/award 且值正确，profileRevision = 2
== 12. 查看器 API 烟测 ==
OK: 查看器 entities=7 facts=37 sources=3，GPA 的一手凭证排在最前

全部通过。演示项目留在：<skill>/tests/.demo-project
```

退出码：0
