# 数据模型

个人 DB 把"事实"存进一个本地 SQLite 库。理解四个概念就够了：

- **实体 entity**：一个人、一段教育、一段经历、一项技能、一个奖项。
- **事实 fact**：挂在某个实体上的一条字段值（如"院校 = 示例大学"）。一条事实带语言标记（locale）、状态（status）和指向来源的证据。
- **来源 source**：一份原始材料文件（简历 PDF、成绩单、获奖证明、口述补充的 txt……），带 SHA-256 指纹。
- **表达 expression**（可选，进阶）：从若干已确认事实里提炼的一段主观叙述/句式（如自我介绍、研究兴趣段落），供不同投递场景复用。

## 实体类型与 entityKey

实体用一个稳定的 `entityKey`（小写，形如 `<类型>.<短标识>`）唯一标识。同一个 entityKey 的多条事实归到同一张卡片。

| entityType | entityKey 例子 | 说明 |
|---|---|---|
| `person` | `person.owner` | **必须存在，且必须叫 `person.owner`**；导出时硬校验。 |
| `education` | `education.bachelor`、`education.master`、`education.minor` | 一段学历一个 key。 |
| `experience` | `experience.intern-abc`、`experience.thesis`、`experience.club-xyz` | 实习/科研/学生工作/项目都属 experience，用 `experience.type` 区分。 |
| `skill` | `skill.python`、`skill.english` | 一项技能一个 key。 |
| `award` | `award.scholarship-2024` | 一个奖项一个 key。 |

`experience.type` 的取值（决定查看器里的分组）：
`professional`(实习) / `research`(科研与论文) / `leadership`(社会实践·学生工作) / `project`(项目与作品，含作品集/开源/摄影等)。

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

## 状态 status

`pending`(候选，待确认) → `confirmed`(本人确认，正式) / `rejected`(否掉) / `conflict`(与已有冲突，待裁决)。
**查看器和导出只显示 `confirmed`。** 事实要变成 confirmed，必须走 decisions 流水线（见 pipeline.md）。

## 日期写法

`YYYY-MM` 或 `YYYY-MM-DD`。查看器按开始时间倒序排列同组卡片。
