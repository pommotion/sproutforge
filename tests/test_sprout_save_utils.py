"""Unit tests for sprout_save_utils.py.

Covers:
- detect_platform (normal, boundary)
- _sanitize_filename (normal, boundary)
- _yaml_escape (normal, boundary)
- _get_getnote_credentials (normal: env/file, boundary: missing, error: invalid json)
- _save_to_obsidian (normal: general/weibo, boundary: collisions, error: write error)
- _getnote_api (normal, boundary: int64 regex, error: http/network failure)
- _save_to_getnote (normal, boundary: missing creds, error: note/batch-add failures)
- save_sprout_to_vault (normal, boundary: truncation, error: partial failure, xfail: null content)
"""

import io
import json
import os
import urllib.error
from unittest.mock import MagicMock, mock_open, patch

import pytest

import sprout_save_utils


# ===========================================================================
# 1. detect_platform
# ===========================================================================
class TestDetectPlatform:
    """Tests for platform detection from note content / title."""

    @pytest.mark.parametrize(
        'content,title,expected',
        [
            # Weibo
            ('platform: weibo', '', 'weibo'),
            ('source: 微博', '', 'weibo'),
            ('https://weibo.com/u/123456', '', 'weibo'),
            ('Check this: http://t.cn/E9abc', '', 'weibo'),
            ('', 'source: 微博 精选', 'weibo'),
            # WeChat
            ('platform: wechat', '', 'wechat'),
            ('source: 微信公众号', '', 'wechat'),
            ('https://mp.weixin.qq.com/s/abcdef123', '', 'wechat'),
            ('', 'mp.weixin.qq.com 文章', 'wechat'),
            # Bilibili
            ('https://www.bilibili.com/video/BV123456', '', 'bilibili'),
            ('short link https://b23.tv/xyz', '', 'bilibili'),
            # YouTube
            ('https://www.youtube.com/watch?v=dQw4w9WgXcQ', '', 'youtube'),
            ('https://youtu.be/dQw4w9WgXcQ', '', 'youtube'),
            # General fallback
            ('Some plain note without any links', 'Daily Log', 'general'),
        ],
    )
    def test_detect_platform_normal(self, content, title, expected):
        assert sprout_save_utils.detect_platform(content, title) == expected

    def test_detect_platform_boundary_empty_and_none(self):
        assert sprout_save_utils.detect_platform('', '') == 'general'
        assert sprout_save_utils.detect_platform(None, None) == 'general'
        assert sprout_save_utils.detect_platform(None, 'source: 微博') == 'weibo'
        assert sprout_save_utils.detect_platform('platform: wechat', None) == 'wechat'

    def test_detect_platform_boundary_case_insensitivity(self):
        assert sprout_save_utils.detect_platform('HTTPS://WEIBO.COM/123') == 'weibo'
        assert sprout_save_utils.detect_platform('HTTPS://MP.WEIXIN.QQ.COM/S/123') == 'wechat'
        assert sprout_save_utils.detect_platform('HTTPS://YOUTU.BE/ABC') == 'youtube'

    def test_detect_platform_boundary_precedence(self):
        # When both weibo and wechat are present, weibo is checked first
        mixed = 'source: 微博 and also mp.weixin.qq.com'
        assert sprout_save_utils.detect_platform(mixed) == 'weibo'


# ===========================================================================
# 2. _sanitize_filename
# ===========================================================================
class TestSanitizeFilename:
    """Tests for filename sanitization."""

    def test_sanitize_filename_normal(self):
        result = sprout_save_utils._sanitize_filename('Sprout Note Title')
        assert result == 'Sprout-Note-Title'

    def test_sanitize_filename_boundary_illegal_characters(self):
        # All of [/\\:*?"<>|] should be replaced with '-'
        raw = 'A/B\\C:D*E?F"G<H>I|J'
        expected = 'A-B-C-D-E-F-G-H-I-J'
        assert sprout_save_utils._sanitize_filename(raw) == expected

    def test_sanitize_filename_boundary_truncation_to_80_chars(self):
        long_title = 'A' * 100
        sanitized = sprout_save_utils._sanitize_filename(long_title)
        assert len(sanitized) == 80
        assert sanitized == 'A' * 80

    def test_sanitize_filename_boundary_empty_and_none(self):
        assert sprout_save_utils._sanitize_filename('') == '未命名'
        assert sprout_save_utils._sanitize_filename(None) == '未命名'


# ===========================================================================
# 3. _yaml_escape
# ===========================================================================
class TestYamlEscape:
    """Tests for YAML frontmatter scalar escaping."""

    def test_yaml_escape_normal(self):
        assert sprout_save_utils._yaml_escape('Hello World') == '"Hello World"'

    def test_yaml_escape_boundary_quotes_and_backslashes(self):
        raw = 'Title with "quotes" and \\backslashes\\'
        expected = '"Title with \\"quotes\\" and \\\\backslashes\\\\"'
        assert sprout_save_utils._yaml_escape(raw) == expected

    def test_yaml_escape_boundary_newlines(self):
        raw = 'First Line\nSecond Line'
        assert sprout_save_utils._yaml_escape(raw) == '"First Line Second Line"'

    def test_yaml_escape_boundary_empty_and_none(self):
        assert sprout_save_utils._yaml_escape('') == '""'
        assert sprout_save_utils._yaml_escape(None) == '""'


# ===========================================================================
# 4. _get_getnote_credentials
# ===========================================================================
class TestGetnoteCredentials:
    """Tests for 3-tier credential resolution."""

    def test_credentials_priority_1_env_vars(self, monkeypatch):
        monkeypatch.setenv('GETNOTE_API_KEY', 'env_api_key_123')
        monkeypatch.setenv('GETNOTE_CLIENT_ID', 'env_client_id_456')
        api_key, client_id = sprout_save_utils._get_getnote_credentials()
        assert api_key == 'env_api_key_123'
        assert client_id == 'env_client_id_456'

    def test_credentials_priority_2_openclaw_json(self, monkeypatch, tmp_path):
        monkeypatch.delenv('GETNOTE_API_KEY', raising=False)
        monkeypatch.delenv('GETNOTE_CLIENT_ID', raising=False)

        config_dir = tmp_path / '.openclaw'
        config_dir.mkdir(parents=True)
        config_file = config_dir / 'openclaw.json'
        config_file.write_text(
            json.dumps({
                'skills': {
                    'entries': {
                        'getnote': {
                            'apiKey': 'file_api_key_789',
                            'env': {'GETNOTE_CLIENT_ID': 'file_client_id_012'},
                        }
                    }
                }
            }),
            encoding='utf-8',
        )

        with patch('os.path.expanduser', return_value=str(config_file)):
            api_key, client_id = sprout_save_utils._get_getnote_credentials()
            assert api_key == 'file_api_key_789'
            assert client_id == 'file_client_id_012'

    def test_credentials_priority_2_alternative_keys(self, monkeypatch, tmp_path):
        monkeypatch.delenv('GETNOTE_API_KEY', raising=False)
        monkeypatch.delenv('GETNOTE_CLIENT_ID', raising=False)

        config_file = tmp_path / 'openclaw.json'
        config_file.write_text(
            json.dumps({
                'skills': {
                    'entries': {
                        'getnote': {
                            'api_key': 'alt_api_key',
                            'client_id': 'alt_client_id',
                        }
                    }
                }
            }),
            encoding='utf-8',
        )

        with patch('os.path.expanduser', return_value=str(config_file)):
            api_key, client_id = sprout_save_utils._get_getnote_credentials()
            assert api_key == 'alt_api_key'
            assert client_id == 'alt_client_id'

    def test_credentials_boundary_missing(self, monkeypatch):
        monkeypatch.delenv('GETNOTE_API_KEY', raising=False)
        monkeypatch.delenv('GETNOTE_CLIENT_ID', raising=False)
        with patch('os.path.isfile', return_value=False):
            api_key, client_id = sprout_save_utils._get_getnote_credentials()
            assert api_key == ''
            assert client_id == ''

    def test_credentials_error_corrupt_json(self, monkeypatch, tmp_path):
        monkeypatch.delenv('GETNOTE_API_KEY', raising=False)
        monkeypatch.delenv('GETNOTE_CLIENT_ID', raising=False)

        bad_file = tmp_path / 'bad_openclaw.json'
        bad_file.write_text('{invalid_json: true,', encoding='utf-8')

        with patch('os.path.expanduser', return_value=str(bad_file)):
            api_key, client_id = sprout_save_utils._get_getnote_credentials()
            assert api_key == ''
            assert client_id == ''


# ===========================================================================
# 5. _save_to_obsidian
# ===========================================================================
class TestSaveToObsidian:
    """Tests for saving markdown notes to Obsidian vault."""

    def test_save_to_obsidian_normal_general(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sprout_save_utils, 'OBSIDIAN_ROOT', str(tmp_path))
        title = 'AI Frontier Notes'
        content = 'Exploring agent architectures and LLM reasoning.'
        source_url = 'https://example.com/article'

        filepath = sprout_save_utils._save_to_obsidian(title, content, platform='general', source_url=source_url)

        assert os.path.exists(filepath)
        assert '09_知识库/05_发芽笔记' in filepath
        assert filepath.endswith('AI-Frontier-Notes__🌱发芽笔记.md')

        with open(filepath, 'r', encoding='utf-8') as f:
            saved_text = f.read()

        assert 'title: "AI Frontier Notes"' in saved_text
        assert 'platform: general' in saved_text
        assert content in saved_text
        assert f'📎 源材料链接: {source_url}' in saved_text
        assert '🌱 发芽工具: sprout-notes / sproutforge' in saved_text

    def test_save_to_obsidian_normal_weibo_date_subdir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sprout_save_utils, 'OBSIDIAN_ROOT', str(tmp_path))
        title = 'Weibo Daily'
        content = 'Weibo summary content.'

        filepath = sprout_save_utils._save_to_obsidian(title, content, platform='weibo')

        assert os.path.exists(filepath)
        assert '09_知识库/04_微博收藏日报' in filepath
        # Weibo uses YYYY/MM subdirectories
        import time
        now = time.localtime()
        expected_part = f'{now.tm_year}/{now.tm_mon:02d}'
        assert expected_part in filepath

    def test_save_to_obsidian_boundary_filename_collision(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sprout_save_utils, 'OBSIDIAN_ROOT', str(tmp_path))
        title = 'Collision Note'
        content = 'Content 1'

        # First save -> Collision-Note__🌱发芽笔记.md
        path1 = sprout_save_utils._save_to_obsidian(title, content, platform='general')
        assert os.path.exists(path1)
        assert not path1.endswith('-1.md')

        # Second save with identical title -> Collision-Note__🌱发芽笔记-1.md
        path2 = sprout_save_utils._save_to_obsidian(title, 'Content 2', platform='general')
        assert os.path.exists(path2)
        assert path2.endswith('__🌱发芽笔记-1.md')

        # Third save -> Collision-Note__🌱发芽笔记-2.md
        path3 = sprout_save_utils._save_to_obsidian(title, 'Content 3', platform='general')
        assert os.path.exists(path3)
        assert path3.endswith('__🌱发芽笔记-2.md')

    def test_save_to_obsidian_boundary_no_source_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sprout_save_utils, 'OBSIDIAN_ROOT', str(tmp_path))
        filepath = sprout_save_utils._save_to_obsidian('No URL Note', 'Body text', platform='general', source_url='')

        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
        assert '📎 源材料链接' not in text

    def test_save_to_obsidian_error_directory_creation_failure(self, monkeypatch):
        with patch('os.makedirs', side_effect=PermissionError('Permission denied')):
            with pytest.raises(PermissionError):
                sprout_save_utils._save_to_obsidian('Test', 'Content', platform='general')


# ===========================================================================
# 6. _getnote_api
# ===========================================================================
class TestGetnoteApi:
    """Tests for GetNote HTTP API calls."""

    def test_getnote_api_normal(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"code": 0, "message": "success", "data": {"note_id": "12345"}}'

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
            res = sprout_save_utils._getnote_api(
                '/open/api/v1/test', {'hello': 'world'}, api_key='k1', client_id='c1'
            )

            assert res['code'] == 0
            assert res['data']['note_id'] == '12345'
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == 'https://openapi.biji.com/open/api/v1/test'
            assert req.headers['Authorization'] == 'k1'
            assert req.headers['X-client-id'] == 'c1'

    def test_getnote_api_boundary_int64_precision_fix(self):
        # Raw response has numeric int64 that would lose precision in JS/some parsers
        raw_json = b'{"code": 0, "data": {"id": 1234567890123456789, "parent_id": 9876543210987654321}}'
        mock_response = MagicMock()
        mock_response.read.return_value = raw_json

        with patch('urllib.request.urlopen', return_value=mock_response):
            res = sprout_save_utils._getnote_api(
                '/open/api/v1/test', {}, api_key='k1', client_id='c1'
            )
            # Both id and parent_id should be stringified
            assert res['data']['id'] == '1234567890123456789'
            assert res['data']['parent_id'] == '9876543210987654321'

    def test_getnote_api_error_http_error(self):
        err = urllib.error.HTTPError(
            url='https://openapi.biji.com/test', code=401, msg='Unauthorized', hdrs={}, fp=io.BytesIO(b'{}')
        )
        with patch('urllib.request.urlopen', side_effect=err):
            with pytest.raises(urllib.error.HTTPError):
                sprout_save_utils._getnote_api('/test', {}, 'k1', 'c1')


# ===========================================================================
# 7. _save_to_getnote
# ===========================================================================
class TestSaveToGetnote:
    """Tests for saving sprout notes to GetNote cloud service."""

    def test_save_to_getnote_normal(self):
        with patch.object(sprout_save_utils, '_get_getnote_credentials', return_value=('key1', 'client1')):
            with patch.object(sprout_save_utils, '_getnote_api') as mock_api:
                # Step 1: save note returns note_id
                # Step 2: batch-add returns success
                mock_api.side_effect = [
                    {'code': 0, 'data': {'note_id': 'note_abc_123'}},
                    {'code': 0, 'data': {'success': True}},
                ]

                note_id, added = sprout_save_utils._save_to_getnote('My Title', 'Note Body', platform='wechat')

                assert note_id == 'note_abc_123'
                assert added is True
                assert mock_api.call_count == 2
                # Verify topic_id corresponds to wechat in PLATFORM_CONFIG
                assert mock_api.call_args_list[1][0][1]['topic_id'] == '0PXQ8q90'

    def test_save_to_getnote_boundary_missing_credentials(self):
        with patch.object(sprout_save_utils, '_get_getnote_credentials', return_value=('', '')):
            note_id, added = sprout_save_utils._save_to_getnote('Title', 'Content')
            assert note_id == ''
            assert added is False

    def test_save_to_getnote_error_note_creation_failed(self):
        with patch.object(sprout_save_utils, '_get_getnote_credentials', return_value=('key1', 'client1')):
            with patch.object(sprout_save_utils, '_getnote_api', return_value={'code': 1001, 'data': {}}):
                note_id, added = sprout_save_utils._save_to_getnote('Title', 'Content')
                assert note_id == ''
                assert added is False

    def test_save_to_getnote_error_batch_add_failed(self):
        with patch.object(sprout_save_utils, '_get_getnote_credentials', return_value=('key1', 'client1')):
            with patch.object(sprout_save_utils, '_getnote_api') as mock_api:
                # Step 1 succeeds, Step 2 raises network/topic error
                mock_api.side_effect = [
                    {'code': 0, 'data': {'note_id': 'note_created'}},
                    Exception('Topic not found'),
                ]
                note_id, added = sprout_save_utils._save_to_getnote('Title', 'Content')
                # Note was created, but not added to topic
                assert note_id == 'note_created'
                assert added is False


# ===========================================================================
# 8. save_sprout_to_vault (Main Entrypoint)
# ===========================================================================
class TestSaveSproutToVault:
    """Tests for the multi-vault distribution coordinator."""

    def test_save_sprout_to_vault_normal_success(self):
        with patch.object(sprout_save_utils, '_save_to_obsidian', return_value='/vault/note.md') as mock_obs:
            with patch.object(sprout_save_utils, '_save_to_getnote', return_value=('gn_123', True)) as mock_gn:
                res = sprout_save_utils.save_sprout_to_vault(
                    title='Test Sprout',
                    content='Insightful content',
                    platform='bilibili',
                    source_url='https://bilibili.com/video/BV1',
                    source_note_id='remio_note_001',
                )

                assert res['obsidian_path'] == '/vault/note.md'
                assert res['getnote_note_id'] == 'gn_123'
                assert res['getnote_added'] is True
                assert res['errors'] == []
                # feishu_meta checks
                fm = res['feishu_meta']
                assert fm['platform'] == 'bilibili'
                assert fm['title'] == 'Test Sprout'
                assert fm['source_note_id'] == 'remio_note_001'
                assert fm['tags'] == ['B站', '视频', '发芽笔记']
                assert fm['base_token'] == 'SuG4bQ84HaCMCAsdo0YcaNAZnzb'

    def test_save_sprout_to_vault_boundary_content_truncation_in_feishu_meta(self):
        long_content = 'X' * 5000
        with patch.object(sprout_save_utils, '_save_to_obsidian', return_value=''):
            with patch.object(sprout_save_utils, '_save_to_getnote', return_value=('', False)):
                res = sprout_save_utils.save_sprout_to_vault(
                    title='Long Content', content=long_content, platform='general'
                )
                assert len(res['feishu_meta']['content']) == 2000

    def test_save_sprout_to_vault_boundary_unknown_platform_fallback(self):
        with patch.object(sprout_save_utils, '_save_to_obsidian', return_value=''):
            with patch.object(sprout_save_utils, '_save_to_getnote', return_value=('', False)):
                res = sprout_save_utils.save_sprout_to_vault(
                    title='Unknown', content='Body', platform='unsupported_platform'
                )
                # Defaults to general config
                assert res['feishu_meta']['tags'] == ['发芽笔记']
                assert res['feishu_meta']['table_id'] == 'tblxGIsRTzp0zIlr'

    def test_save_sprout_to_vault_error_obsidian_failure_partial_success(self):
        with patch.object(sprout_save_utils, '_save_to_obsidian', side_effect=OSError('Disk full')):
            with patch.object(sprout_save_utils, '_save_to_getnote', return_value=('gn_ok', True)):
                res = sprout_save_utils.save_sprout_to_vault(
                    title='Test', content='Body', platform='general'
                )
                assert res['obsidian_path'] == ''
                assert res['getnote_note_id'] == 'gn_ok'
                assert res['getnote_added'] is True
                assert len(res['errors']) == 1
                assert 'Obsidian: Disk full' in res['errors'][0]

    def test_save_sprout_to_vault_error_getnote_failure_partial_success(self):
        with patch.object(sprout_save_utils, '_save_to_obsidian', return_value='/obs/path.md'):
            with patch.object(sprout_save_utils, '_save_to_getnote', side_effect=RuntimeError('Getnote connection timeout')):
                res = sprout_save_utils.save_sprout_to_vault(
                    title='Test', content='Body', platform='general'
                )
                assert res['obsidian_path'] == '/obs/path.md'
                assert res['getnote_note_id'] == ''
                assert res['getnote_added'] is False
                assert len(res['errors']) == 1
                assert 'Get笔记: Getnote connection timeout' in res['errors'][0]

    @pytest.mark.xfail(
        reason="sprout_save_utils.py line 295 does not check for None before slicing content[:2000], causing TypeError: 'NoneType' object is not subscriptable",
        raises=TypeError,
        strict=True,
    )
    def test_save_sprout_to_vault_xfail_none_content_raises_typeerror(self):
        """Source bug demonstration:

        Passing content=None to save_sprout_to_vault causes an unhandled TypeError
        at line 295 ('content': content[:2000]) before any vault operations execute.
        Per instructions, source code is not modified and this test is marked as xfail.
        """
        sprout_save_utils.save_sprout_to_vault(title='Null Content Note', content=None)
