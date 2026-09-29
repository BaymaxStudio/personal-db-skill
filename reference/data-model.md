# 数据模型

个人 DB 把"事实"存进一个本地 SQLite 库。理解四个概念就够了：

- **实体 entity**：一个人、一段教育、一段经历、一项技能、一个奖项。
- **事实 fact**：挂在某个实体上的一条字段值（如"院校 = 示例大学"）。一条事实带语言标记（locale）、状态（status）和指向来源的证据。
- **来源 source**：一份原始材料文件（简历 PDF、成绩单、获奖证明、口述补充的 txt……），带 SHA-256 指纹。
- **表达 expression**（可选，进阶）：从若干已确认事实里提炼的一段主观叙述/句式（如自我介绍、研究兴趣段落），供不同投递场景复用。

当前数据库结构版本为 **3**。`fact_supersessions` 保存新旧事实之间的替代关系；同一实体、字段和语言只能有一条已确认事实，空语言同样受唯一约束。迁移会核对历史版本与迁移 SQL 指纹，发现未知版本、迁移记录不连续或重复确认值时回滚，不自动删除或合并事实。

## 实体类型与 entityKey

实体用一个稳定的 `entityKey`（小写，形如 `<类型>.<短标识>`）唯一标识。同一个 entityKey 的多条事实归到同一张卡片。

| entityType | entityKey 例子 | 说明 |
|---|---|---|
| `person` | `person.owner` | 约定使用此 ID；导出要求恰好一个有确认事实的 person 实体。 |
| `education` | `education.bachelor`、`education.master`、`education.minor` | 一段学历一个 key。 |
| `experience` | `experience.intern-abc`、`experience.thesis`、`experience.club-xyz` | 实习/科研/学生工作/项目都属 experience，用 `experience.type` 区分。 |
| `skill` | `skill.python`、`skill.english` | 一项技能一个 key。 |
| `award` | `award.scholarship-2024` | 一个奖项一个 key。 |

`experience.type` 的取值（决定查看器里的分区）：
`professional`(实习经历) / `research`(科研与论文) / `leadership`(校园经历) / `project`(项目与作品，含作品集/开源/摄影等)。

查看器的其他分区约定：
- 证明人用 `experience.reference-<标识>` 这样的 entityKey，自动归到「证明人」分区。
- 同校的辅修（学位或专业里带「辅修」/ minor）挂在主修下面显示；`experience.fact.url` 落在某个条目 `experience.fact.homeurl` 之下的项目，挂在那个条目下面显示（比如 GitHub 组织下的各个仓库）。
- 个人专属的分区、字段显示名、排序写进 `web/viewer-config.json`，格式见 `viewer-config.example.json`。

## 字段路径 field_path 白名单

只有下列 field_path 能写入（写错会被 validate-candidates 拦下）：

**person**：`person.fullName.zhCN`、`person.fullName.en`、`person.fullName.givenNameEn`、`person.fullName.familyNameEn`、`person.contact.email`、`person.contact.phone`、`person.location.currentCity`、`person.location.currentCountry`

**education**：`education.institution`、`education.degree`、`education.major`、`education.minor`、`education.location`、`education.startDate`、`education.endDate`、`education.gpa`、`education.averageScore`、`education.ranking`

**experience（结构字段）**：`experience.type`、`experience.title`、`experience.organization`、`experience.role`、`experience.location`、`experience.startDate`、`experience.endDate`

**experience（叙述要点）**：`experience.fact.<slug>` —— slug 自由命名（小写字母/数字/`._-`）。常用：`overview`(项目介绍)、`description`(项目描述)、`recognition`(采信/获奖)、`role`(身份)、`duty`(职责)、`chapters`(章节/分工)、`method`(方法)、`samples`(样本)、`prep`(准备阶段)、`fieldwork`(调研执行)、`report`(成果)、`score`(成绩)、`assessment`(评价)、`topic`(作品/论文题目)、`workflow`(协作方式)、`url`(链接)。

**skill**：`skill.category`、`skill.name`、`skill.level`、`skill.details`
（`skill.category` 取值：`language`/`technical`/`research`/`qualification`/`other`）

**award**：`award.name`、`award.issuer`、`award.date`、`award.details`

## 语言 locale

- 中文内容用 `zh-CN`，英文用 `en`。
- 纯数字/日期/邮箱等与语言无关的，可留空（"通用"）。
- 同一字段可以中英各存一条（同 entityKey + 同 field_path + 不同 locale），查看器会成对显示，顶部还能按语言筛选。填英文网申切 English、中文网申切中文。

导出快照使用 `schemaVersion: "1.1.0"`。普通字段保留默认版本的 `factId`、`value` 和可选 `locale`；其他语言保存在 `alternatives` 数组，每个版本都有独立事实 ID，可被表达引用。默认版本按中文、英文、通用、其他语言的顺序选取。语言变体不能嵌套，校验器拒绝重复事实 ID 和重复语言。

叙述要点 `experience.fact.*` 的各语言版本分别保存在 `experiences[].facts[]` 中，每条使用 `factId`、`text` 和可选 `locale`，不使用 `alternatives`。

旧的 1.0.0 快照继续可校验，但不能带 `alternatives`。`experience.type` 和 `skill.category` 作为结构枚举导出，不是可供表达引用的内容事实。

## 状态 status

`pending`(候选，待确认) → `confirmed`(本人确认，正式) / `rejected`(否掉) / `conflict`(与已有冲突，待裁决)。
**查看器和导出只显示 `confirmed`。** 事实要变成 confirmed，必须走 decisions 流水线（见 pipeline.md）。

替换事实时，旧事实置为 `rejected` 并保留全部历史；引用它的已确认表达转为 `pending`，正文与原引用不变，等待复核。查看器也会排除引用失效事实的表达。导出要求每条已确认事实至少有一个来源，并校验表达引用和实体引用完整性。

## 来源分级

查看器按来源 ID 和文件名推测来源等级，并按这个顺序展示；这不是材料真实性认定：

| 级别 | 判断 | 例子 |
|---|---|---|
| 一手 | 其余都算一手 | 成绩单、学位证、实习证明、获奖证书、offer |
| 本人陈述 | sourceId 含 `supplement`，或文件名含「本人 / 陈述 / 表述 / 补充 / 信息采集 / 总结」 | 口述补充落成的 md、学年总结 |
| 二手 | sourceId 或文件名含「简历 / 履历 / resume / cv」 | 各版本简历 |

给来源起 sourceId 时可按这个约定命名（如 `evidence.transcript`、`supplement.award-details-2026`、`source.resume.zh`），但仍需结合原件核验等级。

同一个 sourceId 的文件路径、文档类型和 SHA-256 构成登记身份，新增事实或补挂来源不能用它替换成另一份文件。移动文件走专门的路径迁移命令；文件内容变更则核对版本并登记新来源。

来源指纹核验另行返回 `verified`、`changed`、`missing`、`unreadable`，分别表示一致、内容变更、文件缺失、无法读取。它不改动事实或登记指纹，也不证明文件内容真实。查看器显示核验结果；导出遇到来源文件异常会提示，保留事实及旧证据登记。

## 日期写法

`YYYY-MM` 或 `YYYY-MM-DD`。查看器按开始时间倒序排列同组卡片。
