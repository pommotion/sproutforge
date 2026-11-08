# SproutForge 架构说明

## 1. 项目定位
**SproutForge（发芽锻造器）** 是面向 remio aApp 生态的「从素材到执行」下游自动化执行引擎。它将内容抓取产出的发芽笔记（Sprout Notes）或任意知识库笔记中的潜在行动方向（Directions）提取出来，结合知识库检索（KB-aware）与双层分类路由，转化为结构化执行流水线（Pipelines），链式驱动 Agent 执行深度研究、多源调研、PRD 编写、目标对齐等具体任务，并将成果自动回链和跨平台分发（Obsidian、Get笔记等）。

## 2. 文件结构与职责划分
| 文件 | 角色/职责 | 核心内容 |
|---|---|---|
| `manifest.json` | aApp 清单元数据 | 定义 aApp 标识、版本（v4.6.3）、图标、嵌入式运行时（embedded）、以及对话端快捷菜单（chatMenu） |
| `api.json` | 端点契约定义 | 声明 6 个 UI 端点（如主页、看板、统计）与 11 个 Data 端点（如 extract、confirm、complete、status 等）的请求方法与参数结构 |
| `SKILL.md` | Agent 自动化协作协议 | 描述 Agent 自动执行手册：链式驱动模型（Batch-Execute → Skill 调用 → Complete 循环）、分类路由表与执行规范 |
| `logic.py` | 核心业务运行时与控制器（~2400行） | 承载 SQLite 数据库交互、笔记读取与写入 syscall、KB 感知（2-gram 关键词检索）、规则与 AI 分类、链式状态机、所有端点路由实现 |
| `sprout_save_utils.py` | 多平台知识库保存与分发工具 | 负责来源平台推断（微博/公众号/B站/YouTube/通用）、本地 Obsidian 笔记生成、Get笔记 OpenAPI 同步、以及飞书元数据准备 |
| `icon.svg` / `VERSION` / `README.md` | 静态资产与版本说明 | aApp 图标（🌱）、当前版本号（v4）、项目简要说明与历史 |

## 3. 数据流转全景
```
[素材链接 / @任意笔记]
       │
       ▼
 1. 提取 (POST /extract)
    ├─ 规则解析: 匹配「## 🌿 发芽扩展」章节
    └─ AI 降级: 全文截断 → 提示词提取 3-8 个方向
       │
       ▼
 2. 知识库感知与双层分类 (KB-Aware Classification)
    ├─ C1 检索: 标题 + 前 3 方向 2-gram 关键词 → search_notes 检索知识库关联笔记
    ├─ Layer 1 (规则预分类): 标题命中 research / survey / prd / goal / archive 关键词
    ├─ Layer 2 (AI 兜底): 未命中项调用 run_prompt 分类，并标注 relation (new / update / deepen)
    └─ DB 入库: 写入 pipelines (draft) 与 sprout_actions (pending)
       │
       ▼
 3. 确认与队列管理 (POST /confirm 或 POST /batch-execute)
    └─ pending 状态批量变更为 queued，准备执行
       │
       ▼
 4. 链式驱动执行 (v4.1 核心状态机)
    ┌─ POST /batch-execute (获取 first_action 与 exec_prompt)
    │     │
    │     ▼
    │  Agent 调用对应 Skill (/deep-research, /deep-survey, /prd-writer, /goalpro, 直接执行)
    │     │
    │     ▼
    │  POST /complete (回传 action 成果 result_ref，自动将成果归入 Collection)
    │     │
    └─── 检查响应 next_action:
           ├─ 有下一个: 自动标记 running，Agent 接力执行
           └─ 无下一个: pipeline_completed = True
       │
       ▼
 5. 成果回链与多端分发 (POST /link-results)
    ├─ 创建汇总笔记 (create_note)
    ├─ 原笔记追加「## ⚡ 执行成果」章节 (幂等校验)
    ├─ 执行成果反哺知识库 (检索关联旧笔记并提示更新)
    └─ save_sprout_to_vault: 异步分发至 Obsidian 本地库与 Get笔记云端
```

## 4. 系统入口
- **宿主运行时入口**: `logic.py:handle(request)`，通过 `@router.route` 将 HTTP 请求分发至各处理器。
- **用户交互入口（UI）**:
  - `GET /`: 主页（URL 抓取输入引导 + 笔记提取说明）。
  - `GET /pipeline/:source_id`: 单流水线详情与方向执行卡片。
  - `GET /dashboard_ui`: 进行中流水线全局看板。
  - `GET /history_ui`: 已完成流水线归档历史。
  - `GET /stats_ui`: 知识→行动转化漏斗与沉睡方向监控。
- **Agent/系统自动化入口（API）**:
  - `POST /extract`: 提取方向与智能分类。
  - `POST /batch-execute` 与 `POST /complete`: 驱动全自动链式任务流。
  - `POST /link-results`: 最终归档与分发。
  - `GET /status` 与 `GET /scan_dormant`: 健康度监控与定时巡检。
