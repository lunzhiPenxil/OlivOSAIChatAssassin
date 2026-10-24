import copy
import json
import math
import os
from urllib.parse import urlsplit

import OlivOSAIChatAssassin


NAMESPACE = 'OlivOSAIChatAssassin'
API_VERSION = 1

EVENT_BOOTSTRAP = 'OlivOSAIChatAssassin_WebUI_Bootstrap'
EVENT_GET_CONFIG = 'OlivOSAIChatAssassin_WebUI_GetConfig'
EVENT_SAVE_CONFIG = 'OlivOSAIChatAssassin_WebUI_SaveConfig'
EVENT_MEMORY_OVERVIEW = 'OlivOSAIChatAssassin_WebUI_GetMemoryOverview'
EVENT_MEMORY_SECTION = 'OlivOSAIChatAssassin_WebUI_GetMemorySection'

SECRET_PATHS = ('api_key', 'ocr_api.api_key')
GLOBAL_MEMORY_KEY = '全局'
MEMORY_GROUP_LIMIT = 200
MEMORY_ENTRY_LIMIT = 100
MEMORY_BYTES_LIMIT = 128 * 1024
MEMORY_VALUE_LIMIT = 20000
MAX_REQUEST_BYTES = 1024 * 1024
WEBUI_ASSET_FILES = ('webui/index.html',)

_manifest_cache = None
_webui_asset_cache = None


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


def _validate_string(value, field, errors, allow_empty=False, max_length=100000, url=False):
    if not isinstance(value, str):
        _add_field_error(errors, field, '必须是字符串')
        return None
    if not allow_empty and not value.strip():
        _add_field_error(errors, field, '不能为空')
        return None
    if len(value) > max_length:
        _add_field_error(errors, field, f'不能超过 {max_length} 个字符')
        return None
    if url:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            _add_field_error(errors, field, '必须是 http 或 https 地址')
            return None
    return value


def _validate_number(value, field, errors, minimum, maximum, integer=False):
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
    if value < minimum or value > maximum:
        _add_field_error(errors, field, f'必须在 {minimum} 到 {maximum} 之间')
        return None
    return value


def _validate_bool(value, field, errors):
    if type(value) is not bool:
        _add_field_error(errors, field, '必须是布尔值')
        return None
    return value


def _validate_string_list(value, field, errors):
    if not isinstance(value, list) or len(value) > 1000:
        _add_field_error(errors, field, '必须是最多 1000 项的字符串列表')
        return None
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or len(item) > 256:
            _add_field_error(errors, f'{field}.{index}', '必须是非空字符串且不超过 256 字符')
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
            checked = _validate_string(item, field, errors, allow_empty=True, max_length=4096)
            if checked is not None and checked:
                result[key] = checked
        elif key == 'api_base':
            checked = _validate_string(item, field, errors, max_length=2048, url=True)
            if checked is not None:
                result[key] = checked
        elif key == 'model':
            checked = _validate_string(item, field, errors, max_length=256)
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
            checked = _validate_number(item, field, errors, 1, 128, integer=True)
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
            checked = _validate_string(value, key, errors, allow_empty=True, max_length=4096)
            if checked is not None and checked:
                result[key] = checked
        elif key == 'api_base':
            checked = _validate_string(value, key, errors, max_length=2048, url=True)
            if checked is not None:
                result[key] = checked
        elif key == 'model':
            checked = _validate_string(value, key, errors, max_length=256)
            if checked is not None:
                result[key] = checked
        elif key == 'max_tokens':
            checked = _validate_number(value, key, errors, 1, 1000000, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'temperature':
            checked = _validate_number(value, key, errors, 0, 2)
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
            checked = _validate_number(value, key, errors, 0, 86400)
            if checked is not None:
                result[key] = checked
        elif key == 'personality':
            checked = _validate_string(value, key, errors, allow_empty=True, max_length=100000)
            if checked is not None:
                result[key] = checked
        elif key == 'record_knowledge':
            checked = _validate_bool(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'search_knowledge_deepin':
            checked = _validate_number(value, key, errors, 0, 16, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'retry_count':
            checked = _validate_number(value, key, errors, 1, 10, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'enabled_groups':
            checked = _validate_string_list(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'search_ageing':
            checked = _validate_number(value, key, errors, 1, 2592000)
            if checked is not None:
                result[key] = checked
        elif key == 'history_size_min':
            checked = _validate_number(value, key, errors, 1, 1000, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'history_size':
            checked = _validate_number(value, key, errors, 1, 1000, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'history_dynamic':
            checked = _validate_bool(value, key, errors)
            if checked is not None:
                result[key] = checked
        elif key == 'history_dynamic_size':
            checked = _validate_number(value, key, errors, 1, 10000, integer=True)
            if checked is not None:
                result[key] = checked
        elif key == 'slack_time':
            checked = _validate_number(value, key, errors, 0, 300)
            if checked is not None:
                result[key] = checked
        elif key == 'slack_cooldown_time':
            checked = _validate_number(value, key, errors, 0, 3600)
            if checked is not None:
                result[key] = checked
        elif key == 'reply_probability':
            checked = _validate_number(value, key, errors, 0, 1)
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
            checked = _validate_number(value, key, errors, 1, 1000000, integer=True)
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
            'memory_write': False,
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


def _handle_memory_overview(payload, Proc):
    bot_hash, _bot_info = _require_bot(Proc, payload)
    memory, revision = OlivOSAIChatAssassin.data.gData.snapshot_memory(bot_hash)
    if not isinstance(memory, dict):
        raise WebUIError('memory_invalid', '记忆文件根节点必须是对象')

    global_memory = memory.get(GLOBAL_MEMORY_KEY, {})
    global_sections = []
    if isinstance(global_memory, dict):
        for name in sorted(global_memory.keys(), key=str):
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

    groups = []
    group_keys = [key for key in memory.keys() if key != GLOBAL_MEMORY_KEY]
    group_keys.sort(key=str)
    for group_id in group_keys[:MEMORY_GROUP_LIMIT]:
        value = memory[group_id]
        text, truncated = _display_memory_value(value, limit=200)
        groups.append({
            'group_id': str(group_id),
            'preview': text,
            'length': len(text),
            'truncated': truncated,
        })
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


def _slice_entries(items, offset, limit, metadata_only=False):
    entries = []
    used_bytes = 0
    for key, value in items[offset:]:
        if len(entries) >= limit:
            break
        if metadata_only:
            entry = {'key': str(key)}
            entry.update(_memory_metadata(value))
        else:
            text, truncated = _display_memory_value(value)
            entry = {
                'key': str(key),
                'value': text,
                'truncated': truncated,
            }
        entry_size = len(json.dumps(entry, ensure_ascii=False).encode('utf-8'))
        if entries and used_bytes + entry_size > MEMORY_BYTES_LIMIT:
            break
        entries.append(entry)
        used_bytes += entry_size
    return entries


def _handle_memory_section(payload, Proc):
    bot_hash, _bot_info = _require_bot(Proc, payload)
    scope = payload.get('scope')
    offset = _page_number(payload.get('offset'), 0, 0, 1000000)
    limit = _page_number(payload.get('limit'), 50, 1, MEMORY_ENTRY_LIMIT)
    memory, revision = OlivOSAIChatAssassin.data.gData.snapshot_memory(bot_hash)
    if not isinstance(memory, dict):
        raise WebUIError('memory_invalid', '记忆文件根节点必须是对象')

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
            'truncated': truncated,
        }, revision)

    if scope != 'global':
        raise WebUIError('validation_error', 'scope 必须是 global 或 group')
    section_name = payload.get('section')
    if not isinstance(section_name, str) or not section_name or len(section_name) > 256:
        raise WebUIError('validation_error', 'section 无效')
    global_memory = memory.get(GLOBAL_MEMORY_KEY, {})
    if not isinstance(global_memory, dict) or section_name not in global_memory:
        raise WebUIError('not_found', '该全局记忆区块不存在')
    value = global_memory[section_name]
    metadata_only = section_name == '图片缓存'
    if isinstance(value, dict):
        keys = sorted(value.keys(), key=str)
        entries = _slice_entries(
            [(key, value[key]) for key in keys],
            offset,
            limit,
            metadata_only=metadata_only,
        )
        next_offset = offset + len(entries)
        result = {
            'scope': 'global',
            'section': section_name,
            'kind': 'dict',
            'entries': entries,
            'total': len(keys),
            'offset': offset,
            'next_offset': next_offset,
            'has_more': next_offset < len(keys),
        }
        if metadata_only:
            result['metadata_only'] = True
        return _ok(result, revision)
    if metadata_only:
        return _ok({
            'scope': 'global',
            'section': section_name,
            'kind': 'metadata',
            'metadata_only': True,
            'metadata': _memory_metadata(value),
            'entries': [],
            'total': 0,
            'offset': offset,
            'next_offset': offset,
            'has_more': False,
        }, revision)

    text, truncated = _display_memory_value(value)
    return _ok({
        'scope': 'global',
        'section': section_name,
        'kind': 'text',
        'value': text,
        'truncated': truncated,
    }, revision)


_HANDLERS = {
    EVENT_BOOTSTRAP: _handle_bootstrap,
    EVENT_GET_CONFIG: _handle_get_config,
    EVENT_SAVE_CONFIG: _handle_save_config,
    EVENT_MEMORY_OVERVIEW: _handle_memory_overview,
    EVENT_MEMORY_SECTION: _handle_memory_section,
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
