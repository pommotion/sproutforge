---
name: sproutforge
description: "从素材到执行的一体化入口：粘贴链接自动抓取生成发芽笔记，或 @ 任意笔记提取方向、AI 分类、生成执行计划、成果回链。v4 新增 KB-aware：方向提取时自动检索知识库，标注与已有笔记的关系（新增/更新/深化），执行时注入关联笔记上下文。"
---

# SproutForge 发芽锻造器

从素材到执行的一体化引擎——粘贴链接走完全流程，或直接 @ 任意笔记提取方向、智能分类、生成执行计划、成果回链。

## 三个入口

### 🔗 从链接开始
粘贴任意 URL（文章/视频/帖子）→ 自动调用 content-router 抓取生成发芽笔记 → 提取方向

### 📝 从笔记开始
在对话中 @ 任意笔记（发芽笔记、普通笔记、文章摘要等均可）→ 说「提取方向」即可

### 📊 看板 / 历史记录
查看执行进度和历史

## 工作流

```
链接 → content-router 抓取 → 发芽笔记 → 提取方向 → AI 分类 → 审阅确认 → 执行 → 成果回链
```

也支持直接从已有笔记开始：
```
@ 任意笔记 → AI 提取方向 → AI 分类 → 审阅确认 → 执行 → 成果回链
```

### 方向提取策略

1. **优先**：从笔记中的 `🌿 发芽扩展` 章节直接解析（content-router 产出的发芽笔记）
2. **Fallback**：笔记中没有该章节时，AI 读取全文自动提取 3-8 个可执行方向（支持任意笔记类型）

### 分类路由

| action_type | 适用场景 | 对应 skill | 执行方式 |
|-------------|---------|-----------|----------|
| `research` | 深度研究 | `/deep-research` | 生成研究 prompt |
| `survey` | 多源调研 | `/deep-survey` | 生成调研参数 |
| `prd` | 产品设计 | `/prd-writer` | 生成 PRD 输入 |
| `goal` | 目标对齐 | `/goalpro` | 生成 Goal Contract |
| `exec` | 直接执行 | Agent 直接做 | 生成执行指令 |
| `archive` | 归档参考 | 归档到 Collection | 自动 |

### 双层分类机制（重要）

SproutForge 采用**规则预分类 + AI fallback** 的双层机制：

1. **Layer 1：规则预分类**（`_pre_classify`）
   - 只在**方向标题**上做关键词匹配（避免在长描述文本中误匹配）
   - 优先级：research > survey > prd > goal > archive
   - 命中关键词的方向直接分类，无需 AI 调用
   - 未命中的方向进入 Layer 2

2. **Layer 2：AI 分类**（`_ai_classify`）
   - 仅对 Layer 1 未匹配的方向调用 AI
   - AI 返回结果后，个别仍未拿到结果的方向 fallback 为 `exec`

3. **修正机制**
   - 用户在 pipeline 详情页点击「重分类」按钮，或
   - Agent / 用户直接调用 `POST /reclassify`（不传 action_type 则 AI 重新分类）

### KB-aware 方向提取（v4 新增）

v4 引入知识库感知能力，解决「每次发芽都从零开始」的问题：

1. **C1 KB 检索**：`_extract` 端点在提取方向后、分类前，调用一次 `search_notes`（用笔记标题 + 前3个方向关键词拼接检索），返回最多 5 条相关笔记
2. **C2 KB 关系标注**：每条方向标注与知识库的关系：
   - `new`（新增）：全新话题，知识库中无相关笔记
   - `update`（更新）：与已有笔记内容高度相关，执行时应更新该笔记
   - `deepen`（深化）：在已有笔记涉及的主题上进一步深入
   - 规则预分类的方向用标题简单匹配；AI 分类的方向由 LLM 根据 KB 上下文判断
3. **C3 UI 展示**：pipeline 详情页方向列表中，每条方向显示 🔄更新/🔬深化 徽章 + 关联笔记标题
4. **C4 exec_prompt 注入**：标记为 update/deepen 的方向，其 exec_prompt 追加 KB 关联指引，要求 Agent 执行时先读取关联笔记再在其基础上更新/深化
5. **向后兼容**：KB 检索失败或返回空时，自动降级为全 `new`，不影响现有流程

## Endpoints

### UI 端点

- `GET /` [UI] — 主页：URL 输入 + @ 笔记引导 + 看板/历史按钮
- `GET /pipeline/:source_id` [UI] — 单条流水线详情：方向列表 + 进度
- `GET /dashboard_ui` [UI] — 全局看板：进行中的流水线
- `GET /history_ui` [UI] — 历史记录：已完成的流水线
- `GET /stats_ui` [UI] — 知识→行动转化仪表盘：全局漏斗 + 类型分布 + 沉睡方向
- `GET /scan_dormant` — 扫描沉睡方向（超过N天未执行），可被 scheduler 定时调用

### Data 端点

- `POST /extract` — 从笔记提取方向 + AI 分类
  - params: `note_id` (string) 或 `source_id` (string)
  - 行为：优先从 🌿 发芽扩展章节解析，没有则 AI 全文提取
  - 返回: `{ pipeline_id, source_id, note_id, direction_count, actions[] }`

- `POST /reclassify` — 重新分类某个方向
  - params: `action_id` (string, required), `action_type` (string, optional)
  - 可选: `action_subtype`, `priority`

- `POST /confirm` — 确认分类，进入执行队列
  - params: `source_id` 或 `note_id`, `action_ids` (JSON array string 或逗号分隔)

- `POST /execute` — 执行单个方向（返回 exec_prompt 供 Agent 调用 skill）
  - params: `action_id` (string, required)
  - 返回: `{ exec_prompt, action_type, title }`

- `POST /batch-execute` — 批量生成执行计划
  - params: `source_id` (string, required)
  - 返回: `{ plan[], plan_count }`

- `POST /complete` — 标记完成 + 成果回传
  - params: `action_id` (string, required), `result_ref` (string), `result_summary` (string)

- `POST /skip` — 跳过某个方向
  - params: `action_id` (string, required), `reason` (string, optional)

- `GET /plan/:source_id` — 获取执行计划（Agent 读取执行）。**注意：只返回 `running` 和 `queued` 状态的方向，不包含 `pending`。** Agent 想执行 pending 方向需先调用 `/confirm` 或 `/batch-execute`。

- `POST /link-results` — 成果回链：更新原笔记 + 新建汇总笔记
  - params: `source_id` 或 `note_id`

- `GET /status` — 返回 SproutForge 运行状态（健康度、各状态方向计数、卡住/失败告警、最近活动），供 Agent 聚合查询。无需参数。

## Agent 自动执行协议（关键）

aApp 不能直接调用 agent skill。本协议是 Agent 的操作手册——**Agent 读完本节后，应能在没有用户介入的情况下自动执行所有方向**。

### 触发条件

以下任一情况，Agent 应启动自动执行流程：
- 用户说「执行」「开始执行」「跑一下」「全部执行」等
- SproutForge 返回 batch-execute 结果包含 `"message": "...Agent 请按顺序执行..."`
- Agent 看到 `/execute` 或 `/batch-execute` 返回中有 exec_prompt 且包含 skill 名称（如 /deep-research）

### 执行流程

```
步骤1: GET /plan/:source_id → 读取待执行计划
步骤2: 对每个 item，按 action_type 分发到对应 skill
步骤3: 每个 skill 产出成果后 → POST /complete 回传
步骤4: 全部完成 → POST /link-results 回链+汇总
```

### action_type → skill 路由表

| action_type | Agent 应做什么 | 对应 skill 调用方式 |
|---|---|---|
| `research` | 调用深度研究 skill | `/deep-research` + exec_prompt 作为主题描述 |
| `survey` | 调用多源调研 skill | `/deep-survey` + exec_prompt 作为调研角度 |
| `prd` | 调用 PRD 写作 skill | `/prd-writer` + exec_prompt 作为需求描述 |
| `goal` | 调用目标对齐 skill | `/goalpro` + exec_prompt 作为任务描述 |
| `exec` | Agent 直接执行（无需外部 skill） | 按 exec_prompt 中的指令直接操作 |
| `archive` | 归档到 Collection | `create_note`（内容为方向 description + 来源信息） + `add_note_to_collection`（集合名：SproutForge 产出） |

### 详细步骤

**步骤 1：读取计划**
Agent 调用 `POST /batch-execute`（params: source_id）或 `GET /plan/:source_id`，获得 plan 列表。每个 item 包含 action_id, title, action_type, exec_prompt。

**步骤 2：逐个执行**
按 direction_index 顺序执行。对每个 item：

1. 读取 `action_type` 字段
2. 按路由表调用对应 skill（exec_prompt 已经包含了完整调用指令）
3. skill 执行完成，获得成果（通常是一个 note_id 或文件路径）
4. 如果执行失败或不需要执行，调用 `POST /skip`（params: action_id, reason）

**步骤 3：回传成果**
每个方向成功执行后，Agent 立即调用：
```
POST /complete
params: action_id=<该方向的id>, result_ref=<成果note_id或URL>, result_summary=<一句话总结>
```

**步骤 4：回链汇总**
所有方向状态都是 completed 或 skipped 后，Agent 调用：
```
POST /link-results
params: source_id=<source_id>
```
这会自动：创建汇总笔记 → 在原笔记追加「⚡ 执行成果」章节 → 两个笔记加入 Collection → **汇总笔记分发到 Obsidian + Get笔记**（v3 新增，按来源平台路由）。

#### 多目的地保存（v3 新增）

`/link-results` 创建汇总笔记后，会自动根据 `source_meta.platform` 将汇总笔记分发到 Obsidian + Get笔记。与 sprout-notes 共享相同的路由配置和保存模块。

### 中断恢复

如果执行中断（Agent 会话结束、网络问题等），重新开始时：
1. Agent 调用 `GET /plan/:source_id`，查看哪些方向还是 queued/running
2. 从未完成的方向继续执行
3. 已 completed 的不会重复执行

### 注意事项

- exec_prompt 是预生成好的完整调用指令，Agent 可以直接作为消息理解执行
- 一次只执行一个方向，完成并 `/complete` 后再开始下一个
- `archive` 类型不需要调 skill，直接 create_note + add_note_to_collection 即可
- 如果用户只说「执行」而没指定哪个 source_id，Agent 应先查看 `/dashboard_ui` 或询问用户

## 数据模型

独立 SQLite（`data/db/app.sqlite`），2 张表：

- **`sprout_actions`**: 发芽方向/行动项（id, source_id, note_id, direction_index, title, description, action_type, action_subtype, priority, status, exec_prompt, result_ref, result_summary, created_at, updated_at, kb_relation, kb_note_id, kb_note_title）
  - v4 新增：`kb_relation`（new/update/deepen）、`kb_note_id`、`kb_note_title`
- **`pipelines`**: 执行流水线（id, source_id, note_id, total_actions, completed_actions, status, summary_note_id, meta, created_at, updated_at）

## 运行时

`embedded`（Python logic.py）
