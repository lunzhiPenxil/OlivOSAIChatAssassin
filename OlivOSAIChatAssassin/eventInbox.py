import re
import threading
import time
from collections import deque
from datetime import datetime

import OlivOSAIChatAssassin


EVENT_LIMIT = 200
SNAPSHOT_LIMIT = 30
EVENT_TEXT_LIMIT = 240
EVENT_NAME_LIMIT = 96

_EVENT_META = {
    'private_message': ('私聊消息', 'message'),
    'poke': ('戳一戳', 'interaction'),
    'private_message_recall': ('私聊撤回', 'recall'),
    'group_message_recall': ('群消息撤回', 'recall'),
    'group_member_increase': ('群成员增加', 'group'),
    'group_member_decrease': ('群成员减少', 'group'),
    'group_admin': ('群管理员变动', 'group'),
    'group_ban': ('群禁言', 'group'),
    'group_file_upload': ('群文件上传', 'group'),
}

_MESSAGE_MARKER_RE = re.compile(r'\[(?:CQ|OP):[^\]]*\]', re.IGNORECASE)
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_INVISIBLE_RE = re.compile(r'[\u200b-\u200f\u202a-\u202e\u2066-\u2069]')
_WHITESPACE_RE = re.compile(r'\s+')

_lock = threading.Lock()
_events = {}
_sequence = 0


def _now_iso():
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _clean_text(value, limit):
    if not isinstance(value, str):
        if value is None:
            return ''
        value = str(value)
    text = _MESSAGE_MARKER_RE.sub(' ', value)
    text = _CONTROL_RE.sub(' ', text)
    text = _INVISIBLE_RE.sub('', text)
    text = _WHITESPACE_RE.sub(' ', text).strip()
    if len(text) > limit:
        return text[:max(0, limit - 1)].rstrip() + '…'
    return text


def _id_text(value):
    if value is None:
        return ''
    text = str(value).strip()
    if not text or text == '-1':
        return ''
    return text[:EVENT_NAME_LIMIT]


def _bot_hash(plugin_event):
    bot_info = getattr(plugin_event, 'bot_info', None)
    return _id_text(getattr(bot_info, 'hash', ''))


def _self_id(plugin_event):
    base_info = getattr(plugin_event, 'base_info', None)
    if not isinstance(base_info, dict):
        return ''
    return _id_text(base_info.get('self_id'))


def _get_data(plugin_event):
    return getattr(plugin_event, 'data', None)


def _config(bot_hash):
    manager = OlivOSAIChatAssassin.data.gData
    if manager is None or not bot_hash:
        return {}
    reload_func = getattr(manager, 'reload', None)
    if callable(reload_func):
        try:
            reload_func(bot_hash)
        except Exception as exc:
            OlivOSAIChatAssassin.logger.warn(f'事件接收配置重载失败: {exc}')
    try:
        config = manager.getConfig(bot_hash)
    except Exception as exc:
        OlivOSAIChatAssassin.logger.warn(f'事件接收配置读取失败: {exc}')
        return {}
    return config if isinstance(config, dict) else {}


def _enabled(bot_hash, field):
    config = _config(bot_hash)
    return config.get(
        field,
        OlivOSAIChatAssassin.data.configDefault.get(field, False),
    ) is True


def _sender_name(plugin_event):
    data = _get_data(plugin_event)
    sender = getattr(data, 'sender', None)
    if not isinstance(sender, dict):
        return ''
    return _clean_text(sender.get('nickname') or sender.get('name') or '', EVENT_NAME_LIMIT)


def _append(record):
    global _sequence
    with _lock:
        _sequence += 1
        record['id'] = _sequence
        record['timestamp'] = int(time.time())
        record['at'] = _now_iso()
        bucket = _events.setdefault(record['bot_hash'], deque(maxlen=EVENT_LIMIT))
        bucket.append(record)
    try:
        OlivOSAIChatAssassin.logger.logRaw(
            OlivOSAIChatAssassin.logger.LOG_DEBUG,
            'INBOUND - %s' % record['kind'],
        )
    except Exception:
        pass
    return record


def _record(
    bot_hash,
    kind,
    summary='',
    *,
    scene='',
    group_id='',
    user_id='',
    operator_id='',
    target_id='',
    action='',
    duration=None,
    message_id='',
    file_name='',
    actor_name='',
):
    label, category = _EVENT_META.get(kind, (kind, 'event'))
    return _append({
        'bot_hash': bot_hash,
        'kind': kind,
        'label': label,
        'category': category,
        'scene': scene,
        'group_id': _id_text(group_id),
        'user_id': _id_text(user_id),
        'operator_id': _id_text(operator_id),
        'target_id': _id_text(target_id),
        'action': _clean_text(action, EVENT_NAME_LIMIT),
        'duration': duration if isinstance(duration, (int, float)) else None,
        'message_id': _id_text(message_id),
        'file_name': _clean_text(file_name, EVENT_NAME_LIMIT),
        'actor_name': _clean_text(actor_name, EVENT_NAME_LIMIT),
        'summary': _clean_text(summary, EVENT_TEXT_LIMIT),
    })


def receive_private_message(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not _enabled(bot_hash, 'event_private_message_enabled'):
        return False
    data = _get_data(plugin_event)
    message = _clean_text(getattr(data, 'message', ''), EVENT_TEXT_LIMIT)
    return bool(_record(
        bot_hash,
        'private_message',
        message or '非文本消息',
        scene='private',
        user_id=getattr(data, 'user_id', ''),
        target_id=_self_id(plugin_event),
        message_id=getattr(data, 'message_id', ''),
        actor_name=_sender_name(plugin_event),
    ))


def receive_poke(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not _enabled(bot_hash, 'event_poke_enabled'):
        return False
    data = _get_data(plugin_event)
    group_id = _id_text(getattr(data, 'group_id', ''))
    target_id = _id_text(getattr(data, 'target_id', ''))
    if target_id and target_id == _self_id(plugin_event):
        summary = '戳了戳你'
    elif target_id:
        summary = '戳了戳用户 %s' % target_id
    else:
        summary = '发起了戳一戳'
    return bool(_record(
        bot_hash,
        'poke',
        summary,
        scene='group' if group_id else 'private',
        group_id=group_id,
        user_id=getattr(data, 'user_id', ''),
        target_id=target_id,
        actor_name=_sender_name(plugin_event),
    ))


def receive_private_message_recall(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not (
        _enabled(bot_hash, 'event_recall_enabled') and
        _enabled(bot_hash, 'event_private_message_enabled')
    ):
        return False
    data = _get_data(plugin_event)
    return bool(_record(
        bot_hash,
        'private_message_recall',
        '撤回了一条私聊消息',
        scene='private',
        user_id=getattr(data, 'user_id', ''),
        message_id=getattr(data, 'message_id', ''),
    ))


def receive_group_message_recall(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not _enabled(bot_hash, 'event_recall_enabled'):
        return False
    data = _get_data(plugin_event)
    return bool(_record(
        bot_hash,
        'group_message_recall',
        '撤回了一条群消息',
        scene='group',
        group_id=getattr(data, 'group_id', ''),
        user_id=getattr(data, 'user_id', ''),
        operator_id=getattr(data, 'operator_id', ''),
        message_id=getattr(data, 'message_id', ''),
    ))


def receive_group_member_increase(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not _enabled(bot_hash, 'event_group_advanced_enabled'):
        return False
    data = _get_data(plugin_event)
    action = _clean_text(getattr(data, 'action', ''), EVENT_NAME_LIMIT)
    user_id = _id_text(getattr(data, 'user_id', ''))
    action_label = {
        'approve': '通过申请加入群聊',
        'invite': '被邀请加入群聊',
    }.get(action, '加入群聊')
    return bool(_record(
        bot_hash,
        'group_member_increase',
        '用户 %s %s' % (user_id or '-', action_label),
        scene='group',
        group_id=getattr(data, 'group_id', ''),
        user_id=user_id,
        operator_id=getattr(data, 'operator_id', ''),
        action=action,
    ))


def receive_group_member_decrease(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not _enabled(bot_hash, 'event_group_advanced_enabled'):
        return False
    data = _get_data(plugin_event)
    action = _clean_text(getattr(data, 'action', ''), EVENT_NAME_LIMIT)
    user_id = _id_text(getattr(data, 'user_id', ''))
    action_label = {
        'leave': '主动离开群聊',
        'kick': '被移出群聊',
        'kick_me': '机器人被移出群聊',
    }.get(action, '离开群聊')
    return bool(_record(
        bot_hash,
        'group_member_decrease',
        '用户 %s %s' % (user_id or '-', action_label),
        scene='group',
        group_id=getattr(data, 'group_id', ''),
        user_id=user_id,
        operator_id=getattr(data, 'operator_id', ''),
        action=action,
    ))


def receive_group_admin(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not _enabled(bot_hash, 'event_group_advanced_enabled'):
        return False
    data = _get_data(plugin_event)
    action = _clean_text(getattr(data, 'action', ''), EVENT_NAME_LIMIT)
    user_id = _id_text(getattr(data, 'user_id', ''))
    action_label = '成为管理员' if action == 'set' else '被取消管理员'
    return bool(_record(
        bot_hash,
        'group_admin',
        '用户 %s %s' % (user_id or '-', action_label),
        scene='group',
        group_id=getattr(data, 'group_id', ''),
        user_id=user_id,
        action=action,
    ))


def receive_group_ban(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not _enabled(bot_hash, 'event_group_advanced_enabled'):
        return False
    data = _get_data(plugin_event)
    action = _clean_text(getattr(data, 'action', ''), EVENT_NAME_LIMIT)
    user_id = _id_text(getattr(data, 'user_id', ''))
    duration = getattr(data, 'duration', None)
    if action == 'ban':
        summary = '用户 %s 被禁言 %s 秒' % (user_id or '-', duration or 0)
    else:
        summary = '用户 %s 被解除禁言' % (user_id or '-')
    return bool(_record(
        bot_hash,
        'group_ban',
        summary,
        scene='group',
        group_id=getattr(data, 'group_id', ''),
        user_id=user_id,
        operator_id=getattr(data, 'operator_id', ''),
        action=action,
        duration=duration,
    ))


def receive_group_file_upload(plugin_event, Proc=None):
    bot_hash = _bot_hash(plugin_event)
    if not _enabled(bot_hash, 'event_group_advanced_enabled'):
        return False
    data = _get_data(plugin_event)
    file_obj = getattr(data, 'file', None)
    file_name = file_obj.get('name', '') if isinstance(file_obj, dict) else ''
    user_id = _id_text(getattr(data, 'user_id', ''))
    summary = '用户 %s 上传了群文件' % (user_id or '-')
    if file_name:
        summary += '：%s' % _clean_text(file_name, EVENT_NAME_LIMIT)
    return bool(_record(
        bot_hash,
        'group_file_upload',
        summary,
        scene='group',
        group_id=getattr(data, 'group_id', ''),
        user_id=user_id,
        file_name=file_name,
    ))


def snapshot(bot_hash, limit=SNAPSHOT_LIMIT):
    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = SNAPSHOT_LIMIT
    safe_limit = max(1, min(EVENT_LIMIT, safe_limit))
    with _lock:
        bucket = list(_events.get(_id_text(bot_hash), ()))
    total = len(bucket)
    events = []
    for item in reversed(bucket[-safe_limit:]):
        event = dict(item)
        event.pop('bot_hash', None)
        events.append(event)
    return {
        'events': events,
        'event_count': total,
        'omitted_count': max(0, total - len(events)),
        'retained_limit': EVENT_LIMIT,
    }


def clear_bot(bot_hash):
    with _lock:
        _events.pop(_id_text(bot_hash), None)
    clearer = getattr(getattr(OlivOSAIChatAssassin, 'msg', None), 'clear_poke_state', None)
    if callable(clearer):
        clearer(bot_hash)


def clear_all():
    with _lock:
        _events.clear()
    clearer = getattr(getattr(OlivOSAIChatAssassin, 'msg', None), 'clear_poke_state', None)
    if callable(clearer):
        clearer()
