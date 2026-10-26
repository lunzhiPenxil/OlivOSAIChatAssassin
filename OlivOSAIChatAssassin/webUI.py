import base64
import copy
import io
import json
import math
import os
import threading
import time
import unicodedata
from urllib.parse import urlsplit

import OlivOSAIChatAssassin


NAMESPACE = 'OlivOSAIChatAssassin'
API_VERSION = 1

EVENT_BOOTSTRAP = 'OlivOSAIChatAssassin_WebUI_Bootstrap'
EVENT_GET_CONFIG = 'OlivOSAIChatAssassin_WebUI_GetConfig'
EVENT_SAVE_CONFIG = 'OlivOSAIChatAssassin_WebUI_SaveConfig'
EVENT_MEMORY_OVERVIEW = 'OlivOSAIChatAssassin_WebUI_GetMemoryOverview'
EVENT_MEMORY_SECTION = 'OlivOSAIChatAssassin_WebUI_GetMemorySection'
EVENT_MEMORY_IMAGE = 'OlivOSAIChatAssassin_WebUI_GetMemoryImage'
EVENT_SAVE_MEMORY = 'OlivOSAIChatAssassin_WebUI_SaveMemory'

SECRET_PATHS = ('api_key', 'ocr_api.api_key')
GLOBAL_MEMORY_KEY = '全局'
MEMORY_GROUP_LIMIT = 200
MEMORY_ENTRY_LIMIT = 100
MEMORY_BYTES_LIMIT = 128 * 1024
MEMORY_VALUE_LIMIT = 20000
MEMORY_WRITE_BYTES_LIMIT = 512 * 1024
MEMORY_WRITE_VALUE_LIMIT = 256 * 1024
MEMORY_NAME_LIMIT = 256
MEMORY_SEARCH_LIMIT = 200
MEMORY_SORT_DEFAULT = 'default'
MEMORY_SORT_NAME = 'name'
MEMORY_ORDER_ASC = 'asc'
MEMORY_ORDER_DESC = 'desc'
IMAGE_CACHE_SECTION = '图片缓存'
RELATIONSHIP_SECTION = '人物关系'
RELATIONSHIP_DESCRIPTION_LIMIT = 4096
KNOWLEDGE_SEARCH_SECTION = '知识搜索'
TEXT_ENTRY_SECTIONS = ('用户侧写', '知识缓存', KNOWLEDGE_SEARCH_SECTION)
MEMORY_SPECIAL_SECTIONS = (
    '用户侧写', RELATIONSHIP_SECTION, '知识缓存', KNOWLEDGE_SEARCH_SECTION, IMAGE_CACHE_SECTION,
)
MEMORY_ENTRY_SECTIONS = ('用户侧写', RELATIONSHIP_SECTION, '知识缓存', KNOWLEDGE_SEARCH_SECTION)
IMAGE_CACHE_FIELDS = ('content', 'intent', 'type')
IMAGE_CACHE_FIELD_LIMIT = 4096
IMAGE_TYPE_CONTAINS_FILTERS = ('表情包', '截图', '照片')
IMAGE_TYPE_OTHER = '其它'
IMAGE_TYPE_FILTERS = IMAGE_TYPE_CONTAINS_FILTERS + (IMAGE_TYPE_OTHER,)
IMAGE_THUMBNAIL_MAX_SIZE = (480, 360)
IMAGE_CACHE_INDEX_TTL = 5.0
MAX_IMAGE_SOURCE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
WEBUI_ASSET_FILES = ('webui/index.html',)

_manifest_cache = None
_webui_asset_cache = None
_image_cache_index_lock = threading.Lock()
_image_cache_index_state = {
    'directory': '',
    'signature': None,
    'built_at': 0.0,
    'index': {},
}


class WebUIError(Exception):
    def __init__(self, code: str, message: str, fields=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields if isinstance(fields, dict) else None


def _ok(data=None, revision=None):
    response = {
        'ok': True,
        'data': data if data is not None else {},
    }
    if revision is not None:
        response['revision'] = revision
    return response


def _error(code: str, message: str, fields=None):
    error = {
        'code': code,
        'message': message,
    }
    if isinstance(fields, dict) and fields:
        error['fields'] = fields
    return {
        'ok': False,
        'error': error,
    }


def _load_manifest():
    result = {}
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.json')
        with open(path, 'r', encoding='utf-8') as file_obj:
            manifest = json.load(file_obj)
        if isinstance(manifest, dict):
            result = {
                'name': manifest.get('name', ''),
                'namespace': manifest.get('namespace', NAMESPACE),
                'version': manifest.get('version', ''),
                'compatible_svn': manifest.get('compatible_svn', 0),
                'priority': manifest.get('priority', 0),
            }
    except Exception:
        result = {'namespace': NAMESPACE}
    return result


_manifest_cache = _load_manifest()


def _load_webui_assets():
    result = {}
    root = os.path.dirname(os.path.abspath(__file__))
    for name in WEBUI_ASSET_FILES:
        path = os.path.join(root, *name.split('/'))
        try:
            with open(path, 'rb') as file_obj:
                result[name] = file_obj.read()
        except OSError:
            continue
    return result


_webui_asset_cache = _load_webui_assets()


def restore_webui_assets(Proc):
    """为没有 OPK WebUI 缓存机制的旧宿主恢复静态页面资源。"""
    plugin_models = getattr(Proc, 'plugin_models_dict', None)
    if not isinstance(plugin_models, dict) or not _webui_asset_cache:
        return False
    plugin = plugin_models.get(NAMESPACE)
    if not isinstance(plugin, dict):
        return False
    root = plugin.get('webui_root')
    if not isinstance(root, str) or not root:
        return False
    restored = 0
    failed = 0
    for name, data in _webui_asset_cache.items():
        target = os.path.join(root, *name.split('/'))
        if os.path.isfile(target):
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'wb') as file_obj:
                file_obj.write(data)
            restored += 1
        except OSError:
            failed += 1
    if restored:
        Proc.log(2, f'WEBUI - OPK 页面资源已恢复 - {restored}')
    if failed:
        Proc.log(3, f'WEBUI - OPK 页面资源恢复失败 - {failed}')
    return restored > 0


def _manifest_info():
    return copy.deepcopy(_manifest_cache)


def _proc_bot_dict(Proc):
    proc_data = getattr(Proc, 'Proc_data', None)
    if not isinstance(proc_data, dict):
        return {}
    bot_dict = proc_data.get('bot_info_dict', {})
    return bot_dict if isinstance(bot_dict, dict) else {}


def _public_bot(bot_hash, bot_info):
    platform = getattr(bot_info, 'platform', None)
    if not isinstance(platform, dict):
        platform = {}
    bot_id = getattr(bot_info, 'id', None)
    if not isinstance(bot_id, (str, int, float, bool)) and bot_id is not None:
        bot_id = str(bot_id)
    return {
        'hash': str(bot_hash),
        'id': bot_id,
        'platform': {
            'sdk': platform.get('sdk'),
            'platform': platform.get('platform'),
            'model': platform.get('model'),
        },
        'enable': bool(getattr(bot_info, 'enable', False)),
    }


def _bot_list(Proc):
    bots = []
    for bot_hash, bot_info in _proc_bot_dict(Proc).items():
        if not isinstance(bot_hash, str):
            continue
        bots.append(_public_bot(bot_hash, bot_info))
    bots.sort(key=lambda item: (
        str(item['platform'].get('sdk') or ''),
        str(item['platform'].get('platform') or ''),
        str(item['id'] or ''),
        item['hash'],
    ))
    return bots


def _require_bot(Proc, payload):
    bot_hash = payload.get('bot_hash')
    if not isinstance(bot_hash, str) or not bot_hash or len(bot_hash) > 128:
        raise WebUIError('validation_error', 'bot_hash 无效', {'bot_hash': '必须是有效的账号标识'})
    bot_info = _proc_bot_dict(Proc).get(bot_hash)
    if bot_info is None:
        raise WebUIError('not_found', '账号不存在或尚未加载')
    if OlivOSAIChatAssassin.data.gData is None:
        raise WebUIError('not_ready', '插件数据尚未初始化')
    return bot_hash, bot_info


def _as_payload(payload):
    if not isinstance(payload, dict):
        raise WebUIError('validation_error', '请求 payload 必须是对象')
    try:
        payload_size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    except (TypeError, ValueError):
        raise WebUIError('validation_error', '请求 payload 无法序列化')
    if payload_size > MAX_REQUEST_BYTES:
        raise WebUIError(
            'validation_error',
            '请求内容过大',
            {'payload': '请求体不能超过 1 MiB'},
        )
    api_version = payload.get('api_version', API_VERSION)
    if type(api_version) is not int or api_version != API_VERSION:
        raise WebUIError(
            'unsupported_version',
            f'不支持的接口版本: {api_version}',
            {'api_version': f'当前仅支持 {API_VERSION}'},
        )
    return payload


def _deep_merge_defaults(base: dict, current: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_defaults(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _public_config(config: dict):
    effective = _deep_merge_defaults(OlivOSAIChatAssassin.data.configDefault, config)
    api_key = config.get('api_key', '')
    ocr_config = config.get('ocr_api', {})
    if not isinstance(ocr_config, dict):
        ocr_config = {}
    ocr_key = ocr_config.get('api_key', '')
    effective['api_key'] = ''
    if not isinstance(effective.get('ocr_api'), dict):
        effective['ocr_api'] = {}
    effective['ocr_api']['api_key'] = ''
    secrets = {
        'api_key': {'configured': isinstance(api_key, str) and bool(api_key.strip())},
        'ocr_api.api_key': {'configured': isinstance(ocr_key, str) and bool(ocr_key.strip())},
    }
    return effective, secrets


def _config_warnings(config: dict):
    warnings = []
    expected_types = (
        ('thinking', dict, '思考配置'),
        ('ocr_api', dict, 'OCR 配置'),
        ('enabled_groups', list, '启用群列表'),
        ('reply_keywords', list, '回复关键词'),
        ('ignore_prefixes', list, '忽略前缀'),
    )
    for field, expected_type, label in expected_types:
        if field in config and not isinstance(config[field], expected_type):
            type_name = '对象' if expected_type is dict else '列表'
            warnings.append(f'{label}（{field}）不是{type_name}，页面暂按默认结构显示；请检查配置文件。')
    return warnings


def _add_field_error(errors, field, message):
    errors[field] = message


def _validate_string(value, field, errors, allow_empty=False, max_length=None, url=False):
    if not isinstance(value, str):
        _add_field_error(errors, field, '必须是字符串')
        return None
    if not allow_empty and not value.strip():
        _add_field_error(errors, field, '不能为空')
        return None
    if max_length is not None and len(value) > max_length:
        _add_field_error(errors, field, f'不能超过 {max_length} 个字符')
        return None
    if url:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            _add_field_error(errors, field, '必须是 http 或 https 地址')
            return None
    return value


def _validate_number(value, field, errors, minimum, maximum=None, integer=False):
    if integer:
        if type(value) is not int:
            _add_field_error(errors, field, '必须是整数')
            return None
    elif type(value) not in (int, float):
        _add_field_error(errors, field, '必须是数字')
        return None
    if isinstance(value, float) and not math.isfinite(value):
        _add_field_error(errors, field, '必须是有限数字')
        return None
    if value < minimum:
        _add_field_error(errors, field, f'不能小于 {minimum}')
        return None
    if maximum is not None and value > maximum:
        _add_field_error(errors, field, f'不能大于 {maximum}')
        return None
    return value


def _validate_bool(value, field, errors):
    if type(value) is not bool:
        _add_field_error(errors, field, '必须是布尔值')
        return None
    return value


def _validate_string_list(value, field, errors):
    if not isinstance(value, list):
        _add_field_error(errors, field, '必须是字符串列表')
        return None
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _add_field_error(errors, f'{field}.{index}', '必须是非空字符串')
            return None
        result.append(item)
    return result


def _validate_ocr_patch(value, errors):
    if not isinstance(value, dict):
        _add_field_error(errors, 'ocr_api', '必须是对象')
        return None
    allowed = set(OlivOSAIChatAssassin.data.configDefault['ocr_api'].keys())
    result = {}
    for key, item in value.items():
        field = f'ocr_api.{key}'
        if key not in allowed:
            _add_field_error(errors, field, '不支持的 OCR 配置项')
        elif key == 'api_key':
            checked = _validate_string(item, field, errors, allow_empty=True)
            if checked is not None and checked:
                result[key] = checked
        elif key == 'api_base':
            checked = _validate_string(item, field, errors, url=True)
            if checked is not None:
                result[key] = checked
        elif key == 'model':
            checked = _validate_string(item, field, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'mode':
            if item != 'base64':
                _add_field_error(errors, field, '当前仅支持 base64')
            else:
                result[key] = item
        elif key == 'enable':
            checked = _validate_bool(item, field, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'queue_size':
            checked = _validate_number(item, field, errors, 1, integer=True)
            if checked is not None:
                result[key] = checked
    return result


def _validate_thinking_patch(value, errors):
    if not isinstance(value, dict):
        _add_field_error(errors, 'thinking', '必须是对象')
        return None
    result = {}
    for key, item in value.items():
        field = f'thinking.{key}'
        if key != 'type':
            _add_field_error(errors, field, '不支持的思考配置项')
        elif item not in ('enabled', 'disabled'):
            _add_field_error(errors, field, '必须是 enabled 或 disabled')
        else:
            result[key] = item
    return result


def _validate_config_patch(patch: dict):
    errors = {}
    result = {}
    allowed = set(OlivOSAIChatAssassin.data.configDefault.keys())
    for key, value in patch.items():
        if key not in allowed:
            _add_field_error(errors, key, '不支持的配置项')
            continue
        if key == 'api_key':
            checked = _validate_string(value, key, errors, allow_empty=True)
            if checked is not None and checked:
                result[key] = checked
        elif key == 'api_base':
            checked = _validate_string(value, key, errors, url=True)
            if checked is not None:
                result[key] = checked
        elif key == 'model':
            checked = _validate_string(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'max_tokens':
            checked = _validate_number(value, key, errors, 1, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'api_timeout':
            checked = _validate_number(value, key, errors, 1)
            if checked is not None:
                result[key] = checked
        elif key == 'temperature':
            checked = _validate_number(value, key, errors, 0)
            if checked is not None:
                result[key] = checked
        elif key == 'thinking':
            checked = _validate_thinking_patch(value, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'reasoning_effort':
            if value not in ('high', 'max'):
                _add_field_error(errors, key, '必须是 high 或 max')
            else:
                result[key] = value
        elif key == 'first_thinking':
            checked = _validate_bool(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'first_thinking_cooldown':
            checked = _validate_number(value, key, errors, 0)
            if checked is not None:
                result[key] = checked
        elif key == 'personality':
            checked = _validate_string(value, key, errors, allow_empty=True)
            if checked is not None:
                result[key] = checked
        elif key == 'record_knowledge':
            checked = _validate_bool(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'search_knowledge_deepin':
            checked = _validate_number(value, key, errors, 0, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'retry_count':
            checked = _validate_number(value, key, errors, 1, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'enabled_groups':
            checked = _validate_string_list(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'search_ageing':
            checked = _validate_number(value, key, errors, 1)
            if checked is not None:
                result[key] = checked
        elif key == 'history_size_min':
            checked = _validate_number(value, key, errors, 1, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'history_size':
            checked = _validate_number(value, key, errors, 1, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'history_dynamic':
            checked = _validate_bool(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'history_dynamic_size':
            checked = _validate_number(value, key, errors, 1, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'slack_time':
            checked = _validate_number(value, key, errors, 0)
            if checked is not None:
                result[key] = checked
        elif key == 'slack_cooldown_time':
            checked = _validate_number(value, key, errors, 0)
            if checked is not None:
                result[key] = checked
        elif key == 'reply_probability':
            checked = _validate_number(value, key, errors, 0)
            if checked is not None:
                result[key] = checked
        elif key == 'reply_keywords':
            checked = _validate_string_list(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'mention_reply':
            checked = _validate_bool(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'ignore_prefixes':
            checked = _validate_string_list(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'max_message_length':
            checked = _validate_number(value, key, errors, 1, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'ocr_api':
            checked = _validate_ocr_patch(value, errors)
            if checked is not None:
                result[key] = checked
    if errors:
        raise WebUIError('validation_error', '配置校验失败', errors)
    return result


def _validate_clear_secrets(value, patch):
    if value is None:
        return []
    if not isinstance(value, list):
        raise WebUIError('validation_error', 'clear_secrets 必须是列表')
    result = []
    errors = {}
    for index, item in enumerate(value):
        if item not in SECRET_PATHS:
            _add_field_error(errors, f'clear_secrets.{index}', '不支持清除该字段')
        elif _patch_has_path(patch, item):
            _add_field_error(errors, f'clear_secrets.{index}', '不能同时设置和清除同一密钥')
        elif item not in result:
            result.append(item)
    if errors:
        raise WebUIError('validation_error', '密钥清除参数无效', errors)
    return result


def _patch_has_path(patch: dict, path: str):
    target = patch
    keys = path.split('.')
    for key in keys[:-1]:
        if not isinstance(target, dict) or key not in target:
            return False
        target = target[key]
    return isinstance(target, dict) and keys[-1] in target


def _display_memory_value(value, limit=MEMORY_VALUE_LIMIT):
    if isinstance(value, str):
        return value[:limit], len(value) > limit
    chunks = []
    used = 0
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
        for chunk in encoder.iterencode(value):
            remaining = limit - used
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                return ''.join(chunks), True
            chunks.append(chunk)
            used += len(chunk)
        return ''.join(chunks), False
    except Exception:
        text = str(value)
        return text[:limit], len(text) > limit


def _is_safe_image_cache_name(filename):
    return not (
        not isinstance(filename, str)
        or not filename
        or len(filename) > MEMORY_NAME_LIMIT
        or filename in ('.', '..')
        or '\x00' in filename
        or '/' in filename
        or '\\' in filename
        or ':' in filename
        or os.path.basename(filename) != filename
    )


def _safe_image_cache_path(filename):
    if not _is_safe_image_cache_name(filename):
        return None
    image_dir = os.path.abspath(OlivOSAIChatAssassin.data.gImageDir)
    candidate = os.path.abspath(os.path.join(image_dir, filename))
    try:
        real_dir = os.path.realpath(image_dir)
        real_candidate = os.path.realpath(candidate)
        if os.path.commonpath((real_dir, real_candidate)) != real_dir:
            return None
    except (OSError, ValueError):
        return None
    return real_candidate


def _image_cache_name_key(filename):
    return unicodedata.normalize('NFC', filename).casefold()


def _image_cache_directory_signature(image_dir):
    try:
        info = os.stat(image_dir)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_ctime_ns, info.st_size)


def _build_image_cache_directory_index(image_dir):
    index = {}
    try:
        entries = os.scandir(image_dir)
    except OSError:
        return index
    with entries:
        for entry in entries:
            name = entry.name
            if not _is_safe_image_cache_name(name):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                file_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            index.setdefault(_image_cache_name_key(name), {
                'path': os.path.join(image_dir, name),
                'size': file_stat.st_size,
            })
    return index


def _image_cache_directory_index():
    image_dir = os.path.abspath(OlivOSAIChatAssassin.data.gImageDir)
    signature = _image_cache_directory_signature(image_dir)
    if signature is None:
        return {}
    now = time.monotonic()
    with _image_cache_index_lock:
        state = _image_cache_index_state
        if (
            state['directory'] == image_dir
            and state['signature'] == signature
            and now - state['built_at'] < IMAGE_CACHE_INDEX_TTL
        ):
            return state['index']
        index = _build_image_cache_directory_index(image_dir)
        state['directory'] = image_dir
        state['signature'] = signature
        state['built_at'] = now
        state['index'] = index
        return index


def _image_cache_file_info(filename, directory_index=None):
    if directory_index is not None:
        if not _is_safe_image_cache_name(filename):
            return {'status': 'invalid', 'size': 0}, None
        cached = directory_index.get(_image_cache_name_key(filename))
        if cached is None:
            # 列表接口只需要命中状态，不需要为每条缺失档案重复解析磁盘路径。
            # 生产记忆可能包含数万条已被用户删除的文件记录，逐条 realpath 会显著放大高负载超时风险。
            return {'status': 'missing', 'size': 0}, None
        return {'status': 'available', 'size': cached['size']}, cached['path']
    else:
        path = _safe_image_cache_path(filename)
        if path is None:
            return {'status': 'invalid', 'size': 0}, None
    try:
        if not os.path.isfile(path):
            return {'status': 'missing', 'size': 0}, path
        return {'status': 'available', 'size': os.path.getsize(path)}, path
    except OSError:
        return {'status': 'unavailable', 'size': 0}, path


def _image_cache_fields(value):
    fields = {}
    truncated = False
    extra_count = 0
    legacy = not isinstance(value, dict)
    if isinstance(value, dict):
        extra_count = len([key for key in value if key not in IMAGE_CACHE_FIELDS])
        legacy = legacy or extra_count > 0
        for field in IMAGE_CACHE_FIELDS:
            if field not in value:
                fields[field] = ''
                legacy = True
                continue
            raw_value = value[field]
            if isinstance(raw_value, str):
                text = raw_value
            else:
                text, field_truncated = _display_memory_value(raw_value, limit=IMAGE_CACHE_FIELD_LIMIT)
                truncated = truncated or field_truncated
                legacy = True
            if len(text) > IMAGE_CACHE_FIELD_LIMIT:
                text = text[:IMAGE_CACHE_FIELD_LIMIT]
                truncated = True
            fields[field] = text
    else:
        text, truncated = _display_memory_value(value, limit=IMAGE_CACHE_FIELD_LIMIT)
        fields['content'] = text
        fields['intent'] = ''
        fields['type'] = ''
    return fields, legacy, truncated, extra_count


def _image_cache_records(items, directory_index=None):
    records = []
    for index, (key, value) in enumerate(items):
        key_text = str(key)
        fields, legacy, truncated, extra_count = _image_cache_fields(value)
        file_info, _path = _image_cache_file_info(key_text, directory_index)
        entry = {
            'key': key_text,
            'kind': type(value).__name__,
            'editable': not truncated,
            'deletable': False,
            'truncated': truncated,
            'legacy': legacy,
            'extra_fields': extra_count,
            'fields': fields,
            'image': file_info,
        }
        records.append({
            'index': index,
            'key': key_text,
            'entry': entry,
            'search_text': '\n'.join([key_text] + [fields[field] for field in IMAGE_CACHE_FIELDS]).casefold(),
            'image_type': unicodedata.normalize('NFC', fields['type']).casefold(),
            'image_status': file_info['status'],
            'cache_hit': file_info['status'] in ('available', 'unavailable'),
        })
    return records


def _dict_memory_records(items):
    records = []
    for index, (key, value) in enumerate(items):
        key_text = str(key)
        text, truncated = _display_memory_value(value)
        entry = {
            'key': key_text,
            'kind': type(value).__name__,
            'value': text,
            'truncated': truncated,
            'editable': not truncated,
        }
        records.append({
            'index': index,
            'key': key_text,
            'entry': entry,
            'search_text': (key_text + '\n' + text).casefold(),
        })
    return records


def _relationship_memory_value(value):
    relationship = {
        'recognized': False,
        'format': 'unrecognized',
        'aliases': [],
        'description': '',
        'description_kind': '',
        'extra_fields': 0,
        'aliases_truncated': False,
        'description_truncated': False,
    }
    if not isinstance(value, list) or not value:
        return relationship
    first = value[0]
    if isinstance(first, str):
        aliases = [first]
        format_name = 'single_alias'
    elif first is None:
        aliases = []
        format_name = 'description_only'
    elif isinstance(first, list) and all(isinstance(alias, str) for alias in first):
        aliases = first
        format_name = 'multiple_aliases' if first else 'description_only'
    else:
        return relationship

    relationship['recognized'] = True
    relationship['format'] = format_name
    bounded_aliases = []
    for alias in aliases:
        text, truncated = _display_memory_value(alias, limit=MEMORY_NAME_LIMIT)
        bounded_aliases.append(text)
        relationship['aliases_truncated'] = relationship['aliases_truncated'] or truncated
    relationship['aliases'] = bounded_aliases
    if len(value) > 1:
        description = value[1]
        relationship['description_kind'] = type(description).__name__
        text, truncated = _display_memory_value(description, limit=RELATIONSHIP_DESCRIPTION_LIMIT)
        relationship['description'] = text
        relationship['description_truncated'] = truncated
    relationship['extra_fields'] = max(0, len(value) - 2)
    return relationship


def _relationship_memory_records(items):
    records = []
    for index, (key, value) in enumerate(items):
        key_text = str(key)
        text, truncated = _display_memory_value(value)
        relationship = _relationship_memory_value(value)
        aliases = relationship['aliases']
        entry = {
            'key': key_text,
            'kind': type(value).__name__,
            'value': text,
            'truncated': truncated,
            'editable': not truncated,
            'relationship': relationship,
        }
        sort_text = ' / '.join(aliases) if aliases else key_text
        search_parts = [key_text, text, relationship['description']] + aliases
        records.append({
            'index': index,
            'key': key_text,
            'entry': entry,
            'sort_text': sort_text.casefold(),
            'search_text': '\n'.join(search_parts).casefold(),
        })
    return records


def _prepare_memory_records(records, options, image_cache=False):
    search = options['search'].casefold()
    if search:
        records = [record for record in records if search in record['search_text']]
    image_types = options.get('image_types', [])
    if image_cache and image_types:
        contains_filters = [
            unicodedata.normalize('NFC', item).casefold()
            for item in IMAGE_TYPE_CONTAINS_FILTERS
        ]
        selected_filters = [
            unicodedata.normalize('NFC', item).casefold()
            for item in image_types
            if item in IMAGE_TYPE_CONTAINS_FILTERS
        ]
        include_other = IMAGE_TYPE_OTHER in image_types

        def type_matches(record):
            record_type = record['image_type']
            if any(item in record_type for item in selected_filters):
                return True
            if include_other:
                return not any(item in record_type for item in contains_filters)
            return False

        records = [record for record in records if type_matches(record)]
    if options['sort'] == MEMORY_SORT_NAME:
        records.sort(key=lambda record: (record.get('sort_text') or record['key'].casefold(), record['index']))
    if options['order'] == MEMORY_ORDER_DESC:
        records.reverse()
    if image_cache and options['cached_first']:
        records.sort(key=lambda record: 0 if record['cache_hit'] else 1)
    return records


def _slice_memory_records(records, offset, limit):
    entries = []
    used_bytes = 0
    for record in records[offset:]:
        if len(entries) >= limit:
            break
        entry = record['entry']
        entry_size = len(json.dumps(entry, ensure_ascii=False).encode('utf-8'))
        if entries and used_bytes + entry_size > MEMORY_BYTES_LIMIT:
            break
        entries.append(entry)
        used_bytes += entry_size
    return entries


def _memory_dict_page(records, unfiltered_total, offset, limit, options, image_cache=False):
    total = len(records)
    entries = _slice_memory_records(records, offset, limit)
    next_offset = offset + len(entries)
    result = {
        'kind': 'dict',
        'entries': entries,
        'total': total,
        'unfiltered_total': unfiltered_total,
        'filtered': bool(options['search'] or (image_cache and options['image_types'])),
        'search': options['search'],
        'sort': options['sort'],
        'order': options['order'],
        'cached_first': bool(image_cache and options['cached_first']),
        'offset': offset,
        'next_offset': next_offset,
        'has_more': next_offset < total,
    }
    if image_cache:
        result['image_types'] = list(options['image_types'])
        result['cache_hit_total'] = sum(1 for record in records if record['cache_hit'])
    return result


def _image_mime_type(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'BM'):
        return 'image/bmp'
    return None


def _make_image_thumbnail(path):
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(path) as opened:
            opened.seek(0)
            image = ImageOps.exif_transpose(opened)
            if image.mode not in ('RGB', 'RGBA'):
                image = image.convert('RGBA')
            image.thumbnail(IMAGE_THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)
            output = io.BytesIO()
            if image.mode == 'RGBA':
                image.save(output, format='PNG', optimize=True)
                mime = 'image/png'
            else:
                image.convert('RGB').save(output, format='JPEG', quality=82, optimize=True)
                mime = 'image/jpeg'
            return output.getvalue(), mime, image.size
    except Exception:
        return None


def _read_image_cache_file(path, source_size):
    if source_size > MAX_IMAGE_SOURCE_BYTES:
        raise WebUIError('image_too_large', '缓存图片过大，无法读取')
    try:
        with open(path, 'rb') as file_obj:
            data = file_obj.read(MAX_IMAGE_SOURCE_BYTES + 1)
    except FileNotFoundError:
        raise WebUIError('image_missing', '缓存图片文件已删除')
    except OSError:
        raise WebUIError('image_unreadable', '缓存图片文件无法读取')
    if len(data) > MAX_IMAGE_SOURCE_BYTES:
        raise WebUIError('image_too_large', '缓存图片过大，无法读取')
    mime = _image_mime_type(data)
    if mime is None:
        raise WebUIError('image_invalid', '缓存文件不是可识别的图片')
    return data, mime


def _page_number(value, default, minimum, maximum):
    if value is None:
        return default
    if type(value) is not int or value < minimum or value > maximum:
        raise WebUIError('validation_error', '分页参数无效')
    return value


def _handle_bootstrap(payload, Proc):
    return _ok({
        'api_version': API_VERSION,
        'bots': _bot_list(Proc),
        'plugin': _manifest_info(),
        'capabilities': {
            'config_read': True,
            'config_write': True,
            'memory_read': True,
            'memory_write': True,
        },
    })


def _handle_get_config(payload, Proc):
    bot_hash, _bot_info = _require_bot(Proc, payload)
    config, revision = OlivOSAIChatAssassin.data.gData.snapshot_config(bot_hash)
    public_config, secrets = _public_config(config)
    return _ok({
        'config': public_config,
        'secrets': secrets,
        'warnings': _config_warnings(config),
        'source': OlivOSAIChatAssassin.data.gData.config_source(bot_hash),
    }, revision)


def _handle_save_config(payload, Proc):
    bot_hash, _bot_info = _require_bot(Proc, payload)
    patch = payload.get('patch')
    if not isinstance(patch, dict):
        raise WebUIError('validation_error', 'patch 必须是对象')
    expected_revision = payload.get('expected_revision')
    if not isinstance(expected_revision, str) or not expected_revision:
        raise WebUIError(
            'validation_error',
            'expected_revision 无效',
            {'expected_revision': '必须是当前配置 revision'},
        )
    clean_patch = _validate_config_patch(patch)
    clear_secrets = _validate_clear_secrets(payload.get('clear_secrets'), clean_patch)
    try:
        config, revision = OlivOSAIChatAssassin.data.gData.update_bot_config(
            bot_hash,
            clean_patch,
            expected_revision,
            clear_secrets,
        )
    except OlivOSAIChatAssassin.load.DataRevisionConflict as exc:
        raise WebUIError(
            'conflict',
            '配置文件已被其他操作修改，请刷新后重试',
            {'current_revision': exc.current_revision},
        )
    public_config, secrets = _public_config(config)
    return _ok({
        'config': public_config,
        'secrets': secrets,
        'warnings': _config_warnings(config),
        'source': OlivOSAIChatAssassin.data.gData.config_source(bot_hash),
    }, revision)


def _memory_group_index_entries(memory, group_keys):
    entries = []
    for group_id in group_keys:
        value = memory[group_id]
        text, truncated = _display_memory_value(value, limit=200)
        entries.append({
            'group_id': str(group_id),
            'preview': text,
            'length': len(text),
            'truncated': truncated,
        })
    return entries


def _handle_memory_overview(payload, Proc):
    bot_hash, _bot_info = _require_bot(Proc, payload)
    memory, revision = OlivOSAIChatAssassin.data.gData.snapshot_memory(bot_hash)
    if not isinstance(memory, dict):
        raise WebUIError('memory_invalid', '记忆文件根节点必须是对象')

    global_memory = memory.get(GLOBAL_MEMORY_KEY, {})
    global_sections = []
    if isinstance(global_memory, dict):
        for name in global_memory.keys():
            value = global_memory[name]
            section = {
                'name': str(name),
                'kind': type(value).__name__,
            }
            if isinstance(value, (dict, list, str)):
                section['count'] = len(value)
            else:
                section['count'] = 0
            if isinstance(value, str):
                preview, truncated = _display_memory_value(value, limit=200)
                section['preview'] = preview
                section['truncated'] = truncated
            global_sections.append(section)

    group_keys = [key for key in memory.keys() if key != GLOBAL_MEMORY_KEY]
    group_keys.reverse()
    groups = _memory_group_index_entries(memory, group_keys[:MEMORY_GROUP_LIMIT])
    return _ok({
        'global_sections': global_sections,
        'groups': groups,
        'group_count': len(group_keys),
        'groups_truncated': len(group_keys) > MEMORY_GROUP_LIMIT,
    }, revision)


def _memory_metadata(value):
    result = {'kind': type(value).__name__}
    if isinstance(value, (str, dict, list)):
        result['length' if isinstance(value, str) else 'count'] = len(value)
    return result


def _memory_list_options(payload, image_cache=False):
    sort = payload.get('sort', MEMORY_SORT_DEFAULT)
    if sort not in (MEMORY_SORT_DEFAULT, MEMORY_SORT_NAME):
        raise WebUIError('validation_error', '排序方式无效', {'sort': '必须是 default 或 name'})
    order = payload.get('order', MEMORY_ORDER_DESC)
    if order not in (MEMORY_ORDER_ASC, MEMORY_ORDER_DESC):
        raise WebUIError('validation_error', '排序方向无效', {'order': '必须是 asc 或 desc'})
    search = payload.get('search', '')
    if search is None:
        search = ''
    if not isinstance(search, str) or len(search) > MEMORY_SEARCH_LIMIT:
        raise WebUIError(
            'validation_error',
            '搜索内容无效',
            {'search': f'不能超过 {MEMORY_SEARCH_LIMIT} 个字符'},
        )
    cached_first = payload.get('cached_first', bool(image_cache))
    if type(cached_first) is not bool:
        raise WebUIError('validation_error', '缓存优先级无效', {'cached_first': '必须是布尔值'})
    image_types = []
    if image_cache:
        raw_image_types = payload.get('image_types', [])
        if raw_image_types is None:
            raw_image_types = []
        if (
            not isinstance(raw_image_types, list)
            or any(item not in IMAGE_TYPE_FILTERS for item in raw_image_types)
        ):
            raise WebUIError(
                'validation_error',
                '图片类型筛选无效',
                {'image_types': '必须是表情包、截图、照片或其它组成的数组'},
            )
        image_types = list(dict.fromkeys(raw_image_types))
    return {
        'sort': sort,
        'order': order,
        'search': search.strip(),
        'cached_first': cached_first,
        'image_types': image_types,
    }


def _handle_memory_section(payload, Proc):
    bot_hash, _bot_info = _require_bot(Proc, payload)
    scope = payload.get('scope')
    offset = _page_number(payload.get('offset'), 0, 0, 1000000)
    limit = _page_number(payload.get('limit'), 50, 1, MEMORY_ENTRY_LIMIT)
    memory, revision = OlivOSAIChatAssassin.data.gData.snapshot_memory(bot_hash)
    if not isinstance(memory, dict):
        raise WebUIError('memory_invalid', '记忆文件根节点必须是对象')

    if scope == 'group_index':
        group_keys = [key for key in memory.keys() if key != GLOBAL_MEMORY_KEY]
        group_keys.reverse()
        entries = _memory_group_index_entries(memory, group_keys[offset:offset + limit])
        next_offset = offset + len(entries)
        return _ok({
            'scope': 'group_index',
            'entries': entries,
            'total': len(group_keys),
            'offset': offset,
            'next_offset': next_offset,
            'has_more': next_offset < len(group_keys),
            'can_create': True,
        }, revision)

    if scope == 'group':
        group_id = payload.get('group_id')
        if not isinstance(group_id, str) or not group_id or len(group_id) > 256:
            raise WebUIError('validation_error', 'group_id 无效')
        if group_id not in memory:
            raise WebUIError('not_found', '该群记忆不存在')
        text, truncated = _display_memory_value(memory[group_id])
        return _ok({
            'scope': 'group',
            'group_id': group_id,
            'value': text,
            'value_kind': type(memory[group_id]).__name__,
            'truncated': truncated,
            'editable': not truncated,
            'can_delete': True,
        }, revision)

    if scope != 'global':
        raise WebUIError('validation_error', 'scope 必须是 global 或 group')
    section_name = payload.get('section')
    if not isinstance(section_name, str) or not section_name or len(section_name) > 256:
        raise WebUIError('validation_error', 'section 无效')
    section_editable = section_name in MEMORY_SPECIAL_SECTIONS
    global_memory = memory.get(GLOBAL_MEMORY_KEY, {})
    if not isinstance(global_memory, dict):
        raise WebUIError('not_found', '该全局记忆区块不存在')
    section_present = section_name in global_memory
    if section_present:
        value = global_memory[section_name]
    elif section_name in MEMORY_SPECIAL_SECTIONS:
        value = {}
    else:
        raise WebUIError('not_found', '该全局记忆区块不存在')
    if section_name == IMAGE_CACHE_SECTION:
        if not isinstance(value, dict):
            return _ok({
                'scope': 'global',
                'section': section_name,
                'kind': 'invalid',
                'image_cache': True,
                'editable': False,
                'can_create': False,
                'can_delete': False,
                'warning': '图片缓存档案结构异常：该区块应为对象，当前无法逐条浏览。',
                'metadata': _memory_metadata(value),
                'entries': [],
                'total': 0,
                'offset': offset,
                'next_offset': offset,
                'has_more': False,
            }, revision)
        list_options = _memory_list_options(payload, image_cache=True)
        items = list(value.items())
        records = _prepare_memory_records(
            _image_cache_records(items, _image_cache_directory_index()),
            list_options,
            image_cache=True,
        )
        result = {
            'scope': 'global',
            'section': section_name,
            'image_cache': True,
            'can_create': False,
            'can_delete': False,
        }
        result.update(_memory_dict_page(records, len(items), offset, limit, list_options, image_cache=True))
        return _ok(result, revision)
    if section_name == KNOWLEDGE_SEARCH_SECTION and not isinstance(value, dict):
        return _ok({
            'scope': 'global',
            'section': section_name,
            'kind': 'invalid',
            'editable': False,
            'can_create': False,
            'can_delete': False,
            'warning': '知识搜索结构异常：该区块应为对象，请先修正记忆 JSON 文件。',
            'metadata': _memory_metadata(value),
            'entries': [],
            'total': 0,
            'offset': offset,
            'next_offset': offset,
            'has_more': False,
        }, revision)
    if isinstance(value, dict):
        list_options = _memory_list_options(payload)
        items = list(value.items())
        records_factory = (
            _relationship_memory_records
            if section_name == RELATIONSHIP_SECTION
            else _dict_memory_records
        )
        records = _prepare_memory_records(records_factory(items), list_options)
        result = {
            'scope': 'global',
            'section': section_name,
            'editable': section_editable,
            'can_create': section_editable and section_name in MEMORY_ENTRY_SECTIONS,
            'can_delete': section_editable and section_name in MEMORY_ENTRY_SECTIONS,
        }
        result.update(_memory_dict_page(records, len(items), offset, limit, list_options))
        for entry in result.get('entries', []):
            entry['deletable'] = bool(result['can_delete'])
            if not section_editable:
                entry['editable'] = False
        if section_name == RELATIONSHIP_SECTION:
            result['relationship'] = True
        return _ok(result, revision)

    text, truncated = _display_memory_value(value)
    return _ok({
        'scope': 'global',
        'section': section_name,
        'kind': 'text',
        'value': text,
        'value_kind': type(value).__name__,
        'truncated': truncated,
        'editable': section_editable and not truncated,
        'can_create': False,
        'can_delete': False,
    }, revision)


def _memory_json_size(value):
    try:
        return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    except (TypeError, ValueError):
        raise WebUIError('validation_error', '记忆内容必须是可序列化的 JSON 值')


def _validate_memory_name(value, field, label):
    if not isinstance(value, str) or not value or len(value) > MEMORY_NAME_LIMIT:
        raise WebUIError(
            'validation_error',
            f'{label} 无效',
            {field: f'必须是 1 到 {MEMORY_NAME_LIMIT} 个字符的文本'},
        )
    return value


def _validate_memory_text(value, field='value'):
    if not isinstance(value, str):
        raise WebUIError('validation_error', '记忆文本必须是字符串', {field: '必须是字符串'})
    if _memory_json_size(value) > MEMORY_WRITE_VALUE_LIMIT:
        raise WebUIError('validation_error', '记忆文本过大', {field: '内容不能超过 256 KiB'})
    return value


def _validate_memory_value(value, field='value'):
    if _memory_json_size(value) > MEMORY_WRITE_VALUE_LIMIT:
        raise WebUIError('validation_error', '记忆条目过大', {field: '内容不能超过 256 KiB'})
    return copy.deepcopy(value)


def _validate_memory_entries(entries):
    if not isinstance(entries, list) or not entries:
        raise WebUIError('validation_error', 'entries 必须是非空列表', {'entries': '至少提交一个条目'})
    if len(entries) > MEMORY_ENTRY_LIMIT:
        raise WebUIError('validation_error', '提交的条目过多', {'entries': f'最多 {MEMORY_ENTRY_LIMIT} 条'})
    result = []
    seen = set()
    total_size = 0
    for index, entry in enumerate(entries):
        field = f'entries.{index}'
        if not isinstance(entry, dict):
            raise WebUIError('validation_error', '记忆条目格式无效', {field: '必须是对象'})
        key = _validate_memory_name(entry.get('key'), field + '.key', '记忆条目键')
        if key in seen:
            raise WebUIError('validation_error', '记忆条目重复', {field + '.key': '不能重复提交同一个键'})
        if 'value' not in entry:
            raise WebUIError('validation_error', '记忆条目缺少 value', {field + '.value': '必须有 value'})
        value = _validate_memory_value(entry['value'], field + '.value')
        total_size += _memory_json_size(value)
        if total_size > MEMORY_WRITE_BYTES_LIMIT:
            raise WebUIError('validation_error', '提交的记忆内容过大', {'entries': '总大小不能超过 512 KiB'})
        seen.add(key)
        result.append((key, value))
    return result


def _validate_image_cache_entries(entries):
    if not isinstance(entries, list) or not entries:
        raise WebUIError('validation_error', 'entries 必须是非空列表', {'entries': '至少提交一个条目'})
    if len(entries) > MEMORY_ENTRY_LIMIT:
        raise WebUIError('validation_error', '提交的条目过多', {'entries': f'最多 {MEMORY_ENTRY_LIMIT} 条'})
    result = []
    seen = set()
    total_size = 0
    expected_fields = set(IMAGE_CACHE_FIELDS)
    for index, entry in enumerate(entries):
        field = f'entries.{index}'
        if not isinstance(entry, dict):
            raise WebUIError('validation_error', '图片缓存条目格式无效', {field: '必须是对象'})
        key = _validate_memory_name(entry.get('key'), field + '.key', '图片缓存条目键')
        if key in seen:
            raise WebUIError('validation_error', '图片缓存条目重复', {field + '.key': '不能重复提交同一个键'})
        value = entry.get('value')
        if not isinstance(value, dict):
            raise WebUIError('validation_error', '图片缓存字段格式无效', {field + '.value': '必须是对象'})
        received_fields = set(value.keys())
        if received_fields != expected_fields:
            raise WebUIError(
                'validation_error',
                '图片缓存字段必须固定为 content、intent、type',
                {field + '.value': '只能提交 content、intent、type 三个字符串字段'},
            )
        clean_value = {}
        for item in IMAGE_CACHE_FIELDS:
            text = value[item]
            if not isinstance(text, str):
                raise WebUIError(
                    'validation_error',
                    '图片缓存字段必须是字符串',
                    {field + '.value.' + item: '必须是字符串'},
                )
            if len(text) > IMAGE_CACHE_FIELD_LIMIT:
                raise WebUIError(
                    'validation_error',
                    '图片缓存字段过长',
                    {field + '.value.' + item: f'不能超过 {IMAGE_CACHE_FIELD_LIMIT} 个字符'},
                )
            clean_value[item] = text
        total_size += _memory_json_size(clean_value)
        if total_size > MEMORY_WRITE_BYTES_LIMIT:
            raise WebUIError('validation_error', '提交的图片缓存内容过大', {'entries': '总大小不能超过 512 KiB'})
        seen.add(key)
        result.append((key, clean_value))
    return result


def _handle_memory_image(payload, Proc):
    bot_hash, _bot_info = _require_bot(Proc, payload)
    filename = payload.get('filename')
    if not isinstance(filename, str) or not filename or len(filename) > MEMORY_NAME_LIMIT:
        raise WebUIError('validation_error', 'filename 无效')
    variant = payload.get('variant', 'thumbnail')
    if variant not in ('thumbnail', 'original'):
        raise WebUIError('validation_error', 'variant 必须是 thumbnail 或 original')

    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = OlivOSAIChatAssassin.data.gData.getMemory(bot_hash)
        global_memory = memory.get(GLOBAL_MEMORY_KEY, {}) if isinstance(memory, dict) else {}
        image_cache = global_memory.get(IMAGE_CACHE_SECTION, {}) if isinstance(global_memory, dict) else {}
        if not isinstance(image_cache, dict) or filename not in image_cache:
            raise WebUIError('not_found', '图片缓存档案不存在')

    file_info, path = _image_cache_file_info(filename)
    if file_info['status'] == 'invalid' or path is None:
        raise WebUIError('image_invalid', '图片缓存文件路径无效')
    if file_info['status'] == 'missing':
        raise WebUIError('image_missing', '缓存图片文件已删除')
    if file_info['status'] == 'unavailable':
        raise WebUIError('image_unreadable', '缓存图片文件无法读取')
    if file_info['size'] > MAX_IMAGE_SOURCE_BYTES:
        raise WebUIError('image_too_large', '缓存图片过大，无法读取')

    output_size = None
    actual_variant = variant
    if variant == 'thumbnail':
        thumbnail = _make_image_thumbnail(path)
        if thumbnail is not None:
            data, mime, output_size = thumbnail
        else:
            data, mime = _read_image_cache_file(path, file_info['size'])
            actual_variant = 'original'
    else:
        data, mime = _read_image_cache_file(path, file_info['size'])

    result = {
        'filename': filename,
        'variant': actual_variant,
        'mime': mime,
        'bytes': len(data),
        'source_bytes': file_info['size'],
        'data_url': f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
    }
    if output_size is not None:
        result['width'] = output_size[0]
        result['height'] = output_size[1]
    return _ok(result)


def _handle_save_memory(payload, Proc):
    bot_hash, _bot_info = _require_bot(Proc, payload)
    expected_revision = payload.get('expected_revision')
    if not isinstance(expected_revision, str) or not expected_revision:
        raise WebUIError(
            'validation_error',
            'expected_revision 无效',
            {'expected_revision': '必须是当前记忆 revision'},
        )

    action = payload.get('action', 'update')
    if not isinstance(action, str) or action not in ('update', 'create', 'delete'):
        raise WebUIError(
            'validation_error',
            'action 无效',
            {'action': '必须是 update、create 或 delete'},
        )
    scope = payload.get('scope')
    has_value = 'value' in payload
    has_entries = 'entries' in payload
    group_id = None
    section_name = None
    memory_value = None
    entries = None
    entry_key = None
    if scope == 'group':
        group_id = _validate_memory_name(payload.get('group_id'), 'group_id', 'group_id')
        if action in ('create', 'delete') and group_id == GLOBAL_MEMORY_KEY:
            raise WebUIError(
                'validation_error',
                '群记忆键不能使用保留名称',
                {'group_id': f'不能使用 {GLOBAL_MEMORY_KEY}'},
            )
        if action == 'delete':
            if has_value or has_entries:
                raise WebUIError('validation_error', '删除群记忆时不能提交 value 或 entries')
        else:
            if has_entries or not has_value:
                raise WebUIError('validation_error', '群记忆必须提交 value')
            memory_value = (
                _validate_memory_text(payload.get('value'))
                if action == 'create'
                else _validate_memory_value(payload.get('value'))
            )
    elif scope == 'global':
        section_name = _validate_memory_name(payload.get('section'), 'section', 'section')
        if section_name not in MEMORY_SPECIAL_SECTIONS:
            raise WebUIError(
                'read_only',
                '该分类在 WebUI 中只读，请直接修改记忆 JSON 文件',
                {'section': '该分类不支持 WebUI 编辑'},
            )
        if action in ('create', 'delete') and section_name == IMAGE_CACHE_SECTION:
            raise WebUIError(
                'unsupported_action',
                '图片缓存仅支持修改已有档案，不支持新增或删除',
                {'section': '图片缓存档案只能修改 content、intent 和 type'},
            )
        if action in ('create', 'delete') and section_name not in MEMORY_ENTRY_SECTIONS:
            raise WebUIError(
                'unsupported_action',
                '该记忆区块不支持条目级新增或删除',
                {'section': '只有用户侧写、人物关系、知识缓存和知识搜索支持新增或删除条目'},
            )
        if action == 'delete':
            if has_value or has_entries:
                raise WebUIError('validation_error', '删除记忆条目时不能提交 value 或 entries')
            entry_key = _validate_memory_name(payload.get('key'), 'key', '记忆条目键')
        elif action == 'create':
            if has_entries or not has_value:
                raise WebUIError('validation_error', '新增记忆条目必须提交 value')
            entry_key = _validate_memory_name(payload.get('key'), 'key', '记忆条目键')
            memory_value = (
                _validate_memory_text(payload.get('value'))
                if section_name in TEXT_ENTRY_SECTIONS
                else _validate_memory_value(payload.get('value'))
            )
        elif section_name == IMAGE_CACHE_SECTION:
            if has_value or not has_entries:
                raise WebUIError('validation_error', '图片缓存必须按条目提交 entries')
            entries = _validate_image_cache_entries(payload.get('entries'))
        else:
            if has_value == has_entries:
                raise WebUIError('validation_error', '全局记忆必须且只能提交 value 或 entries')
            if has_value:
                memory_value = _validate_memory_value(payload.get('value'))
            else:
                entries = _validate_memory_entries(payload.get('entries'))
    else:
        raise WebUIError('validation_error', 'scope 必须是 global 或 group')

    def apply(memory):
        if not isinstance(memory, dict):
            raise WebUIError('memory_invalid', '记忆文件根节点必须是对象')
        if scope == 'group':
            if action == 'delete':
                if group_id not in memory:
                    raise WebUIError('not_found', '该群记忆不存在')
                del memory[group_id]
                return
            if action == 'create':
                if group_id in memory:
                    raise WebUIError('already_exists', '该群记忆已存在')
                memory[group_id] = memory_value
                return
            if group_id not in memory:
                raise WebUIError('not_found', '该群记忆不存在')
            current = memory[group_id]
            if isinstance(current, str) and not isinstance(memory_value, str):
                raise WebUIError('validation_error', '群记忆当前是文本，不能保存为其他类型', {'value': '必须是字符串'})
            memory[group_id] = memory_value
            return

        global_memory = memory.get(GLOBAL_MEMORY_KEY)
        if action == 'delete':
            if not isinstance(global_memory, dict) or section_name not in global_memory:
                raise WebUIError('not_found', '该全局记忆区块不存在')
            current = global_memory[section_name]
            if not isinstance(current, dict):
                raise WebUIError('memory_invalid', '该记忆区块结构异常：应为对象')
            if entry_key not in current:
                raise WebUIError('not_found', f'记忆条目不存在: {entry_key}')
            del current[entry_key]
            return
        if action == 'create':
            if not isinstance(global_memory, dict):
                if GLOBAL_MEMORY_KEY in memory:
                    raise WebUIError('memory_invalid', '全局记忆结构异常：应为对象')
                global_memory = {}
                memory[GLOBAL_MEMORY_KEY] = global_memory
            if section_name in global_memory:
                current = global_memory[section_name]
                if not isinstance(current, dict):
                    raise WebUIError('memory_invalid', '该记忆区块结构异常：应为对象')
            else:
                current = {}
                global_memory[section_name] = current
            if entry_key in current:
                raise WebUIError('already_exists', f'记忆条目已存在: {entry_key}')
            current[entry_key] = memory_value
            return
        if not isinstance(global_memory, dict) or section_name not in global_memory:
            raise WebUIError('not_found', '该全局记忆区块不存在')
        current = global_memory[section_name]
        if isinstance(current, dict):
            if entries is None:
                raise WebUIError('validation_error', '该记忆区块是字典，请提交 entries')
            for key, value in entries:
                if key not in current:
                    raise WebUIError('not_found', f'记忆条目不存在: {key}')
                if section_name == IMAGE_CACHE_SECTION:
                    previous = current[key]
                    merged_value = copy.deepcopy(previous) if isinstance(previous, dict) else {}
                    merged_value.update(value)
                    current[key] = merged_value
                else:
                    current[key] = value
            return
        if section_name == IMAGE_CACHE_SECTION:
            raise WebUIError('memory_invalid', '图片缓存档案结构异常：该区块应为对象')
        if section_name == KNOWLEDGE_SEARCH_SECTION:
            raise WebUIError('memory_invalid', '知识搜索结构异常：该区块应为对象，请先修正记忆 JSON 文件')
        if not has_value:
            raise WebUIError('validation_error', '该记忆区块是文本，请提交 value')
        if isinstance(current, str) and not isinstance(memory_value, str):
            raise WebUIError('validation_error', '文本记忆区块只能保存字符串', {'value': '必须是字符串'})
        global_memory[section_name] = memory_value

    try:
        _memory, revision = OlivOSAIChatAssassin.data.gData.update_bot_memory(
            bot_hash,
            expected_revision,
            apply,
        )
    except OlivOSAIChatAssassin.load.DataRevisionConflict as exc:
        raise WebUIError(
            'conflict',
            '记忆已被其他操作修改，请刷新记忆后重试',
            {'current_revision': exc.current_revision},
        )
    result = {'scope': scope, 'action': action}
    if group_id is not None:
        result['group_id'] = group_id
    if section_name is not None:
        result['section'] = section_name
    if entry_key is not None:
        result['key'] = entry_key
    return _ok(result, revision)


_HANDLERS = {
    EVENT_BOOTSTRAP: _handle_bootstrap,
    EVENT_GET_CONFIG: _handle_get_config,
    EVENT_SAVE_CONFIG: _handle_save_config,
    EVENT_MEMORY_OVERVIEW: _handle_memory_overview,
    EVENT_MEMORY_SECTION: _handle_memory_section,
    EVENT_MEMORY_IMAGE: _handle_memory_image,
    EVENT_SAVE_MEMORY: _handle_save_memory,
}


def _dispatch(event_name, payload, Proc):
    handler = _HANDLERS.get(event_name)
    if handler is None:
        raise WebUIError('unknown_event', '未知的 WebUI 事件')
    return handler(_as_payload(payload), Proc)


def handle_menu(plugin_event, Proc):
    event_data = getattr(plugin_event, 'data', None)
    if getattr(event_data, 'namespace', None) != NAMESPACE:
        return False
    webui_context = getattr(event_data, 'webui', None)
    if not isinstance(webui_context, dict):
        return False
    request_id = webui_context.get('request_id')
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        return False

    event_name = getattr(event_data, 'event', None)
    payload = getattr(event_data, 'payload', None)
    try:
        response = _dispatch(event_name, payload, Proc)
    except WebUIError as exc:
        response = _error(exc.code, exc.message, exc.fields)
    except Exception as exc:
        OlivOSAIChatAssassin.logger.warn(f'WEBUI - INTERNAL ERROR - {type(exc).__name__}')
        response = _error('internal_error', '插件处理请求时发生内部错误')
    try:
        plugin_event.send('webui', request_id, response)
    except Exception as exc:
        OlivOSAIChatAssassin.logger.warn(f'WEBUI - REPLY ERROR - {type(exc).__name__}')
    return True
