"""图片 OCR、图片下载与记忆写回链路的离线覆盖率测试。"""

import base64
import copy
import hashlib
import json
import sys
import threading
from collections import deque
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_APP = REPO_ROOT
PLUGIN_ROOT = PLUGIN_APP / 'OlivOSAIChatAssassin'
if not (PLUGIN_ROOT / 'app.json').is_file():
    pytest.skip('OlivOSAIChatAssassin 未安装，跳过插件专项测试', allow_module_level=True)
if str(PLUGIN_APP) not in sys.path:
    sys.path.insert(0, str(PLUGIN_APP))

import OlivOSAIChatAssassin  # noqa: E402
from OlivOSAIChatAssassin import load, msg, tools, webTools  # noqa: E402


BOT_HASH = 'bot-media'
GROUP_ID = 'group-media'


class FakeResponse:
    def __init__(self, content=b'image-bytes', content_type='image/png'):
        self.content = content
        self.headers = {'Content-Type': content_type}

    def raise_for_status(self):
        return None


class FakeData:
    def __init__(self, config, memory):
        self.config = config
        self.memory = memory

    def getConfig(self, _bot_hash):
        return self.config

    def getMemory(self, _bot_hash):
        return self.memory


class DataRevisionConflict(Exception):
    pass


class FakeMemoryManager:
    def __init__(self, memory, revisions=('rev-1', 'rev-2'), failures=None):
        self.memory = copy.deepcopy(memory)
        self.revisions = list(revisions)
        self.failures = list(failures or [])
        self.update_calls = []

    def snapshot_memory(self, _bot_hash):
        revision = self.revisions.pop(0) if len(self.revisions) > 1 else self.revisions[0]
        return copy.deepcopy(self.memory), revision

    def update_bot_memory(self, _bot_hash, revision, updater):
        self.update_calls.append(revision)
        if self.failures:
            raise self.failures.pop(0)
        updater(self.memory)
        return copy.deepcopy(self.memory), 'rev-updated'


class LegacyMemoryManager:
    def __init__(self, memory):
        self.memory = memory

    def getMemory(self, _bot_hash):
        return self.memory


def make_config(ocr_enabled=True, ocr_mode='base64'):
    config = copy.deepcopy(OlivOSAIChatAssassin.data.configDefault)
    config['ocr_api'].update({
        'enable': ocr_enabled,
        'mode': ocr_mode,
        'queue_size': 8,
    })
    return config


def install_data(monkeypatch, config, memory):
    fake_data = FakeData(config, memory)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', fake_data)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMemoryLock', threading.RLock())
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gProc', None)
    return fake_data


@pytest.fixture(autouse=True)
def reset_media_state(monkeypatch):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageCache', {})


def valid_ocr_result():
    return {'content': '一只猫', 'intent': '分享日常', 'type': '照片'}


# OP 参数解析会保留值中的等号。
def test_opcode_parse_params_preserves_equals_in_value():
    parsed = tools.opcode_parse_params(
        'image',
        '[OP:image,file=a.png,url=https://example.test/a.png?token=a=b]',
    )
    assert parsed == {
        'file': 'a.png',
        'url': 'https://example.test/a.png?token=a=b',
    }


# 图片下载会自动创建目录并返回绝对路径。
def test_download_image_to_local_creates_directory_and_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(webTools.requests, 'get', lambda *args, **kwargs: FakeResponse(b'png-data'))
    save_dir = tmp_path / 'nested' / 'images'

    path = webTools.download_image_to_local('https://example.test/a.png', str(save_dir), 'a.png')

    assert path == str((save_dir / 'a.png').resolve())
    assert (save_dir / 'a.png').read_bytes() == b'png-data'


# 非图片响应不会落盘。
def test_download_image_to_local_rejects_non_image_response(tmp_path, monkeypatch):
    response = FakeResponse(b'html', content_type='text/html')
    monkeypatch.setattr(webTools.requests, 'get', lambda *args, **kwargs: response)

    result = webTools.download_image_to_local(
        'https://example.test/a.png',
        str(tmp_path),
        'a.png',
    )

    assert result is None
    assert not (tmp_path / 'a.png').exists()


# 图片请求异常会返回 None。
def test_download_image_to_local_handles_request_failure(tmp_path, monkeypatch):
    def fail_request(*args, **kwargs):
        raise webTools.requests.Timeout('timeout')

    monkeypatch.setattr(webTools.requests, 'get', fail_request)

    assert webTools.download_image_to_local(
        'https://example.test/a.png',
        str(tmp_path),
        'a.png',
    ) is None


# 保存目录创建失败会被兜底处理。
def test_download_image_to_local_handles_directory_creation_failure(tmp_path):
    save_dir = tmp_path / 'not-a-directory'
    save_dir.write_text('occupied', encoding='utf-8')

    result = webTools.download_image_to_local(
        'https://example.test/a.png',
        str(save_dir),
        'a.png',
    )

    assert result is None


# 已知图片扩展名会生成对应 MIME 的 Data URL。
def test_image_to_base64_returns_data_url_for_known_extension(tmp_path):
    path = tmp_path / 'image.PNG'
    path.write_bytes(b'png-data')

    result = webTools.image_to_base64(str(path), include_data_url=True)

    assert result == 'data:image/png;base64,' + base64.b64encode(b'png-data').decode('ascii')


# 关闭前缀时只返回纯 Base64 内容。
def test_image_to_base64_returns_raw_payload_when_prefix_disabled(tmp_path):
    path = tmp_path / 'image.jpg'
    path.write_bytes(b'jpg-data')

    result = webTools.image_to_base64(str(path), include_data_url=False)

    assert result == base64.b64encode(b'jpg-data').decode('ascii')


# 未知扩展名使用通用二进制 MIME。
def test_image_to_base64_uses_octet_stream_for_unknown_extension(tmp_path):
    path = tmp_path / 'image.bin'
    path.write_bytes(b'bin-data')

    result = webTools.image_to_base64(str(path), include_data_url=True)

    assert result == 'data:application/octet-stream;base64,' + base64.b64encode(b'bin-data').decode('ascii')


# 文件不存在时不会读取或编码。
def test_image_to_base64_returns_none_for_missing_file(tmp_path):
    assert webTools.image_to_base64(str(tmp_path / 'missing.png')) is None


# 文件读取异常会返回 None。
def test_image_to_base64_handles_read_failure(tmp_path, monkeypatch):
    path = tmp_path / 'image.png'
    path.write_bytes(b'png-data')
    monkeypatch.setattr(webTools.os.path, 'isfile', lambda _path: True)

    def fail_open(*args, **kwargs):
        raise OSError('read failed')

    monkeypatch.setattr('builtins.open', fail_open)

    assert webTools.image_to_base64(str(path)) is None


# 下载失败时 Base64 转换直接返回 None。
def test_download_image_to_base64_returns_none_when_download_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(webTools, 'download_image_to_local', lambda **kwargs: None)

    assert webTools.download_image_to_base64(
        'https://example.test/a.png',
        str(tmp_path),
        'a.png',
    ) is None


# 下载成功后 Base64 转换返回 Data URL。
def test_download_image_to_base64_converts_downloaded_file(tmp_path, monkeypatch):
    monkeypatch.setattr(webTools.requests, 'get', lambda *args, **kwargs: FakeResponse(b'png-data'))

    result = webTools.download_image_to_base64(
        'https://example.test/a.png',
        str(tmp_path),
        'a.png',
    )

    assert result == 'data:image/png;base64,' + base64.b64encode(b'png-data').decode('ascii')


# 下载过程中的请求异常会被 Base64 包装函数捕获。
def test_download_image_to_base64_handles_request_exception(tmp_path, monkeypatch):
    def fail_download(**kwargs):
        raise webTools.requests.RequestException('download failed')

    monkeypatch.setattr(webTools, 'download_image_to_local', fail_download)

    assert webTools.download_image_to_base64(
        'https://example.test/a.png',
        str(tmp_path),
        'a.png',
    ) is None


# OCR 关闭时图片标记保持原样。
def test_msg_trans_keeps_image_markup_when_ocr_disabled(monkeypatch):
    install_data(monkeypatch, make_config(ocr_enabled=False), {'全局': {}})
    markup = '[OP:image,file=a.png,url=https://example.test/a.png]'

    assert msg.msg_trans(markup, GROUP_ID, bot_hash=BOT_HASH) == markup


# OCR 成功时会规范化 HTTP 文件名、写入图片记忆并追加群内图片缓存。
def test_msg_trans_ocr_success_writes_cache_and_returns_image_description(tmp_path, monkeypatch):
    image_dir = tmp_path / 'images'
    image_dir.mkdir()
    memory = {'全局': {}}
    install_data(monkeypatch, make_config(), memory)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageDir', str(image_dir))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageCache', {(BOT_HASH, GROUP_ID): []})
    write_calls = []
    monkeypatch.setattr(load, 'write_memory', lambda bot_hash: write_calls.append(bot_hash))
    monkeypatch.setattr(
        webTools,
        'download_image_to_base64',
        lambda **kwargs: 'data:image/png;base64,AAAA',
    )
    captured = {}

    def call_ocr(_config, prompt, image_url):
        captured.update({'prompt': prompt, 'image_url': image_url})
        return json.dumps(valid_ocr_result(), ensure_ascii=False)

    monkeypatch.setattr(webTools, 'call_ai_ocr', call_ocr)
    source_url = 'https://example.test/a.png'
    expected_name = hashlib.md5(source_url.encode('utf-8')).hexdigest()

    result = msg.msg_trans(f'[OP:image,file={source_url}]', GROUP_ID, bot_hash=BOT_HASH)

    assert result == '[图片：一只猫；意图：分享日常；类型：照片]'
    assert captured['image_url'] == 'data:image/png;base64,AAAA'
    assert '以JSON格式输出' in captured['prompt']
    assert memory['全局']['图片缓存'][expected_name] == valid_ocr_result()
    assert OlivOSAIChatAssassin.data.gImageCache[(BOT_HASH, GROUP_ID)] == [
        (expected_name, valid_ocr_result())
    ]
    assert write_calls == [BOT_HASH]


# 旧版非 Base64 分支会直接把原始图片 URL 交给 OCR，并使用字典图片缓存。
def test_msg_trans_url_mode_uses_original_url_and_dict_cache(monkeypatch):
    install_data(monkeypatch, make_config(ocr_mode=''), {'全局': {}})
    monkeypatch.setattr(
        OlivOSAIChatAssassin.data,
        'gImageCache',
        {(BOT_HASH, GROUP_ID): {}},
    )
    monkeypatch.setattr(load, 'write_memory', lambda bot_hash: None)
    monkeypatch.setattr(
        webTools,
        'download_image_to_base64',
        lambda **kwargs: pytest.fail('URL 模式不应下载 Base64'),
    )
    captured = {}

    def call_ocr(_config, prompt, image_url):
        captured['image_url'] = image_url
        return json.dumps(valid_ocr_result(), ensure_ascii=False)

    monkeypatch.setattr(webTools, 'call_ai_ocr', call_ocr)

    result = msg.msg_trans(
        '[OP:image,file=abc.png,url=https://example.test/a.png]',
        GROUP_ID,
        bot_hash=BOT_HASH,
    )

    assert result == '[图片：一只猫；意图：分享日常；类型：照片]'
    assert captured['image_url'] == 'https://example.test/a.png'
    assert OlivOSAIChatAssassin.data.gImageCache[(BOT_HASH, GROUP_ID)] == {
        'abc.png': valid_ocr_result()
    }


# 图片缓存命中且文件仍在本地时不会重新下载或调用 OCR。
def test_msg_trans_reuses_cached_image_when_local_file_exists(tmp_path, monkeypatch):
    image_dir = tmp_path / 'images'
    image_dir.mkdir()
    (image_dir / 'a.png').write_bytes(b'cached')
    memory = {'全局': {'图片缓存': {'a.png': valid_ocr_result()}}}
    install_data(monkeypatch, make_config(), memory)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageDir', str(image_dir))
    monkeypatch.setattr(
        OlivOSAIChatAssassin.data,
        'gImageCache',
        {(BOT_HASH, GROUP_ID): deque(maxlen=8)},
    )
    monkeypatch.setattr(
        webTools,
        'download_image_to_local',
        lambda **kwargs: pytest.fail('缓存命中且文件存在时不应下载'),
    )
    monkeypatch.setattr(
        webTools,
        'call_ai_ocr',
        lambda *args, **kwargs: pytest.fail('缓存命中时不应调用 OCR'),
    )

    result = msg.msg_trans(
        '[OP:image,file=a.png,url=https://example.test/a.png]',
        GROUP_ID,
        bot_hash=BOT_HASH,
    )

    assert result == '[图片：一只猫；意图：分享日常；类型：照片]'


# 图片缓存命中但本地文件被删除时会补下载。
def test_msg_trans_redownloads_cached_image_when_local_file_is_missing(tmp_path, monkeypatch):
    image_dir = tmp_path / 'images'
    image_dir.mkdir()
    memory = {'全局': {'图片缓存': {'a.png': valid_ocr_result()}}}
    install_data(monkeypatch, make_config(), memory)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageDir', str(image_dir))
    downloaded = []

    def download(**kwargs):
        downloaded.append(kwargs)
        return str(image_dir / 'a.png')

    monkeypatch.setattr(webTools, 'download_image_to_local', download)
    monkeypatch.setattr(
        webTools,
        'call_ai_ocr',
        lambda *args, **kwargs: pytest.fail('缓存命中时不应调用 OCR'),
    )

    result = msg.msg_trans(
        '[OP:image,file=a.png,url=https://example.test/a.png]',
        GROUP_ID,
        bot_hash=BOT_HASH,
    )

    assert result == '[图片：一只猫；意图：分享日常；类型：照片]'
    assert downloaded == [{
        'url': 'https://example.test/a.png',
        'save_dir': str(image_dir),
        'filename': 'a.png',
    }]


# OCR 返回畸形 JSON 时保留原始图片标记，不污染图片记忆。
def test_msg_trans_malformed_ocr_json_keeps_original_markup(monkeypatch):
    memory = {'全局': {}}
    install_data(monkeypatch, make_config(), memory)
    write_calls = []
    monkeypatch.setattr(load, 'write_memory', lambda bot_hash: write_calls.append(bot_hash))
    monkeypatch.setattr(
        webTools,
        'download_image_to_base64',
        lambda **kwargs: 'data:image/png;base64,AAAA',
    )
    monkeypatch.setattr(webTools, 'call_ai_ocr', lambda *args, **kwargs: 'not-json')
    markup = '[OP:image,file=a.png,url=https://example.test/a.png]'

    result = msg.msg_trans(markup, GROUP_ID, bot_hash=BOT_HASH)

    assert result == markup
    assert memory['全局'] == {}
    assert write_calls == []


# OCR 字段缺失时保留原始图片标记。
def test_msg_trans_incomplete_ocr_result_keeps_original_markup(monkeypatch):
    memory = {'全局': {}}
    install_data(monkeypatch, make_config(), memory)
    monkeypatch.setattr(
        webTools,
        'download_image_to_base64',
        lambda **kwargs: 'data:image/png;base64,AAAA',
    )
    monkeypatch.setattr(
        webTools,
        'call_ai_ocr',
        lambda *args, **kwargs: json.dumps({'content': '一只猫', 'intent': '分享日常'}),
    )
    markup = '[OP:image,file=a.png,url=https://example.test/a.png]'

    assert msg.msg_trans(markup, GROUP_ID, bot_hash=BOT_HASH) == markup
    assert memory['全局'] == {}


# OCR 调用异常不会中断消息转换。
def test_msg_trans_ocr_exception_keeps_original_markup(monkeypatch):
    install_data(monkeypatch, make_config(), {'全局': {}})
    monkeypatch.setattr(
        webTools,
        'download_image_to_base64',
        lambda **kwargs: 'data:image/png;base64,AAAA',
    )

    def fail_ocr(*args, **kwargs):
        raise RuntimeError('ocr failed')

    monkeypatch.setattr(webTools, 'call_ai_ocr', fail_ocr)
    markup = '[OP:image,file=a.png,url=https://example.test/a.png]'

    assert msg.msg_trans(markup, GROUP_ID, bot_hash=BOT_HASH) == markup


# OCR 返回非字符串对象时按失败处理。
def test_msg_trans_non_string_ocr_result_keeps_original_markup(monkeypatch):
    install_data(monkeypatch, make_config(), {'全局': {}})
    monkeypatch.setattr(
        webTools,
        'download_image_to_base64',
        lambda **kwargs: 'data:image/png;base64,AAAA',
    )
    monkeypatch.setattr(webTools, 'call_ai_ocr', lambda *args, **kwargs: valid_ocr_result())
    markup = '[OP:image,file=a.png,url=https://example.test/a.png]'

    assert msg.msg_trans(markup, GROUP_ID, bot_hash=BOT_HASH) == markup


# 图片缓存中的旧值格式错误时不会转成图片描述。
def test_msg_trans_invalid_cached_image_value_keeps_original_markup(monkeypatch):
    memory = {'全局': {'图片缓存': {'a.png': {'content': '缺少意图和类型'}}}}
    install_data(monkeypatch, make_config(), memory)
    monkeypatch.setattr(
        OlivOSAIChatAssassin.data,
        'gImageCache',
        {(BOT_HASH, GROUP_ID): {}},
    )
    markup = '[OP:image,file=a.png,url=https://example.test/a.png]'

    assert msg.msg_trans(markup, GROUP_ID, bot_hash=BOT_HASH) == markup
    assert OlivOSAIChatAssassin.data.gImageCache[(BOT_HASH, GROUP_ID)] == {}


# 非对象记忆更新会被拒绝。
def test_memory_update_rejects_non_object_payload():
    memory = {'全局': {}}

    assert msg._update_memory_from_ai_data(memory, GROUP_ID, ['invalid']) is False


# 全局记忆不是对象时拒绝整次更新。
def test_memory_update_rejects_non_object_global_section():
    memory = {'全局': 'invalid'}

    assert msg._update_memory_from_ai_data(memory, GROUP_ID, {'g': '群记忆'}) is False
    assert memory['全局'] == 'invalid'


# 关闭知识记录时跳过 k，但仍接受用户侧写和群记忆。
def test_memory_update_can_skip_knowledge_updates():
    memory = {'全局': {'知识缓存': {'old': 'keep'}}}

    changed = msg._update_memory_from_ai_data(
        memory,
        GROUP_ID,
        {'k': {'new': 'skip'}, 'u': {'user': '新侧写'}, 'g': '新群记忆'},
        record_knowledge=False,
    )

    assert changed is True
    assert memory['全局']['知识缓存'] == {'old': 'keep'}
    assert memory['全局']['用户侧写'] == {'user': '新侧写'}
    assert memory[GROUP_ID] == '新群记忆'


# 非对象的知识或侧写字段会被忽略。
@pytest.mark.parametrize('payload_key', [
    # 知识缓存字段。
    'k',
    # 用户侧写字段。
    'u',
])
def test_memory_update_ignores_non_object_sections(payload_key):
    memory = {'全局': {}}

    assert msg._update_memory_from_ai_data(memory, GROUP_ID, {payload_key: ['bad']}) is False
    assert memory['全局'] == {}


# 已存在的非对象分区不会被覆盖。
def test_memory_update_ignores_existing_non_object_section():
    memory = {'全局': {'知识缓存': 'invalid'}}

    assert msg._update_memory_from_ai_data(memory, GROUP_ID, {'k': {'new': 'skip'}}) is False
    assert memory['全局']['知识缓存'] == 'invalid'


# 非字符串键和值不会写入记忆。
def test_memory_update_ignores_non_string_keys_and_values():
    memory = {'全局': {}}

    changed = msg._update_memory_from_ai_data(
        memory,
        GROUP_ID,
        {'k': {1: 'skip', 'bad': 2, 'ok': 'keep'}},
    )

    assert changed is True
    assert memory['全局']['知识缓存'] == {'ok': 'keep'}


# 首次创建空分区也会被视为有效结构更新。
def test_memory_update_creates_missing_section_even_when_empty():
    memory = {'全局': {}}

    assert msg._update_memory_from_ai_data(memory, GROUP_ID, {'k': {}}) is True
    assert memory['全局']['知识缓存'] == {}


# 非字符串群记忆会被忽略，不覆盖旧值。
def test_memory_update_rejects_non_string_group_memory():
    memory = {'全局': {}, GROUP_ID: '旧群记忆'}

    assert msg._update_memory_from_ai_data(memory, GROUP_ID, {'g': 42}) is False
    assert memory[GROUP_ID] == '旧群记忆'


# DataManager 路径成功写回时会失效知识缓存。
def test_apply_memory_data_manager_path_invalidates_cache(monkeypatch):
    manager = FakeMemoryManager({'全局': {}})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)
    invalidated = []
    monkeypatch.setattr(
        OlivOSAIChatAssassin.tools,
        'invalidate_peak_up_cache',
        lambda bot_hash: invalidated.append(bot_hash),
    )

    changed = msg._apply_memory_data(BOT_HASH, GROUP_ID, {'g': '新群记忆'}, True)

    assert changed is True
    assert manager.memory[GROUP_ID] == '新群记忆'
    assert manager.update_calls == ['rev-1']
    assert invalidated == [BOT_HASH]


# 内容没有变化时 DataManager 路径不会写盘或失效缓存。
def test_apply_memory_data_manager_path_skips_unchanged(monkeypatch):
    manager = FakeMemoryManager({'全局': {}, GROUP_ID: '相同记忆'})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)
    invalidated = []
    monkeypatch.setattr(
        OlivOSAIChatAssassin.tools,
        'invalidate_peak_up_cache',
        lambda bot_hash: invalidated.append(bot_hash),
    )

    changed = msg._apply_memory_data(BOT_HASH, GROUP_ID, {'g': '相同记忆'}, True)

    assert changed is False
    assert manager.update_calls == []
    assert invalidated == []


# revision 冲突后第二次尝试可以成功。
def test_apply_memory_data_retries_after_revision_conflict(monkeypatch):
    manager = FakeMemoryManager(
        {'全局': {}},
        revisions=('rev-1', 'rev-2'),
        failures=[DataRevisionConflict('rev-2')],
    )
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'invalidate_peak_up_cache', lambda _bot: None)

    changed = msg._apply_memory_data(BOT_HASH, GROUP_ID, {'g': '冲突后写入'}, True)

    assert changed is True
    assert manager.update_calls == ['rev-1', 'rev-2']
    assert manager.memory[GROUP_ID] == '冲突后写入'


# 连续两次 revision 冲突返回失败。
def test_apply_memory_data_returns_false_after_two_revision_conflicts(monkeypatch):
    manager = FakeMemoryManager(
        {'全局': {}},
        revisions=('rev-1', 'rev-2'),
        failures=[
            DataRevisionConflict('rev-2'),
            DataRevisionConflict('rev-3'),
        ],
    )
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)

    assert msg._apply_memory_data(BOT_HASH, GROUP_ID, {'g': '不会写入'}, True) is False
    assert len(manager.update_calls) == 2


# 非 revision 冲突异常会向上抛出。
def test_apply_memory_data_propagates_unexpected_error(monkeypatch):
    manager = FakeMemoryManager(
        {'全局': {}},
        failures=[RuntimeError('disk failed')],
    )
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)

    with pytest.raises(RuntimeError, match='disk failed'):
        msg._apply_memory_data(BOT_HASH, GROUP_ID, {'g': '新群记忆'}, True)


# 旧版直接写入路径成功后会保存记忆并失效缓存。
def test_apply_memory_data_legacy_path_writes_memory(monkeypatch):
    manager = LegacyMemoryManager({'全局': {}})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMemoryLock', threading.Lock())
    writes = []
    invalidated = []
    monkeypatch.setattr(load, 'write_memory', lambda bot_hash: writes.append(bot_hash))
    monkeypatch.setattr(
        OlivOSAIChatAssassin.tools,
        'invalidate_peak_up_cache',
        lambda bot_hash: invalidated.append(bot_hash),
    )

    changed = msg._apply_memory_data(BOT_HASH, GROUP_ID, {'g': '旧版写入'}, True)

    assert changed is True
    assert manager.memory[GROUP_ID] == '旧版写入'
    assert writes == [BOT_HASH]
    assert invalidated == [BOT_HASH]


# 旧版路径没有变化时不会保存或失效缓存。
def test_apply_memory_data_legacy_path_skips_unchanged(monkeypatch):
    manager = LegacyMemoryManager({'全局': {}, GROUP_ID: '相同记忆'})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMemoryLock', threading.Lock())
    writes = []
    invalidated = []
    monkeypatch.setattr(load, 'write_memory', lambda bot_hash: writes.append(bot_hash))
    monkeypatch.setattr(
        OlivOSAIChatAssassin.tools,
        'invalidate_peak_up_cache',
        lambda bot_hash: invalidated.append(bot_hash),
    )

    assert msg._apply_memory_data(BOT_HASH, GROUP_ID, {'g': '相同记忆'}, True) is False
    assert writes == []
    assert invalidated == []
