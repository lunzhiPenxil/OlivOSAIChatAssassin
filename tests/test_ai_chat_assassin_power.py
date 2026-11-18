"""Power 分群运行开关的离线回归测试。"""

import contextlib
import copy
import json
import os
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
import OlivOSAIChatAssassin.main as assassin_main  # noqa: E402
from OlivOSAIChatAssassin import activity, data, load, manager as power, msg  # noqa: E402


BOT_HASH = 'power-bot'
GROUP_ID = '123456'


def make_config(**overrides):
    config = copy.deepcopy(data.configDefault)
    config.update({
        'api_key': 'test-key',
        'enabled_groups': ['all'],
        'history_size_min': 1,
        'history_size': 8,
        'slack_time': 0,
        'slack_cooldown_time': 0,
        'reply_probability': 1,
        'power_control_enabled': True,
        'power_default_mode': 'on',
    })
    config['ocr_api']['enable'] = False
    config.update(overrides)
    return config


class FakeConfigManager:
    def __init__(self, config):
        self.config = config

    def reload(self, _bot_hash):
        return None

    def getConfig(self, _bot_hash):
        return self.config


class FakeEvent:
    def __init__(self, **values):
        self.bot_info = SimpleNamespace(hash=BOT_HASH)
        self.base_info = {'self_id': 10001}
        self.data = SimpleNamespace(**values)
        self.replies = []

    def reply(self, text):
        self.replies.append(text)


@pytest.fixture(autouse=True)
def isolated_power(tmp_path, monkeypatch):
    monkeypatch.setattr(data, 'gPowerPath', str(tmp_path / 'power_overrides.json'))
    monkeypatch.setattr(data, 'gMessageHistory', {})
    monkeypatch.setattr(data, 'gGroupLock', {})
    monkeypatch.setattr(
        OlivOSAIChatAssassin,
        'logger',
        SimpleNamespace(log=lambda *_args: None, warn=lambda *_args: None),
    )
    data.gHistoryWindowGuard.clear_all()
    activity.clear_all()
    msg.clear_poke_state()
    power.reset()
    yield
    data.gHistoryWindowGuard.clear_all()
    activity.clear_all()
    msg.clear_poke_state()
    power.reset()


def use_config(monkeypatch, config):
    monkeypatch.setattr(data, 'gData', FakeConfigManager(config))


# 覆盖文件缺失时默认模式为开启。
def test_default_mode_is_on_when_override_file_is_missing():
    assert data.configDefault['power_control_enabled'] is False
    assert data.configDefault['power_default_mode'] == 'on'
    assert data.configDefault['command_prefixes'] == ['.', '。', '/']
    assert power.state(BOT_HASH, GROUP_ID, make_config()) == {
        'mode': 'on',
        'source': 'default',
    }


# 默认模式可设为关闭，非法值回退为开启。
def test_default_mode_can_be_off_and_invalid_values_fall_back_to_on():
    assert power.state(BOT_HASH, GROUP_ID, make_config(power_default_mode='off')) == {
        'mode': 'off',
        'source': 'default',
    }
    assert power.state(BOT_HASH, GROUP_ID, make_config(power_default_mode='invalid')) == {
        'mode': 'on',
        'source': 'default',
    }


# 关闭控制时完全沉浸，并拒绝群开关写入。
def test_disabled_control_is_fully_immersive_and_rejects_group_writes(monkeypatch):
    config = make_config(power_control_enabled=False, power_default_mode='off')
    use_config(monkeypatch, config)
    assert power.set_override(BOT_HASH, GROUP_ID, 'off') == (True, '')

    assert power.state(BOT_HASH, GROUP_ID, config) == {
        'mode': 'off',
        'source': 'override',
    }
    assert power.is_enabled(BOT_HASH, GROUP_ID, config) is True
    assert power.group_status(BOT_HASH, GROUP_ID, config) == {
        'control_enabled': False,
        'mode': 'off',
        'source': 'override',
        'effective_enabled': True,
        'default_mode': 'off',
    }

    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='.power on',
        message_id='message-disabled-control',
        sender={'nickname': '管理员', 'role': 'admin'},
    )
    assert power.handle_command(event, GROUP_ID, '.power on', config) is None
    assert event.replies == []
    assert power.set_group_enabled(BOT_HASH, GROUP_ID, True, config) == (
        False,
        'control_disabled',
    )


# 关闭控制时 power 指令按普通消息进入流程。
def test_disabled_control_leaves_power_command_in_the_normal_message_flow(monkeypatch):
    config = make_config(power_control_enabled=False, power_default_mode='off')
    use_config(monkeypatch, config)
    power.set_override(BOT_HASH, GROUP_ID, 'off')
    received = []
    monkeypatch.setattr(msg, '_register_history_arrival', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(msg, '_get_group_lock', lambda *_args, **_kwargs: contextlib.nullcontext())
    monkeypatch.setattr(
        msg,
        'unity_group_message_router',
        lambda plugin_event, *_args, **_kwargs: received.append(plugin_event.data.message),
    )
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='.power on',
        message_id='message-immersive-command',
        sender={'nickname': '管理员', 'role': 'admin'},
    )

    assassin_main.Event.group_message(event, None)

    assert received == ['.power on']
    assert event.replies == []
    assert power.state(BOT_HASH, GROUP_ID, config) == {
        'mode': 'off',
        'source': 'override',
    }


# 开启控制时群开关会写入覆盖记录。
def test_group_enabled_writes_an_override_when_control_is_enabled():
    config = make_config(power_control_enabled=True, power_default_mode='on')

    assert power.set_group_enabled(BOT_HASH, GROUP_ID, False, config) == (True, '')
    assert power.group_status(BOT_HASH, GROUP_ID, config) == {
        'control_enabled': True,
        'mode': 'off',
        'source': 'override',
        'effective_enabled': False,
        'default_mode': 'on',
    }
    assert power.is_enabled(BOT_HASH, GROUP_ID, config) is False
    assert power.set_group_enabled(BOT_HASH, GROUP_ID, True, config) == (True, '')
    assert power.is_enabled(BOT_HASH, GROUP_ID, config) is True


# 覆盖记录按 bot 和群隔离。
def test_overrides_are_isolated_by_bot_and_group(tmp_path):
    assert power.set_override('bot-a', '1', 'off') == (True, '')
    assert power.set_override('bot-a', '2', 'on') == (True, '')
    assert power.set_override('bot-b', '1', 'on') == (True, '')

    assert power.state('bot-a', '1', make_config())['mode'] == 'off'
    assert power.state('bot-a', '2', make_config())['mode'] == 'on'
    assert power.state('bot-a', '3', make_config())['mode'] == 'on'
    assert power.state('bot-b', '1', make_config())['mode'] == 'on'
    document = json.loads((tmp_path / 'power_overrides.json').read_text(encoding='utf-8'))
    assert document['bots']['bot-a']['1'] == 'off'
    assert document['bots']['bot-a']['2'] == 'on'


# clear 只移除当前 bot 与群的覆盖。
def test_clear_only_removes_the_current_bot_and_group_override(tmp_path):
    power.set_override('bot-a', '1', 'off')
    power.set_override('bot-a', '2', 'off')

    assert power.set_override('bot-a', '1', None) == (True, '')
    assert power.state('bot-a', '1', make_config())['mode'] == 'on'
    assert power.state('bot-a', '2', make_config())['mode'] == 'off'
    document = json.loads((tmp_path / 'power_overrides.json').read_text(encoding='utf-8'))
    assert '1' not in document['bots']['bot-a']
    assert document['bots']['bot-a']['2'] == 'off'


# 外部修改会按版本重新加载。
def test_external_edit_is_reloaded_by_revision(tmp_path):
    path = tmp_path / 'power_overrides.json'
    path.write_text(
        json.dumps({'version': 1, 'bots': {'bot-a': {'1': 'on'}}}),
        encoding='utf-8',
    )
    assert power.state('bot-a', '1', make_config())['mode'] == 'on'

    path.write_text(
        json.dumps({'version': 1, 'bots': {'bot-a': {'1': 'off'}}}),
        encoding='utf-8',
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert power.state('bot-a', '1', make_config())['mode'] == 'off'


# 非法条目被忽略，未知顶层字段被保留。
def test_invalid_entries_are_ignored_and_unknown_top_level_fields_are_preserved(tmp_path):
    path = tmp_path / 'power_overrides.json'
    path.write_text(
        json.dumps({
            'version': 1,
            'future': {'keep': True},
            'bots': {
                'bot-a': {'1': 'off', '2': 'invalid', '3': 'on'},
                'bot-b': 'invalid',
            },
        }),
        encoding='utf-8',
    )
    assert power.state('bot-a', '1', make_config())['mode'] == 'off'
    assert power.state('bot-a', '2', make_config())['mode'] == 'on'

    assert power.set_override('bot-c', '9', 'off') == (True, '')
    document = json.loads(path.read_text(encoding='utf-8'))
    assert document['future'] == {'keep': True}
    assert document['bots']['bot-a']['1'] == 'off'
    assert document['bots']['bot-c']['9'] == 'off'


# 写入失败不提交内存缓存状态。
def test_write_failure_does_not_commit_cached_state(monkeypatch):
    assert power.set_override('bot-a', '1', 'off') == (True, '')

    def fail_write(_path, _document):
        raise OSError('disk full')

    monkeypatch.setattr(
        OlivOSAIChatAssassin.load.DataManager,
        '_write_json_atomic',
        staticmethod(fail_write),
    )

    assert power.set_override('bot-a', '1', 'on') == (False, 'write_failed')
    assert power.state('bot-a', '1', make_config())['mode'] == 'off'


# 指令解析严格，同时允许 @ 自己和大小写差异。
@pytest.mark.parametrize(
    'message, expected',
    (
        ('.power on', 'on'),
        ('  。Power OFF  ', 'off'),
        ('[OP:at,id=10001] /power clear', 'clear'),
        ('[OP:at,id=10001,name=bot] .power status', 'status'),
        ('[CQ:at,id=10001,name=bot] .power status', None),
        ('[OP:at,id=20002] .power on', None),
        ('power on', None),
        ('Power OFF', None),
        ('.power on please', None),
        ('我说的 .power off', None),
        ('.power on [OP:image,file=x]', None),
        ('.power\tstatus', 'status'),
        ('。 power status', 'status'),
    ),
)
def test_command_parser_is_strict_but_allows_self_mention_and_case(message, expected):
    assert power.parse_command(message, 10001) == expected


# 指令前缀可配置，空前缀会被拒绝。
def test_command_prefixes_are_configurable_and_empty_prefix_is_rejected():
    custom = {'command_prefixes': ['!', '!!']}
    assert power.parse_command('!power on', 10001, custom) == 'on'
    assert power.parse_command('!!power off', 10001, custom) == 'off'
    assert power.parse_command('!! power status', 10001, custom) == 'status'
    assert power.parse_command('.power on', 10001, custom) is None
    assert power.parse_command('power on', 10001, custom) is None
    assert power.parse_command('.power on', 10001, {'command_prefixes': []}) is None
    assert power.parse_command('power on', 10001, {'command_prefixes': ['']}) is None


# 无前缀的 power 指令不会被消费。
def test_unprefixed_power_command_is_not_consumed(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)
    monkeypatch.setattr(msg, '_register_history_arrival', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(msg, 'should_reply', lambda *_args, **_kwargs: False)
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='power off',
        message_id='message-1',
        sender={'nickname': '管理员', 'role': 'owner'},
    )

    assassin_main.Event.group_message(event, None)

    assert event.replies == []
    entries = list(msg._get_history(BOT_HASH, GROUP_ID) or [])
    assert [entry['message'] for entry in entries] == ['power off']


# 指令权限只看身份字段。
@pytest.mark.parametrize(
    'sender, allowed, reason',
    (
        ({'role': 'owner'}, True, ''),
        ({'role': 'admin'}, True, ''),
        ({'is_owner': True}, True, ''),
        ({'is_admin': True}, True, ''),
        ({'role': 'member'}, False, 'permission_denied'),
        ({'role': 'admin', 'is_bot': True}, True, ''),
        ({'role': 'member', 'is_bot': True}, False, 'permission_denied'),
        ({'is_bot': True}, True, 'permission_open_no_role'),
        ({}, True, 'permission_open_no_role'),
        ({'nickname': '无身份组平台用户'}, True, 'permission_open_no_role'),
        ({'role': None}, False, 'permission_unknown'),
        ({'role': ''}, False, 'permission_unknown'),
        (None, False, 'permission_unknown'),
    ),
)
def test_command_permission_uses_identity_fields_only(sender, allowed, reason):
    event = SimpleNamespace(data=SimpleNamespace(sender=sender))
    assert power.permission(event) == (allowed, reason)


# 管理员指令回复后不进入聊天历史。
def test_admin_command_replies_without_entering_history(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)
    monkeypatch.setattr(
        msg,
        '_register_history_arrival',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('must not queue')),
    )
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='.power off',
        message_id='message-1',
        sender={'nickname': '管理员', 'role': 'admin'},
    )

    assassin_main.Event.group_message(event, None)

    assert event.replies == ['群聊刺客：当前群已关闭。']
    assert power.state(BOT_HASH, GROUP_ID, config)['mode'] == 'off'
    assert data.gMessageHistory == {}


# 平台没有身份组字段时仍允许指令。
def test_command_is_allowed_when_platform_has_no_role_field(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='.power off',
        message_id='message-no-role',
        sender={'nickname': '普通群友'},
    )

    assassin_main.Event.group_message(event, None)

    assert event.replies == ['群聊刺客：当前群已关闭。']
    assert power.state(BOT_HASH, GROUP_ID, config)['mode'] == 'off'
    assert data.gMessageHistory == {}


# status 与 clear 指令报告状态和配置来源。
def test_status_and_clear_commands_report_state_and_source(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)
    power.set_override(BOT_HASH, GROUP_ID, 'off')
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='.power status',
        message_id='message-1',
        sender={'nickname': '管理员', 'role': 'admin'},
    )

    status_result = power.handle_command(event, GROUP_ID, '.power status', config)
    clear_result = power.handle_command(event, GROUP_ID, '.power clear', config)

    assert status_result['response'] == '群聊刺客：当前为关闭（群覆盖）。'
    assert clear_result['response'] == '群聊刺客：已清除当前群覆盖，恢复默认开启。'
    assert event.replies == [status_result['response'], clear_result['response']]
    assert power.state(BOT_HASH, GROUP_ID, config) == {'mode': 'on', 'source': 'default'}


# 数据管理器会为旧配置补齐默认模式。
def test_data_manager_fills_power_default_for_old_config(tmp_path):
    config_dir = tmp_path / 'config'
    memory_dir = tmp_path / 'memory'
    config_dir.mkdir()
    (config_dir / f'config_{BOT_HASH}.json').write_text(
        json.dumps({'api_key': 'old-config'}),
        encoding='utf-8',
    )
    manager = load.DataManager(
        config_dir=str(config_dir),
        memory_dir=str(memory_dir),
        default_config=copy.deepcopy(data.configDefault),
    )

    manager.load_bot(BOT_HASH)

    assert manager.getConfig(BOT_HASH)['power_control_enabled'] is False
    assert manager.getConfig(BOT_HASH)['power_default_mode'] == 'on'
    assert manager.getConfig(BOT_HASH)['command_prefixes'] == ['.', '。', '/']


# power on 会在门槛前恢复被关闭的群。
def test_power_on_recovers_a_disabled_group_before_the_gate(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)
    power.set_override(BOT_HASH, GROUP_ID, 'off')
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='.power on',
        message_id='message-1',
        sender={'nickname': '群主', 'role': 'owner'},
    )

    assassin_main.Event.group_message(event, None)

    assert event.replies == ['群聊刺客：当前群已开启。']
    assert power.state(BOT_HASH, GROUP_ID, config)['mode'] == 'on'
    assert data.gMessageHistory == {}


# power off 会在写历史前拦截普通消息。
def test_power_off_blocks_regular_messages_before_history(monkeypatch):
    config = make_config(power_default_mode='off')
    use_config(monkeypatch, config)
    monkeypatch.setattr(
        msg,
        '_register_history_arrival',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('must not queue')),
    )
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='普通聊天',
        message_id='message-1',
        sender={'nickname': '群友', 'role': 'member'},
    )

    assassin_main.Event.group_message(event, None)

    assert event.replies == []
    assert data.gMessageHistory == {}


# 排队等待期间执行 power off 会在写历史前重新检查。
def test_power_off_during_queue_wait_is_rechecked_before_history(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)

    def disable_before_queue(*_args, **_kwargs):
        power.set_override(BOT_HASH, GROUP_ID, 'off')
        return None

    monkeypatch.setattr(msg, '_register_history_arrival', disable_before_queue)
    monkeypatch.setattr(
        msg,
        'should_reply',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('must not reply')),
    )
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='排队期间被关闭',
        message_id='message-1',
        sender={'nickname': '群友', 'role': 'member'},
    )

    assassin_main.Event.group_message(event, None)

    assert event.replies == []
    assert data.gMessageHistory == {}


# 非管理员指令按普通消息处理。
def test_non_admin_command_is_handled_as_a_regular_message(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)
    monkeypatch.setattr(msg, '_register_history_arrival', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(msg, 'should_reply', lambda *_args, **_kwargs: False)
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='.power off',
        message_id='message-1',
        sender={'nickname': '普通群友', 'role': 'member'},
    )

    assassin_main.Event.group_message(event, None)

    assert event.replies == []
    assert power.state(BOT_HASH, GROUP_ID, config)['mode'] == 'on'
    entries = list(msg._get_history(BOT_HASH, GROUP_ID) or [])
    assert [entry['message'] for entry in entries] == ['.power off']


# 静态禁用群不能通过指令开启。
def test_disabled_static_group_cannot_be_enabled_by_command(monkeypatch):
    config = make_config(enabled_groups=['other-group'])
    use_config(monkeypatch, config)
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='.power on',
        message_id='message-1',
        sender={'nickname': '群主', 'role': 'owner'},
    )

    assassin_main.Event.group_message(event, None)

    assert event.replies == []
    assert power.state(BOT_HASH, GROUP_ID, config)['mode'] == 'on'
    assert data.gMessageHistory == {}


# power off 会阻止群戳一戳写入历史。
def test_power_off_blocks_group_poke_history(monkeypatch):
    config = make_config(power_default_mode='off')
    use_config(monkeypatch, config)
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        target_id='20003',
        sender={'nickname': '群友'},
    )

    msg.unity_group_poke(event, None)

    assert event.replies == []
    assert data.gMessageHistory == {}


# 等待戳一戳锁期间执行 power off 会阻止历史写入。
def test_power_off_while_waiting_for_poke_lock_blocks_history(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)

    class DisablingLock:
        def __enter__(self):
            power.set_override(BOT_HASH, GROUP_ID, 'off')
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(msg, '_get_group_lock', lambda *_args, **_kwargs: DisablingLock())
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        target_id='20003',
        sender={'nickname': '群友'},
    )

    msg.unity_group_poke(event, None)

    assert event.replies == []
    entries = list(msg._get_history(BOT_HASH, GROUP_ID) or [])
    assert entries == []


# 模型响应后执行 power off 会丢弃输出。
def test_power_off_after_model_response_discards_output(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)
    msg.add_message_to_history(
        GROUP_ID,
        '历史消息一',
        'user-1',
        '群友一',
        bot_hash=BOT_HASH,
        role='user',
    )
    msg.add_message_to_history(
        GROUP_ID,
        '历史消息二',
        'user-2',
        '群友二',
        bot_hash=BOT_HASH,
        role='user',
    )
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *_args, **_kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *_args, **_kwargs: {})

    def generate_envelope(*_args, **_kwargs):
        power.set_override(BOT_HASH, GROUP_ID, 'off')
        return {'r': ['不应发送'], 'a': []}

    monkeypatch.setattr(msg, 'get_reply_envelope_with_retry', generate_envelope)
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='当前消息',
        message_id='message-3',
        sender={'nickname': '群友三'},
    )

    msg.reply_to_group(event, GROUP_ID, '当前消息')

    assert event.replies == []
    entries = list(msg._get_history(BOT_HASH, GROUP_ID) or [])
    assert all(entry.get('role') != 'assistant' for entry in entries)


# 发送等待期间执行 power off 会丢弃输出。
def test_power_off_during_send_delay_discards_output(monkeypatch):
    config = make_config()
    use_config(monkeypatch, config)
    msg.add_message_to_history(
        GROUP_ID,
        '历史消息一',
        'user-1',
        '群友一',
        bot_hash=BOT_HASH,
        role='user',
    )
    msg.add_message_to_history(
        GROUP_ID,
        '历史消息二',
        'user-2',
        '群友二',
        bot_hash=BOT_HASH,
        role='user',
    )
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *_args, **_kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *_args, **_kwargs: {})
    monkeypatch.setattr(msg, '_submit_memory_extraction', lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        msg,
        'get_reply_envelope_with_retry',
        lambda *_args, **_kwargs: {'r': ['不应发送'], 'a': []},
    )

    def disable_during_sleep(*_args, **_kwargs):
        power.set_override(BOT_HASH, GROUP_ID, 'off')

    monkeypatch.setattr(msg.OlivOSAIChatAssassin.tools, 'sleep', disable_during_sleep)
    event = FakeEvent(
        group_id=GROUP_ID,
        user_id='20002',
        message='当前消息',
        message_id='message-3',
        sender={'nickname': '群友三'},
    )

    msg.reply_to_group(event, GROUP_ID, '当前消息')

    assert event.replies == []
    entries = list(msg._get_history(BOT_HASH, GROUP_ID) or [])
    assert all(entry.get('role') != 'assistant' for entry in entries)


# 关闭时跳过记忆提炼。
def test_memory_extraction_skips_when_power_is_off(monkeypatch):
    config = make_config(power_default_mode='off')
    use_config(monkeypatch, config)
    monkeypatch.setattr(
        OlivOSAIChatAssassin.webTools,
        'call_ai',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('must not call')),
    )

    state = msg._run_memory_extraction(
        BOT_HASH,
        GROUP_ID,
        history_snapshot=[{'role': 'user', 'message': '普通消息'}],
    )

    assert state == activity.MEMORY_STATE_SKIPPED
