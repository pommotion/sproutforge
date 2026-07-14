# SproutForge 🌱

> 从素材到执行的一体化引擎——粘贴链接走完全流程，或直接 @ 任意笔记提取方向、智能分类、生成执行计划、成果回链。

remio aApp，由 violin 开发。

## 版本历史

| 版本 | 关键特性 |
|------|---------|
| v1 | 从发芽笔记提取方向 + AI 分类 + 执行计划 + 成果回链 |
| v2 | 多目的地保存（Obsidian/Get笔记）+ 沉睡扫描 + 转化仪表盘 |
| v4 | KB-aware 知识库感知：方向提取时检索知识库，标注 new/update/deepen |

> v3 遗失：部署时间窗口（07-14 ~ 07-15）无 TM 快照覆盖，无法恢复。

## 技术栈

- **运行时**: embedded (Python logic.py)
- **数据库**: SQLite
- **AI**: run_prompt (规则预分类 + AI fallback)
- **KB 检索**: search_notes syscall + 2-gram 中文关键词匹配

## 文件结构

```
├── manifest.json       # aApp 清单
├── api.json            # 端点定义
├── logic.py            # 核心逻辑（~2000 行）
├── sprout_save_utils.py # 多平台保存工具
├── SKILL.md            # Agent 使用说明
└── icon.svg            # 图标
```
