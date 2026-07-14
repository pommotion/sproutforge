"""发芽笔记多目的地保存模块 — 分发到 Obsidian + Get笔记 + 飞书标记.

复用 weibo-save / wechat-article-save 的保存模式，按来源平台路由到对应内容库。
飞书写入由 Agent 后置处理（aApp 内无法调用 lark-cli）。
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

from remio_sdk import create_aapp_logger

_LOG_DIR = os.environ.get('REMIO_AAPP_LOG_DIR', '/tmp/sprout-save-logs')
LOGGER = create_aapp_logger('sprout-save-utils', _LOG_DIR, component='save')

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
OBSIDIAN_ROOT = os.environ.get('SPOUT_OBSIDIAN_ROOT', '/Volumes/ai-notes')
GETNOTE_API_BASE = 'https://openapi.biji.com'

PLATFORM_CONFIG = {
    'weibo': {
        'obsidian_subpath': '09_知识库/04_微博收藏日报',
        'getnote_topic_id': 'nW9meoD0',
        'feishu_base_token': 'T5o8bAfiraUEI5sf1d0cQGAHnVd',
        'feishu_table_id': 'tblgXzbq712PlmwX',
        'tags': ['微博', '发芽笔记'],
    },
    'wechat': {
        'obsidian_subpath': '09_知识库/02_公众号文章',
        'getnote_topic_id': '0PXQ8q90',
        'feishu_base_token': 'Y98wbR6wAa86JgsXYoScqGLXnOf',
        'feishu_table_id': 'tbljrD0L57sMwcP0',
        'tags': ['公众号', '发芽笔记'],
    },
    'bilibili': {
        'obsidian_subpath': '09_知识库/01_BibiGPT视频总结/01_B站',
        'getnote_topic_id': '0PXeeMr0',
        'feishu_base_token': 'SuG4bQ84HaCMCAsdo0YcaNAZnzb',
        'feishu_table_id': 'tblPLsUNYzDmlX0r',
        'tags': ['B站', '视频', '发芽笔记'],
    },
    'youtube': {
        'obsidian_subpath': '09_知识库/01_BibiGPT视频总结/02_YouTube',
        'getnote_topic_id': 'ndK99ZRJ',
        'feishu_base_token': 'SuG4bQ84HaCMCAsdo0YcaNAZnzb',
        'feishu_table_id': 'tblPLsUNYzDmlX0r',
        'tags': ['YouTube', '视频', '发芽笔记'],
    },
    'general': {
        'obsidian_subpath': '09_知识库/05_发芽笔记',
        'getnote_topic_id': 'n3EPP3g0',
        'feishu_base_token': 'Eh2pbrDmZa9cFRsFcfmcAVsKnaf',
        'feishu_table_id': 'tblxGIsRTzp0zIlr',
        'tags': ['发芽笔记'],
    },
}


# ---------------------------------------------------------------------------
# 平台检测
# ---------------------------------------------------------------------------
def detect_platform(note_content, note_title=''):
    """从源笔记内容/标题推断来源平台。"""
    text = (note_content or '') + ' ' + (note_title or '')
    text_l = text.lower()

    # 微博
    if 'platform: weibo' in text or 'source: 微博' in text:
        return 'weibo'
    if 'weibo.com' in text_l or 't.cn' in text_l:
        return 'weibo'

    # 公众号
    if 'platform: wechat' in text or 'source: 微信公众号' in text:
        return 'wechat'
    if 'mp.weixin.qq.com' in text_l:
        return 'wechat'

    # B站
    if 'bilibili.com' in text_l or 'b23.tv' in text_l:
        return 'bilibili'

    # YouTube
    if 'youtube.com' in text_l or 'youtu.be' in text_l:
        return 'youtube'

    return 'general'


# ---------------------------------------------------------------------------
# Get笔记凭证管理
# ---------------------------------------------------------------------------
def _get_getnote_credentials():
    """3 级查找：环境变量 → openclaw.json → 返回空。"""
    # 1. 环境变量
    api_key = os.environ.get('GETNOTE_API_KEY', '')
    client_id = os.environ.get('GETNOTE_CLIENT_ID', '')
    if api_key and client_id:
        return api_key, client_id

    # 2. openclaw.json
    try:
        cfg_path = os.path.expanduser('~/.openclaw/openclaw.json')
        if os.path.isfile(cfg_path):
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            entry = cfg.get('skills', {}).get('entries', {}).get('getnote', {})
            api_key = entry.get('apiKey', '') or entry.get('api_key', '')
            client_id = entry.get('env', {}).get('GETNOTE_CLIENT_ID', '') or entry.get('client_id', '')
            if api_key and client_id:
                return api_key, client_id
    except Exception as e:
        LOGGER.warn('credentials', f'failed to read openclaw.json: {e}', {})

    return '', ''


# ---------------------------------------------------------------------------
# Obsidian 保存
# ---------------------------------------------------------------------------
def _sanitize_filename(name):
    """清理文件名：去非法字符，截断到 80 字符。"""
    safe = re.sub(r'[/\\:*?"<>|]', '-', name or '未命名')
    safe = safe.strip().replace(' ', '-')
    if len(safe) > 80:
        safe = safe[:80]
    return safe


def _yaml_escape(value):
    """Escape a string for safe YAML scalar value."""
    if not value:
        return '""'
    # Quote and escape inner quotes/backslashes
    safe = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    return f'"{safe}"'


def _save_to_obsidian(title, content, platform='general', source_url=''):
    """将发芽笔记写入 Obsidian 对应目录。"""
    config = PLATFORM_CONFIG.get(platform, PLATFORM_CONFIG['general'])
    subdir = os.path.join(OBSIDIAN_ROOT, config['obsidian_subpath'])

    # 微博源用 YYYY/MM 子目录
    if platform == 'weibo':
        now = time.localtime()
        subdir = os.path.join(subdir, str(now.tm_year), f'{now.tm_mon:02d}')

    os.makedirs(subdir, exist_ok=True)

    safe_title = _sanitize_filename(title)
    filename = f'{safe_title}__🌱发芽笔记.md'
    filepath = os.path.join(subdir, filename)

    # 文件名冲突处理
    if os.path.exists(filepath):
        i = 1
        while os.path.exists(os.path.join(subdir, f'{safe_title}__🌱发芽笔记-{i}.md')):
            i += 1
        filepath = os.path.join(subdir, f'{safe_title}__🌱发芽笔记-{i}.md')

    # 构建 frontmatter (YAML-safe)
    escaped_title = _yaml_escape(title)
    frontmatter = f"""---
title: {escaped_title}
source_title: {escaped_title}
platform: {platform}
sprout_date: {time.strftime('%Y-%m-%d')}
---

"""
    # 构建文件内容
    footer = '\n\n---\n\n'
    if source_url:
        footer += f'📎 源材料链接: {source_url}\n'
    footer += f'🌱 发芽工具: sprout-notes / sproutforge\n'

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(frontmatter + content + footer)

    LOGGER.info('obsidian_save', f'saved to {filepath}', {'platform': platform})
    return filepath


# ---------------------------------------------------------------------------
# Get笔记 API
# ---------------------------------------------------------------------------
def _getnote_api(path, payload, api_key, client_id, timeout=15):
    """调用 Get笔记 API。"""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{GETNOTE_API_BASE}{path}',
        data=data,
        headers={
            'Authorization': api_key,
            'X-Client-ID': client_id,
            'Content-Type': 'application/json; charset=utf-8',
        },
        method='POST',
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    raw = resp.read().decode('utf-8')

    # int64 精度修复
    safe = re.sub(r'"(id|note_id|parent_id)"\s*:\s*(\d+)', r'"\1":"\2"', raw)
    return json.loads(safe)


def _save_to_getnote(title, content, platform='general', source_url=''):
    """保存发芽笔记到 Get笔记知识库。"""
    api_key, client_id = _get_getnote_credentials()
    if not api_key or not client_id:
        LOGGER.warn('getnote_save', 'credentials not found, skipping', {})
        return '', False

    config = PLATFORM_CONFIG.get(platform, PLATFORM_CONFIG['general'])

    # 构建笔记内容
    full_content = content
    if source_url:
        full_content += f'\n\n📎 源材料链接: {source_url}'

    # 1. 新建笔记
    result = _getnote_api(
        '/open/api/v1/resource/note/save',
        {
            'title': f'{title} 🌱发芽笔记',
            'content': full_content,
            'note_type': 'plain_text',
            'tags': config['tags'],
            'parent_id': 0,
        },
        api_key,
        client_id,
    )

    note_id = result.get('data', {}).get('note_id', '')
    if not note_id:
        LOGGER.warn('getnote_save', 'note creation failed', {'result': result})
        return '', False

    # 2. 存入知识库
    try:
        _getnote_api(
            '/open/api/v1/resource/knowledge/note/batch-add',
            {
                'topic_id': config['getnote_topic_id'],
                'note_ids': [note_id],
            },
            api_key,
            client_id,
        )
        LOGGER.info('getnote_save', f'saved to topic {config["getnote_topic_id"]}', {'note_id': note_id})
        return note_id, True
    except Exception as e:
        LOGGER.warn('getnote_save', f'note created but topic-add failed: {e}', {'note_id': note_id})
        return note_id, False


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def save_sprout_to_vault(title, content, platform='general', source_url='', source_note_id=''):
    """
    将发芽报告分发到 Obsidian + Get笔记。

    飞书由 Agent 后置处理：返回 feishu_meta 供调用方写入 aApp state。

    Returns:
        {
            'obsidian_path': str,
            'getnote_note_id': str,
            'getnote_added': bool,
            'feishu_meta': dict,   # Agent 后置飞书写入所需信息
            'errors': list[str],
        }
    """
    config = PLATFORM_CONFIG.get(platform, PLATFORM_CONFIG['general'])
    result = {
        'obsidian_path': '',
        'getnote_note_id': '',
        'getnote_added': False,
        'feishu_meta': {
            'platform': platform,
            'base_token': config['feishu_base_token'],
            'table_id': config['feishu_table_id'],
            'source_url': source_url,
            'source_note_id': source_note_id,
            'title': title,
            'content': content[:2000],  # 截断防超限
            'tags': config['tags'],
            'created_at': int(time.time()),
        },
        'errors': [],
    }

    # 1. Obsidian
    try:
        result['obsidian_path'] = _save_to_obsidian(title, content, platform, source_url)
    except Exception as e:
        result['errors'].append(f'Obsidian: {e}')
        LOGGER.error('obsidian_save', f'failed: {e}', {'platform': platform})

    # 2. Get笔记
    try:
        note_id, added = _save_to_getnote(title, content, platform, source_url)
        result['getnote_note_id'] = note_id
        result['getnote_added'] = added
    except Exception as e:
        result['errors'].append(f'Get笔记: {e}')
        LOGGER.error('getnote_save', f'failed: {e}', {'platform': platform})

    return result
