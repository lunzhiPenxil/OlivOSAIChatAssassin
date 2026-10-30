import math
import re
import time

import OlivOSAIChatAssassin


ACTION_REACTION = 'reaction'
ACTION_POKE = 'poke'

VALID_ACTIONS = (
    ACTION_REACTION,
    ACTION_POKE,
)

ACTION_LABELS = {
    ACTION_REACTION: '表情回应',
    ACTION_POKE: '戳一戳',
}

# 语义名用于模型和界面；编号由 OlivOS 通用反应接口接收。
REACTION_CATALOG = (
    ('like', '赞', '76'),
    ('laugh', '笑', '13'),
    ('tears', '笑哭', '182'),
    ('biglaugh', '大笑', '193'),
    ('doge', '狗头', '179'),
    ('surprise', '惊讶', '0'),
    ('question', '疑问', '32'),
    ('speechless', '无语', '462'),
    ('applause', '鼓掌', '99'),
    ('cheer', '打call', '311'),
    ('fingerheart', '比心', '319'),
    ('heart', '爱心', '66'),
    ('rose', '玫瑰', '63'),
    ('cry', '流泪', '5'),
    ('shy', '害羞', '6'),
    ('hug', '抱抱', '222'),
    ('watermelon', '吃瓜', '271'),
    ('slack', '摸鱼', '285'),
    ('ok', 'OK', '124'),
)

REACTION_LABELS = {
    name: label for name, label, _face_id in REACTION_CATALOG
}

REACTION_FACE_IDS = {
    name: face_id for name, _label, face_id in REACTION_CATALOG
}

FAILURE_COOLDOWN = 60.0
_SAFE_TARGET_RE = re.compile(r'^[^\s\[\],&]{1,160}$')


def _config_list(config, key):
    value = config.get(key, []) if isinstance(config, dict) else []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def config_enabled(config):
    return isinstance(config, dict) and config.get('interaction_enabled') is True


def configured_actions(config):
    allowed = set(VALID_ACTIONS)
    return [item for item in _config_list(config, 'interaction_actions') if item in allowed]


def configured_reactions(config):
    allowed = set(REACTION_LABELS)
    return [item for item in _config_list(config, 'interaction_reactions') if item in allowed]


def cooldown_seconds(config):
    value = config.get(
        'interaction_cooldown',
        OlivOSAIChatAssassin.data.configDefault['interaction_cooldown'],
    ) if isinstance(config, dict) else 0
    if type(value) not in (int, float):
        return float(OlivOSAIChatAssassin.data.configDefault['interaction_cooldown'])
    value = float(value)
    if not math.isfinite(value):
        return float(OlivOSAIChatAssassin.data.configDefault['interaction_cooldown'])
    return max(0.0, value)


def _safe_target(value):
    if value is None:
        return ''
    text = str(value)
    if not _SAFE_TARGET_RE.fullmatch(text):
        return ''
    return text


def _action_target(value):
    text = _safe_target(value)
    if text in ('-1', '0'):
        return ''
    return text


def _message_target(plugin_event):
    data = getattr(plugin_event, 'data', None)
    return _action_target(getattr(data, 'message_id', None))


def _user_target(plugin_event):
    data = getattr(plugin_event, 'data', None)
    return _action_target(getattr(data, 'user_id', None))


def _sender_is_bot(plugin_event):
    data = getattr(plugin_event, 'data', None)
    sender = getattr(data, 'sender', None)
    if not isinstance(sender, dict):
        return False
    return sender.get('is_bot') is True or sender.get('bot') is True


def supports_action(action, plugin_event, group_id):
    if action == ACTION_REACTION:
        return bool(_message_target(plugin_event))
    if action == ACTION_POKE:
        return bool(_action_target(group_id) and _user_target(plugin_event))
    return False


def _state_key(bot_hash, group_id):
    return (str(bot_hash), str(group_id))


def _failure_key(bot_hash, group_id, action):
    return (str(bot_hash), str(group_id), str(action))


def clear_bot(bot_hash):
    bot_hash = str(bot_hash)
    with OlivOSAIChatAssassin.data.gInteractionLock:
        for key in list(OlivOSAIChatAssassin.data.gInteractionLastAttempt):
            if key[0] == bot_hash:
                OlivOSAIChatAssassin.data.gInteractionLastAttempt.pop(key, None)
        for key in list(OlivOSAIChatAssassin.data.gInteractionFailureUntil):
            if key[0] == bot_hash:
                OlivOSAIChatAssassin.data.gInteractionFailureUntil.pop(key, None)


def clear_all():
    with OlivOSAIChatAssassin.data.gInteractionLock:
        OlivOSAIChatAssassin.data.gInteractionLastAttempt.clear()
        OlivOSAIChatAssassin.data.gInteractionFailureUntil.clear()


def _result(action=None, reason='', value='', detail=''):
    return {
        'action': action,
        'reason': reason,
        'value': value,
        'detail': detail,
    }


def select_action(raw_actions, config, plugin_event, group_id):
    "只校验并预留第一个动作，任何不可执行条件都不向后回退。"
    if not config_enabled(config):
        return _result(reason='disabled')
    if not isinstance(raw_actions, list) or not raw_actions:
        return _result(reason='absent')
    raw_action = raw_actions[0]
    if not isinstance(raw_action, dict):
        return _result(reason='invalid_schema')
    action = raw_action.get('t')
    if action not in VALID_ACTIONS or action not in configured_actions(config):
        return _result(reason='not_enabled')
    if _sender_is_bot(plugin_event):
        return _result(reason='bot_sender')

    value = ''
    if action == ACTION_REACTION:
        value = raw_action.get('v')
        if not isinstance(value, str) or value not in configured_reactions(config):
            return _result(reason='reaction_not_enabled')
        if value not in REACTION_FACE_IDS:
            return _result(reason='reaction_unsupported')
    if not supports_action(action, plugin_event, group_id):
        return _result(reason='target_missing')

    plugin_info = getattr(plugin_event, 'bot_info', None)
    bot_hash = getattr(plugin_info, 'hash', '')
    now = time.monotonic()
    key = _state_key(bot_hash, group_id)
    failure_key = _failure_key(bot_hash, group_id, action)
    with OlivOSAIChatAssassin.data.gInteractionLock:
        failure_until = OlivOSAIChatAssassin.data.gInteractionFailureUntil.get(failure_key, 0.0)
        if failure_until > now:
            return _result(reason='failure_cooldown')
        last_attempt = OlivOSAIChatAssassin.data.gInteractionLastAttempt.get(key, 0.0)
        cooldown = cooldown_seconds(config)
        if cooldown > 0 and last_attempt > 0 and now - last_attempt < cooldown:
            return _result(reason='cooldown')
        OlivOSAIChatAssassin.data.gInteractionLastAttempt[key] = now

    detail = action_detail(action, value)
    return _result(action=action, reason='ok', value=value, detail=detail)


def action_detail(action, value=''):
    label = ACTION_LABELS.get(action, '互动')
    if action == ACTION_REACTION:
        return f'{label} · {REACTION_LABELS.get(value, value)}'
    return label


def _reaction_platform_value(value):
    face_id = REACTION_FACE_IDS.get(value)
    if face_id is None:
        return None
    return int(face_id)


def execute_action(plugin_event, group_id, selection, activity_token=None):
    action = selection.get('action') if isinstance(selection, dict) else None
    activity = OlivOSAIChatAssassin.activity
    if action not in (ACTION_REACTION, ACTION_POKE):
        return {'ok': False, 'action': action or '', 'error': 'invalid_action'}

    detail = action_detail(action, selection.get('value', ''))
    activity.update(activity_token, stage=activity.STAGE_INTERACTION)
    activity.note(
        activity_token,
        activity.STAGE_INTERACTION,
        f'{detail}处理中',
        activity.TRACE_ACTIVE,
    )
    try:
        if action == ACTION_REACTION:
            emoji_id = _reaction_platform_value(selection.get('value', ''))
            if emoji_id is None:
                raise ValueError('reaction value unsupported')
            plugin_event.set_msg_emoji_like(
                _message_target(plugin_event),
                emoji_id,
                is_set=True,
                group_id=group_id,
            )
        else:
            plugin_event.group_poke(group_id, _user_target(plugin_event))
    except Exception as exc:
        plugin_info = getattr(plugin_event, 'bot_info', None)
        bot_hash = getattr(plugin_info, 'hash', '')
        failure_key = _failure_key(bot_hash, group_id, action)
        with OlivOSAIChatAssassin.data.gInteractionLock:
            OlivOSAIChatAssassin.data.gInteractionFailureUntil[failure_key] = (
                time.monotonic() + FAILURE_COOLDOWN
            )
        OlivOSAIChatAssassin.logger.warn(
            f'INTERACTION - {action.upper()} FAILED [{type(exc).__name__}]'
        )
        activity.note(
            activity_token,
            activity.STAGE_INTERACTION,
            f'{detail}失败',
            activity.TRACE_FAILED,
        )
        return {'ok': False, 'action': action, 'error': 'platform_failed'}

    OlivOSAIChatAssassin.logger.log(f'INTERACTION - {action.upper()} DISPATCHED')
    activity.note(
        activity_token,
        activity.STAGE_INTERACTION,
        f'{detail}已执行',
    )
    return {'ok': True, 'action': action, 'error': ''}


def prompt_rules(config):
    actions = configured_actions(config) if config_enabled(config) else []
    if not actions:
        return ''
    reactions = configured_reactions(config)
    action_text = '、'.join(f'{item}（{ACTION_LABELS[item]}）' for item in actions)
    lines = [
        '- 可选字段 a 是群内互动动作数组；本轮最多使用一项，多余项会被丢弃',
        f'- 当前允许的动作只有：{action_text}',
        '- 动作必须只作用于当前这条群消息；不得输出群号、用户号、消息号、路径或原始消息段',
        '- 动作比文本更稀疏，不要为了使用能力而附加动作，也不要连续多轮重复同一动作',
    ]
    if ACTION_REACTION in actions and reactions:
        reaction_text = '、'.join(f'{item}（{REACTION_LABELS[item]}）' for item in reactions)
        lines.append(f'- reaction 的 v 只能是：{reaction_text}')
    elif ACTION_REACTION in actions:
        lines.append('- reaction 当前没有可用的语义表情，不要输出 reaction')
    if ACTION_POKE in actions:
        lines.append('- poke 只会戳当前发送者，不要编造目标')
    independent = []
    if ACTION_REACTION in actions and reactions:
        independent.append('reaction')
    if ACTION_POKE in actions:
        independent.append('poke')
    if independent:
        lines.append(
            '- 如果只想表达态度、不想补文本，可以令 r 为空并附带一个可用动作：'
            + '、'.join(independent)
        )
    lines.append('- 如果不打算互动且不打算说话，只输出 {"r": []}，不要编造动作')
    return '\n'.join(lines)
