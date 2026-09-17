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

> **导出要求存在 person 实体**：`export` 会硬校验至少有一个 `person.owner` 实体。
> 所以第一批候选里应当包含姓名等 `person.*` 字段（entityKey = `person.owner`）。

## 候选与决定文件格式

- 候选：`templates/candidates.template.json`。要点：
  - `sources[].sourceId` 是小写稳定 id（≥2 字符，形如 `evidence.resume`）。
  - `sources[].absolutePath` 用**绝对路径**，且指向 `materials/` 内的文件。
  - `sources[].sha256` 是该文件的 SHA-256（`shasum -a 256`）。
  - 每个候选：`entityType` / `entityKey` / `fieldPath` / `proposedValue` / `locale` / `status:"pending"` / `sourceRefs[]`（含 `sourceId` `locator` `excerpt`）。
- 决定：`templates/decisions.template.json`。`action` ∈ `accept` / `reject` / `replace`；`replace` 时用 `replacementValue` 覆盖后采纳。

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

## 查看器（只读网页）

```bash
cd web && python3 serve.py --port 8733     # 然后浏览器打开 http://127.0.0.1:8733
```
macOS 上也可直接双击 `web/打开资料库.command`（自定位、后台常驻）。
查看器只读打开数据库，只显示 `confirmed` 事实；可搜索、按中英筛选、逐条一键复制、点"N证"看出处。

## 边界

- 数据库、候选、决定、导出快照、备份、materials —— **都不进 Git，不联网处理，不交给第三方。**
- 未确认、推测、冲突的内容一律不导出。

## 一个坑

写库时若报 `sqlite3.OperationalError: disk I/O error`：先停掉查看器服务，删掉 `data/*.sqlite3-journal`（库已是 WAL 模式），再重试。
