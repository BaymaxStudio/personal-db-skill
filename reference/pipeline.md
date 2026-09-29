# 写入流水线与命令

**铁律：不能直接改数据库。** 任何事实都要走"候选 → 校验 → 本人拍板 → 入库 → 导出"这条线。
CLI 只用 Python 标准库，无需 pip 安装。以下命令都在项目根目录（`career_profile.py` 所在处）执行。

## 两条硬约束（写死在校验里，绕不过）

1. **每条事实必须带来源，且来源文件必须在 `materials/` 目录内。**
   口述补充的内容，要先落成一份文件（如 `materials/口述-2026.txt`）放进去，再作为来源引用。
   （可用环境变量 `PERSONAL_DB_MATERIALS` 或项目内 `config.json` 的 `{"materials":[...]}` 改允许目录。）
2. **来源是 md / html / txt 时，`excerpt`（证据摘录）必须逐字出现在该文件里**（包含性检查）。
   PDF / docx 不做逐字检查（程序不解析二进制），但仍要求文件存在且 SHA-256 匹配。

## 五步

```bash
# 0) 首次：初始化库（可重复执行，不清空数据；会建好 materials/ staging/ review/ exports/）
python3 career_profile.py init

# 1) 写候选文件（暂存）。见 templates/candidates.template.json
#    先把材料放进 materials/，再算 SHA-256：
shasum -a 256 materials/简历.pdf
#    然后写 staging/candidates-<批次名>.json

# 2) 校验结构（来源在不在 materials/、SHA 对不对、excerpt 逐不逐字、field_path 合不合法）
python3 career_profile.py validate-candidates staging/candidates-<批次名>.json

# 3) 写决定文件（本人拍板）。见 templates/decisions.template.json
#    每个 candidateId 给 accept / reject / replace
#    → review/decisions-<批次名>.json

# 4) 入库（写库前自动备份到 backups/）
python3 career_profile.py apply-decisions staging/candidates-<批次名>.json review/decisions-<批次名>.json

# 5) 更新导出快照并校验共享契约
python3 career_profile.py export exports/career-profile.snapshot.json
python3 career_profile.py validate-export exports/career-profile.snapshot.json
```

> **导出要求恰好一个有确认事实的 person 实体**。
> 所以第一批候选里应当包含姓名等 `person.*` 字段，约定使用 entityKey = `person.owner`。

`init` 将旧库升级到结构版本 3，保留原有事实和证据。写入命令先取得写事务，再通过 SQLite 在线备份保存已提交状态，迁移和实际修改整体提交或回滚。备份包含 WAL 中已提交的数据；不要用只复制 `.sqlite3` 主文件的方法备份运行中的库。

导出在一致的事务中生成 1.1.0 快照，普通字段的其他语言放在 `alternatives` 数组中，各自保留事实 ID；先校验内存中的快照，再完整写入同目录临时文件并替换正式快照。生成失败时保留原文件。叙述要点的各语言版本仍分别保存在 `experiences[].facts[]`。`validate-export` 兼容 1.0.0 和 1.1.0，检查结构及引用完整性，不证明事实或材料语义真实。

## 候选与决定文件格式

- 候选：`templates/candidates.template.json`。要点：
  - `sources[].sourceId` 是小写稳定 id（3–120 字符，形如 `evidence.resume`）。
  - `sources[].absolutePath` 用**绝对路径**，且指向 `materials/` 内的文件。
  - `sources[].sha256` 是该文件的 SHA-256（`shasum -a 256`）。
  - 每个候选：`entityType` / `entityKey` / `fieldPath` / `proposedValue` / `locale` / `status:"pending"` / `sourceRefs[]`（含 `sourceId` `locator` `excerpt`）。
- 决定：`templates/decisions.template.json`。`action` ∈ `accept` / `reject` / `replace`；`replace` 时用 `replacementValue` 覆盖后采纳。

来源 ID 已登记时，路径、文档类型、SHA-256 必须与登记一致；不能用原 ID 登记修改后的材料。同一实体、字段和语言只能有一个确认值，空语言也受此约束；有冲突时按来源裁决，不覆盖旧事实。

## 表达（进阶，可选）

把已确认事实提炼成一段可复用的主观叙述（自我介绍、研究兴趣……）：

```bash
python3 career_profile.py add-expression \
  --id expr.intro.zh --purpose self-intro --locale zh-CN \
  --text "……一段话……" \
  --source-fact-ids <fact_id1>,<fact_id2> \
  --target-roles ra,phd --max-chars 300
python3 career_profile.py list-expressions
```

表达只可引用已确认、带来源且会进入导出的内容事实。`experience.type`、`skill.category` 是结构枚举，不能作为 `sourceFactIds`。其他语言的内容事实仍保留自己的 ID，可作为引用依据。

## 替换已有事实的值

候选里加 `"supersedes": "<旧 fact_id>"`。新值被采纳（accept / replace）时，旧事实在同一事务里置为 `rejected`，留痕不删；
否掉新值则旧事实原样保留。旧事实必须是 confirmed，且与新候选同一 entityKey、fieldPath、locale。

新旧事实关系写入 `fact_supersessions`；引用旧事实的已确认表达改为 `pending`，正文和原引用保留，复核前不显示在正式查看器或导出中。系统不会自动把旧表达的依据换成新事实。

## 给已有事实补挂来源（事实的值不变）

```bash
python3 career_profile.py validate-links staging/links-<批次名>.json
python3 career_profile.py apply-links staging/links-<批次名>.json review/decisions-<批次名>.json
```

补挂文件格式（样例见 `examples/demo-links.template.json`）：

```json
{
  "linkVersion": "1.0.0",
  "generatedAt": "2026-09-27T00:00:00Z",
  "sources": [ { "sourceId": "evidence.transcript", "absolutePath": "…", "documentType": "pdf", "sha256": "…" } ],
  "links":   [ { "linkId": "link.gpa", "factId": "<已确认的 fact_id>", "sourceRefs": [ { "sourceId": "evidence.transcript", "locator": "页眉", "excerpt": "…" } ] } ],
  "unlinks": [ { "unlinkId": "unlink.wrong", "factId": "<fact_id>", "sourceId": "<挂错的来源>", "reason": "为什么撤" } ]
}
```

- 规则与候选相同：来源在素材目录内、SHA-256 一致、文本来源摘录逐字。
- 同一事实不能重复挂同一来源；已登记过的 sourceId 必须指向同一份未改动的文件，文件改过就换新的 sourceId。
- 按实际接受的补挂和撤销操作核验结果，每条事实至少保留一个来源；不能靠拒绝补挂、接受撤销的组合留下无来源的事实。
- 决定文件沿用 decisions 格式，`candidateId` 填 linkId / unlinkId，只接受 accept / reject。

## 挪动材料后同步登记路径

```bash
python3 career_profile.py relocate-sources moves.json          # 预览
python3 career_profile.py relocate-sources moves.json --apply  # 写库（自动备份）
```

`moves.json` 形如 `{"moves": {"旧绝对路径": "新绝对路径"}}`。只改 `sources.absolute_path`，不改登记时的 SHA-256；
新位置必须在素材目录内且文件存在；登记后被改过的文件会单独列出，路径照常更新。
挪动前先停掉查看器。

## 来源完整性检查（只读）

```bash
python3 source_integrity.py
python3 source_integrity.py --json
```

检查 SQLite 完整性、外键和来源文件指纹，并统计受影响的确认事实；不会更新事实、路径或原登记指纹。通过返回退出码 0，发现异常返回 1，无法打开数据库返回 2。文件改过、缺失或无法读取时，查看器会提示，导出也会警告；没有任何来源的确认事实则阻止导出。

先寻找原材料版本，或经本人确认登记新来源；不要覆盖旧指纹消除异常提示。

## 查看器（只读网页）

```bash
cd web && python3 serve.py --port 8733     # 然后浏览器打开 http://127.0.0.1:8733
```
macOS 上也可直接双击 `web/打开资料库.command`（自定位、后台常驻）。
查看器只读打开数据库，只显示 `confirmed` 事实；可搜索、中英切换或对照、逐字段一键复制；「来源」开关按一手 → 本人陈述 → 二手的顺序显示出处。
来源等级由 ID 和文件名推测，不替代原件核验；文件是否与登记指纹一致另行显示。API 在同一读事务中组织结果，页面静态服务仅公开查看器 HTML，不提供源代码或个人配置文件下载。
个人专属的分区或字段名写进 `web/viewer-config.json`（不进 Git），格式见 `web/viewer-config.example.json`。

## 边界

- 数据库、候选、决定、导出快照、备份、materials —— **都不进 Git，不联网处理，不交给第三方。**
- 未确认、推测、冲突的内容一律不导出。

## 一个坑

写库时若报 `sqlite3.OperationalError: disk I/O error`：停止新的写入，保留数据库及其 WAL/journal 文件，检查磁盘空间、文件权限和最近的在线备份。不要手动删除日志文件；其中可能包含恢复所需的数据。
