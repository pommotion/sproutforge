"""SproutForge - 发芽笔记的下游执行引擎.

Workflow: 发芽笔记 → 方向提取 → AI 分类 → 执行计划 → 成果回链
"""

import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta

from remio_sdk import create_aapp_logger, get_state, router, run_prompt, set_state, syscall

# 多目的地保存模块
from sprout_save_utils import detect_platform, save_sprout_to_vault

# ---------------------------------------------------------------------------
# Paths & logger
# ---------------------------------------------------------------------------
AAPP_DIR = os.environ.get('REMIO_AAPP_DIR', os.getcwd())
DATA_DIR = os.environ.get('REMIO_AAPP_DATA_DIR', os.path.join(os.path.dirname(AAPP_DIR), 'data'))
LOG_DIR = os.environ.get('REMIO_AAPP_LOG_DIR', os.path.join(os.path.dirname(AAPP_DIR), 'log'))
LOGGER = create_aapp_logger('sproutforge', LOG_DIR, 'sproutforge-logic')

# ---------------------------------------------------------------------------
# Action type metadata
# ---------------------------------------------------------------------------
ACTION_META = {
    'research': {'label': '深度研究', 'icon': '🔬', 'skill': '/deep-research'},
    'survey': {'label': '多源调研', 'icon': '📊', 'skill': '/deep-survey'},
    'prd': {'label': '产品设计', 'icon': '📐', 'skill': '/prd-writer'},
    'goal': {'label': '目标对齐', 'icon': '🎯', 'skill': '/goalpro'},
    'exec': {'label': '直接执行', 'icon': '⚡', 'skill': None},
    'archive': {'label': '归档参考', 'icon': '📦', 'skill': None},
}

VALID_ACTION_TYPES = list(ACTION_META.keys())
VALID_PRIORITIES = ['high', 'medium', 'low']
VALID_STATUSES = ['pending', 'queued', 'running', 'completed', 'skipped', 'failed']
VALID_PIPELINE_STATUSES = ['draft', 'queued', 'running', 'completed', 'paused']

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
_db_initialized = False

def _open_db():
    from remio_sdk import open_db
    db = open_db(DATA_DIR)
    global _db_initialized
    if not _db_initialized:
        _init_db(db)
        _db_initialized = True
    return db


def _init_db(db):
    db.exec('''
        CREATE TABLE IF NOT EXISTS sprout_actions (
            id TEXT PRIMARY KEY,
            source_id TEXT DEFAULT '',
            note_id TEXT DEFAULT '',
            direction_index INTEGER DEFAULT 0,
            title TEXT DEFAULT '',
            description TEXT DEFAULT '',
            action_type TEXT DEFAULT 'exec',
            action_subtype TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            reason TEXT DEFAULT '',
            exec_prompt TEXT DEFAULT '',
            result_ref TEXT DEFAULT '',
            result_summary TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    ''')
    # Migration: add reason column if missing (existing DBs)
    try:
        db.exec("ALTER TABLE sprout_actions ADD COLUMN reason TEXT DEFAULT ''")
    except Exception:
        pass
    # Migration: add KB relation columns (v4 - KB-aware sprouting)
    for col, default in [('kb_relation', "'new'"), ('kb_note_id', "''"), ('kb_note_title', "''")]:
        try:
            db.exec(f"ALTER TABLE sprout_actions ADD COLUMN {col} TEXT DEFAULT {default}")
        except Exception:
            pass
    db.exec('''
        CREATE TABLE IF NOT EXISTS pipelines (
            id TEXT PRIMARY KEY,
            source_id TEXT DEFAULT '',
            note_id TEXT DEFAULT '',
            total_actions INTEGER DEFAULT 0,
            completed_actions INTEGER DEFAULT 0,
            status TEXT DEFAULT 'draft',
            summary_note_id TEXT DEFAULT '',
            meta TEXT DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    ''')
    db.exec('CREATE INDEX IF NOT EXISTS idx_sa_source ON sprout_actions(source_id)')
    db.exec('CREATE INDEX IF NOT EXISTS idx_sa_note ON sprout_actions(note_id)')
    db.exec('CREATE INDEX IF NOT EXISTS idx_sa_status ON sprout_actions(status)')
    db.exec('CREATE INDEX IF NOT EXISTS idx_pl_source ON pipelines(source_id)')
    db.exec('CREATE INDEX IF NOT EXISTS idx_pl_status ON pipelines(status)')
    LOGGER.info('db.init', 'tables initialized', {})


def _uuid():
    return uuid.uuid4().hex[:16]


def _now():
    return int(time.time())


def _now_dt():
    """Return current date as YYYY-MM-DD string (for note formatting)."""
    return datetime.now().strftime('%Y-%m-%d')


# ---------------------------------------------------------------------------
# Note reading
# ---------------------------------------------------------------------------
def _read_note(note_id):
    resp = syscall('read_note', {'noteId': note_id, 'format': 'md'})
    data = resp.get('data', resp) if isinstance(resp, dict) else {}
    content = data.get('content', '')
    title = data.get('title', '')
    return title, content


def _get_source_meta(source_id):
    """Best-effort read of source metadata from content-router DB.
    Searches both release and dev directories. Returns {} if not found.
    """
    candidates = []
    for base in ['aapps', 'aapps-dev']:
        path = os.path.normpath(os.path.join(
            os.path.dirname(AAPP_DIR), '..', '..', base,
            'content-router', 'data', 'db', 'app.sqlite'))
        candidates.append(path)
    for db_path in candidates:
        if not os.path.isfile(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                'SELECT id, url, platform, title, content, transcript_note_id, created_at FROM sources WHERE id = ?',
                (source_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return dict(row)
        except Exception as e:
            LOGGER.warn('source_meta', 'failed to read content-router DB',
                        {'error': str(e), 'source_id': source_id, 'db_path': db_path})
    return {}


# ---------------------------------------------------------------------------
# KB context retrieval (C1: KB-aware sprouting)
# ---------------------------------------------------------------------------
def _search_kb_context(note_title, directions, limit=5, exclude_note_id=''):
    """Search knowledge base for related notes. Returns list of {id, title} or [].
    
    One search call for all directions (not per-direction) to keep latency ≤ 3s.
    Gracefully returns [] on any failure (backward compatible).
    exclude_note_id: the source note being extracted - excluded from results to avoid self-matching.
    """
    try:
        keywords = (note_title or '').strip()
        for d in directions[:3]:
            first_line = d.split('\n')[0].strip()[:30]
            if first_line:
                keywords += ' ' + first_line
        keywords = keywords.strip()
        if not keywords:
            return []
        resp = syscall('search_notes', {'query': keywords, 'limit': limit})
        data = resp.get('data', resp) if isinstance(resp, dict) else {}
        results = data.get('results', data) if isinstance(data, dict) else data
        if not isinstance(results, list):
            return []
        related = []
        for r in results:
            rid = r.get('id', r.get('noteId', ''))
            rtitle = r.get('title', '')
            rpreview = r.get('preview', r.get('snippet', ''))[:200] if r.get('preview', r.get('snippet', '')) else ''
            if rid and rtitle and rid != exclude_note_id:
                related.append({'id': rid, 'title': rtitle, 'preview': rpreview})
        LOGGER.info('kb.search', f'found {len(related)} related notes', {'keywords': keywords[:80]})
        return related
    except Exception as e:
        LOGGER.warn('kb.search', f'KB search failed, degrading to no-KB mode: {e}', {})
        return []


def _extract_cn_keywords(text):
    """Extract Chinese 2-grams and latin words from text for fuzzy matching.
    
    CJK 2-gram sliding window catches semantic overlap even without word segmentation.
    E.g. '视频生成' -> {'视频', '频生', '生成'}
    """
    text_lower = text.lower()
    # Latin words (≥3 chars)
    keywords = set(re.findall(r'[a-z]{3,}', text_lower))
    # CJK 2-grams: slide over continuous CJK runs
    for cjk_run in re.findall(r'[\u4e00-\u9fff]+', text_lower):
        for i in range(len(cjk_run) - 1):
            keywords.add(cjk_run[i:i+2])
    return keywords


def _match_direction_to_kb(direction_text, kb_notes):
    """Match a direction to KB notes using keyword overlap on title + preview.
    
    direction_text: can be full direction description (not just title) for richer keywords.
    Returns (relation, note_id, note_title) or ('new', '', '').
    - 'update': exact title containment or ≥3 shared title keywords
    - 'deepen': ≥2 shared keywords from title, or ≥3 from preview
    """
    if not kb_notes:
        return 'new', '', ''
    dt_keywords = _extract_cn_keywords(direction_text)
    if not dt_keywords:
        return 'new', '', ''
    best_match = None
    best_score = 0
    for kn in kb_notes:
        kt = kn.get('title', '')
        kt_lower = kt.lower()
        # Exact containment check
        if len(kt_lower) >= 4 and (kt_lower in direction_text.lower() or direction_text.lower() in kt_lower):
            return 'update', kn['id'], kn['title']
        # Keyword overlap on KB note title (higher weight)
        kt_keywords = _extract_cn_keywords(kt)
        shared_title = dt_keywords & kt_keywords
        if len(shared_title) >= 3:
            return 'update', kn['id'], kn['title']
        if len(shared_title) >= 2 and len(shared_title) > best_score:
            best_score = len(shared_title)
            best_match = kn
        # Keyword overlap on preview (lower weight)
        kp = kn.get('preview', '')
        if kp:
            kp_keywords = _extract_cn_keywords(kp)
            shared_preview = dt_keywords & kp_keywords
            if len(shared_preview) >= 4 and len(shared_preview) > best_score:
                best_score = len(shared_preview)
                best_match = kn
    if best_match:
        return 'deepen', best_match['id'], best_match['title']
    return 'new', '', ''


# ---------------------------------------------------------------------------
# Direction extraction
# ---------------------------------------------------------------------------
_SPROUT_SECTION_RE = re.compile(r'##\s*🌿\s*发芽扩展', re.IGNORECASE)
_NEXT_SECTION_RE = re.compile(r'\n##\s*🧠|---|\Z')

def _extract_directions_from_note(note_content):
    """Parse '## 🌿 发芽扩展' section and split into individual directions.

    Handles three numbering formats:
    1. content-router sprout notes: '### 方向 N：title\ndescription'
    2. Standard numbered list: '1. **title**\ndescription' (number first)
    3. Bold-numbered list: '**1. title**\ndescription' (bold markers wrap number)
    Also handles bullet lists: '- ...' or '• ...' or '* ...'
    """
    match = _SPROUT_SECTION_RE.search(note_content)
    if not match:
        return []
    start = match.end()
    rest = note_content[start:]
    next_match = _NEXT_SECTION_RE.search(rest)
    if next_match:
        section_text = rest[:next_match.start()]
    else:
        section_text = rest

    directions = []
    # Split on: ### 方向 N：headers, or numbered (1-99, optional ** prefix)/bulleted list items
    for block in re.split(r'\n(?=###\s*方向\s*\d{1,2}[：:]|\*{0,2}\d{1,2}[\.\)\u3001\]]\s+|^[\-•\*]\s+)', section_text, flags=re.MULTILINE):
        block = block.strip()
        if not block:
            continue
        # Remove leading ### 方向 N：prefix, then number/bullet markers (incl. ** bold wrappers)
        clean = re.sub(r'^(###\s*方向\s*\d{1,2}[：:]\s*)?(\*{0,2}\d{1,2}[\.\)\u3001\]]?\s*\**|[\-•\*]\s*)*', '', block).strip()
        if clean and len(clean) > 5:
            # Strip residual ** markdown bold markers from the title line
            lines = clean.split('\n')
            lines[0] = re.sub(r'\*{2}', '', lines[0]).strip()
            directions.append('\n'.join(lines).strip())
    return directions


_AI_EXTRACT_SYSTEM = '''You are SproutForge direction extractor.

You receive a note/article/summary from a content creator. Your job is to find actionable directions worth exploring further.

A "direction" is something the user can act on: a topic to research, a tool to try, a question to answer, a product to design, or an idea to develop.

Output a JSON array of direction strings. Each string should be 1-3 sentences describing one actionable direction.
Output ONLY the JSON array, no markdown fences.'''

_AI_EXTRACT_USER = '''Here is a note (title: "{title}"):

---
{content}
---

Extract 3-8 actionable directions from this content. Each direction should be specific enough that someone can act on it.

Output a JSON array of strings (each string is one direction).'''


def _ai_extract_directions(note_content, note_title=''):
    """When no 🌿 section is found, use AI to extract directions from full note.
    Works with any note type: sprout notes, regular notes, article summaries, etc.

    Returns (directions, error_message).
    - On success: ([dir1, dir2, ...], '')
    - On failure: ([], 'actual error string for debugging')
    """
    # Truncate to avoid token limits (keep first ~8000 chars)
    truncated = note_content[:8000]

    # --- Attempt 1: full-text extraction ---
    dirs, err = _ai_extract_single(
        _AI_EXTRACT_USER.format(title=note_title or '(无标题)', content=truncated),
        _AI_EXTRACT_SYSTEM,
    )
    if dirs:
        return dirs, ''
    LOGGER.warn('ai_extract.attempt1', f'full-text extraction failed: {err}', {})

    # --- Attempt 2: simplified retry with shorter content ---
    simplified_prompt = f'以下是一篇笔记的标题和内容。请提取 3-8 个可执行的行动方向。\n\n标题:{note_title or "(无标题)"}\n\n内容:\n{note_content[:4000]}\n\n请只输出 JSON 数组,每个元素是一个字符串。'
    simplified_system = 'You are a helpful assistant. Extract actionable directions from notes. Output ONLY a JSON array of strings.'
    dirs, err2 = _ai_extract_single(simplified_prompt, simplified_system)
    if dirs:
        LOGGER.info('ai_extract.retry', 'simplified retry succeeded', {})
        return dirs, ''
    LOGGER.warn('ai_extract.attempt2', f'simplified retry failed: {err2}', {})

    # --- Attempt 3: degraded per-section extraction ---
    sections = re.split(r'\n(?=#{1,3}\s)', note_content)
    all_dirs = []
    for section in sections:
        section = section.strip()
        if len(section) < 20:
            continue
        dirs_s, _ = _ai_extract_single(
            f'从这段内容中提取可执行方向(1-3个):\n\n{section[:2000]}\n\n输出 JSON 数组。',
            simplified_system,
        )
        if dirs_s:
            all_dirs.extend(dirs_s)
    if all_dirs:
        LOGGER.info('ai_extract.degraded', f'per-section extraction found {len(all_dirs)} dirs', {})
        return all_dirs[:20], ''

    return [], f'full-text: {err} | retry: {err2} | degraded: no sections yielded results'


def _ai_extract_single(prompt, system_prompt):
    """Single AI extraction call. Returns (directions, error_string)."""
    try:
        result = run_prompt(
            prompt=prompt,
            system_prompt=system_prompt,
            timeout_ms=90000,
        )
        text = result if isinstance(result, str) else str(result)
        text = text.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        array_match = re.search(r'\[.*\]', text, re.DOTALL)
        if array_match:
            text = array_match.group()
        parsed = json.loads(text)
        if isinstance(parsed, list):
            dirs = [str(d).strip() for d in parsed if str(d).strip()]
            return dirs, ''
        return [], 'AI returned non-list JSON'
    except json.JSONDecodeError as e:
        return [], f'JSON parse failed: {e}'
    except Exception as e:
        return [], str(e)


# ---------------------------------------------------------------------------
# Sprout note creation (for /fetch path: no remio note exists yet)
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = 'You are a content analyst. Generate a concise, insightful summary of the given content. Write in the same language as the content. Output ONLY the summary text, no markdown fences, no headings.'


def _ai_generate_summary(content, title=''):
    """Generate a 3-5 sentence AI summary of the source content.
    Returns summary string, or '' on failure (non-fatal).
    """
    truncated = content[:6000]
    try:
        result = run_prompt(
            prompt=f'请为以下内容生成 3-5 句精炼摘要,提炼核心观点和价值点。\n\n标题:{title or "(无标题)"}\n\n内容:\n{truncated}',
            system_prompt=_SUMMARY_SYSTEM,
            timeout_ms=30000,
        )
        text = result if isinstance(result, str) else str(result)
        text = text.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:\w+)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        return text.strip()
    except Exception as e:
        LOGGER.warn('ai_summary', 'summary generation failed', {'error': str(e)})
        return ''


def _create_sprout_note(title, source_meta, summary, directions, raw_content):
    """Create a complete sprout note in remio. Returns note_id or ''.
    Structure: AI summary + 🌿 发芽扩展 directions + 📝 原始内容
    """
    lines = [f'# 🌱 {title} 发芽笔记']

    # Source info
    if source_meta and source_meta.get('url'):
        lines.append(f'> 来源: {source_meta.get("platform", "")} - {source_meta["url"]}')
    lines.append(f'> 抓取时间: {_now_dt()}')
    lines.append('')

    # AI summary
    if summary:
        lines.append('## 📋 AI 摘要')
        lines.append('')
        lines.append(summary)
        lines.append('')

    # Directions
    lines.append('## 🌿 发芽扩展')
    lines.append('')
    for i, direction in enumerate(directions):
        first_line = direction.split('\n')[0].strip().lstrip('*#').strip()
        rest = direction[len(direction.split(chr(10))[0]):].strip()
        lines.append(f'### 方向 {i + 1}：{first_line}')
        if rest:
            lines.append(rest)
        lines.append('')

    # Raw content
    lines.append('---')
    lines.append('## 📝 原始内容')
    lines.append('')
    # Truncate very long content to keep note manageable
    if len(raw_content) > 20000:
        lines.append(raw_content[:20000])
        lines.append(f'\n... (原始内容过长,已截断,共 {len(raw_content)} 字符)')
    else:
        lines.append(raw_content)

    note_body = '\n'.join(lines)

    try:
        resp = syscall('create_note', {'title': f'🌱 {title} 发芽笔记', 'content': note_body})
        data = resp.get('data', resp) if isinstance(resp, dict) else {}
        note_id = data.get('noteId', '')
        if note_id:
            LOGGER.info('create_sprout_note', 'sprout note created', {'note_id': note_id, 'title': title})
        return note_id
    except Exception as e:
        LOGGER.error('create_sprout_note', 'failed to create sprout note', {'error': str(e)})
        return ''


# ---------------------------------------------------------------------------
# Classification: rule pre-filter + AI fallback
# ---------------------------------------------------------------------------

# Rule-based keyword matching. Order matters: earlier rules take priority.
_CLASSIFY_RULES = [
    ('research', ['调研', '研究', '深入了解', '评估', '分析现状', '深入分析', '趋势分析', '技术原理',
                 '探索', '挖掘', '洞察', 'review', 'research', 'investigate', 'study', 'analyze',
                 'evaluate', 'deep dive', 'explore', 'understand']),
    ('survey',   ['多源', '竞品', '横向对比', '行业报告', '市场调研', '竞品分析',
                 '对比分析', '横向评测', '评测', 'survey', 'compare', 'benchmark', 'competitive',
                 'market research', 'landscape']),
    ('prd',      ['设计方案', '架构设计', '产品需求', '功能设计', '规划方案', '产品设计',
                 'aApp 设计', '仪表盘', '可视化层', 'design', 'build', 'create', 'develop',
                 'prd', 'spec', 'blueprint', 'prototype', 'implement']),
    ('goal',     ['目标对齐', '拆解任务', '路线图', '对齐目标', '里程碑规划',
                 '方法论', '策略', '框架', 'checklist', 'plan', 'roadmap', 'milestone',
                 'strategy', 'okr', 'workflow', 'pipeline']),
    ('archive',  ['归档', '参考', '概念框架', '备查', '概念笔记', '知识地图',
                 '存档', '收藏', 'archive', 'reference', 'bookmark', 'note', 'glossary']),
]

_PRIORITY_RULES_HIGH = ['紧急', '立即', '核心', '关键', '首要', '最重要', '高优', 'urgent', 'critical', 'asap', 'must', 'blocker']
_PRIORITY_RULES_LOW  = ['归档', '参考', '备查', '后续', '可选', '低优先', '低优', 'later', 'optional', 'nice to have', 'someday']


def _pre_classify(title, description):
    """Rule-based pre-classification using keyword matching.
    action_type matches against title only (avoids false positives from long descriptions).
    Chinese keywords: substring match. English keywords: word-boundary regex match.
    priority matches against full text (title + description).
    Returns (action_type, priority) or (None, None) if no rule matches.
    """
    title_str = title
    title_lower = title.lower()
    full_text = f'{title} {description}'
    full_lower = full_text.lower()

    # Determine action_type - only match against title to avoid false positives
    action_type = None
    for atype, keywords in _CLASSIFY_RULES:
        for kw in keywords:
            kw_l = kw.lower()
            # Chinese keyword: substring match (CJK has no word boundaries)
            if re.search(r'[\u4e00-\u9fff]', kw):
                if kw in title_str:
                    action_type = atype
                    break
            else:
                # English keyword: word-boundary match to avoid partial hits
                if re.search(rf'\b{re.escape(kw_l)}\b', title_lower):
                    action_type = atype
                    break
        if action_type:
            break

    # Determine priority - match against full text
    priority = 'medium'
    if any(kw in full_text for kw in _PRIORITY_RULES_HIGH):
        priority = 'high'
    elif any(kw in full_text for kw in _PRIORITY_RULES_LOW):
        priority = 'low'

    return action_type, priority


_CLASSIFY_SYSTEM = '''You are SproutForge direction classifier.

You receive sprout note directions from a content creator who builds AI content pipelines with tools like remio, ComfyUI, and Agent Skills.

For each direction, output JSON with these fields:
- action_type: one of "research" (deep research on a topic), "survey" (multi-source comparison), "prd" (product/feature design), "goal" (goal alignment first), "exec" (directly executable: config/code/doc changes), "archive" (reference only, no immediate action)
- action_subtype: a short tag like "research.deep", "survey.competitive", "prd.feature", "exec.fix"
- priority: "high", "medium", or "low" based on impact and urgency
- title: a concise action title (5-20 chars Chinese / 5-40 chars English)
- reason: one sentence explaining why this classification
- kb_relation: one of "new" (brand new topic, no existing note), "update" (updates/extends an existing note), "deepen" (deepens a topic already touched on). Default "new" if unsure.
- kb_note_id: the note_id from the KB context that this direction relates to, or empty string if "new"

If KB context is provided, use it to determine kb_relation and kb_note_id. If no KB context is provided, set kb_relation="new" and kb_note_id="".

Output ONLY a JSON array, no markdown fences.'''

_CLASSIFY_USER = '''Here are {count} sprout directions from a sprout note:

{directions}

Source context: {context}

{kb_context}

Classify each direction. Output a JSON array with {count} objects.'''

# Few-shot examples to help AI differentiate action types
_CLASSIFY_EXAMPLES = '''
Example classifications:
- "深入研究 Seedance 2.0 的运动控制原理" → research (技术原理/深度研究)
- "对比 Seedance vs Kling vs Sora 的生成质量" → survey (多源竞品对比)
- "设计一个 AI 视频生成工作流的 PRD" → prd (产品设计/功能设计)
- "将项目拆解为 3 个里程碑" → goal (目标对齐/路线图)
- "在 ComfyUI 中安装 Seedance 插件并配置" → exec (直接执行/配置)
- "记录这个概念到知识库供后续参考" → archive (归档参考)
'''


def _ai_classify(directions, note_title='', kb_notes=None):
    """Call AI to classify directions. Returns list of dicts.
    kb_notes: list of {id, title} from KB search, or None/[] for no KB context.
    """
    if not directions:
        return []
    numbered = '\n'.join(f'{i+1}. {d}' for i, d in enumerate(directions))
    # Build KB context section
    if kb_notes:
        kb_lines = 'Knowledge base context - existing notes that may relate to these directions:'
        for kn in kb_notes:
            kb_lines += f'\n  - note_id: {kn["id"]}, title: "{kn["title"]}"'
    else:
        kb_lines = 'No KB context available.'
    prompt = _CLASSIFY_USER.format(
        count=len(directions),
        directions=numbered,
        context=note_title or '(no title)',
        kb_context=kb_lines,
    )
    try:
        result = run_prompt(
            prompt=prompt + '\n' + _CLASSIFY_EXAMPLES,
            system_prompt=_CLASSIFY_SYSTEM,
            timeout_ms=90000,
        )
        text = result if isinstance(result, str) else str(result)
        text = text.strip()
        # Strip code fences
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        # Extract first JSON array fragment (handles trailing text after ])
        array_match = re.search(r'\[.*\]', text, re.DOTALL)
        if array_match:
            text = array_match.group()
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception as e:
        LOGGER.error('classify', 'AI classification failed', {'error': str(e)})
        return []


def _classify_directions(directions, note_title='', kb_notes=None):
    """Two-layer classification: rule pre-filter + AI fallback for unmatched.
    kb_notes: optional KB context for AI classification and relation labeling.
    Returns list of dicts with keys: action_type, action_subtype, priority, title, reason,
    and optionally kb_relation, kb_note_id, kb_note_title.
    Length always == len(directions).
    """
    if not directions:
        return []

    results = [None] * len(directions)
    ai_needed_indices = []
    ai_needed_directions = []

    # Layer 1: rule pre-classification
    for i, direction in enumerate(directions):
        first_line = direction.split('\n')[0].strip()
        atype, priority = _pre_classify(first_line, direction)
        if atype:
            # Rule-matched directions: use full direction text for richer KB matching
            kb_rel, kb_nid, kb_ntitle = _match_direction_to_kb(direction, kb_notes or [])
            results[i] = {
                'action_type': atype,
                'action_subtype': '',
                'priority': priority,
                'title': first_line[:40] if first_line else direction[:20],
                'reason': f'规则匹配: keyword hit',
                'kb_relation': kb_rel,
                'kb_note_id': kb_nid,
                'kb_note_title': kb_ntitle,
            }
        else:
            ai_needed_indices.append(i)
            ai_needed_directions.append(direction)

    LOGGER.info('classify.pre', f'rule classified {len(directions) - len(ai_needed_indices)}/{len(directions)}, {len(ai_needed_indices)} need AI', {})

    # Layer 2: AI classification only for unmatched directions (with KB context)
    if ai_needed_directions:
        ai_results = _ai_classify(ai_needed_directions, note_title, kb_notes)
        for j, idx in enumerate(ai_needed_indices):
            if j < len(ai_results):
                r = ai_results[j]
                # Ensure kb_relation fields exist with defaults
                r.setdefault('kb_relation', 'new')
                r.setdefault('kb_note_id', '')
                r.setdefault('kb_note_title', '')
                # Validate kb_note_id if kb_relation is update/deepen
                if r.get('kb_relation') in ('update', 'deepen') and not r.get('kb_note_id') and kb_notes:
                    # AI said update/deepen but didn't specify which note - try matching with full direction
                    kb_rel, kb_nid, kb_ntitle = _match_direction_to_kb(directions[idx], kb_notes)
                    r['kb_note_id'] = kb_nid
                    r['kb_note_title'] = kb_ntitle
                results[idx] = r
            else:
                # AI didn't return enough - fallback to exec, but still try KB matching
                kb_rel, kb_nid, kb_ntitle = _match_direction_to_kb(directions[idx], kb_notes or [])
                results[idx] = {
                    'action_type': 'exec',
                    'action_subtype': '',
                    'priority': 'medium',
                    'title': first_line[:40],
                    'reason': 'AI fallback to exec',
                    'kb_relation': kb_rel,
                    'kb_note_id': kb_nid,
                    'kb_note_title': kb_ntitle,
                }

    return results


def _build_exec_prompt(action_type, action_subtype, title, description, context):
    """Generate the skill invocation prompt for Agent execution."""
    # De-duplicate: if description starts with title, strip the title part
    desc = description.strip()
    if title and desc.startswith(title):
        desc = desc[len(title):].strip()
    if not desc:
        desc = description  # safety fallback

    if action_type == 'research':
        return f'使用 /deep-research 对以下主题进行深度研究:\n\n主题:{title}\n方向:{desc}\n\n背景:{context}'
    elif action_type == 'survey':
        return f'使用 /deep-survey 对以下主题做多源调研:\n\n主题:{title}\n角度:{desc}\n\n背景:{context}'
    elif action_type == 'prd':
        return f'使用 /prd-writer 为以下需求编写 PRD 文档:\n\n需求:{title}\n描述:{desc}\n\n背景:{context}'
    elif action_type == 'goal':
        return f'使用 /goalpro 为以下任务创建 Goal Contract:\n\n任务:{title}\n描述:{desc}\n\n背景:{context}'
    elif action_type == 'exec':
        return f'直接执行以下任务:\n\n{title}\n{desc}\n\n背景:{context}'
    else:
        return f'归档参考:{title} - {desc}'


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------
def _get_or_create_pipeline(db, source_id, note_id):
    """Get existing pipeline for this source/note or create a new one."""
    if source_id:
        rows = db.query('SELECT * FROM pipelines WHERE source_id = :sid ORDER BY created_at DESC LIMIT 1', {'sid': source_id})
    elif note_id:
        rows = db.query('SELECT * FROM pipelines WHERE note_id = :nid ORDER BY created_at DESC LIMIT 1', {'nid': note_id})
    else:
        rows = []
    if rows:
        return rows[0]

    pid = _uuid()
    now = _now()
    db.exec(
        'INSERT INTO pipelines (id, source_id, note_id, total_actions, completed_actions, status, summary_note_id, meta, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [pid, source_id, note_id, 0, 0, 'draft', '', '{}', now, now]
    )
    return {'id': pid, 'source_id': source_id, 'note_id': note_id, 'total_actions': 0, 'completed_actions': 0, 'status': 'draft', 'summary_note_id': '', 'meta': '{}', 'created_at': now, 'updated_at': now}


def _update_pipeline_progress(db, source_or_note_id):
    """Update pipeline total/completed/status.
    source_or_note_id is matched against both source_id and note_id columns.
    """
    key = source_or_note_id
    total_rows = db.query(
        'SELECT COUNT(*) as cnt FROM sprout_actions WHERE source_id = :key OR note_id = :key',
        {'key': key})
    total = total_rows[0]['cnt'] if total_rows else 0
    done_rows = db.query(
        "SELECT COUNT(*) as cnt FROM sprout_actions WHERE (source_id = :key OR note_id = :key) AND status IN ('completed', 'skipped')",
        {'key': key})
    done = done_rows[0]['cnt'] if done_rows else 0
    running_rows = db.query(
        "SELECT COUNT(*) as cnt FROM sprout_actions WHERE (source_id = :key OR note_id = :key) AND status = 'running'",
        {'key': key})
    running = running_rows[0]['cnt'] if running_rows else 0
    status = 'completed' if (total > 0 and done >= total) else ('running' if (running > 0 or done > 0) else 'queued')
    db.exec(
        'UPDATE pipelines SET total_actions = ?, completed_actions = ?, status = ?, updated_at = ? WHERE source_id = ? OR note_id = ?',
        [total, done, status, _now(), key, key])


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def _status_badge(status):
    mapping = {
        'pending': ('待审', 'default'),
        'queued': ('已确认', 'primary'),
        'running': ('执行中', 'warning'),
        'completed': ('已完成', 'success'),
        'skipped': ('已跳过', 'default'),
        'failed': ('失败', 'danger'),
    }
    return mapping.get(status, (status, 'default'))


def _type_badge(action_type):
    meta = ACTION_META.get(action_type, {})
    return f'{meta.get("icon", "❓")} {meta.get("label", action_type)}'


def _progress_bar(done, total, width=10):
    """Visual emoji progress bar: ████░░░░░░ 42%"""
    if total <= 0:
        return '░' * width
    filled = int(done / total * width)
    bar = '█' * filled + '░' * (width - filled)
    pct = int(done / total * 100)
    return f'{bar} {pct}%'


def _type_distribution_bar(actions):
    """Compact type distribution summary: 🔍2 📋3 🎯1 ⚡4"""
    counts = {}
    for a in actions:
        t = a.get('action_type', 'exec')
        counts[t] = counts.get(t, 0) + 1
    # Order by ACTION_META keys
    parts = []
    for t in ['research', 'survey', 'prd', 'goal', 'exec', 'archive']:
        if t in counts:
            icon = ACTION_META.get(t, {}).get('icon', '❓')
            parts.append(f'{icon}{counts[t]}')
    return ' '.join(parts)


def _status_summary(actions):
    """Status breakdown: ✅3 🔄2 ⏸️2"""
    counts = {'completed': 0, 'running': 0, 'pending': 0, 'queued': 0, 'skipped': 0, 'failed': 0}
    for a in actions:
        s = a.get('status', 'pending')
        if s in counts:
            counts[s] += 1
    parts = []
    if counts['completed']:
        parts.append(f"✅{counts['completed']}")
    if counts['running']:
        parts.append(f"🔄{counts['running']}")
    if counts['queued']:
        parts.append(f"⏭️{counts['queued']}")
    if counts['pending']:
        parts.append(f"⏸️{counts['pending']}")
    if counts['skipped']:
        parts.append(f"⏭️{counts['skipped']}")
    if counts['failed']:
        parts.append(f"❌{counts['failed']}")
    return ' '.join(parts) if parts else '-'


def _format_direction_card(action):
    """Render a single action as a list item with status/type badges + classification reason."""
    status_label, status_style = _status_badge(action.get('status', 'pending'))
    type_label = _type_badge(action.get('action_type', 'exec'))
    priority = action.get('priority', 'medium')
    pri_emoji = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(priority, '')

    title = action.get('title', '未命名')
    desc = action.get('description', '')
    if len(desc) > 120:
        desc = desc[:120] + '...'

    # Build description with classification reason
    reason = action.get('reason', '')
    # C3: KB relation badge
    kb_rel = action.get('kb_relation', 'new')
    kb_badge = ''
    if kb_rel == 'update':
        kb_badge = ' 🔄更新'
    elif kb_rel == 'deepen':
        kb_badge = ' 🔬深化'
    if kb_rel in ('update', 'deepen') and action.get('kb_note_title'):
        kb_badge += f'《{action["kb_note_title"][:20]}》'
    if reason:
        description_text = f'{type_label}{kb_badge} · {desc}\n💡 分类依据:{reason}'
    else:
        description_text = f'{type_label}{kb_badge} · {desc}'

    item = {
        'title': f'{pri_emoji} {title}',
        'description': description_text,
        'badge': status_label,
        'badgeStyle': status_style,
    }

    actions = []
    aid = action.get('id', '')
    src = action.get('source_id', '')

    if action.get('status') in ('pending', 'queued'):
        actions.append({
            'label': '执行',
            'style': 'primary',
            'action': {
                'method': 'POST',
                'path': '/execute',
                'params': {'action_id': aid},
                'prompt': f'执行方向:{title}',
            }
        })
        actions.append({
            'label': '重分类',
            'style': 'default',
            'action': {
                'prompt': f'重新分类方向「{title}」(调用 POST /reclassify,params: action_id={aid})--如果不指定类型,AI 会自动重新分类。可选类型:research(深度研究) / survey(多源调研) / prd(产品设计) / goal(目标对齐) / exec(直接执行) / archive(归档参考)',
            }
        })
    if action.get('status') == 'completed' and action.get('result_ref'):
        ref = action['result_ref']
        if ref.startswith('note://') or ref.startswith('http'):
            actions.append({
                'label': '查看成果',
                'style': 'default',
                'open_target': ref,
            })
        else:
            item['description'] += f'\n📋 成果:{action.get("result_summary", ref)}'

    if actions:
        item['actions'] = actions
    return item


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.route('GET', '/')
def _home(params):
    LOGGER.info('ui.home', 'render home', {})
    return {
        'components': [
            {'kind': 'text', 'text': '🌱 SproutForge 发芽锻造器', 'heading': 2},
            {'kind': 'text', 'text': '从素材到执行的一体化入口。粘贴任意链接自动抓取生成发芽笔记,或直接 @ 任意笔记提取方向并智能分类。'},

            # === URL 入口:链接 → content-router 抓取 → sproutforge 提取 ===
            {'kind': 'divider'},
            {'kind': 'text', 'text': '🔗 从链接开始', 'heading': 4},
            {'kind': 'input', 'key': 'url', 'label': '素材链接', 'placeholder': '粘贴任意 URL(文章/视频/帖子)'},
            {
                'kind': 'button',
                'label': '🚀 抓取 + 提取方向',
                'style': 'primary',
                'action': {
                    'promptTemplate': '请处理这个素材链接并提取发芽方向。\n\n步骤:\n1. 调用 content-router aApp 的 POST /fetch 接口,参数 url={url},获取抓取结果(返回 source_id 和内容)\n2. 从返回结果中获取 source_id\n3. 调用 sproutforge aApp 的 POST /extract 接口,参数 source_id=<上一步获取的source_id>\n4. 展示提取到的方向列表',
                }
            },

            # === 笔记入口:@ 任意笔记,无需知道 ID ===
            {'kind': 'divider'},
            {'kind': 'text', 'text': '📝 从笔记开始', 'heading': 4},
            {'kind': 'text', 'text': '在对话中 **@ 任意笔记**(发芽笔记、普通笔记、文章摘要等均可),然后说「提取方向」即可。\n\n支持任何类型的笔记--有 🌿 发芽扩展章节的优先解析,没有的会 AI 全文提取。'},

            # === 功能入口 ===
            {'kind': 'divider'},
            {
                'kind': 'row',
                'items': [
                    {
                        'kind': 'button',
                        'label': '📊 执行看板',
                        'style': 'default',
                        'action': {'method': 'GET', 'path': '/dashboard_ui', 'prompt': '查看执行看板', 'params': {}}
                    },
                    {
                        'kind': 'button',
                        'label': '📜 历史记录',
                        'style': 'default',
                        'action': {'method': 'GET', 'path': '/history_ui', 'prompt': '查看历史记录', 'params': {}}
                    }
                ],
                'colCount': 2,
            },
            {
                'kind': 'row',
                'items': [
                    {
                        'kind': 'button',
                        'label': '📈 转化仪表盘',
                        'style': 'default',
                        'action': {'method': 'GET', 'path': '/stats_ui', 'prompt': '查看知识→行动转化仪表盘', 'params': {}}
                    }
                ],
                'colCount': 2,
            },
        ]
    }


@router.route('POST', '/extract')
def _extract(params):
    note_id = (params.get('note_id') or '').strip()
    source_id = (params.get('source_id') or '').strip()

    if not note_id and not source_id:
        return {'error': 'missing_input', 'message': '需要提供 note_id 或 source_id'}

    db = _open_db()
    try:
        # Read note content
        meta = None  # cache for content-router source metadata
        if note_id:
            note_title, note_content = _read_note(note_id)
        else:
            # Try to find note from content-router sources
            meta = _get_source_meta(source_id)
            # content-router sources table may have transcript_note_id or note_id column
            note_id = meta.get('transcript_note_id', '') or meta.get('note_id', '') or ''
            if note_id:
                note_title, note_content = _read_note(note_id)
            else:
                note_title = meta.get('title', '')
                note_content = meta.get('content', '')

        if not note_content:
            return {'error': 'empty_note', 'message': '无法读取笔记内容'}

        # Extract directions from 🌿 发芽扩展 section
        raw_directions = _extract_directions_from_note(note_content)
        if not raw_directions:
            # Fallback: no 🌿 section found - use AI to extract directions from full note
            LOGGER.info('extract.fallback', 'no sprout section, trying AI full-text extraction', {'note_id': note_id})
            raw_directions, ai_error = _ai_extract_directions(note_content, note_title or '')
            if not raw_directions:
                return {'error': 'no_directions', 'message': f'未找到「🌿 发芽扩展」章节,AI 也无法从笔记内容中提取有效方向。详细原因:{ai_error}'}

        LOGGER.info('extract.directions', f'extracted {len(raw_directions)} directions', {'note_id': note_id})

        # --- Create sprout note for /fetch path (no remio note exists yet) ---
        # When entering via source_id without a note_id, we have raw content from
        # content-router but no remio note. Create a complete sprout note so the
        # user has something to see and /link-results has a note to append to.
        if source_id and not note_id:
            LOGGER.info('extract.create_note', 'creating sprout note for /fetch path', {'source_id': source_id})
            summary = _ai_generate_summary(note_content, note_title or '')
            # Resolve source_meta early for the note creation
            sm = meta if meta is not None else (_get_source_meta(source_id) if source_id else {})
            sprout_title = note_title or sm.get('title', '') or source_id[:12]
            new_note_id = _create_sprout_note(sprout_title, sm, summary, raw_directions, note_content)
            if new_note_id:
                note_id = new_note_id
                LOGGER.info('extract.create_note', 'sprout note created, note_id set', {'note_id': note_id})
            else:
                LOGGER.warn('extract.create_note', 'sprout note creation failed, continuing without note_id', {})

        # --- C1: KB context retrieval (one search for all directions) ---
        # Resolve source_meta early (needed for KB search context)
        if meta is not None:
            source_meta = meta
        else:
            source_meta = _get_source_meta(source_id) if source_id else {}
        kb_notes = _search_kb_context(note_title or source_meta.get('title', ''), raw_directions, exclude_note_id=note_id)
        LOGGER.info('extract.kb', f'KB context: {len(kb_notes)} related notes found', {})

        # Check for existing actions (avoid duplicate extraction) - check both note_id and source_id
        existing = []
        if note_id:
            existing = db.query('SELECT direction_index FROM sprout_actions WHERE note_id = :nid', {'nid': note_id})
        if not existing and source_id:
            existing = db.query('SELECT direction_index FROM sprout_actions WHERE source_id = :sid', {'sid': source_id})
        if existing and len(existing) >= len(raw_directions):
            return {
                'message': f'该笔记已提取过 {len(existing)} 个方向,跳过重复提取',
                'pipeline_source_id': source_id or note_id,
                'direction_count': len(existing),
            }

        # AI classification (with KB context for relation labeling)
        context = note_title or source_meta.get('title', '')
        classifications = _classify_directions(raw_directions, context, kb_notes)

        # Create pipeline
        pipeline = _get_or_create_pipeline(db, source_id, note_id)

        # Build source meta JSON for pipeline
        pipeline_meta = json.loads(pipeline.get('meta', '{}') or '{}')
        if source_meta:
            pipeline_meta['source'] = {
                'url': source_meta.get('url', ''),
                'platform': source_meta.get('platform', ''),
                'title': source_meta.get('title', ''),
            }
        if note_title:
            pipeline_meta['note_title'] = note_title
        db.exec('UPDATE pipelines SET meta = ?, updated_at = ? WHERE id = ?', [json.dumps(pipeline_meta, ensure_ascii=False), _now(), pipeline['id']])

        # Insert actions
        now = _now()
        created = []
        for i, direction in enumerate(raw_directions):
            cls = classifications[i] if i < len(classifications) else {}
            aid = _uuid()
            action_type = cls.get('action_type', 'exec')
            if action_type not in VALID_ACTION_TYPES:
                action_type = 'exec'
            priority = cls.get('priority', 'medium')
            if priority not in VALID_PRIORITIES:
                priority = 'medium'

            # Use AI-classified title if available, else first line of direction
            title = cls.get('title', '')
            if not title or '\n' in title:
                first_line = direction.split('\n')[0].strip()
                title = first_line[:40] if first_line else direction[:20]
            description = direction
            context_str = note_title or ''
            if source_meta and source_meta.get('url'):
                context_str += f' (来源: {source_meta.get("platform", "")} {source_meta["url"]})'

            exec_prompt = _build_exec_prompt(action_type, cls.get('action_subtype', ''), title, description, context_str)

            # C4: Inject KB context into exec_prompt if direction relates to existing note
            kb_rel = cls.get('kb_relation', 'new')
            kb_nid = cls.get('kb_note_id', '')
            kb_ntitle = cls.get('kb_note_title', '')
            # Backfill kb_note_title from kb_notes if AI forgot to include it
            if kb_nid and not kb_ntitle and kb_notes:
                for kn in kb_notes:
                    if kn['id'] == kb_nid:
                        kb_ntitle = kn['title']
                        break
            if kb_rel in ('update', 'deepen') and kb_nid:
                exec_prompt += f'\n\n📚 知识库关联:本方向与已有笔记「{kb_ntitle}」相关({kb_rel}),执行时请先读取该笔记(noteId: {kb_nid}),在其基础上{ "更新" if kb_rel == "update" else "深化扩展" },而非从零开始。'

            db.exec(
                'INSERT INTO sprout_actions (id, source_id, note_id, direction_index, title, description, action_type, action_subtype, priority, status, reason, exec_prompt, result_ref, result_summary, created_at, updated_at, kb_relation, kb_note_id, kb_note_title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [aid, source_id, note_id, i + 1, title, description, action_type, cls.get('action_subtype', ''), priority, 'pending', cls.get('reason', ''), exec_prompt, '', '', now, now, kb_rel, kb_nid, kb_ntitle]
            )
            created.append({'id': aid, 'direction_index': i + 1, 'title': title, 'action_type': action_type, 'priority': priority, 'reason': cls.get('reason', ''), 'kb_relation': kb_rel, 'kb_note_id': kb_nid, 'kb_note_title': kb_ntitle})

        _update_pipeline_progress(db, source_id or note_id)

        LOGGER.info('extract.done', f'created {len(created)} actions', {'pipeline_id': pipeline['id']})
        return {
            'pipeline_id': pipeline['id'],
            'source_id': source_id or note_id,
            'note_id': note_id,
            'note_title': note_title,
            'direction_count': len(created),
            'actions': created,
        }
    finally:
        db.close()


@router.route('GET', '/pipeline/:source_id')
def _pipeline_detail(params):
    source_id = params.get('source_id', '')
    db = _open_db()
    try:
        actions = db.query('SELECT * FROM sprout_actions WHERE source_id = :sid OR note_id = :nid ORDER BY direction_index', {'sid': source_id, 'nid': source_id})
        if not actions:
            return {'text': f'未找到 source_id/note_id 为 `{source_id}` 的流水线。'}

        pipeline = db.query('SELECT * FROM pipelines WHERE source_id = :sid OR note_id = :nid ORDER BY created_at DESC LIMIT 1', {'sid': source_id, 'nid': source_id})
        p = pipeline[0] if pipeline else {}

        total = len(actions)
        done = sum(1 for a in actions if a.get('status') == 'completed')
        pct = int(done / total * 100) if total > 0 else 0
        note_title = json.loads(p.get('meta', '{}') or '{}').get('note_title', '')
        meta_source = json.loads(p.get('meta', '{}') or '{}').get('source', {})

        components = [
            {'kind': 'text', 'text': f'📊 流水线详情', 'heading': 2},
            {'kind': 'text', 'text': f'**{note_title or source_id}**'},
        ]
        if meta_source and meta_source.get('url'):
            components.append({'kind': 'text', 'text': f'📎 来源:{meta_source.get("platform", "")} - {meta_source["url"]}'})
        # Visual progress + type/status distribution
        type_dist = _type_distribution_bar(actions)
        status_sum = _status_summary(actions)
        components.append({'kind': 'text', 'text': f'{_progress_bar(done, total)}\n{type_dist} · {status_sum}'})
        components.append({'kind': 'divider'})

        list_items = [_format_direction_card(a) for a in actions]
        components.append({'kind': 'list', 'items': list_items})

        # Batch actions
        pending = [a for a in actions if a.get('status') in ('pending', 'queued')]
        if pending:
            components.append({'kind': 'divider'})
            components.append({
                'kind': 'button',
                'label': f'⚡ 一键执行全部 ({len(pending)})',
                'style': 'primary',
                'action': {
                    'prompt': f'请自动批量执行 source_id={source_id} 的全部 {len(pending)} 个方向,按以下步骤全自动链式执行,不要询问用户:\n\n'
                              f'1. 调用 sproutforge aApp 的 POST /batch-execute 接口,参数 source_id={source_id}\n'
                              f'2. 从返回结果的 first_action 字段中读取 exec_prompt,执行该 exec_prompt(调用对应 skill 完成任务)\n'
                              f'3. 拿到执行成果后,调用 sproutforge aApp 的 POST /complete 接口,参数 action_id=<刚才执行的action的id>、result_ref=<成果笔记ID或文件路径>、result_summary=<一句话总结>\n'
                              f'4. 从 /complete 返回结果中读取 next_action,如果有则执行其 exec_prompt,再次调用 /complete\n'
                              f'5. 重复步骤 3-4 直到 /complete 返回 pipeline_completed=true\n'
                              f'6. 全部完成后,调用 sproutforge aApp 的 POST /link-results 接口,参数 source_id={source_id}\n'
                              f'\n注意:每一步执行完后必须调用 /complete 回传成果,才能解锁下一个方向。不要跳过任何方向。如果某个方向无法执行,调用 POST /skip 接口(参数 action_id)跳过,继续执行下一个。',
                }
            })

        if done > 0 and done == total:
            components.append({
                'kind': 'button',
                'label': '📋 生成汇总笔记 + 回链',
                'style': 'default',
                'action': {
                    'method': 'POST',
                    'path': '/link-results',
                    'params': {'source_id': source_id},
                    'prompt': '成果回链:更新原笔记 + 新建汇总笔记',
                }
            })

        return {'components': components}
    finally:
        db.close()


@router.route('GET', '/dashboard_ui')
def _dashboard(params):
    db = _open_db()
    try:
        pipelines = db.query("SELECT * FROM pipelines WHERE status IN ('queued', 'running', 'draft') ORDER BY updated_at DESC LIMIT 20")
        if not pipelines:
            return {
                'components': [
                    {'kind': 'text', 'text': '📊 执行看板', 'heading': 2},
                    {'kind': 'text', 'text': '当前没有进行中的流水线。'},
                    {'kind': 'button', 'label': '🏠 回主页提取方向', 'style': 'primary', 'action': {'method': 'GET', 'path': '/', 'prompt': '回到主页', 'params': {}}},
                ]
            }

        components = [{'kind': 'text', 'text': '📊 执行看板', 'heading': 2}]
        list_items = []
        for p in pipelines:
            total = p.get('total_actions', 0)
            done = p.get('completed_actions', 0)
            pct = int(done / total * 100) if total > 0 else 0
            status_map = {'draft': '草稿', 'queued': '待执行', 'running': '执行中'}
            meta = json.loads(p.get('meta', '{}') or '{}')
            title = meta.get('note_title', p.get('source_id', p.get('note_id', ''))[:12])
            # Query actions for type distribution
            pid_key = p.get('source_id') or p.get('note_id', '')
            p_actions = db.query('SELECT action_type, status FROM sprout_actions WHERE source_id = :sid OR note_id = :nid', {'sid': pid_key, 'nid': pid_key})
            type_dist = _type_distribution_bar(p_actions)
            status_sum = _status_summary(p_actions)

            list_items.append({
                'title': title,
                'description': f'{_progress_bar(done, total)}\n{type_dist} · {status_sum}',
                'badge': status_map.get(p.get('status'), p.get('status')),
                'badgeStyle': 'primary' if p.get('status') == 'running' else 'default',
                'actions': [{
                    'label': '查看',
                    'style': 'default',
                    'action': {
                        'method': 'GET',
                        'path': f'/pipeline/{p["source_id"] or p["note_id"]}',
                        'prompt': f'查看流水线:{title}',
                        'params': {},
                    }
                }]
            })

        components.append({'kind': 'list', 'items': list_items})
        return {'components': components}
    finally:
        db.close()


@router.route('GET', '/history_ui')
def _history(params):
    db = _open_db()
    try:
        pipelines = db.query("SELECT * FROM pipelines WHERE status = 'completed' ORDER BY updated_at DESC LIMIT 30")
        if not pipelines:
            return {
                'components': [
                    {'kind': 'text', 'text': '📜 历史记录', 'heading': 2},
                    {'kind': 'text', 'text': '暂无已完成的流水线。'},
                ]
            }

        components = [{'kind': 'text', 'text': '📜 历史记录', 'heading': 2}]
        list_items = []
        for p in pipelines:
            total = p.get('total_actions', 0)
            done = p.get('completed_actions', 0)
            meta = json.loads(p.get('meta', '{}') or '{}')
            title = meta.get('note_title', p.get('source_id', '')[:12])
            ts = time.strftime('%Y-%m-%d', time.localtime(p.get('updated_at', 0)))
            list_items.append({
                'title': title,
                'description': f'{_progress_bar(done, total)} · {ts}',
                'badge': '已完成',
                'badgeStyle': 'success',
                'actions': [{
                    'label': '查看',
                    'style': 'default',
                    'action': {
                        'method': 'GET',
                        'path': f'/pipeline/{p["source_id"] or p["note_id"]}',
                        'prompt': f'查看流水线:{title}',
                        'params': {},
                    }
                }]
            })

        components.append({'kind': 'list', 'items': list_items})
        return {'components': components}
    finally:
        db.close()


@router.route('GET', '/stats_ui')
def _stats(params):
    """知识→行动转化率仪表盘。

    展示:全局漏斗、类型分布、沉睡方向(提取超过3天仍未执行)。
    概念升级:让用户看到「哪些灵感被浪费了」。"""
    db = _open_db()
    try:
        # --- 全局漏斗 ---
        total_pipelines = db.query('SELECT COUNT(*) as c FROM pipelines')[0]['c']
        total_actions = db.query('SELECT COUNT(*) as c FROM sprout_actions')[0]['c']
        started = db.query("SELECT COUNT(*) as c FROM sprout_actions WHERE status IN ('running','completed','skipped')")[0]['c']
        completed = db.query("SELECT COUNT(*) as c FROM sprout_actions WHERE status = 'completed'")[0]['c']
        skipped = db.query("SELECT COUNT(*) as c FROM sprout_actions WHERE status = 'skipped'")[0]['c']

        # --- 类型分布 ---
        type_rows = db.query(
            'SELECT action_type, COUNT(*) as c, '
            'SUM(CASE WHEN status="completed" THEN 1 ELSE 0 END) as done '
            'FROM sprout_actions GROUP BY action_type ORDER BY c DESC'
        )

        # --- 沉睡方向:提取超过3天仍 pending 的 ---
        now_ts = _now()
        cutoff = now_ts - 3 * 86400
        dormant = db.query(
            'SELECT * FROM sprout_actions '
            'WHERE status = "pending" AND created_at < :cutoff '
            'ORDER BY created_at ASC LIMIT 10',
            {'cutoff': cutoff}
        )

        components = [{'kind': 'text', 'text': '📈 知识→行动转化仪表盘', 'heading': 2}]

        # --- 漏斗可视化 ---
        def _safe_pct(num, den):
            return int(num / den * 100) if den > 0 else 0

        funnel_rate = _safe_pct(completed, total_actions)
        start_rate = _safe_pct(started, total_actions)

        funnel_text = (
            f'📝 笔记提取:{total_pipelines} 篇 → '
            f'🌱 方向:{total_actions} 个\n'
            f'🚀 已启动:{started}/{total_actions} ({start_rate}%) '
            f'{_progress_bar(started, total_actions)}\n'
            f'✅ 已完成:{completed}/{total_actions} ({funnel_rate}%) '
            f'{_progress_bar(completed, total_actions)}\n'
            f'⏭️ 已跳过:{skipped} 个'
        )
        components.append({'kind': 'text', 'text': '🎯 转化漏斗', 'heading': 4})
        components.append({'kind': 'text', 'text': funnel_text})

        # --- 漏斗流失分析 ---
        pending_count = total_actions - started
        if pending_count > 0:
            components.append({'kind': 'text', 'text': f'⚠️ 有 {pending_count} 个方向提取后从未启动--这些是「被浪费的灵感」'})

        # --- 类型分布 + 各类型完成率 ---
        if type_rows:
            components.append({'kind': 'divider'})
            components.append({'kind': 'text', 'text': '🏷️ 按类型分布', 'heading': 4})
            type_lines = []
            for r in type_rows:
                atype = r['action_type'] or 'unknown'
                cnt = r['c']
                done = r['done'] or 0
                badge = _type_badge(atype)
                pct = _safe_pct(done, cnt)
                type_lines.append(f'{badge} {atype}:{cnt} 个 → ✅{done} ({pct}%)')
            components.append({'kind': 'text', 'text': '\n'.join(type_lines)})

        # --- 沉睡方向 ---
        if dormant:
            components.append({'kind': 'divider'})
            components.append({'kind': 'text', 'text': f'💤 沉睡方向(提取超过3天未执行)', 'heading': 4})
            dormant_items = []
            for a in dormant:
                days_ago = int((now_ts - a['created_at']) / 86400)
                title = a.get('title', '')[:40]
                atype_badge = _type_badge(a.get('action_type', ''))
                dormant_items.append({
                    'title': f'{atype_badge} {title}',
                    'description': f'⏰ 提取于 {days_ago} 天前 · 状态:待审',
                    'badge': f'{days_ago}d',
                    'badgeStyle': 'danger',
                    'actions': [{
                        'label': '去执行',
                        'style': 'primary',
                        'action': {
                            'method': 'POST',
                            'path': '/execute',
                            'params': {'action_id': a['id']},
                            'prompt': f'执行沉睡方向:{title}',
                            'aapp_id': 'sproutforge',
                        }
                    }]
                })
            components.append({'kind': 'list', 'items': dormant_items})

        # --- 总结洞察 ---
        components.append({'kind': 'divider'})
        if total_actions == 0:
            insight = '还没有任何方向被提取。从一篇笔记开始吧!'
        elif funnel_rate >= 80:
            insight = f'🔥 转化率 {funnel_rate}%--执行力很强!'
        elif funnel_rate >= 50:
            insight = f'💪 转化率 {funnel_rate}%--还不错,继续推进剩余方向。'
        elif funnel_rate >= 20:
            insight = f'🤔 转化率 {funnel_rate}%--有灵感但执行力跟不上,先完成最重要的。'
        else:
            insight = f'❄️ 转化率仅 {funnel_rate}%--大量灵感在沉睡。挑一个最重要的开始吧!'
        components.append({'kind': 'text', 'text': insight})

        # --- 底部导航 ---
        components.append({'kind': 'divider'})
        components.append({
            'kind': 'row',
            'items': [
                {'kind': 'button', 'label': '🏠 主页', 'style': 'default',
                 'action': {'method': 'GET', 'path': '/', 'prompt': '回到主页', 'params': {}}},
                {'kind': 'button', 'label': '📊 看板', 'style': 'default',
                 'action': {'method': 'GET', 'path': '/dashboard_ui', 'prompt': '查看看板', 'params': {}}},
            ],
            'colCount': 2,
        })

        return {'components': components}
    finally:
        db.close()


@router.route('POST', '/reclassify')
def _reclassify(params):
    action_id = (params.get('action_id') or '').strip()
    new_type = (params.get('action_type') or '').strip()
    if not action_id:
        return {'error': 'missing_action_id'}
    if new_type and new_type not in VALID_ACTION_TYPES:
        return {'error': 'invalid_action_type', 'valid_types': VALID_ACTION_TYPES}

    db = _open_db()
    try:
        rows = db.query('SELECT * FROM sprout_actions WHERE id = :aid', {'aid': action_id})
        if not rows:
            return {'error': 'not_found', 'message': f'action {action_id} not found'}
        action = rows[0]

        if not new_type:
            # AI reclassify (with KB context if available)
            kb_notes = _search_kb_context(action.get('title', ''), [action['description']])
            classifications = _classify_directions([action['description']], action.get('title', ''), kb_notes)
            if classifications:
                new_type = classifications[0].get('action_type', 'exec')
                new_subtype = classifications[0].get('action_subtype', '')
                new_priority = classifications[0].get('priority', 'medium')
                new_kb_rel = classifications[0].get('kb_relation', 'new')
                new_kb_nid = classifications[0].get('kb_note_id', '')
                new_kb_ntitle = classifications[0].get('kb_note_title', '')
            else:
                return {'error': 'classify_failed'}
        else:
            new_subtype = params.get('action_subtype', '')
            new_priority = params.get('priority', action.get('priority'))
            new_kb_rel = action.get('kb_relation', 'new')
            new_kb_nid = action.get('kb_note_id', '')
            new_kb_ntitle = action.get('kb_note_title', '')

        now = _now()
        # Get note title from pipeline meta for proper context (not direction title)
        p_rows = db.query('SELECT meta FROM pipelines WHERE source_id = :sid OR note_id = :nid ORDER BY created_at DESC LIMIT 1', {'sid': action.get('source_id', ''), 'nid': action.get('note_id', '')})
        context = action.get('title', '')
        if p_rows:
            p_meta = json.loads(p_rows[0].get('meta', '{}') or '{}')
            context = p_meta.get('note_title', '') or context
        exec_prompt = _build_exec_prompt(new_type, new_subtype, action['title'], action['description'], context)
        # C4: Re-inject KB context on reclassify
        if new_kb_rel in ('update', 'deepen') and new_kb_nid:
            exec_prompt += f'\n\n📚 知识库关联:本方向与已有笔记「{new_kb_ntitle}」相关({new_kb_rel}),执行时请先读取该笔记(noteId: {new_kb_nid}),在其基础上{"更新" if new_kb_rel == "update" else "深化扩展"},而非从零开始。'
        db.exec(
            'UPDATE sprout_actions SET action_type = ?, action_subtype = ?, priority = ?, exec_prompt = ?, kb_relation = ?, kb_note_id = ?, kb_note_title = ?, updated_at = ? WHERE id = ?',
            [new_type, new_subtype, new_priority, exec_prompt, new_kb_rel, new_kb_nid, new_kb_ntitle, now, action_id]
        )
        LOGGER.info('reclassify', f'action {action_id} reclassified to {new_type}', {})
        return {'action_id': action_id, 'action_type': new_type, 'action_subtype': new_subtype, 'priority': new_priority, 'kb_relation': new_kb_rel, 'kb_note_id': new_kb_nid, 'kb_note_title': new_kb_ntitle}
    finally:
        db.close()


@router.route('POST', '/confirm')
def _confirm(params):
    source_id = (params.get('source_id') or '').strip()
    note_id = (params.get('note_id') or '').strip()
    action_ids = params.get('action_ids', [])
    # Accept string-encoded JSON or comma-separated
    if isinstance(action_ids, str):
        try:
            action_ids = json.loads(action_ids)
        except Exception:
            action_ids = [a.strip() for a in action_ids.split(',') if a.strip()]
    if not action_ids:
        return {'error': 'missing_action_ids'}

    db = _open_db()
    try:
        if not source_id and not note_id:
            return {'error': 'missing_source', 'message': '需要提供 source_id 或 note_id'}
        now = _now()
        bind = {'status_new': 'queued', 'now': now, 'status_old': 'pending'}
        bind.update({f'id{i}': aid for i, aid in enumerate(action_ids)})
        placeholders = ','.join([f':id{i}' for i in range(len(action_ids))])
        db.exec(f'UPDATE sprout_actions SET status = :status_new, updated_at = :now WHERE id IN ({placeholders}) AND status = :status_old', bind)

        key = source_id or note_id
        _update_pipeline_progress(db, key)
        # Find pipeline
        p = db.query('SELECT id FROM pipelines WHERE source_id = :sid OR note_id = :nid ORDER BY created_at DESC LIMIT 1', {'sid': source_id, 'nid': note_id})
        if p:
            db.exec('UPDATE pipelines SET status = ?, updated_at = ? WHERE id = ?', ['queued', now, p[0]['id']])

        LOGGER.info('confirm', f'confirmed {len(action_ids)} actions', {})
        return {'confirmed': len(action_ids), 'source_id': source_id or note_id}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Merge suggest: AI 分析相似方向,合并/去重
# ---------------------------------------------------------------------------

_MERGE_SYSTEM = 'You are an editorial assistant. Analyze the given action directions and identify groups of highly similar or overlapping ones that should be merged. Output ONLY a JSON array of merge groups. Each group: {"keep": "<id to keep>", "merge": ["<ids to merge into keep>"], "merged_title": "<new title>", "reason": "<why merge>"}. If no similar groups exist, output []. Write in the same language as the content.'


@router.route('POST', '/merge-suggest')
def _merge_suggest(params):
    source_id = (params.get('source_id') or '').strip()
    note_id = (params.get('note_id') or '').strip()
    auto_apply = (params.get('auto_apply') or '').strip().lower() in ('true', '1', 'yes')

    if not source_id and not note_id:
        return {'error': 'missing_source', 'message': '需要提供 source_id 或 note_id'}

    db = _open_db()
    try:
        actions = db.query(
            "SELECT * FROM sprout_actions WHERE (source_id = :sid OR note_id = :nid) AND status IN ('pending', 'queued') ORDER BY direction_index",
            {'sid': source_id, 'nid': note_id}
        )
        if not actions:
            return {'error': 'no_actions', 'message': '没有 pending/queued 的方向可分析'}
        if len(actions) < 2:
            return {'source_id': source_id or note_id, 'merge_groups': [], 'message': '方向不足 2 个,无需合并'}

        items = [{'id': a['id'], 'title': a['title'], 'description': (a.get('description') or '')[:200], 'type': a['action_type']} for a in actions]
        items_json = json.dumps(items, ensure_ascii=False)

        prompt = f'分析以下 {len(actions)} 个方向,找出高度相似/可合并的组:\n\n{items_json}\n\n只返回 JSON 数组,不要其他文字。'
        try:
            result = run_prompt(prompt=prompt, system_prompt=_MERGE_SYSTEM, timeout_ms=60000)
            text = result if isinstance(result, str) else str(result)
            text = text.strip()
            if text.startswith('```'):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            array_match = re.search(r'\[.*\]', text, re.DOTALL)
            if array_match:
                text = array_match.group()
            groups = json.loads(text)
        except Exception as e:
            LOGGER.warn('merge_suggest', 'AI analysis failed', {'error': str(e)})
            return {'error': 'ai_failed', 'message': f'AI 分析失败: {e}'}

        if not isinstance(groups, list):
            groups = []

        applied = []
        if auto_apply and groups:
            now = _now()
            for g in groups:
                keep_id = g.get('keep', '')
                merge_ids = g.get('merge', [])
                merged_title = g.get('merged_title', '')
                if not keep_id or not merge_ids:
                    continue
                if merged_title:
                    db.exec('UPDATE sprout_actions SET title = ?, updated_at = ? WHERE id = ?', [merged_title, now, keep_id])
                for mid in merge_ids:
                    db.exec('UPDATE sprout_actions SET status = ?, reason = ?, updated_at = ? WHERE id = ?', ['skipped', f'已合并到 {keep_id}', now, mid])
                applied.append({'keep': keep_id, 'merged': merge_ids, 'merged_title': merged_title})
            LOGGER.info('merge_suggest', f'auto-applied {len(applied)} merges', {'source_id': source_id})

        return {
            'source_id': source_id or note_id,
            'total_actions': len(actions),
            'merge_groups': groups,
            'auto_applied': applied if auto_apply else [],
            'message': f'识别 {len(groups)} 个合并组' + (f',已自动合并 {len(applied)} 组' if auto_apply and applied else '')
        }
    finally:
        db.close()


@router.route('POST', '/execute')
def _execute(params):
    action_id = (params.get('action_id') or '').strip()
    if not action_id:
        return {'error': 'missing_action_id'}

    db = _open_db()
    try:
        rows = db.query('SELECT * FROM sprout_actions WHERE id = :aid', {'aid': action_id})
        if not rows:
            return {'error': 'not_found'}
        action = rows[0]

        now = _now()
        db.exec('UPDATE sprout_actions SET status = ?, updated_at = ? WHERE id = ?', ['running', now, action_id])
        _update_pipeline_progress(db, action.get('source_id') or action.get('note_id'))

        LOGGER.info('execute.start', f'executing action {action_id}', {'type': action['action_type']})
        return {
            'action_id': action_id,
            'title': action['title'],
            'action_type': action['action_type'],
            'exec_prompt': action.get('exec_prompt', ''),
            'message': f'开始执行「{action["title"]}」,Agent 将根据 exec_prompt 调用对应 skill。完成后请调用 /complete 回传成果。',
        }
    finally:
        db.close()


@router.route('POST', '/batch-execute')
def _batch_execute(params):
    source_id = (params.get('source_id') or '').strip()
    if not source_id:
        return {'error': 'missing_source_id'}

    db = _open_db()
    try:
        actions = db.query("SELECT * FROM sprout_actions WHERE (source_id = :sid OR note_id = :nid) AND status IN ('pending', 'queued') ORDER BY direction_index", {'sid': source_id, 'nid': source_id})
        if not actions:
            return {'error': 'no_pending', 'message': '没有待执行的方向'}

        now = _now()
        plan = []
        for a in actions:
            db.exec('UPDATE sprout_actions SET status = ?, updated_at = ? WHERE id = ?', ['queued', now, a['id']])
            plan.append({
                'action_id': a['id'],
                'title': a['title'],
                'action_type': a['action_type'],
                'priority': a.get('priority', 'medium'),
                'exec_prompt': a.get('exec_prompt', ''),
            })

        _update_pipeline_progress(db, source_id)
        LOGGER.info('batch_execute', f'queued {len(plan)} actions for execution', {'source_id': source_id})
        return {
            'source_id': source_id,
            'plan_count': len(plan),
            'plan': plan,
            'first_action': plan[0] if plan else None,
            'message': f'已准备 {len(plan)} 个方向的执行计划。正在执行第一个: 「{plan[0]["title"] if plan else ""}」。Agent 请执行 first_action 的 exec_prompt,完成后调用 /complete。',
        }
    finally:
        db.close()


def _extract_note_id(ref):
    """从 result_ref 字符串中提取 note_id。

    支持格式:
      - note://xxx
      - :remio-inlink[title]{#xxx}
      - 纯 note_id (20+ 字符的字母数字串)
      - 文件路径(不提取 note_id,返回 None)
    """
    if not ref:
        return ''
    # note://xxx
    m = re.search(r'note://([a-z0-9]+)', ref)
    if m:
        return m.group(1)
    # :remio-inlink[...]{#xxx}
    m = re.search(r'\{#([a-z0-9]+)\}', ref)
    if m:
        return m.group(1)
    # 纯 ID(16+ 字符的字母数字串,适配 remio noteId 格式)
    m = re.match(r'^([a-z0-9]{16,})$', ref.strip())
    if m:
        return m.group(1)
    return ''


def _add_to_sprout_collection(note_id, collection_name='SproutForge 产出'):
    """把笔记归入 SproutForge collection,出错不抛异常。"""
    if not note_id:
        return False
    try:
        syscall('add_note_to_collection', {'noteId': note_id, 'title': collection_name})
        return True
    except Exception as e:
        LOGGER.error('add_to_collection', f'failed to add note {note_id} to collection', {'error': str(e)})
        return False


@router.route('POST', '/complete')
def _complete(params):
    action_id = (params.get('action_id') or '').strip()
    result_ref = (params.get('result_ref') or '').strip()
    result_summary = (params.get('result_summary') or '').strip()

    if not action_id:
        return {'error': 'missing_action_id'}

    db = _open_db()
    try:
        rows = db.query('SELECT * FROM sprout_actions WHERE id = :aid', {'aid': action_id})
        if not rows:
            return {'error': 'not_found'}
        action = rows[0]

        now = _now()
        db.exec(
            'UPDATE sprout_actions SET status = ?, result_ref = ?, result_summary = ?, updated_at = ? WHERE id = ?',
            ['completed', result_ref, result_summary, now, action_id]
        )
        _update_pipeline_progress(db, action.get('source_id') or action.get('note_id'))

        LOGGER.info('complete', f'action {action_id} completed', {'result_ref': result_ref[:80]})

        # ✅ 立即把成果笔记归入 collection,不要等 /link-results
        result_note_id = _extract_note_id(result_ref)
        if result_note_id:
            _add_to_sprout_collection(result_note_id)
            LOGGER.info('complete.collection', f'added result note {result_note_id} to SproutForge collection')

        # --- 链式驱动 + 进度推送 ---
        source_id = action.get('source_id') or action.get('note_id')

        # 统计完成进度
        all_actions = db.query(
            "SELECT id, title, action_type, exec_prompt, status, direction_index FROM sprout_actions WHERE (source_id = :sid OR note_id = :nid) ORDER BY direction_index",
            {'sid': source_id, 'nid': source_id}
        )
        total = len(all_actions)
        done_count = sum(1 for a in all_actions if a['status'] in ('completed', 'skipped'))

        # 查找下一个待执行方向（queued 优先，其次 pending 自动提升）
        next_queued = None
        for a in all_actions:
            if a['status'] in ('queued', 'pending'):
                next_queued = a
                break

        # 标记下一个 queued 方向为 running（不依赖 send_chat_message）
        if next_queued:
            now2 = _now()
            db.exec('UPDATE sprout_actions SET status = ?, updated_at = ? WHERE id = ?', ['running', now2, next_queued['id']])

        # 推送进度（send_chat_message 失败不影响状态流转）
        try:
            from remio_sdk import send_chat_message
            if next_queued is None:
                send_chat_message(f'🎉 [{done_count}/{total}] 全部 {total} 个方向执行完成，正在回链...')
            else:
                send_chat_message(f'✅ [{done_count}/{total}] 「{action["title"]}」已完成，下一个: 「{next_queued["title"]}」')
        except Exception:
            pass

        result = {
            'action_id': action_id,
            'status': 'completed',
            'result_ref': result_ref,
            'result_note_added_to_collection': bool(result_note_id),
            'progress': f'{done_count}/{total}',
        }

        if next_queued:
            result['next_action'] = {
                'action_id': next_queued['id'],
                'title': next_queued['title'],
                'action_type': next_queued['action_type'],
                'exec_prompt': next_queued.get('exec_prompt', ''),
            }
            # pipeline_completed 基于实际完成数，而非 next_queued 是否存在
            result['pipeline_completed'] = (done_count >= total)
        else:
            result['next_action'] = None
            # 无下一个待执行方向，但仍按实际完成数判断
            result['pipeline_completed'] = (done_count >= total)

        return result
    finally:
        db.close()


@router.route('POST', '/skip')
def _skip(params):
    action_id = (params.get('action_id') or '').strip()
    reason = (params.get('reason') or '').strip()
    if not action_id:
        return {'error': 'missing_action_id'}

    db = _open_db()
    try:
        rows = db.query('SELECT * FROM sprout_actions WHERE id = :aid', {'aid': action_id})
        if not rows:
            return {'error': 'not_found'}
        action = rows[0]

        now = _now()
        result_summary = f'[跳过] {reason}' if reason else '[跳过]'
        db.exec('UPDATE sprout_actions SET status = ?, result_summary = ?, updated_at = ? WHERE id = ?', ['skipped', result_summary, now, action_id])
        _update_pipeline_progress(db, action.get('source_id') or action.get('note_id'))

        LOGGER.info('skip', f'action {action_id} skipped', {'reason': reason})
        return {'action_id': action_id, 'status': 'skipped'}
    finally:
        db.close()


@router.route('POST', '/reset-stuck')
def _reset_stuck(params):
    """重置超时 running 的方向为 queued，支持中断恢复。"""
    source_id = (params.get('source_id') or '').strip()
    timeout_minutes = int(params.get('timeout_minutes') or 10)
    timeout_seconds = timeout_minutes * 60

    db = _open_db()
    try:
        now = _now()
        cutoff = now - timeout_seconds

        if source_id:
            stuck = db.query(
                "SELECT * FROM sprout_actions WHERE (source_id = :sid OR note_id = :nid) AND status = 'running' AND updated_at < :cutoff",
                {'sid': source_id, 'nid': source_id, 'cutoff': cutoff}
            )
        else:
            stuck = db.query(
                "SELECT * FROM sprout_actions WHERE status = 'running' AND updated_at < :cutoff",
                {'cutoff': cutoff}
            )

        if not stuck:
            return {'reset_count': 0, 'message': '没有检测到卡住的方向'}

        now_val = now
        for a in stuck:
            db.exec('UPDATE sprout_actions SET status = ?, updated_at = ? WHERE id = ?', ['queued', now_val, a['id']])
            if source_id:
                _update_pipeline_progress(db, source_id)

        # 如果没指定 source_id，逐个 pipeline 更新进度
        if not source_id:
            reset_sources = set()
            for a in stuck:
                sid = a.get('source_id') or a.get('note_id')
                if sid:
                    reset_sources.add(sid)
            for sid in reset_sources:
                _update_pipeline_progress(db, sid)

        LOGGER.info('reset_stuck', f'reset {len(stuck)} stuck actions to queued', {'timeout_minutes': timeout_minutes})
        return {
            'reset_count': len(stuck),
            'reset_actions': [{'action_id': a['id'], 'title': a['title']} for a in stuck],
            'message': f'已重置 {len(stuck)} 个卡住的方向为 queued，可重新执行',
        }
    finally:
        db.close()


@router.route('GET', '/plan/:source_id')
def _get_plan(params):
    """Agent reads this to get the execution plan."""
    source_id = params.get('source_id', '')
    db = _open_db()
    try:
        actions = db.query("SELECT * FROM sprout_actions WHERE (source_id = :sid OR note_id = :nid) AND status IN ('running', 'queued') ORDER BY direction_index", {'sid': source_id, 'nid': source_id})
        if not actions:
            return {'error': 'no_plan', 'message': '没有待执行的计划'}

        return {
            'source_id': source_id,
            'pending_count': len(actions),
            'items': [{
                'action_id': a['id'],
                'title': a['title'],
                'action_type': a['action_type'],
                'priority': a['priority'],
                'exec_prompt': a.get('exec_prompt', ''),
            } for a in actions],
        }
    finally:
        db.close()


@router.route('POST', '/link-results')
def _link_results(params):
    source_id = (params.get('source_id') or '').strip()
    note_id = (params.get('note_id') or '').strip()
    if not source_id and not note_id:
        return {'error': 'missing_source'}

    db = _open_db()
    try:
        # Resolve note_id: try source_id column first, then note_id column
        if not note_id:
            rows = db.query('SELECT note_id FROM sprout_actions WHERE (source_id = :sid OR note_id = :nid) AND note_id != "" LIMIT 1', {'sid': source_id, 'nid': source_id})
            note_id = rows[0]['note_id'] if rows else ''

        actions = db.query('SELECT * FROM sprout_actions WHERE source_id = :sid OR note_id = :nid ORDER BY direction_index', {'sid': source_id, 'nid': source_id or note_id})
        if not actions:
            return {'error': 'no_actions'}

        pipeline = db.query('SELECT * FROM pipelines WHERE source_id = :sid OR note_id = :nid ORDER BY created_at DESC LIMIT 1', {'sid': source_id, 'nid': note_id})
        p = pipeline[0] if pipeline else {}
        meta = json.loads(p.get('meta', '{}') or '{}')
        note_title = meta.get('note_title', '')
        source_meta = meta.get('source', {})

        # Build summary note
        completed = [a for a in actions if a.get('status') == 'completed']
        lines = [f'# 📋 SproutForge 汇总:{note_title or source_id}\n']
        if source_meta and source_meta.get('url'):
            lines.append(f'📎 素材来源:{source_meta.get("platform", "")} - {source_meta["url"]}\n')
        lines.append(f'共 {len(actions)} 个方向,已完成 {len(completed)} 个。\n')

        lines.append('## ⚡ 执行成果\n')
        for a in actions:
            icon = ACTION_META.get(a['action_type'], {}).get('icon', '❓')
            status_icon = '✅' if a.get('status') == 'completed' else ('⏭️' if a.get('status') == 'skipped' else '⬜')
            lines.append(f'### {status_icon} {icon} {a["title"]}')
            lines.append(f'**类型**:{_type_badge(a["action_type"])} | **状态**:{_status_badge(a["status"])[0]}\n')
            lines.append(f'{a["description"]}\n')
            if a.get('result_ref'):
                lines.append(f'**成果**:{a["result_ref"]}\n')
            if a.get('result_summary'):
                lines.append(f'{a["result_summary"]}\n')
            lines.append('')

        summary_body = '\n'.join(lines)

        # Create summary note
        summary_title = f'📋 SproutForge 汇总:{note_title or source_id[:12]}'
        try:
            cn = syscall('create_note', {'title': summary_title, 'content': summary_body})
            cn_data = cn.get('data', cn) if isinstance(cn, dict) else {}
            summary_note_id = cn_data.get('noteId', '')
        except Exception as e:
            LOGGER.error('link.create_note', 'failed to create summary note', {'error': str(e)})
            summary_note_id = ''

        # Update original note: append ⚡ 执行成果 section (idempotent - skip if already exists)
        appended = False
        if note_id:
            try:
                _, orig_content = _read_note(note_id)
                if '## ⚡ 执行成果' in orig_content:
                    LOGGER.info('link.idempotent', 'original note already has results section, skipping append', {'note_id': note_id})
                else:
                    append_section = '\n\n---\n## ⚡ 执行成果\n\n'
                    append_section += f'> 由 SproutForge 自动生成 | 汇总笔记:'
                    if summary_note_id:
                        append_section += f':remio-inlink[{summary_title}]{{#{summary_note_id}}}\n\n'
                    else:
                        append_section += f'{summary_title}\n\n'

                    for a in completed:
                        icon = ACTION_META.get(a['action_type'], {}).get('icon', '❓')
                        append_section += f'- {icon} **{a["title"]}** - {a.get("result_summary", "已完成")}'
                        if a.get('result_ref'):
                            ref = a['result_ref']
                            if ref.startswith('note://'):
                                append_section += f' → {ref}'
                            else:
                                append_section += f' → {ref[:60]}'
                        append_section += '\n'

                    syscall('update_note', {'noteId': note_id, 'append': append_section})
                    appended = True
            except Exception as e:
                LOGGER.error('link.update_note', 'failed to update original note', {'error': str(e)})

        # Add all notes to collection: original note + summary note + each action's result note
        collection_name = 'SproutForge 产出'
        notes_to_add = [note_id, summary_note_id]
        # 也把每个方向产出的成果笔记归入
        for a in actions:
            if a.get('status') == 'completed' and a.get('result_ref'):
                rid = _extract_note_id(a['result_ref'])
                if rid and rid not in notes_to_add:
                    notes_to_add.append(rid)
        added_count = 0
        for nid in notes_to_add:
            if _add_to_sprout_collection(nid, collection_name):
                added_count += 1
        LOGGER.info('link_results.collection', f'added {added_count}/{len(notes_to_add)} notes to collection', {'note_ids': notes_to_add})

        # Update pipeline
        if p:
            now = _now()
            db.exec('UPDATE pipelines SET summary_note_id = ?, status = ?, updated_at = ? WHERE id = ?', [summary_note_id, 'completed', now, p['id']])

        LOGGER.info('link_results', 'results linked', {'note_id': note_id, 'summary_id': summary_note_id, 'appended': appended})

        # 多目的地保存:Obsidian + Get笔记(汇总笔记分发到内容库)
        vault_status = ''
        try:
            sf_platform = source_meta.get('platform', '') if source_meta else ''
            if not sf_platform or sf_platform not in ('weibo', 'wechat', 'bilibili', 'youtube'):
                sf_platform = detect_platform(summary_body, note_title)
            vault_result = save_sprout_to_vault(
                title=note_title or source_id,
                content=summary_body,
                platform=sf_platform,
                source_url=source_meta.get('url', '') if source_meta else '',
                source_note_id=summary_note_id,
            )
            parts = []
            if vault_result.get('obsidian_path'):
                parts.append('Obsidian')
            if vault_result.get('getnote_added'):
                parts.append('Get笔记')
            vault_status = ' + '.join(parts) if parts else '跳过'

            # 标记待 Agent 处理飞书写入
            feishu_meta = vault_result.get('feishu_meta', {})
            feishu_meta['remio_note_id'] = summary_note_id
            feishu_meta['obsidian_path'] = vault_result.get('obsidian_path', '')
            state = get_state()
            pending_feishu = state.get('pending_feishu', [])
            pending_feishu.append(feishu_meta)
            pending_feishu = pending_feishu[-50:]
            state['pending_feishu'] = pending_feishu
            set_state(state)

            LOGGER.info('link_results.multi_save', f'platform={sf_platform} vault={vault_status}', {})
        except Exception as e:
            vault_status = f'失败: {e}'
            LOGGER.error('link_results.multi_save', f'failed: {e}', {})

        # --- 方向 C:执行成果反哺知识库 ---
        # 检测知识库中是否有与本次成果相关的旧笔记
        related_notes = []
        try:
            # 用完成方向的标题作为检索词
            search_terms = [a['title'][:30] for a in completed[:3] if a.get('title')]
            if search_terms:
                sr = syscall('search_notes', {'query': ' '.join(search_terms), 'limit': 5})
                sr_data = sr.get('data', sr) if isinstance(sr, dict) else {}
                results = sr_data.get('results', sr_data) if isinstance(sr_data, dict) else sr_data
                if isinstance(results, list):
                    for r in results:
                        rid = r.get('id', r.get('noteId', ''))
                        rtitle = r.get('title', '')
                        # 排除当前笔记自身和汇总笔记
                        if rid and rid != note_id and rid != summary_note_id and rtitle:
                            related_notes.append({'id': rid, 'title': rtitle})
        except Exception as e:
            LOGGER.error('link.rag_feedback', 'failed to search related notes', {'error': str(e)})

        # 如果找到相关旧笔记,追加到汇总笔记
        if related_notes:
            try:
                feedback_section = '\n\n---\n## 🔄 相关旧笔记\n\n'
                feedback_section += '以下知识库笔记可能与本次成果相关,建议检查是否需要更新:\n\n'
                for rn in related_notes[:5]:
                    feedback_section += f'- :remio-inlink[{rn["title"]}]{{#{rn["id"]}}}\n'
                if summary_note_id:
                    syscall('update_note', {'noteId': summary_note_id, 'append': feedback_section})
                LOGGER.info('link.rag_feedback', 'found related notes', {'count': len(related_notes)})
            except Exception as e:
                LOGGER.error('link.rag_feedback', 'failed to append related notes', {'error': str(e)})

        return {
            'source_id': source_id,
            'note_id': note_id,
            'summary_note_id': summary_note_id,
            'summary_title': summary_title,
            'original_note_appended': appended,
            'completed_count': len(completed),
            'total_count': len(actions),
            'related_notes_found': len(related_notes),
            'notes_added_to_collection': added_count,
            'vault_status': vault_status,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Auto-schedule: 将 queued 方向排期到日历
# ---------------------------------------------------------------------------

@router.route('POST', '/auto-schedule')
def _auto_schedule(params):
    source_id = (params.get('source_id') or '').strip()
    note_id = (params.get('note_id') or '').strip()
    days_ahead = params.get('days_ahead', 7)
    try:
        days_ahead = int(days_ahead)
    except (ValueError, TypeError):
        days_ahead = 7
    duration_min = params.get('duration_min', 90)
    try:
        duration_min = int(duration_min)
    except (ValueError, TypeError):
        duration_min = 90

    if not source_id and not note_id:
        return {'error': 'missing_source', 'message': '需要提供 source_id 或 note_id'}

    db = _open_db()
    try:
        actions = db.query(
            "SELECT * FROM sprout_actions WHERE (source_id = :sid OR note_id = :nid) AND status = 'queued' ORDER BY direction_index",
            {'sid': source_id, 'nid': note_id}
        )
        if not actions:
            return {'error': 'no_queued', 'message': '没有 queued 状态的方向可排期'}

        # Find an available calendar
        try:
            cal_resp = syscall('list_calendars', {})
            cal_data = cal_resp.get('data', cal_resp) if isinstance(cal_resp, dict) else {}
            calendars = cal_data.get('calendars', [])
        except Exception as e:
            return {'error': 'calendar_unavailable', 'message': f'无法获取日历: {e}'}

        if not calendars:
            return {'error': 'no_calendar', 'message': '没有可用日历,请先连接日历账户'}

        calendar_id = calendars[0]['id']
        LOGGER.info('auto_schedule', f'using calendar {calendar_id}', {})

        # Fetch existing events for the scheduling window
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = today.strftime('%Y-%m-%d')
        window_end = (today + timedelta(days=days_ahead + 1)).strftime('%Y-%m-%d')

        try:
            search_params = {'time_filter': {'start': window_start, 'end': window_end}, 'limit': 100}
            if isinstance(calendar_id, int) and calendar_id > 0:
                search_params['calendarIds'] = [calendar_id]
            ev_resp = syscall('search_events', search_params)
            ev_data = ev_resp.get('data', ev_resp) if isinstance(ev_resp, dict) else {}
            existing_events = ev_data.get('events', [])
        except Exception:
            existing_events = []

        # Build busy time map: {date_str: [(start_minutes, end_minutes), ...]}
        busy_map = {}
        for ev in existing_events:
            try:
                st = datetime.fromisoformat(ev['startTime'])
                et = datetime.fromisoformat(ev['endTime'])
                date_key = st.strftime('%Y-%m-%d')
                busy_map.setdefault(date_key, []).append((st.hour * 60 + st.minute, et.hour * 60 + et.minute))
            except Exception:
                continue

        # Working hours in minutes: 9:00-12:00, 14:00-18:00
        WORK_SLOTS = [(9 * 60, 12 * 60), (14 * 60, 18 * 60)]
        PRIORITY_DAY_OFFSET = {'high': (0, 2), 'medium': (1, 4), 'low': (3, 7)}

        def find_slot(priority):
            offset_min, offset_max = PRIORITY_DAY_OFFSET.get(priority, (1, 4))
            max_day = min(offset_max + 1, days_ahead + 1)
            for day_offset in range(offset_min, max_day):
                day = today + timedelta(days=day_offset)
                date_key = day.strftime('%Y-%m-%d')
                day_busy = sorted(busy_map.get(date_key, []))
                for slot_start, slot_end in WORK_SLOTS:
                    cursor = slot_start
                    for busy_start, busy_end in day_busy:
                        if busy_end <= cursor:
                            continue
                        if busy_start >= cursor + duration_min:
                            break  # gap found before this busy block
                        cursor = max(cursor, busy_end)
                    if cursor + duration_min <= slot_end:
                        start_dt = day.replace(hour=cursor // 60, minute=cursor % 60)
                        end_min = cursor + duration_min
                        end_dt = day.replace(hour=end_min // 60, minute=end_min % 60)
                        return date_key, start_dt, end_dt
            return None

        scheduled = []
        skipped = []
        now = _now()

        for a in actions:
            priority = a.get('priority', 'medium')
            slot = find_slot(priority)
            if not slot:
                skipped.append({'action_id': a['id'], 'title': a['title'], 'reason': '无空闲时段'})
                continue

            date_key, start_dt, end_dt = slot
            title = f'🌱 {a["title"]}'
            description = a.get('exec_prompt') or a.get('description') or ''

            try:
                ev_resp = syscall('create_event', {
                    'calendarId': calendar_id,
                    'title': title,
                    'startTime': start_dt.isoformat(),
                    'endTime': end_dt.isoformat(),
                    'description': description[:2000],
                })
                ev_data = ev_resp.get('data', ev_resp) if isinstance(ev_resp, dict) else {}
                event_id = ev_data.get('eventId', '')
            except Exception as e:
                LOGGER.warn('auto_schedule', f'create_event failed for {a["id"]}', {'error': str(e)})
                skipped.append({'action_id': a['id'], 'title': a['title'], 'reason': f'日历创建失败: {e}'})
                continue

            db.exec('UPDATE sprout_actions SET status = ?, updated_at = ? WHERE id = ?', ['running', now, a['id']])
            busy_map.setdefault(date_key, []).append((start_dt.hour * 60 + start_dt.minute, end_dt.hour * 60 + end_dt.minute))
            scheduled.append({
                'action_id': a['id'],
                'title': a['title'],
                'event_id': event_id,
                'date': date_key,
                'start': start_dt.strftime('%H:%M'),
                'end': end_dt.strftime('%H:%M'),
                'duration_min': duration_min,
            })

        _update_pipeline_progress(db, source_id or note_id)

        LOGGER.info('auto_schedule', f'scheduled {len(scheduled)}/{len(actions)}', {'source_id': source_id})
        return {
            'source_id': source_id or note_id,
            'total_queued': len(actions),
            'scheduled_count': len(scheduled),
            'skipped_count': len(skipped),
            'scheduled': scheduled,
            'skipped': skipped,
            'message': f'已排期 {len(scheduled)}/{len(actions)} 个方向到日历'
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 方向 B:沉睡方向扫描(可被 scheduler 定时调用)
# ---------------------------------------------------------------------------

@router.route('GET', '/scan_dormant')
def _scan_dormant(params):
    """扫描沉睡方向:提取超过 N 天仍未启动的方向。

    可被 scheduler aApp 定时调用,实现「自主提醒」。
    返回沉睡方向列表 + 汇总统计,供 Agent 生成提醒消息。
    """
    days = int(params.get('days', 3))
    db = _open_db()
    try:
        now_ts = _now()
        cutoff = now_ts - days * 86400

        # 查找沉睡方向
        dormant = db.query(
            'SELECT a.*, p.meta as p_meta '
            'FROM sprout_actions a '
            'LEFT JOIN pipelines p ON (a.source_id = p.source_id OR a.note_id = p.note_id) '
            'WHERE a.status = "pending" AND a.created_at < :cutoff '
            'ORDER BY a.created_at ASC LIMIT 50',
            {'cutoff': cutoff}
        )

        if not dormant:
            return {
                'status': 'ok',
                'message': f'没有沉睡方向(超过 {days} 天未执行)',
                'dormant_count': 0,
                'dormant': []
            }

        # 按笔记分组
        by_note = {}
        for a in dormant:
            key = a.get('source_id') or a.get('note_id', '')
            meta_str = a.get('p_meta', '{}') or '{}'
            try:
                meta = json.loads(meta_str)
            except Exception:
                meta = {}
            note_title = meta.get('note_title', key[:20])
            if key not in by_note:
                by_note[key] = {'note_id': a.get('note_id', ''), 'note_title': note_title, 'actions': []}
            days_ago = int((now_ts - a['created_at']) / 86400)
            by_note[key]['actions'].append({
                'action_id': a['id'],
                'title': a.get('title', '')[:50],
                'action_type': a.get('action_type', ''),
                'priority': a.get('priority', ''),
                'days_ago': days_ago,
            })

        # 构建提醒消息
        total = len(dormant)
        note_count = len(by_note)
        oldest = max(int((now_ts - a['created_at']) / 86400) for a in dormant)

        reminder = f'💤 SproutForge 提醒:你有 {total} 个方向提取后超过 {days} 天未执行(来自 {note_count} 篇笔记),最早已沉睡 {oldest} 天。'

        # 构建精简列表
        dormant_list = []
        for key, info in by_note.items():
            for act in info['actions']:
                dormant_list.append({
                    'note_title': info['note_title'],
                    'note_id': info['note_id'],
                    'action_id': act['action_id'],
                    'title': act['title'],
                    'action_type': act['action_type'],
                    'priority': act['priority'],
                    'days_ago': act['days_ago'],
                })

        LOGGER.info('scan_dormant', f'found {total} dormant actions across {note_count} notes', {'days': days})

        return {
            'status': 'ok',
            'message': reminder,
            'dormant_count': total,
            'note_count': note_count,
            'oldest_days': oldest,
            'dormant': dormant_list,
            'action': {
                'method': 'GET',
                'path': '/stats_ui',
                'prompt': '查看转化仪表盘',
                'params': {},
                'aapp_id': 'sproutforge',
            },
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Status endpoint for Agent aggregation
# ---------------------------------------------------------------------------
@router.route('GET', '/status')
def _get_status(_params):
    """Return sproutforge runtime status for Agent aggregation."""
    db = _open_db()
    try:
        total_actions = db.query('SELECT COUNT(*) as c FROM sprout_actions')[0]['c']
        pending = db.query("SELECT COUNT(*) as c FROM sprout_actions WHERE status = 'pending'")[0]['c']
        running = db.query("SELECT COUNT(*) as c FROM sprout_actions WHERE status = 'running'")[0]['c']
        completed = db.query("SELECT COUNT(*) as c FROM sprout_actions WHERE status = 'completed'")[0]['c']
        failed = db.query("SELECT COUNT(*) as c FROM sprout_actions WHERE status = 'failed'")[0]['c']

        # Alerts: stuck running (>10 min) or failed items
        alerts = []
        now_ts = _now()
        stuck_cutoff = now_ts - 10 * 60  # 10 分钟
        stuck_rows = db.query(
            "SELECT id, title, updated_at FROM sprout_actions WHERE status = 'running' AND updated_at < :cutoff",
            {'cutoff': stuck_cutoff}
        )
        stuck_count = len(stuck_rows)
        stuck_actions = []
        for row in stuck_rows:
            running_min = int((now_ts - int(row.get('updated_at') or 0)) / 60)
            stuck_actions.append({
                'action_id': row['id'],
                'title': str(row.get('title') or '')[:50],
                'running_for_minutes': running_min,
            })
        if stuck_count > 0:
            alerts.append({
                'level': 'warning',
                'message': f'{stuck_count} directions stuck in running for >10 min, call POST /reset-stuck to recover'
            })
        if failed > 0:
            alerts.append({
                'level': 'error',
                'message': f'{failed} directions failed'
            })

        # Recent 5 actions by updated_at
        recent_rows = db.query(
            'SELECT title, action_type, status, updated_at FROM sprout_actions ORDER BY updated_at DESC LIMIT 5'
        )
        recent = []
        for row in recent_rows:
            ts = int(row.get('updated_at') or 0)
            recent.append({
                'title': str(row.get('title') or '')[:50],
                'type': str(row.get('action_type') or ''),
                'status': str(row.get('status') or ''),
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(ts)) if ts > 0 else '',
            })

        overall = 'error' if failed > 0 else ('warning' if stuck_count > 0 else 'healthy')
        return {
            'aapp_id': 'sproutforge',
            'status': overall,
            'summary': {
                'active_items': total_actions,
                'pending_items': pending,
                'running_items': running,
                'completed_items': completed,
                'failed_items': failed,
            },
            'alerts': alerts,
            'stuck_actions': stuck_actions,
            'recent': recent,
            'last_updated': time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(now_ts)),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def handle(request):
    return router.handle(request)
