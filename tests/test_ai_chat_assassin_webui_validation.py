"""WebUI 配置校验、记忆校验与图片缓存兜底的离线覆盖率测试。"""

import math
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_APP = REPO_ROOT
PLUGIN_ROOT = PLUGIN_APP / 'OlivOSAIChatAssassin'
if not (PLUGIN_ROOT / 'app.json').is_file():
    pytest.skip('OlivOSAIChatAssassin 未安装，跳过插件专项测试', allow_module_level=True)
if str(PLUGIN_APP) not in sys.path:
    sys.path.insert(0, str(PLUGIN_APP))

from OlivOSAIChatAssassin import webUI  # noqa: E402


def image_entry(key='image.png', **field_overrides):
    value = {'content': '内容', 'intent': '意图', 'type': '照片'}
    value.update(field_overrides)
    return {'key': key, 'value': value}


def memory_entry(key='name', value='value'):
    return {'key': key, 'value': value}


# 配置校验会拒绝非字符串字段名。
def test_validate_config_patch_rejects_non_string_field_name():
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_config_patch({1: 'value'})

    assert raised.value.fields == {'1': '配置字段名必须是非空字符串'}


# 额外字段支持完整 JSON 值，并拒绝非有限浮点数。
def test_validate_config_patch_handles_extra_json_values():
    result = webUI._validate_config_patch({
        'custom_number': 1,
        'custom_float': 1.5,
        'custom_none': None,
        'custom_list': [True, 'text', None],
        'custom_object': {'nested': 2},
    })

    assert result == {
        'custom_number': 1,
        'custom_float': 1.5,
        'custom_none': None,
        'custom_list': [True, 'text', None],
        'custom_object': {'nested': 2},
    }
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_config_patch({'custom_float': math.inf})
    assert raised.value.fields == {'custom_float': '必须是有限数字'}


# 额外字段会拒绝非字符串对象键与不可序列化值。
def test_validate_config_patch_rejects_invalid_extra_values():
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_config_patch({'custom_object': {1: 'value', 'ok': 2}})
    assert raised.value.fields == {'custom_object': 'JSON 对象的键必须是非空字符串'}

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_config_patch({'custom_value': object()})
    assert raised.value.fields == {'custom_value': '必须是可序列化的 JSON 值'}


# 内置配置字段的类型、范围或枚举错误会带字段定位返回。
@pytest.mark.parametrize(
    ('patch', 'field'),
    [
        # 最大 token 数必须大于 0。
        ({'max_tokens': 0}, 'max_tokens'),
        # 温度必须是有限数值。
        ({'temperature': float('nan')}, 'temperature'),
        # 推理强度只能取受支持的枚举值。
        ({'reasoning_effort': 'low'}, 'reasoning_effort'),
        # 首次思考开关必须是布尔值。
        ({'first_thinking': 'yes'}, 'first_thinking'),
        # 启用群列表中的每个群号都必须是非空字符串。
        ({'enabled_groups': ['ok', '']}, 'enabled_groups.1'),
        # 动态历史窗口开关必须是布尔值。
        ({'history_dynamic': 'yes'}, 'history_dynamic'),
        # 提及回复开关必须是布尔值。
        ({'mention_reply': 1}, 'mention_reply'),
        # 指令前缀必须是非空字符串列表。
        ({'command_prefixes': 'not-a-list'}, 'command_prefixes'),
    ],
)
def test_validate_config_patch_rejects_invalid_known_fields(patch, field):
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_config_patch(patch)

    assert field in raised.value.fields


# OCR 配置会拒绝非对象、未知字段和非法值。
def test_validate_ocr_patch_rejects_invalid_fields():
    errors = {}
    result = webUI._validate_ocr_patch({
        'unknown': 1,
        'api_key': 1,
        'api_base': 'ftp://example.test',
        'model': '',
        'mode': 'url',
        'enable': 'yes',
        'queue_size': 0,
    }, errors)

    assert result == {}
    assert set(errors) == {
        'ocr_api.unknown',
        'ocr_api.api_key',
        'ocr_api.api_base',
        'ocr_api.model',
        'ocr_api.mode',
        'ocr_api.enable',
        'ocr_api.queue_size',
    }
    assert webUI._validate_ocr_patch('bad', {}) is None


# OCR 配置接受空密钥并忽略空值。
def test_validate_ocr_patch_accepts_empty_secret():
    errors = {}

    result = webUI._validate_ocr_patch({
        'api_key': '',
        'api_base': 'https://ocr.example.test/v1',
        'model': 'ocr-model',
        'mode': 'base64',
        'enable': True,
        'queue_size': 2,
    }, errors)

    assert errors == {}
    assert result == {
        'api_base': 'https://ocr.example.test/v1',
        'model': 'ocr-model',
        'mode': 'base64',
        'enable': True,
        'queue_size': 2,
    }


# 思考配置会拒绝非对象、未知键和非法枚举。
def test_validate_thinking_patch_rejects_invalid_fields():
    errors = {}

    assert webUI._validate_thinking_patch('bad', errors) is None
    result = webUI._validate_thinking_patch({'unknown': 1, 'type': 'auto'}, errors)

    assert result == {}
    assert set(errors) == {
        'thinking',
        'thinking.unknown',
        'thinking.type',
    }


# 记忆条目要求非空列表且数量有限。
@pytest.mark.parametrize('entries', [
    # 空列表没有任何可保存的条目。
    [],
    # 非列表输入会被拒绝。
    'bad',
])
def test_validate_memory_entries_rejects_empty_or_non_list(entries):
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries(entries)

    assert raised.value.fields == {'entries': '至少提交一个条目'}


# 记忆条目超过上限时拒绝提交。
def test_validate_memory_entries_rejects_too_many_entries():
    entries = [memory_entry(f'key-{index}') for index in range(webUI.MEMORY_ENTRY_LIMIT + 1)]

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries(entries)

    assert raised.value.fields == {'entries': f'最多 {webUI.MEMORY_ENTRY_LIMIT} 条'}


# 记忆条目必须是对象、键不能重复且必须带 value。
def test_validate_memory_entries_rejects_invalid_entry_shapes():
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries(['bad'])
    assert raised.value.fields == {'entries.0': '必须是对象'}

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries([memory_entry(), memory_entry()])
    assert raised.value.fields == {'entries.1.key': '不能重复提交同一个键'}

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries([{'key': 'name'}])
    assert raised.value.fields == {'entries.0.value': '必须有 value'}


# 记忆条目键和值的长度及可序列化性会被校验。
def test_validate_memory_entries_rejects_invalid_keys_and_values():
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries([memory_entry('')])
    assert raised.value.fields == {'entries.0.key': '必须是 1 到 256 个字符的文本'}

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries([memory_entry(value={'bad': {1, 2}})])
    assert raised.value.message == '记忆内容必须是可序列化的 JSON 值'
    assert raised.value.fields is None


# 单条记忆值和总提交大小都有上限。
def test_validate_memory_entries_enforces_size_limits():
    oversized = 'x' * (webUI.MEMORY_WRITE_VALUE_LIMIT + 1)
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries([memory_entry(value=oversized)])
    assert raised.value.fields == {'entries.0.value': '内容不能超过 256 KiB'}

    entries = [
        memory_entry(f'key-{index}', 'x' * 200000)
        for index in range(3)
    ]
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_memory_entries(entries)
    assert raised.value.fields == {'entries': '总大小不能超过 512 KiB'}


# 图片缓存条目要求固定三字段且字段长度受限。
def test_validate_image_cache_entries_rejects_invalid_fields():
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_image_cache_entries([image_entry(content=1)])
    assert raised.value.fields == {'entries.0.value.content': '必须是字符串'}

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_image_cache_entries([
            image_entry(content='x' * (webUI.IMAGE_CACHE_FIELD_LIMIT + 1))
        ])
    assert raised.value.fields == {
        'entries.0.value.content': f'不能超过 {webUI.IMAGE_CACHE_FIELD_LIMIT} 个字符'
    }


# 图片缓存条目要求对象、非重复键和固定字段集合。
def test_validate_image_cache_entries_rejects_invalid_entry_shapes():
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_image_cache_entries(['bad'])
    assert raised.value.fields == {'entries.0': '必须是对象'}

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_image_cache_entries([
            image_entry('same.png'),
            image_entry('same.png'),
        ])
    assert raised.value.fields == {'entries.1.key': '不能重复提交同一个键'}

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_image_cache_entries([{'key': 'a.png', 'value': 'bad'}])
    assert raised.value.fields == {'entries.0.value': '必须是对象'}

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_image_cache_entries([
            {'key': 'a.png', 'value': {'content': 'c', 'intent': 'i'}}
        ])
    assert raised.value.fields == {
        'entries.0.value': '只能提交 content、intent、type 三个字符串字段'
    }


# 图片缓存条目总大小超过上限时拒绝提交。
def test_validate_image_cache_entries_enforces_total_size_limit():
    entries = [
        image_entry(
            f'{index}.png',
            content='x' * webUI.IMAGE_CACHE_FIELD_LIMIT,
            intent='y' * webUI.IMAGE_CACHE_FIELD_LIMIT,
            type='z' * webUI.IMAGE_CACHE_FIELD_LIMIT,
        )
        for index in range(webUI.MEMORY_ENTRY_LIMIT)
    ]

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._validate_image_cache_entries(entries)
    assert raised.value.fields == {'entries': '总大小不能超过 512 KiB'}


# 合法图片缓存条目会转换为键值元组。
def test_validate_image_cache_entries_returns_clean_pairs():
    result = webUI._validate_image_cache_entries([image_entry('a.png')])

    assert result == [('a.png', {'content': '内容', 'intent': '意图', 'type': '照片'})]


# 图片 MIME 类型按文件签名识别。
@pytest.mark.parametrize(
    ('data', 'mime'),
    [
        # JPEG 按 SOI 标记识别。
        (b'\xff\xd8\xffdata', 'image/jpeg'),
        # PNG 按固定八字节签名识别。
        (b'\x89PNG\r\n\x1a\ndata', 'image/png'),
        # GIF 按 GIF89a 标记识别。
        (b'GIF89adata', 'image/gif'),
        # WebP 按 RIFF 容器中的 WEBP 标记识别。
        (b'RIFFxxxxWEBPdata', 'image/webp'),
        # BMP 按 BM 标记识别。
        (b'BMdata', 'image/bmp'),
        # 未知签名不会误判为图片。
        (b'not-an-image', None),
    ],
)
def test_image_mime_type_detects_supported_signatures(data, mime):
    assert webUI._image_mime_type(data) == mime


# 图片缓存读取会拒绝过大、缺失、不可读和非法文件。
def test_read_image_cache_file_rejects_invalid_sources(tmp_path):
    with pytest.raises(webUI.WebUIError, match='缓存图片过大') as raised:
        webUI._read_image_cache_file(str(tmp_path / 'missing.png'), webUI.MAX_IMAGE_SOURCE_BYTES + 1)
    assert raised.value.code == 'image_too_large'

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._read_image_cache_file(str(tmp_path / 'missing.png'), 1)
    assert raised.value.code == 'image_missing'

    with pytest.raises(webUI.WebUIError) as raised:
        webUI._read_image_cache_file(str(tmp_path), 1)
    assert raised.value.code == 'image_unreadable'

    empty = tmp_path / 'empty.png'
    empty.write_bytes(b'')
    with pytest.raises(webUI.WebUIError) as raised:
        webUI._read_image_cache_file(str(empty), 0)
    assert raised.value.code == 'image_invalid'


# 正常图片读取会返回原始数据和 MIME。
def test_read_image_cache_file_returns_valid_data(tmp_path):
    path = tmp_path / 'a.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\ndata')

    data, mime = webUI._read_image_cache_file(str(path), path.stat().st_size)

    assert data == b'\x89PNG\r\n\x1a\ndata'
    assert mime == 'image/png'


# Pillow 缺失时缩略图生成会安全返回 None。
def test_make_image_thumbnail_returns_none_without_pillow(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'PIL':
            raise ImportError('no pillow')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    assert webUI._make_image_thumbnail(str(tmp_path / 'a.png')) is None


# RGB 图片缩略图会输出 JPEG。
def test_make_image_thumbnail_rgb_outputs_jpeg(tmp_path):
    Image = pytest.importorskip('PIL.Image')
    path = tmp_path / 'rgb.png'
    Image.new('RGB', (800, 600), 'red').save(path)

    data, mime, size = webUI._make_image_thumbnail(str(path))

    assert data.startswith(b'\xff\xd8\xff')
    assert mime == 'image/jpeg'
    assert size == (480, 360)


# RGBA 图片缩略图会输出 PNG。
def test_make_image_thumbnail_rgba_outputs_png(tmp_path):
    Image = pytest.importorskip('PIL.Image')
    path = tmp_path / 'rgba.png'
    Image.new('RGBA', (800, 600), (255, 0, 0, 128)).save(path)

    data, mime, size = webUI._make_image_thumbnail(str(path))

    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    assert mime == 'image/png'
    assert size == (480, 360)


# 损坏图片不会让缩略图生成抛出异常。
def test_make_image_thumbnail_handles_broken_image(tmp_path):
    path = tmp_path / 'broken.png'
    path.write_bytes(b'not-an-image')

    assert webUI._make_image_thumbnail(str(path)) is None
