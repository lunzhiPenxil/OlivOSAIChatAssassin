"""群内互动动作与目标安全边界测试。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_APP = REPO_ROOT
PLUGIN_ROOT = PLUGIN_APP / 'OlivOSAIChatAssassin'
if not (PLUGIN_ROOT / 'app.json').is_file():
    pytest.skip('OlivOSAIChatAssassin 未安装，跳过插件专项测试', allow_module_level=True)
if str(PLUGIN_APP) not in sys.path:
    sys.path.insert(0, str(PLUGIN_APP))

import OlivOSAIChatAssassin  # noqa: E402
from OlivOSAIChatAssassin import interaction  # noqa: E402


def make_config(**overrides):
    config = {
        'interaction_enabled': True,
        'interaction_actions': ['reaction', 'poke'],
        'interaction_reactions': ['like', 'laugh', 'doge', 'surprise'],
        'interaction_cooldown': 0,
    }
    config.update(overrides)
    return config


def make_event(
    *,
    model='napcat_default',
    bot_hash='bot-1',
    message_id='10001',
    user_id='42',
    sender=None,
):
    calls = []
    plugin_event = SimpleNamespace(
        platform={'sdk': 'onebot', 'platform': 'qq', 'model': model},
        bot_info=SimpleNamespace(hash=bot_hash),
        base_info={'self_id': '10001'},
        data=SimpleNamespace(
            message_id=message_id,
            user_id=user_id,
            sender=sender if sender is not None else {'nickname': '群友'},
        ),
    )

    def set_msg_emoji_like(message_id_value, emoji_id, is_set=True, group_id=None):
        calls.append(('reaction', message_id_value, emoji_id, is_set, group_id))

    def group_poke(group_id_value, user_id_value):
        calls.append(('poke', group_id_value, user_id_value))

    plugin_event.set_msg_emoji_like = set_msg_emoji_like
    plugin_event.group_poke = group_poke
    plugin_event.calls = calls
    return plugin_event


@pytest.fixture(autouse=True)
def quiet_interaction_state(monkeypatch):
    events = []
    logger = SimpleNamespace(
        log=lambda message: events.append(('log', message)),
        warn=lambda message: events.append(('warn', message)),
    )
    monkeypatch.setattr(OlivOSAIChatAssassin, 'logger', logger)
    interaction.clear_all()
    yield events
    interaction.clear_all()


def test_config_helpers_accept_only_known_values_and_no_upper_limit():
    config = make_config(
        interaction_actions=['quote', 'unknown', 'reaction', 'poke'],
        interaction_reactions=['doge', 'invalid', 'like'],
        interaction_cooldown=10 ** 12,
    )

    assert interaction.config_enabled(config) is True
    assert interaction.configured_actions(config) == ['reaction', 'poke']
    assert interaction.configured_reactions(config) == ['doge', 'like']
    assert interaction.cooldown_seconds(config) == float(10 ** 12)


def test_reaction_catalog_has_extended_lagrange_safe_faces():
    assert len(interaction.REACTION_CATALOG) >= 16
    assert interaction.REACTION_FACE_IDS['like'] == '76'
    assert interaction.REACTION_FACE_IDS['tears'] == '182'
    assert interaction.REACTION_FACE_IDS['applause'] == '99'
    assert interaction.REACTION_FACE_IDS['cheer'] == '311'
    assert interaction.REACTION_FACE_IDS['speechless'] == '462'
    assert set(interaction.REACTION_LABELS) == set(interaction.REACTION_FACE_IDS)
    assert all(
        face_id.isdigit() and len(face_id) <= 3
        for face_id in interaction.REACTION_FACE_IDS.values()
    )


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_non_finite_cooldown_falls_back_to_default(value):
    assert interaction.cooldown_seconds(make_config(interaction_cooldown=value)) == float(
        OlivOSAIChatAssassin.data.configDefault['interaction_cooldown']
    )


def test_select_action_never_falls_back_after_invalid_first_item():
    event = make_event()
    config = make_config()

    selection = interaction.select_action(
        [{'t': 'unknown'}, {'t': 'reaction', 'v': 'like'}],
        config,
        event,
        '20001',
    )

    assert selection['action'] is None
    assert selection['reason'] == 'not_enabled'
    assert event.calls == []


def test_select_action_requires_enabled_known_reaction_value():
    event = make_event()

    disabled = interaction.select_action(
        [{'t': 'reaction', 'v': 'like'}],
        make_config(interaction_reactions=[]),
        event,
        '20001',
    )
    unknown = interaction.select_action(
        [{'t': 'reaction', 'v': 'not_a_reaction'}],
        make_config(),
        event,
        '20001',
    )

    assert disabled['reason'] == 'reaction_not_enabled'
    assert unknown['reason'] == 'reaction_not_enabled'


def test_select_action_enforces_group_cooldown_and_clear_bot():
    event = make_event()
    config = make_config(interaction_cooldown=30)
    now = [100.0]
    original_monotonic = interaction.time.monotonic

    def monotonic():
        return now[0]

    interaction.time.monotonic = monotonic
    try:
        first = interaction.select_action(
            [{'t': 'reaction', 'v': 'like'}], config, event, '20001'
        )
        now[0] = 110.0
        cooling = interaction.select_action(
            [{'t': 'reaction', 'v': 'like'}], config, event, '20001'
        )
        now[0] = 131.0
        after_cooldown = interaction.select_action(
            [{'t': 'reaction', 'v': 'like'}], config, event, '20001'
        )
        interaction.clear_bot('bot-1')
        after_clear = interaction.select_action(
            [{'t': 'reaction', 'v': 'like'}], config, event, '20001'
        )
    finally:
        interaction.time.monotonic = original_monotonic

    assert first['reason'] == 'ok'
    assert cooling['reason'] == 'cooldown'
    assert after_cooldown['reason'] == 'ok'
    assert after_clear['reason'] == 'ok'


def test_reaction_and_poke_use_backend_resolved_targets():
    event = make_event(message_id='10001', user_id='42')
    config = make_config()

    reaction = interaction.select_action(
        [{'t': 'reaction', 'v': 'like'}], config, event, '20001'
    )
    assert interaction.execute_action(event, '20001', reaction)['ok'] is True
    assert event.calls[-1] == ('reaction', '10001', 76, True, '20001')

    interaction.clear_all()
    poke = interaction.select_action(
        [{'t': 'poke'}], config, event, '20001'
    )
    assert interaction.execute_action(event, '20001', poke)['ok'] is True
    assert event.calls[-1] == ('poke', '20001', '42')


def test_poke_alias_resolves_to_history_user():
    event = make_event(user_id='42')
    targets = {
        'p1': {'user_id': '101', 'name': '甲'},
        'p2': {'user_id': '202', 'name': '乙'},
    }

    selection = interaction.select_action(
        [{'t': 'poke', 'v': 'p2'}],
        make_config(),
        event,
        '20001',
        poke_targets=targets,
    )

    assert selection['reason'] == 'ok'
    assert selection['target_id'] == '202'
    assert selection['target_name'] == '乙'
    assert selection['detail'] == '戳一戳 · 乙'
    assert interaction.execute_action(event, '20001', selection)['ok'] is True
    assert event.calls[-1] == ('poke', '20001', '202')


@pytest.mark.parametrize(
    ('value', 'reason'),
    [
        ('42', 'poke_target_invalid'),
        ('', 'poke_target_invalid'),
        ('P1', 'poke_target_invalid'),
        ('nickname', 'poke_target_invalid'),
        ('p9', 'poke_target_missing'),
    ],
)
def test_poke_alias_rejects_invalid_or_unknown_targets(value, reason):
    event = make_event(user_id='42')

    selection = interaction.select_action(
        [{'t': 'poke', 'v': value}],
        make_config(),
        event,
        '20001',
        poke_targets={'p1': {'user_id': '101', 'name': '甲'}},
    )

    assert selection['action'] is None
    assert selection['reason'] == reason
    assert event.calls == []


def test_poke_alias_rejects_the_bot_itself():
    event = make_event(user_id='42')

    selection = interaction.select_action(
        [{'t': 'poke', 'v': 'p1'}],
        make_config(),
        event,
        '20001',
        poke_targets={'p1': {'user_id': '10001', 'name': '机器人'}},
    )

    assert selection['action'] is None
    assert selection['reason'] == 'poke_target_self'
    assert event.calls == []


def test_platform_failure_sets_action_specific_failure_cooldown():
    event = make_event()
    event.set_msg_emoji_like = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('offline'))
    config = make_config()
    selection = interaction.select_action(
        [{'t': 'reaction', 'v': 'like'}], config, event, '20001'
    )

    result = interaction.execute_action(event, '20001', selection)
    blocked = interaction.select_action(
        [{'t': 'reaction', 'v': 'like'}], config, event, '20001'
    )
    poke_still_allowed = interaction.select_action(
        [{'t': 'poke'}], config, event, '20001'
    )

    assert result == {'ok': False, 'action': 'reaction', 'error': 'platform_failed'}
    assert blocked['reason'] == 'failure_cooldown'
    assert poke_still_allowed['reason'] == 'ok'


def test_quote_and_mention_are_not_interaction_actions():
    assert interaction.VALID_ACTIONS == ('reaction', 'poke')
    assert not hasattr(interaction, 'ACTION_QUOTE')
    assert not hasattr(interaction, 'ACTION_MENTION')
    assert not hasattr(interaction, 'apply_text_action')


def test_success_log_reports_dispatch_instead_of_unknown_platform_state(quiet_interaction_state):
    event = make_event()
    selection = interaction.select_action([{'t': 'poke'}], make_config(), event, '20001')

    result = interaction.execute_action(event, '20001', selection)

    assert result == {'ok': True, 'action': 'poke', 'error': ''}
    assert ('log', 'INTERACTION - POKE DISPATCHED') in quiet_interaction_state
    assert ('log', 'INTERACTION - POKE OK') not in quiet_interaction_state


@pytest.mark.parametrize('model', ['shamrock_default', 'milky_default', 'unknown_model'])
def test_actions_do_not_gate_on_platform_or_model(model):
    config = make_config()
    event = make_event(model=model)
    del event.platform

    reaction = interaction.select_action(
        [{'t': 'reaction', 'v': 'like'}],
        config,
        event,
        '20001',
    )
    poke = interaction.select_action(
        [{'t': 'poke'}],
        config,
        event,
        '20001',
    )

    assert reaction['reason'] == 'ok'
    assert poke['reason'] == 'ok'


def test_action_targets_accept_opaque_non_numeric_event_ids():
    event = make_event(message_id='message:abc', user_id='user:abc')
    config = make_config()

    reaction = interaction.select_action(
        [{'t': 'reaction', 'v': 'like'}], config, event, 'group:abc'
    )
    assert interaction.execute_action(event, 'group:abc', reaction)['ok'] is True
    assert event.calls[-1] == ('reaction', 'message:abc', 76, True, 'group:abc')

    interaction.clear_all()
    poke = interaction.select_action(
        [{'t': 'poke'}], config, event, 'group:abc'
    )
    assert interaction.execute_action(event, 'group:abc', poke)['ok'] is True
    assert event.calls[-1] == ('poke', 'group:abc', 'user:abc')


def test_action_selection_requires_targets_from_the_current_event():
    config = make_config()

    reaction = interaction.select_action(
        [{'t': 'reaction', 'v': 'like'}],
        config,
        make_event(message_id='-1'),
        '20001',
    )
    poke = interaction.select_action(
        [{'t': 'poke'}],
        config,
        make_event(user_id=''),
        '20001',
    )
    missing_group = interaction.select_action(
        [{'t': 'poke'}],
        config,
        make_event(),
        '',
    )

    assert reaction['reason'] == 'target_missing'
    assert poke['reason'] == 'target_missing'
    assert missing_group['reason'] == 'target_missing'


def test_bot_sender_and_non_list_actions_are_rejected_without_calls():
    event = make_event(sender={'is_bot': True})
    config = make_config()

    bot_selection = interaction.select_action(
        [{'t': 'reaction', 'v': 'like'}], config, event, '20001'
    )
    malformed_selection = interaction.select_action(
        {'t': 'reaction', 'v': 'like'}, config, event, '20001'
    )

    assert bot_selection['reason'] == 'bot_sender'
    assert malformed_selection['reason'] == 'absent'
    assert event.calls == []


def test_prompt_rules_only_include_enabled_actions():
    assert interaction.prompt_rules(make_config(interaction_enabled=False)) == ''
    rules = interaction.prompt_rules(
        make_config(
            interaction_actions=['reaction', 'quote', 'poke'],
            interaction_reactions=['doge'],
        )
    )

    assert 'reaction（表情回应）' in rules
    assert 'doge（狗头）' in rules
    assert 'quote' not in rules
    assert 'mention' not in rules
    assert 'poke 的 v 可省略' in rules
    assert '“当前互动目标”中的 pN 别名' in rules
    assert '禁止使用昵称、QQ号、@文本或原始消息段' in rules
    assert '可用动作：reaction、poke' in rules
    assert '只输出 {"r": []}' in rules

    no_reaction_values = interaction.prompt_rules(
        make_config(interaction_actions=['reaction'], interaction_reactions=[])
    )
    assert 'reaction 当前没有可用的语义表情' in no_reaction_values
    assert '可以令 r 为空' not in no_reaction_values
