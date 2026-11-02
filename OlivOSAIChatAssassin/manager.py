import copy
import json
import os
import re
import threading

import OlivOSAIChatAssassin


POWER_ON = 'on'
POWER_OFF = 'off'
VALID_MODES = (POWER_ON, POWER_OFF)
DEFAULT_COMMAND_PREFIXES = ('.', '。', '/')
DEFAULT_CONTROL_ENABLED = False

_COMMAND_RE = re.compile(r'^power[ \t]+(on|off|clear|status)$', re.IGNORECASE)
_SELF_MENTION_RE = re.compile(
    r'^\[OP:at,id=([^,\]]+)(?:,[^\]]*)?\][ \t]*',
    re.IGNORECASE,
)

_lock = threading.RLock()
_cache_path = ''
_cache_revision = None
_cache_document = {'version': 1, 'bots': {}}
_invalid_default_warned = {}


def _empty_document():
    return {'version': 1, 'bots': {}}


def _power_path():
    path = str(getattr(OlivOSAIChatAssassin.data, 'gPowerPath', '') or '')
    return os.path.abspath(path) if path else ''


def _revision_from_path(path):
    try:
        file_stat = os.stat(path)
        return f'{file_stat.st_mtime_ns}:{file_stat.st_size}'
    except OSError:
        return ''


def _log(message):
    try:
        OlivOSAIChatAssassin.logger.log(message)
    except Exception:
        pass


def _warn(message):
    try:
        OlivOSAIChatAssassin.logger.warn(message)
    except Exception:
        pass


def _normalize_document(raw):
    if not isinstance(raw, dict):
        _warn(f'POWER - INVALID DOC [{type(raw).__name__}]')
        return _empty_document()

    document = copy.deepcopy(raw)
    if 'version' not in document:
        document['version'] = 1
    bots = document.get('bots')
    if bots is None:
        bots = {}
    elif not isinstance(bots, dict):
        _warn('POWER - INVALID DOC [bots]')
        bots = {}

    normalized_bots = {}
    for bot_hash, group_states in bots.items():
        if not isinstance(bot_hash, str) or not bot_hash or not isinstance(group_states, dict):
            _warn('POWER - INVALID DOC [bot entry]')
            continue
        normalized_groups = {}
        for group_id, state in group_states.items():
            if not isinstance(group_id, str) or not group_id:
                _warn('POWER - INVALID DOC [group id]')
                continue
            if not isinstance(state, str) or state.lower() not in VALID_MODES:
                _warn(f'POWER - INVALID DOC [state={group_id}]')
                continue
            normalized_groups[group_id] = state.lower()
        normalized_bots[bot_hash] = normalized_groups
    document['bots'] = normalized_bots
    return document


def _read_disk_unlocked(path):
    if not path or not os.path.exists(path):
        return _empty_document(), ''
    try:
        with open(path, 'r', encoding='utf-8') as file_obj:
            raw = json.load(file_obj)
    except FileNotFoundError:
        return _empty_document(), ''
    except Exception as exc:
        _warn(f'POWER - LOAD FAILED [{type(exc).__name__}]')
        return _empty_document(), _revision_from_path(path)
    return _normalize_document(raw), _revision_from_path(path)


def _ensure_loaded_unlocked():
    global _cache_path, _cache_revision, _cache_document
    path = _power_path()
    revision = _revision_from_path(path)
    if path == _cache_path and revision == _cache_revision:
        return _cache_document
    document, loaded_revision = _read_disk_unlocked(path)
    _cache_path = path
    _cache_revision = loaded_revision
    _cache_document = document
    return _cache_document


def reset():
    global _cache_path, _cache_revision, _cache_document, _invalid_default_warned
    with _lock:
        _cache_path = ''
        _cache_revision = None
        _cache_document = _empty_document()
        _invalid_default_warned = {}


def reload():
    with _lock:
        _ensure_loaded_unlocked()
        return copy.deepcopy(_cache_document)


def _default_mode(bot_hash, config):
    if not isinstance(config, dict):
        config = {}
    value = config.get(
        'power_default_mode',
        OlivOSAIChatAssassin.data.configDefault['power_default_mode'],
    )
    if value in VALID_MODES:
        _invalid_default_warned.pop(str(bot_hash), None)
        return value
    warned_value = repr(value)
    if _invalid_default_warned.get(str(bot_hash)) != warned_value:
        _invalid_default_warned[str(bot_hash)] = warned_value
        _warn(f'POWER - INVALID DEFAULT [{bot_hash}] -> on')
    return POWER_ON


def _resolve_config(bot_hash, config):
    if isinstance(config, dict):
        return config
    try:
        return OlivOSAIChatAssassin.data.gData.getConfig(str(bot_hash))
    except Exception:
        return {}


def control_enabled(config=None, bot_hash=None):
    config = _resolve_config(bot_hash, config)
    value = config.get('power_control_enabled', DEFAULT_CONTROL_ENABLED)
    return value if isinstance(value, bool) else DEFAULT_CONTROL_ENABLED


def command_prefixes(config=None):
    if not isinstance(config, dict):
        config = {}
    default = OlivOSAIChatAssassin.data.configDefault.get(
        'command_prefixes',
        DEFAULT_COMMAND_PREFIXES,
    )
    raw_prefixes = config.get('command_prefixes', default)
    if not isinstance(raw_prefixes, (list, tuple)):
        raw_prefixes = default
    prefixes = []
    seen = set()
    for item in raw_prefixes:
        if not isinstance(item, str):
            continue
        prefix = item.strip()
        if not prefix or prefix in seen:
            continue
        seen.add(prefix)
        prefixes.append(prefix)
    return prefixes


def state(bot_hash, group_id, config=None):
    bot_hash = str(bot_hash)
    group_id = str(group_id)
    config = _resolve_config(bot_hash, config)
    with _lock:
        document = _ensure_loaded_unlocked()
        override = document.get('bots', {}).get(bot_hash, {}).get(group_id)
    if override in VALID_MODES:
        return {'mode': override, 'source': 'override'}
    return {'mode': _default_mode(bot_hash, config), 'source': 'default'}


def group_status(bot_hash, group_id, config=None):
    config = _resolve_config(bot_hash, config)
    current = state(bot_hash, group_id, config)
    control = control_enabled(config, bot_hash)
    return {
        'control_enabled': control,
        'mode': current.get('mode'),
        'source': current.get('source'),
        'effective_enabled': True if not control else current.get('mode') == POWER_ON,
        'default_mode': _default_mode(bot_hash, config),
    }


def is_enabled(bot_hash, group_id, config=None):
    config = _resolve_config(bot_hash, config)
    if not control_enabled(config, bot_hash):
        return True
    return state(bot_hash, group_id, config).get('mode') == POWER_ON


def set_override(bot_hash, group_id, mode=None):
    if mode is not None and mode not in VALID_MODES:
        return False, 'invalid_mode'
    bot_hash = str(bot_hash)
    group_id = str(group_id)
    if not bot_hash or not group_id:
        return False, 'invalid_target'

    global _cache_path, _cache_revision, _cache_document
    with _lock:
        path = _power_path()
        current_revision = _revision_from_path(path)
        if path == _cache_path and current_revision == _cache_revision:
            document = copy.deepcopy(_cache_document)
        else:
            document, current_revision = _read_disk_unlocked(path)
        bots = document.setdefault('bots', {})
        if not isinstance(bots, dict):
            bots = {}
            document['bots'] = bots
        if mode is None:
            group_states = bots.get(bot_hash)
            if isinstance(group_states, dict):
                group_states.pop(group_id, None)
                if not group_states:
                    bots.pop(bot_hash, None)
        else:
            group_states = bots.get(bot_hash)
            if not isinstance(group_states, dict):
                group_states = {}
                bots[bot_hash] = group_states
            group_states[group_id] = mode
        try:
            OlivOSAIChatAssassin.load.DataManager._write_json_atomic(path, document)
        except Exception as exc:
            _warn(f'POWER - WRITE FAILED [{type(exc).__name__}]')
            return False, 'write_failed'
        _cache_path = path
        _cache_revision = _revision_from_path(path)
        _cache_document = document
    return True, ''


def set_group_enabled(bot_hash, group_id, enabled, config=None):
    if type(enabled) is not bool:
        return False, 'invalid_enabled'
    config = _resolve_config(bot_hash, config)
    if not control_enabled(config, bot_hash):
        return False, 'control_disabled'
    return set_override(bot_hash, group_id, POWER_ON if enabled else POWER_OFF)


def parse_command(message, self_id, config=None):
    if not isinstance(message, str):
        return None
    text = message.strip()
    self_id_text = str(self_id or '').strip()
    if self_id_text:
        matched = _SELF_MENTION_RE.match(text)
        if matched and str(matched.group(1)).strip() == self_id_text:
            text = text[matched.end():].strip()
    matched_prefix = None
    for prefix in sorted(command_prefixes(config), key=len, reverse=True):
        if text.startswith(prefix):
            matched_prefix = prefix
            break
    if matched_prefix is None:
        return None
    text = text[len(matched_prefix):].lstrip(' \t')
    matched = _COMMAND_RE.fullmatch(text)
    return matched.group(1).lower() if matched else None


def permission(plugin_event):
    event_data = getattr(plugin_event, 'data', None)
    sender = getattr(event_data, 'sender', None)
    if not isinstance(sender, dict):
        return False, 'permission_unknown'
    if sender.get('is_owner') is True or sender.get('is_admin') is True:
        return True, ''
    if 'role' not in sender:
        return True, 'permission_open_no_role'
    role = sender.get('role')
    if isinstance(role, str) and role.strip().lower() in ('owner', 'admin'):
        return True, ''
    if isinstance(role, str) and role.strip():
        return False, 'permission_denied'
    return False, 'permission_unknown'


def _status_text(state_value):
    label = '开启' if state_value.get('mode') == POWER_ON else '关闭'
    source = '群覆盖' if state_value.get('source') == 'override' else '默认状态'
    return f'群聊刺客：当前为{label}（{source}）。'


def handle_command(plugin_event, group_id, message, config=None):
    bot_info = getattr(plugin_event, 'bot_info', None)
    bot_hash = str(getattr(bot_info, 'hash', ''))
    if not control_enabled(config, bot_hash):
        return None
    base_info = getattr(plugin_event, 'base_info', None)
    if not isinstance(base_info, dict):
        base_info = {}
    action = parse_command(message, base_info.get('self_id'), config)
    if action is None:
        return None

    allowed, reason = permission(plugin_event)
    if not allowed:
        _log(f'POWER - IGNORE [{reason}]')
        return None

    if action == 'status':
        current = state(bot_hash, group_id, config)
        response = _status_text(current)
        ok = True
        error = ''
    else:
        mode = action if action in VALID_MODES else None
        ok, error = set_override(bot_hash, group_id, mode)
        if ok:
            current = state(bot_hash, group_id, config)
            if action == 'clear':
                label = '开启' if current.get('mode') == POWER_ON else '关闭'
                response = f'群聊刺客：已清除当前群覆盖，恢复默认{label}。'
            else:
                label = '开启' if mode == POWER_ON else '关闭'
                response = f'群聊刺客：当前群已{label}。'
        else:
            response = '群聊刺客：运行状态更新失败。'

    try:
        plugin_event.reply(response)
    except Exception as exc:
        _warn(f'POWER - REPLY FAILED [{type(exc).__name__}]')
    if ok:
        current = state(bot_hash, group_id, config)
        _log(
            f'POWER - {action.upper()} [{current.get("mode")}/{current.get("source")}]'
        )
    else:
        _warn(f'POWER - {action.upper()} FAILED [{error}]')
    return {
        'handled': True,
        'ok': ok,
        'action': action,
        'response': response,
        'reason': error,
    }
