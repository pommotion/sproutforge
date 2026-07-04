---
name: sproutforge
description: "从素材到执行的一体化入口：粘贴链接自动抓取生成发芽笔记，或 @ 任意笔记提取方向、AI 分类、生成执行计划、成果回链。"
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

## Endpoints

### UI 端点

- `GET /` [UI] — 主页：URL 输入 + @ 笔记引导 + 看板/历史按钮
- `GET /pipeline/:source_id` [UI] — 单条流水线详情：方向列表 + 进度
- `GET /dashboard_ui` [UI] — 全局看板：进行中的流水线
- `GET /history_ui` [UI] — 历史记录：已完成的流水线

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
这会自动：创建汇总笔记 → 在原笔记追加「⚡ 执行成果」章节 → 两个笔记加入 Collection。

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

- **`sprout_actions`**: 发芽方向/行动项（id, source_id, note_id, direction_index, title, description, action_type, action_subtype, priority, status, exec_prompt, result_ref, result_summary, created_at, updated_at）
- **`pipelines`**: 执行流水线（id, source_id, note_id, total_actions, completed_actions, status, summary_note_id, meta, created_at, updated_at）

## 运行时

`embedded`（Python logic.py）
