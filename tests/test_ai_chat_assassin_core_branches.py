"""核心回复、戳一戳与思考时间工具的关键分支测试。"""

import copy
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
from OlivOSAIChatAssassin import interaction, msg, tools  # noqa: E402


BOT_HASH = 'core-bot'
GROUP_ID = 'core-group'


class FakeData:
    def __init__(self, config):
        self.config = config

    def reload(self, _bot_hash):
        return None

    def getConfig(self, _bot_hash):
        return self.config


class FakeEvent:
    def __init__(self, **values):
        self.bot_info = SimpleNamespace(hash=BOT_HASH)
        self.base_info = values.pop('base_info', {'self_id': '10001'})
        self.data = SimpleNamespace(**values)
        self.replies = []
        self.pokes = []

    def reply(self, *args, **kwargs):
        self.replies.append((args, kwargs))

    def group_poke(self, *args, **kwargs):
        self.pokes.append((args, kwargs))


class FakeGroupLock:
    def __init__(self, slack_result=True, slack_error=None):
        self.slack_result = slack_result
        self.slack_error = slack_error

    def slack(self):
        if self.slack_error is not None:
            raise self.slack_error
        return self.slack_result

    def getRemaining(self):
        return 0.0


def make_config(**overrides):
    config = copy.deepcopy(OlivOSAIChatAssassin.data.configDefault)
    config.update({
        'api_key': 'test-key',
        'history_size_min': 1,
        'history_size': 8,
    })
    config.update(overrides)
    return config


def install_config(monkeypatch, config):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    return config


def seed_history():
    history = msg._ensure_history(BOT_HASH, GROUP_ID)
    msg.add_message_to_history(
        GROUP_ID,
        '历史消息一',
        '20001',
        '群友一',
        bot_hash=BOT_HASH,
        persist=False,
    )
    msg.add_message_to_history(
        GROUP_ID,
        '历史消息二',
        '20002',
        '群友二',
        bot_hash=BOT_HASH,
        persist=False,
    )
    return history


def manager_sequence(monkeypatch, values):
    calls = []
    sequence = list(values)

    def is_enabled(*args, **kwargs):
        index = len(calls)
        calls.append((args, kwargs))
        return sequence[min(index, len(sequence) - 1)]

    monkeypatch.setattr(OlivOSAIChatAssassin.manager, 'is_enabled', is_enabled)
    return calls


@pytest.fixture(autouse=True)
def reset_core_state(monkeypatch):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMessageHistory', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gGroupLock', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageCache', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gHistoryStore', None)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gProc', None)
    OlivOSAIChatAssassin.data.gHistoryWindowGuard.clear_all()
    msg.clear_poke_state()
    interaction.clear_all()
    yield
    OlivOSAIChatAssassin.data.gHistoryWindowGuard.clear_all()
    msg.clear_poke_state()
    interaction.clear_all()


# 未配置 API 时回复链路会直接跳过。
def test_reply_to_group_skips_without_api_key(monkeypatch):
    install_config(monkeypatch, make_config(api_key=''))
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert finished == [(msg.activity.STATE_SKIPPED, 'api_not_configured')]


# 回复链路在初始 power off 时不会读取历史。
def test_reply_to_group_stops_when_power_is_off_at_start(monkeypatch):
    install_config(monkeypatch, make_config())
    manager_sequence(monkeypatch, [False])
    stopped = []
    monkeypatch.setattr(
        msg,
        '_stop_for_power_off',
        lambda _token, phase, note=None: stopped.append(phase),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert stopped == ['reply_start']


# 没有历史消息时回复链路会跳过。
def test_reply_to_group_skips_when_history_is_missing(monkeypatch):
    install_config(monkeypatch, make_config())
    manager_sequence(monkeypatch, [True])
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert finished == [(msg.activity.STATE_SKIPPED, 'history_missing')]


# 历史条数不足时回复链路会跳过。
def test_reply_to_group_skips_when_history_is_short(monkeypatch):
    install_config(monkeypatch, make_config(history_size_min=5))
    manager_sequence(monkeypatch, [True])
    history = msg._ensure_history(BOT_HASH, GROUP_ID)
    msg.add_message_to_history(
        GROUP_ID,
        '唯一历史',
        '20001',
        '群友',
        bot_hash=BOT_HASH,
        persist=False,
    )
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert len(history) == 1
    assert finished == [(msg.activity.STATE_SKIPPED, 'history_short')]


# 回忆阶段会记录命中的知识、关系和图片数量。
def test_reply_to_group_records_recall_summary(monkeypatch):
    install_config(monkeypatch, make_config())
    seed_history()
    manager_sequence(monkeypatch, [True, False])
    monkeypatch.setattr(
        msg,
        '_build_memory_payload',
        lambda *args, **kwargs: ({
            '全局': {
                '知识缓存': {'k1': {}},
                '知识库': {'s1': {}},
                '知识搜索': {},
                '人物关系': {'p1': {}},
                '用户侧写': {},
            },
        }, 5),
    )
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *args, **kwargs: {'a.png': {}})
    notes = []
    monkeypatch.setattr(
        msg.activity,
        'note',
        lambda _token, _stage, text, *args, **kwargs: notes.append(text),
    )
    stopped = []
    monkeypatch.setattr(
        msg,
        '_stop_for_power_off',
        lambda _token, phase, note=None: stopped.append(phase),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert '知识缓存 1 · 知识库 1 · 关系 1 · 图片 1' in notes
    assert stopped == ['reply_after_recall']


# 没有外层锁时 slack 抛出的 RuntimeError 不会丢弃回复。
def test_reply_to_group_allows_direct_call_without_outer_lock(monkeypatch):
    install_config(monkeypatch, make_config())
    seed_history()
    manager_sequence(monkeypatch, [True, True, False])
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *args, **kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *args, **kwargs: {})
    monkeypatch.setattr(
        msg,
        '_get_group_lock',
        lambda *args, **kwargs: FakeGroupLock(slack_error=RuntimeError('unlocked')),
    )
    monkeypatch.setattr(msg, 'get_ai_context', lambda *args, **kwargs: [])
    stopped = []
    monkeypatch.setattr(
        msg,
        '_stop_for_power_off',
        lambda _token, phase, note=None: stopped.append(phase),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert stopped == ['reply_after_slack']


# slack 主动取消时会以 superseded 状态结束。
def test_reply_to_group_skips_when_slack_is_canceled(monkeypatch):
    install_config(monkeypatch, make_config())
    seed_history()
    manager_sequence(monkeypatch, [True, True])
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *args, **kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *args, **kwargs: {})
    monkeypatch.setattr(msg, '_get_group_lock', lambda *args, **kwargs: FakeGroupLock(False))
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert finished == [(msg.activity.STATE_SUPERSEDED, 'slack_canceled')]


# 模型返回后 power off 会丢弃输出。
def test_reply_to_group_stops_when_power_is_off_before_commit(monkeypatch):
    install_config(monkeypatch, make_config())
    seed_history()
    manager_sequence(monkeypatch, [True, True, True, False])
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *args, **kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *args, **kwargs: {})
    monkeypatch.setattr(msg, '_get_group_lock', lambda *args, **kwargs: FakeGroupLock())
    monkeypatch.setattr(msg, 'get_ai_context', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        msg,
        'get_reply_envelope_with_retry',
        lambda *args, **kwargs: {'r': ['不应发送'], 'a': []},
    )
    stopped = []
    monkeypatch.setattr(
        msg,
        '_stop_for_power_off',
        lambda _token, phase, note=None: stopped.append(phase),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert stopped == ['reply_before_commit']


# 只有戳一戳动作时提示词会附带动作示例。
def test_reply_prompt_includes_poke_only_example(monkeypatch):
    install_config(monkeypatch, make_config(
        interaction_enabled=True,
        interaction_actions=['poke'],
        interaction_reactions=[],
    ))
    seed_history()
    manager_sequence(monkeypatch, [True, True, True, True])
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *args, **kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *args, **kwargs: {})
    monkeypatch.setattr(msg, '_get_group_lock', lambda *args, **kwargs: FakeGroupLock())
    captured = []

    def capture_context(_config, _history, content, *args, **kwargs):
        captured.append(content)
        return []

    monkeypatch.setattr(msg, 'get_ai_context', capture_context)
    monkeypatch.setattr(msg, 'get_reply_envelope_with_retry', lambda *args, **kwargs: None)

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert '# 仅动作互动参考输出' in captured[0]
    assert '"t": "poke"' in captured[0]


@pytest.mark.parametrize(
    ('execute_result', 'expected_state', 'expected_error'),
    [
        ({'ok': True}, OlivOSAIChatAssassin.activity.STATE_FINISHED, 'interaction_only'),
        ({'ok': False}, OlivOSAIChatAssassin.activity.STATE_FAILED, 'interaction_failed'),
    ],
)
# 仅动作回复根据执行结果落为完成或失败。
def test_reply_to_group_interaction_only_result(monkeypatch, execute_result, expected_state, expected_error):
    install_config(monkeypatch, make_config(
        interaction_enabled=True,
        interaction_actions=['poke'],
        interaction_reactions=[],
    ))
    seed_history()
    manager_sequence(monkeypatch, [True, True, True, True])
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *args, **kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *args, **kwargs: {})
    monkeypatch.setattr(msg, '_get_group_lock', lambda *args, **kwargs: FakeGroupLock())
    monkeypatch.setattr(msg, 'get_ai_context', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        msg,
        'get_reply_envelope_with_retry',
        lambda *args, **kwargs: {'r': [], 'a': [{'t': 'poke'}]},
    )
    monkeypatch.setattr(interaction, 'execute_action', lambda *args, **kwargs: execute_result)
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    assert finished[-1] == (expected_state, expected_error)


# 文本发送异常会持久化历史并向上抛出。
def test_reply_to_group_propagates_send_failure(monkeypatch):
    install_config(monkeypatch, make_config())
    seed_history()
    manager_sequence(monkeypatch, [True, True, True, True])
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *args, **kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *args, **kwargs: {})
    monkeypatch.setattr(msg, '_get_group_lock', lambda *args, **kwargs: FakeGroupLock())
    monkeypatch.setattr(msg, 'get_ai_context', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        msg,
        'get_reply_envelope_with_retry',
        lambda *args, **kwargs: {'r': ['要发送的文本'], 'a': []},
    )
    monkeypatch.setattr(msg, '_submit_memory_extraction', lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, 'sleep', lambda _seconds: None)
    persisted = []
    monkeypatch.setattr(msg, '_persist_history', lambda *args, **kwargs: persisted.append(args))

    def fail_reply(*args, **kwargs):
        raise RuntimeError('send failed')

    monkeypatch.setattr(msg, 'reply', fail_reply)

    with pytest.raises(RuntimeError, match='send failed'):
        msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')
    assert persisted


# 发送等待期间 power off 会移除刚写入的 assistant 历史。
def test_reply_to_group_stops_when_power_is_off_before_send(monkeypatch):
    install_config(monkeypatch, make_config())
    seed_history()
    manager_sequence(monkeypatch, [True, True, True, True, False])
    monkeypatch.setattr(msg, '_build_memory_payload', lambda *args, **kwargs: ({}, 0))
    monkeypatch.setattr(msg, '_image_cache_mapping', lambda *args, **kwargs: {})
    monkeypatch.setattr(msg, '_get_group_lock', lambda *args, **kwargs: FakeGroupLock())
    monkeypatch.setattr(msg, 'get_ai_context', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        msg,
        'get_reply_envelope_with_retry',
        lambda *args, **kwargs: {'r': ['不应发送'], 'a': []},
    )
    monkeypatch.setattr(msg, '_submit_memory_extraction', lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, 'sleep', lambda _seconds: None)
    stopped = []
    monkeypatch.setattr(
        msg,
        '_stop_for_power_off',
        lambda _token, phase, note=None: stopped.append(phase),
    )

    msg.reply_to_group(FakeEvent(message_id='1', user_id='20001'), GROUP_ID, '当前消息')

    history = list(msg._get_history(BOT_HASH, GROUP_ID) or [])
    assert stopped == ['reply_before_send']
    assert all(entry.get('role') != 'assistant' for entry in history)


# 戳一戳缺少用户标识时不会写入历史。
def test_poke_without_user_id_is_ignored(monkeypatch):
    install_config(monkeypatch, make_config())

    msg.unity_group_poke(FakeEvent(group_id=GROUP_ID, user_id=None, target_id='10001'), None)

    assert msg._get_history(BOT_HASH, GROUP_ID) is None


# 配置缺失时戳一戳安全结束，且兼容非字典 base_info。
def test_poke_without_configuration_is_ignored(monkeypatch):
    install_config(monkeypatch, {})
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.unity_group_poke(
        FakeEvent(
            group_id=GROUP_ID,
            user_id='20001',
            target_id='10001',
            base_info=None,
        ),
        None,
    )

    assert finished == [(msg.activity.STATE_SKIPPED, 'configuration_missing')]


# 群被静态禁用时戳一戳不会写入历史。
def test_poke_in_disabled_group_is_ignored(monkeypatch):
    install_config(monkeypatch, make_config(enabled_groups=['other-group']))

    msg.unity_group_poke(
        FakeEvent(group_id=GROUP_ID, user_id='20001', target_id='10001'),
        None,
    )

    assert msg._get_history(BOT_HASH, GROUP_ID) is None


# 历史追加失败时戳一戳安全结束。
def test_poke_stops_when_history_append_fails(monkeypatch):
    install_config(monkeypatch, make_config(interaction_response_enabled=True))
    manager_sequence(monkeypatch, [True])
    monkeypatch.setattr(msg, 'add_message_to_history', lambda *args, **kwargs: None)
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.unity_group_poke(
        FakeEvent(group_id=GROUP_ID, user_id='20001', target_id='10001'),
        None,
    )

    assert finished == [(msg.activity.STATE_FAILED, 'history_write_failed')]


# 戳一戳滑出历史窗口时会以 superseded 状态让位。
def test_poke_stops_when_history_window_is_evicted(monkeypatch):
    install_config(monkeypatch, make_config(interaction_response_enabled=True))
    manager_sequence(monkeypatch, [True])

    def evict(*args, **kwargs):
        raise msg._HistoryWindowCancelled('before_poke_reply', 1, 0)

    monkeypatch.setattr(msg, '_raise_if_history_window_evicted', evict)
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.unity_group_poke(
        FakeEvent(group_id=GROUP_ID, user_id='20001', target_id='10001'),
        None,
    )

    assert finished == [(msg.activity.STATE_SUPERSEDED, 'retrieval_out_of_window')]


# 戳一戳未命中回复条件时只写入历史并跳过回复。
def test_poke_not_selected_writes_history_and_skips_reply(monkeypatch):
    install_config(monkeypatch, make_config(interaction_response_enabled=True))
    manager_sequence(monkeypatch, [True])
    monkeypatch.setattr(msg, 'should_reply', lambda *args, **kwargs: False)
    finished = []
    monkeypatch.setattr(
        msg.activity,
        'finish',
        lambda _token, state, error=None: finished.append((state, error)),
    )

    msg.unity_group_poke(
        FakeEvent(group_id=GROUP_ID, user_id='20001', target_id='10001'),
        None,
    )

    history = list(msg._get_history(BOT_HASH, GROUP_ID) or [])
    assert len(history) == 1
    assert finished == [(msg.activity.STATE_SKIPPED, 'not_selected')]


# 图片缓存映射兼容列表和字典两种运行期形态。
def test_image_cache_mapping_supports_list_and_dict(monkeypatch):
    key = (BOT_HASH, GROUP_ID)
    monkeypatch.setitem(
        OlivOSAIChatAssassin.data.gImageCache,
        key,
        [('a.png', {'content': 'A'}), ('invalid',), ('b.png', {'content': 'B'})],
    )
    assert msg._image_cache_mapping(BOT_HASH, GROUP_ID) == {
        'a.png': {'content': 'A'},
        'b.png': {'content': 'B'},
    }

    monkeypatch.setitem(
        OlivOSAIChatAssassin.data.gImageCache,
        key,
        {'c.png': {'content': 'C'}},
    )
    assert msg._image_cache_mapping(BOT_HASH, GROUP_ID) == {'c.png': {'content': 'C'}}


# 思考时间工具在冷却结束前返回 False，结束后返回 True。
def test_set_and_get_think_respects_cooldown(monkeypatch):
    install_config(monkeypatch, make_config(first_thinking_cooldown=60))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gThinkTS', {})
    monkeypatch.setattr(tools.time, 'perf_counter', lambda: 100.0)
    tools.set_think(BOT_HASH, GROUP_ID)
    assert OlivOSAIChatAssassin.data.gThinkTS[BOT_HASH][GROUP_ID] == 100.0

    monkeypatch.setattr(tools.time, 'perf_counter', lambda: 130.0)
    assert tools.get_think(BOT_HASH, GROUP_ID) is False

    monkeypatch.setattr(tools.time, 'perf_counter', lambda: 161.0)
    assert tools.get_think(BOT_HASH, GROUP_ID) is True
