# SproutForge 核心代码风险与架构坏味道分析报告

本文档基于对 `logic.py`（约 2440 行，~110KB）源码的全面通读与分析，整理出当前系统中最严重的 5 个代码风险与坏味道。每条均指出真实代码行号范围、潜在危害及重构优化建议。

---

## 风险 1：空字符串 JOIN 导致笛卡尔积爆炸与元数据串扰
- **位置**：`logic.py` 第 2280～2287 行（`_scan_dormant` 沉睡方向扫描接口）
```python
2280:         dormant = db.query(
2281:             'SELECT a.*, p.meta as p_meta '
2282:             'FROM sprout_actions a '
2283:             'LEFT JOIN pipelines p ON (a.source_id = p.source_id OR a.note_id = p.note_id) '
2284:             'WHERE a.status = "pending" AND a.created_at < :cutoff '
2285:             'ORDER BY a.created_at ASC LIMIT 50',
2286:             {'cutoff': cutoff}
2287:         )
```
- **会出什么问题**：
  - 在 `_init_db` 中，`sprout_actions` 与 `pipelines` 表的 `source_id` 和 `note_id` 字段默认值皆为空字符串 `''`。
  - 用户通过「@ 任意笔记」入口提取方向时，没有外部 `source_id`，因此 `source_id` 在两张表中均存为 `''`。
  - 在 SQLite 中，`'' = ''` 为真。当存在多个通过笔记直接提取的流水线时，条件 `a.source_id = p.source_id` 会使所有 `source_id = ''` 的 action 错误地匹配到所有 `source_id = ''` 的 pipeline 记录，形成 **$N \times M$ 的笛卡尔积失控爆炸**。
  - **严重后果**：查询结果被无故放大数倍甚至数十倍，且 action 关联到的 `p_meta`（包含笔记标题等信息）发生相互串扰（A 笔记的方向错误显示为 B 笔记的标题），直接导致沉睡扫描返回混乱数据，并在数据量增大时拖垮 SQLite。
- **怎么改**：
  在 JOIN 条件中增加非空判断，避免空字符串互等：
  ```sql
  LEFT JOIN pipelines p ON (
      (a.source_id != '' AND a.source_id = p.source_id) OR
      (a.note_id != '' AND a.note_id = p.note_id)
  )
  ```

---

## 风险 2：贪婪正则抽取 LLM JSON 导致高频解析崩溃
- **位置**：`logic.py` 第 386～389 行（`_ai_extract_single`）、第 633～636 行（`_ai_classify`）、第 1578～1581 行（`_merge_suggest`）
```python
386:         array_match = re.search(r'\[.*\]', text, re.DOTALL)
387:         if array_match:
388:             text = array_match.group()
389:         parsed = json.loads(text)
```
- **会出什么问题**：
  - 正则表达式 `r'\[.*\]'` 配合 `re.DOTALL` 属于**最大贪婪匹配**。它会从文本中的**第一个 `[`** 匹配到**最后一个 `]`**。
  - 大模型生成内容时，常在前后附带中括号说明文字，例如开头输出 `[思考过程：分析如下]` 或末尾附带 `[注意：以上方向仅供参考]`。
  - 贪婪模式会将前后的中文说明与括号一同截取，构成如 `[思考过程：...]\n[{"action_type": ...}]\n[注意：...]` 形式的混合文本。
  - **严重后果**：紧接着执行 `json.loads(text)` 必抛出 `json.JSONDecodeError`。这会导致提取方向降级失败、AI 分类整体回退到全空或异常，甚至使合流推荐（merge-suggest）直接返回 500 级业务错误。
- **怎么改**：
  1. 使用非贪婪括号匹配配合平衡计数器（bracket depth counter）抽取合法的最外层 JSON 结构；
  2. 或使用标准库 `json.JSONDecoder().raw_decode()` 从首个 `[` 位置安全流式解析有效 JSON：
  ```python
  def _safe_json_array_parse(text):
      start = text.find('[')
      if start == -1:
          return []
      decoder = json.JSONDecoder()
      try:
          obj, _ = decoder.raw_decode(text[start:])
          return obj if isinstance(obj, list) else []
      except json.JSONDecodeError:
          return []
  ```

---

## 风险 3：链式执行状态机失控（未审先跑与事务缺失）
- **位置**：`logic.py` 第 1762～1773 行、第 1799～1805 行 与 第 779～783 行（`_complete` 与 `_update_pipeline_progress`）
```python
1762:         # 查找下一个待执行方向（queued 优先，其次 pending 自动提升）
1763:         next_queued = None
1764:         for a in all_actions:
1765:             if a['status'] in ('queued', 'pending'):
1766:                 next_queued = a
1767:                 break
1768: 
1769:         # 标记下一个 queued 方向为 running（不依赖 send_chat_message）
1770:         if next_queued:
1771:             now2 = _now()
1772:             db.exec('UPDATE sprout_actions SET status = ?, updated_at = ? WHERE id = ?', ['running', now2, next_queued['id']])
```
- **会出什么问题**：
  1. **越权执行未确认任务**：第 1765 行中将 `pending` 与 `queued` 并列。当用户仅确认了部分方向时，前序方向一旦完成，系统会自动将尚未确认的 `pending` 方向提拔为 `running` 并交付 Agent 执行，直接违反了「待审方向必须经由 `/confirm` 审阅或 `/batch-execute` 显式批量入队方可执行」的产品协议与安全设计。
  2. **状态更新逆序与统计不一致**：第 1741 行先调用 `_update_pipeline_progress`，此时 `next_queued` 尚未变更为 `running`。随后在第 1772 行才更新 `status = 'running'`，导致 pipelines 表中的进度和状态落后于实际方向状态。
  3. **非原子事务**：`_complete` 涉及更新当前 action、推进 pipeline、归入 collection、变更下一个 action 等 4~5 次数据库写入与外部调用，均运行在无事务包裹的自提交模式下，中途发生异常将造成状态割裂（如完成标记成功但下一个方向未激活，流水线永久卡死）。
- **怎么改**：
  - 链式接力仅允许唤醒处于 `queued` 状态的方向，坚决剔除 `pending`：
    ```python
    if a['status'] == 'queued':
        next_queued = a
        break
    ```
  - 将整个状态变更与流水线进度计算包裹在同一原子事务内，并在变更全部完成后统一刷新流水线统计。

---

## 风险 4：脆弱的幂等性校验导致方向数据重复增殖
- **位置**：`logic.py` 第 1056～1068 行（`_extract` 提取端点）
```python
1056:         # Check for existing actions (avoid duplicate extraction) - check both note_id and source_id
1057:         existing = []
1058:         if note_id:
1059:             existing = db.query('SELECT direction_index FROM sprout_actions WHERE note_id = :nid', {'nid': note_id})
1060:         if not existing and source_id:
1061:             existing = db.query('SELECT direction_index FROM sprout_actions WHERE source_id = :sid', {'sid': source_id})
1062:         if existing and len(existing) >= len(raw_directions):
1063:             return {
1064:                 'message': f'该笔记已提取过 {len(existing)} 个方向,跳过重复提取',
1065:                 'pipeline_source_id': source_id or note_id,
1066:                 'direction_count': len(existing),
1067:             }
```
- **会出什么问题**：
  - 重复防范判断完全依赖 `len(existing) >= len(raw_directions)` 这一数量比较。
  - **典型触发场景**：某笔记首次提取出 3 个方向入库。若用户修改笔记追加了内容，或由于 LLM 温度浮动导致本次提取产出了 4 个方向，由于 `3 >= 4` 为假，防重逻辑被彻底击穿。
  - 代码随后会在没有清理或比对旧记录的情况下，重新生成 4 条新方向直接 `INSERT INTO sprout_actions`，其 `direction_index` 同样从 1 开始排布。
  - **严重后果**：数据库中同一篇笔记存在两批 `direction_index` 相同的冗余记录（累计 7 条）。详情页与执行看板展示严重混乱，进度计算错误分母放大，甚至重复触发已完成的指令。
- **怎么改**：
  - 在重提模式下引入显式参数（如 `force=True`），在事务中先清理未启动的 `pending` 记录；
  - 或者为每个方向生成基于 `(note_id, title_hash)` 的唯一约束或指纹，采用 Upsert 机制更新，防止盲目累加。

---

## 风险 5：超时恢复循环中的 N+1 级联查询与死锁隐患
- **位置**：`logic.py` 第 1863～1877 行（`_reset_stuck` 卡死恢复端点）
```python
1863:         now_val = now
1864:         for a in stuck:
1865:             db.exec('UPDATE sprout_actions SET status = ?, updated_at = ? WHERE id = ?', ['queued', now_val, a['id']])
1866:             if source_id:
1867:                 _update_pipeline_progress(db, source_id)
1868: 
1869:         # 如果没指定 source_id，逐个 pipeline 更新进度
1870:         if not source_id:
1871:             reset_sources = set()
1872:             for a in stuck:
1873:                 sid = a.get('source_id') or a.get('note_id')
1874:                 if sid:
1875:                     reset_sources.add(sid)
1876:             for sid in reset_sources:
1877:                 _update_pipeline_progress(db, sid)
```
- **会出什么问题**：
  - 在指定 `source_id` 的场景下，代码在 `for a in stuck:` 循环内部，每更新一个 action 就调用一次 `_update_pipeline_progress(db, source_id)`。
  - 查看 `_update_pipeline_progress` 实现，每次调用均执行 3 次全表 `COUNT(*)` 查询（统计 total、done、running）加 1 次 `UPDATE pipelines`。
  - **性能骤降**：若积压了 20 个超时任务，仅恢复处理就将在单次 HTTP 请求中触发 $20 \times 4 + 20 = 100$ 次连续的 SQLite 读写。
  - **并发死锁**：SQLite 采用库级锁，频繁无事务的离散短更新与查询极易引发 `database is locked`（OperationalError），导致该接口在大规模超时清理时直接崩溃。
- **怎么改**：
  1. 将多个 action 的状态更新合并为单条 SQL 批量执行：
     ```sql
     UPDATE sprout_actions SET status = 'queued', updated_at = :now WHERE id IN (...)
     ```
  2. 将 `_update_pipeline_progress(db, source_id)` 移至循环外部，确保单条流水线无论重置多少任务，其进度重算仅执行一次。
