// Validate imported snapshots against the bundled public contract.
import { snapshotSchema } from "./snapshot-schema.js";

export const SUPPORTED_SCHEMA_VERSION = "1.1.0";
const SUPPORTED_KEYWORDS = new Set([
  "$schema", "$id", "title", "description", "$defs", "$ref", "type", "required",
  "properties", "additionalProperties", "minProperties", "allOf", "if", "then", "not",
  "const", "enum", "minItems", "uniqueItems", "items", "minLength", "maxLength",
  "pattern", "format", "minimum", "maximum",
]);

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function matchesType(value, kind) {
  return {
    object: isObject(value), array: Array.isArray(value), string: typeof value === "string",
    integer: Number.isInteger(value), boolean: typeof value === "boolean", null: value === null,
  }[kind];
}

function isIsoDateTime(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?(Z|[+-](\d{2}):(\d{2}))$/.exec(value);
  if (!match || match[0] !== value) return false;
  const [, year, month, day, hour, minute, second, , , offsetHour = "0", offsetMinute = "0"] = match;
  const y = Number(year), m = Number(month), d = Number(day);
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return y >= 1 && m >= 1 && m <= 12 && d >= 1 && d <= days[m - 1]
    && Number(hour) <= 23 && Number(minute) <= 59 && Number(second) <= 59
    && Number(offsetHour) <= 23 && Number(offsetMinute) <= 59;
}

function validateSchema(value, rule, root, path) {
  const unknown = Object.keys(rule).filter((key) => !SUPPORTED_KEYWORDS.has(key));
  if (unknown.length) return [`${path}: 契约包含尚未支持的约束 ${unknown.join(", ")}`];
  if (rule.format !== undefined && rule.format !== "date-time") return [`${path}: 契约包含尚未支持的 format ${rule.format}`];
  if (rule.$ref) {
    const target = rule.$ref.slice(2).split("/").reduce((node, part) => node[part], root);
    return validateSchema(value, target, root, path);
  }
  const errors = [];
  for (const branch of rule.allOf ?? []) errors.push(...validateSchema(value, branch, root, path));
  if (rule.if && validateSchema(value, rule.if, root, path).length === 0) {
    errors.push(...validateSchema(value, rule.then ?? {}, root, path));
  }
  if (rule.not && validateSchema(value, rule.not, root, path).length === 0) {
    errors.push(`${path}: 当前版本禁止此字段组合`);
  }
  if (rule.type !== undefined) {
    const kinds = Array.isArray(rule.type) ? rule.type : [rule.type];
    if (!kinds.some((kind) => matchesType(value, kind))) return [...errors, `${path}: 类型必须为 ${kinds.join(" / ")}`];
  }
  if (Object.hasOwn(rule, "const") && value !== rule.const) errors.push(`${path}: 必须等于 ${String(rule.const)}`);
  if (rule.enum && !rule.enum.includes(value)) errors.push(`${path}: 不是允许的枚举值`);
  if (isObject(value)) {
    for (const key of rule.required ?? []) {
      if (!Object.hasOwn(value, key)) errors.push(`${path}: 缺少必填字段 ${key}`);
    }
    if (Object.keys(value).length < (rule.minProperties ?? 0)) errors.push(`${path}: 字段数量不足`);
    const properties = rule.properties ?? {};
    for (const [key, child] of Object.entries(value)) {
      const childPath = path ? `${path}.${key}` : key;
      if (Object.hasOwn(properties, key)) errors.push(...validateSchema(child, properties[key], root, childPath));
      else if (rule.additionalProperties === false) {
        errors.push(path === "" ? `${key} 不是允许的顶层字段` : `${childPath}: 不是允许字段`);
      } else if (isObject(rule.additionalProperties)) errors.push(...validateSchema(child, rule.additionalProperties, root, childPath));
    }
  }
  if (Array.isArray(value)) {
    if (value.length < (rule.minItems ?? 0)) errors.push(`${path}: 数组项目不足`);
    if (rule.uniqueItems && new Set(value.map((item) => JSON.stringify(item))).size !== value.length) errors.push(`${path}: 不允许重复项`);
    if (rule.items) value.forEach((child, index) => errors.push(...validateSchema(child, rule.items, root, `${path}[${index}]`)));
  }
  if (typeof value === "string") {
    const length = Array.from(value).length;
    if (length < (rule.minLength ?? 0) || length > (rule.maxLength ?? Infinity)) errors.push(`${path}: 字符串长度超出契约范围`);
    if (rule.pattern) {
      const match = new RegExp(rule.pattern).exec(value);
      if (!match || match[0] !== value) errors.push(`${path}: 格式不符（ID 或语言标签）`);
    }
    if (rule.format === "date-time" && !isIsoDateTime(value)) errors.push(`${path}: 必须是 ISO 8601 日期时间`);
  }
  if (typeof value === "number" && rule.minimum !== undefined && value < rule.minimum) errors.push(`${path}: 数值小于 ${rule.minimum}`);
  if (typeof value === "number" && rule.maximum !== undefined && value > rule.maximum) errors.push(`${path}: 数值大于 ${rule.maximum}`);
  return errors;
}

export function validateProfileSnapshot(profile) {
  const errors = validateSchema(profile, snapshotSchema, snapshotSchema, "");
  if (errors.length) return { valid: false, errors };
  const entityIds = new Set(), expressionIds = new Set(), factIds = new Set();
  const entities = [profile.person, ...profile.education, ...profile.experiences, ...profile.skills, ...profile.awards];
  function register(id, registry, path) {
    if (registry.has(id)) errors.push(`${path}: 重复 ID ${id}`);
    registry.add(id);
  }
  function collect(value, path) {
    if (isObject(value)) {
      if (Object.hasOwn(value, "factId")) {
        register(value.factId, factIds, path);
        if (value.alternatives) {
          const locales = [value, ...value.alternatives].map((item) => item.locale ?? null);
          if (new Set(locales).size !== locales.length) errors.push(`${path}: 同一字段不允许重复 locale`);
        }
      }
      for (const [key, child] of Object.entries(value)) collect(child, `${path}.${key}`);
    } else if (Array.isArray(value)) value.forEach((child, index) => collect(child, `${path}[${index}]`));
  }
  for (const entity of entities) {
    register(entity.id, entityIds, "entity.id");
    collect(entity, entity.id);
  }
  for (const expression of profile.expressions) {
    register(expression.id, expressionIds, "expression.id");
    for (const factId of expression.sourceFactIds) {
      if (!factIds.has(factId)) errors.push(`${expression.id}: sourceFactIds 引用不存在的事实 ${factId}`);
    }
    for (const entityId of expression.subjectIds ?? []) {
      if (!entityIds.has(entityId)) errors.push(`${expression.id}: subjectIds 引用不存在的实体 ${entityId}`);
    }
  }
  for (const id of entityIds) if (factIds.has(id)) errors.push(`ID 在实体与事实中重复：${id}`);
  for (const id of expressionIds) if (entityIds.has(id) || factIds.has(id)) errors.push(`ID 在表达与实体或事实中重复：${id}`);
  if (profile.education.filter((record) => record.isHighest === true).length > 1) errors.push("education 只能有一条 isHighest=true 的记录");
  return { valid: errors.length === 0, errors };
}

function addFactValue(catalog, byRef, byKey, factValue, options) {
  if (!factValue) {
    return;
  }
  const item = {
    ref: factValue.factId,
    key: options.key,
    category: options.category,
    title: options.title,
    valueType: options.valueType ?? "text",
    locale: factValue.locale ?? options.locale ?? null,
    value: factValue.value,
  };
  if (!byRef.has(item.ref)) {
    catalog.push(item);
    byRef.set(item.ref, item);
  }
  byKey.set(item.key, byRef.get(item.ref));
  for (const alias of options.aliases ?? []) {
    byKey.set(alias, byRef.get(item.ref));
  }
  for (const alternative of factValue.alternatives ?? []) {
    // 原字段键继续指向默认语言；每种语言另有显式键，模型可用 factId 选择。
    addFactValue(catalog, byRef, byKey, alternative, {
      ...options,
      key: `${options.key}@${alternative.locale ?? "und"}`,
      aliases: (options.aliases ?? []).map((alias) => `${alias}@${alternative.locale ?? "und"}`),
    });
  }
}

function experienceTypeLabel(type) {
  return {
    professional: "工作经历",
    research: "研究经历",
    leadership: "校园经历",
    project: "项目经历",
  }[type] ?? "经历";
}

export function buildProfileIndex(profile) {
  const validation = validateProfileSnapshot(profile);
  if (!validation.valid) {
    throw new Error(`Career Profile 无效：${validation.errors.join("；")}`);
  }

  const catalog = [];
  const byRef = new Map();
  const byKey = new Map();
  const personFields = [
    ["fullName.zhCN", profile.person.fullName.zhCN, "中文姓名"],
    ["fullName.en", profile.person.fullName.en, "英文姓名"],
    ["fullName.givenNameEn", profile.person.fullName.givenNameEn, "英文名"],
    ["fullName.familyNameEn", profile.person.fullName.familyNameEn, "英文姓"],
    ["contact.email", profile.person.contact.email, "电子邮箱"],
    ["contact.phone", profile.person.contact.phone, "手机号码"],
    ["location.currentCity", profile.person.location.currentCity, "当前城市"],
    ["location.currentCountry", profile.person.location.currentCountry, "当前国家或地区"],
  ];
  for (const [path, factValue, title] of personFields) {
    addFactValue(catalog, byRef, byKey, factValue, {
      key: `person.${path}`,
      category: "person",
      title,
    });
  }

  const highestEducation = profile.education.find((record) => record.isHighest) ?? profile.education[0];
  for (const record of profile.education) {
    const institution = record.institution?.value ?? "教育经历";
    for (const field of [
      "institution",
      "degree",
      "major",
      "minor",
      "location",
      "startDate",
      "endDate",
      "gpa",
      "averageScore",
      "ranking",
    ]) {
      const aliases = record === highestEducation ? [`education.highest.${field}`] : [];
      addFactValue(catalog, byRef, byKey, record[field], {
        key: `education.${record.id}.${field}`,
        aliases,
        category: "education",
        title: `教育经历·${institution}·${field}`,
        valueType: field.toLowerCase().includes("date") ? "date" : "text",
      });
    }
  }

  for (const record of profile.experiences) {
    const recordTitle = record.title?.value ?? experienceTypeLabel(record.type);
    for (const field of ["title", "organization", "role", "location", "startDate", "endDate"]) {
      addFactValue(catalog, byRef, byKey, record[field], {
        key: `experience.${record.id}.${field}`,
        category: `experience.${record.type}`,
        title: `${experienceTypeLabel(record.type)}·${recordTitle}·${field}`,
        valueType: field.toLowerCase().includes("date") ? "date" : "text",
      });
    }
    record.facts.forEach((fact, index) => {
      addFactValue(
        catalog,
        byRef,
        byKey,
        { factId: fact.factId, value: fact.text, locale: fact.locale },
        {
          key: `experience.${record.id}.fact.${index + 1}`,
          category: `experience.${record.type}`,
          title: `${experienceTypeLabel(record.type)}·${recordTitle}·事实 ${index + 1}`,
          valueType: "narrative",
        },
      );
    });
  }

  for (const record of profile.skills) {
    for (const field of ["name", "level", "details"]) {
      addFactValue(catalog, byRef, byKey, record[field], {
        key: `skill.${record.id}.${field}`,
        category: `skill.${record.category}`,
        title: `技能·${record.name?.value ?? record.id}·${field}`,
      });
    }
  }

  for (const record of profile.awards) {
    for (const field of ["name", "issuer", "date", "details"]) {
      addFactValue(catalog, byRef, byKey, record[field], {
        key: `award.${record.id}.${field}`,
        category: "award",
        title: `奖项·${record.name?.value ?? record.id}·${field}`,
        valueType: field === "date" ? "date" : "text",
      });
    }
  }

  for (const expression of profile.expressions) {
    const item = {
      ref: expression.id,
      key: `expression.${expression.id}`,
      category: "expression",
      title: `表达版本·${expression.purpose}·${expression.maxChars ?? "不限"}字`,
      valueType: "expression",
      locale: expression.locale,
      value: expression.text,
      sourceFactIds: [...expression.sourceFactIds],
    };
    catalog.push(item);
    byRef.set(item.ref, item);
    byKey.set(item.key, item);
  }

  return { catalog, byRef, byKey };
}

export function shareableCatalog(catalog) {
  return catalog.map(({ ref, category, title, valueType, locale }) => ({
    ref,
    category,
    title,
    valueType,
    locale,
  }));
}
