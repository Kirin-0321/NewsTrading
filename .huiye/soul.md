# soul.md（已迁移为 Cursor 规则）

> ⚠️ **本文件正文已于 2026-05-28 迁移至 [.cursor/rules/huiye-persona.mdc](../.cursor/rules/huiye-persona.mdc)**，
> 通过 `alwaysApply: true` 强制注入每次会话，**单一真相源**在那边。
>
> 本文件仅作为旧链接的兼容跳板保留，**禁止在此处编辑正文**——所有改动一律改规则文件。

---

## 跳转

- 📌 **当前生效版本**：[.cursor/rules/huiye-persona.mdc](../.cursor/rules/huiye-persona.mdc)
- 📂 全部 Cursor 规则目录：`.cursor/rules/`
  - `huiye-persona.mdc` — 角色规约 / 工作流程 / 核心原则 / 文档归档边界（**本文件迁移目标**）
  - `doc-workflow.mdc` — 文档协作规约（条款 A 链接同步 / 条款 B 双文档流程）
  - `cli-first-development.mdc` — 接口 CLI 化（边写边测 / Web 化前置工程）

## 历史链接兼容

外部文档若引用了 `soul.md §记忆与文档归档` 等锚点，请改指 `.cursor/rules/huiye-persona.mdc` 内对应小节：

| 旧锚点 | 新位置 |
|--------|--------|
| `soul.md §角色设定` | `huiye-persona.mdc` `# ===== 角色设定 =====` |
| `soul.md §工作流程` | `huiye-persona.mdc` `# ===== 工作流程 =====` |
| `soul.md §记忆与文档归档` | `huiye-persona.mdc` `## 记忆与文档归档` |
| `soul.md §核心原则` | `huiye-persona.mdc` `# ===== 核心原则 =====` |
| `soul.md §文档编写指南` | `huiye-persona.mdc` `# ===== 文档编写指南 =====` |
| `soul.md §项目文档分类` | `huiye-persona.mdc` `# ===== 项目文档分类 =====` |

## 迁移动机

- `.cursor/rules/*.mdc` 通过 `alwaysApply: true` 自动注入，无需手动 `@.huiye/soul.md` 引用
- 与已有的 `doc-workflow.mdc` / `cli-first-development.mdc` 三件套对齐，规则集中在一个目录下管理
- 避免主人/辉夜在不同会话里因为忘了 `@` 而少注入角色设定
