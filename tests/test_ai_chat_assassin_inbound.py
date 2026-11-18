"""Inbound event tests for the OlivOSAIChatAssassin plugin."""

import json
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

import OlivOSAIChatAssassin.main as assassin_main  # noqa: E402
from OlivOSAIChatAssassin import data, eventInbox  # noqa: E402
from OlivOSAIChatAssassin import msg as assassin_msg  # noqa: E402


BOT_HASH = 'inbound-bot'


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
        self.sent = []

    def reply(self, *args, **kwargs):
        self.sent.append(('reply', args, kwargs))

    def send(self, *args, **kwargs):
        self.sent.append(('send', args, kwargs))

    def group_poke(self, *args, **kwargs):
        self.sent.append(('group_poke', args, kwargs))

    def friend_poke(self, *args, **kwargs):
        self.sent.append(('friend_poke', args, kwargs))


@pytest.fixture(autouse=True)
def clear_event_inbox():
    eventInbox.clear_all()
    yield
    eventInbox.clear_all()


@pytest.fixture(autouse=True)
def isolated_message_state(monkeypatch):
    monkeypatch.setattr(data, 'gMessageHistory', {})
    monkeypatch.setattr(data, 'gGroupLock', {})
    data.gHistoryWindowGuard.clear_all()
    assassin_msg.clear_poke_state()
    yield
    data.gHistoryWindowGuard.clear_all()
    assassin_msg.clear_poke_state()


@pytest.fixture
def inbound_config(monkeypatch):
    config = {
        'event_private_message_enabled': False,
        'event_poke_enabled': False,
        'interaction_response_enabled': False,
        'event_recall_enabled': False,
        'event_group_advanced_enabled': False,
    }
    monkeypatch.setattr(data, 'gData', FakeConfigManager(config))
    monkeypatch.setattr(data, 'gProc', None)
    return config


# 事件接收开关默认全部关闭。
def test_event_receive_defaults_are_disabled():
    for field in (
        'event_private_message_enabled',
        'event_poke_enabled',
        'interaction_response_enabled',
        'event_recall_enabled',
        'event_group_advanced_enabled',
    ):
        assert data.configDefault[field] is False


# 私聊消息开关只记录事件，不触发发送。
def test_private_message_receive_switch_records_without_sending(inbound_config):
    event = FakeEvent(
        user_id='20002',
        sender={'nickname': '测试用户'},
        message='[CQ:at,id=10001] 私聊内容',
        message_id='message-1',
    )
    assert assassin_main.Event.private_message(event, None) is None
    assert eventInbox.snapshot(BOT_HASH)['events'] == []

    inbound_config['event_private_message_enabled'] = True
    assassin_main.Event.private_message(event, None)
    snapshot = eventInbox.snapshot(BOT_HASH)
    assert snapshot['event_count'] == 1
    record = snapshot['events'][0]
    assert record['kind'] == 'private_message'
    assert record['scene'] == 'private'
    assert record['summary'] == '私聊内容'
    assert record['actor_name'] == '测试用户'
    assert event.sent == []


# 戳一戳开关区分群聊戳和私聊戳。
def test_poke_switch_distinguishes_group_and_private_pokes(inbound_config):
    inbound_config['event_poke_enabled'] = True
    private_event = FakeEvent(group_id=None, user_id='20002', target_id='10001')
    group_event = FakeEvent(group_id='123456', user_id='20003', target_id='20004')

    assassin_main.Event.poke(private_event, None)
    assassin_main.Event.poke(group_event, None)

    events = eventInbox.snapshot(BOT_HASH)['events']
    assert [event['scene'] for event in events] == ['group', 'private']
    assert events[0]['group_id'] == '123456'
    assert events[1]['summary'] == '戳了戳你'
    assert private_event.sent == []
    assert group_event.sent == []


# 私聊撤回需要私聊消息开关开启。
def test_private_recall_requires_private_message_switch(inbound_config):
    inbound_config['event_recall_enabled'] = True
    private_event = FakeEvent(user_id='20002', message_id='private-message')
    group_event = FakeEvent(
        group_id='123456',
        operator_id='20003',
        user_id='20002',
        message_id='group-message',
    )

    assassin_main.Event.private_message_recall(private_event, None)
    assassin_main.Event.group_message_recall(group_event, None)
    events = eventInbox.snapshot(BOT_HASH)['events']
    assert [event['kind'] for event in events] == ['group_message_recall']
    assert private_event.sent == []
    assert group_event.sent == []

    inbound_config['event_private_message_enabled'] = True
    assassin_main.Event.private_message_recall(private_event, None)
    events = eventInbox.snapshot(BOT_HASH)['events']
    assert events[0]['kind'] == 'private_message_recall'


# 群高级事件开关分别控制各类群事件。
@pytest.mark.parametrize(
    ('callback_name', 'values', 'kind'),
    (
        (
            'group_member_increase',
            {'group_id': '123456', 'operator_id': '20001', 'user_id': '20002', 'action': 'approve'},
            'group_member_increase',
        ),
        (
            'group_member_decrease',
            {'group_id': '123456', 'operator_id': '20001', 'user_id': '20002', 'action': 'kick'},
            'group_member_decrease',
        ),
        (
            'group_admin',
            {'group_id': '123456', 'user_id': '20002', 'action': 'set'},
            'group_admin',
        ),
        (
            'group_ban',
            {
                'group_id': '123456', 'operator_id': '20001', 'user_id': '20002',
                'duration': 60, 'action': 'ban',
            },
            'group_ban',
        ),
        (
            'group_file_upload',
            {'group_id': '123456', 'user_id': '20002', 'file': {'name': '资料.txt'}},
            'group_file_upload',
        ),
    ),
)
def test_group_advanced_switch_controls_each_event(
    inbound_config,
    callback_name,
    values,
    kind,
):
    event = FakeEvent(**values)
    callback = getattr(assassin_main.Event, callback_name)

    callback(event, None)
    assert eventInbox.snapshot(BOT_HASH)['events'] == []

    inbound_config['event_group_advanced_enabled'] = True
    callback(event, None)
    events = eventInbox.snapshot(BOT_HASH)['events']
    assert len(events) == 1
    assert events[0]['kind'] == kind
    assert event.sent == []


# 入站快照按最新优先排序，并有数量上限。
def test_inbound_snapshot_is_newest_first_and_bounded(monkeypatch, inbound_config):
    monkeypatch.setattr(eventInbox, 'EVENT_LIMIT', 3)
    for index in range(5):
        eventInbox._record(
            BOT_HASH,
            'poke',
            '第 %s 次戳一戳' % index,
            scene='private',
            user_id='20002',
        )

    snapshot = eventInbox.snapshot(BOT_HASH, limit=10)
    assert snapshot['event_count'] == 3
    assert snapshot['retained_limit'] == 3
    assert [event['summary'] for event in snapshot['events']] == [
        '第 4 次戳一戳',
        '第 3 次戳一戳',
        '第 2 次戳一戳',
    ]


# 非戳一戳事件不会修改群历史或发送消息。
def test_non_poke_inbound_handlers_never_mutate_group_history_or_send(inbound_config):
    inbound_config.update({
        'event_private_message_enabled': True,
        'event_recall_enabled': True,
        'event_group_advanced_enabled': True,
    })
    fake_events = (
        FakeEvent(user_id='20002', sender={'nickname': '用户'}, message='你好', message_id='1'),
        FakeEvent(group_id='123456', operator_id='20001', user_id='20002', message_id='2'),
        FakeEvent(group_id='123456', operator_id='20001', user_id='20002', action='approve'),
    )

    assassin_main.Event.private_message(fake_events[0], None)
    assassin_main.Event.group_message_recall(fake_events[1], None)
    assassin_main.Event.group_member_increase(fake_events[2], None)

    assert data.gMessageHistory == {}
    assert eventInbox.snapshot(BOT_HASH)['event_count'] == 3
    assert all(event.sent == [] for event in fake_events)


# 群戳机器人会写入结构化历史，但不直接回复。
def test_group_poke_targeting_bot_writes_structured_history_without_response(
    monkeypatch,
    inbound_config,
):
    inbound_config['event_poke_enabled'] = True
    monkeypatch.setattr(
        assassin_msg,
        'should_reply',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not decide reply')),
    )
    event = FakeEvent(
        group_id='123456',
        user_id='20002',
        target_id='10001',
        sender={'nickname': '测试群友'},
    )

    assassin_main.Event.poke(event, None)

    entries = list(assassin_msg._get_history(BOT_HASH, '123456') or [])
    assert len(entries) == 1
    entry = entries[0]
    assert entry['role'] == 'user'
    assert entry['user_id'] == '20002'
    assert entry['nickname'] == '测试群友'
    assert entry['message'] == '戳一戳：测试群友戳了戳你'
    assert entry['message_type'] == 'poke'
    assert entry['target_id'] == '10001'
    assert entry['target_is_self'] is True
    assert event.sent == []


# 群戳其他人会复用正常回复链路。
def test_group_poke_targeting_other_reuses_normal_reply_path(monkeypatch, inbound_config):
    inbound_config.update({
        'event_poke_enabled': True,
        'interaction_response_enabled': True,
    })
    decisions = []
    replies = []

    def decide(*args, **kwargs):
        decisions.append(kwargs.get('is_addressed'))
        return True

    def reply(plugin_event, group_id, message, **kwargs):
        replies.append({
            'group_id': group_id,
            'message': message,
            'history_entry': kwargs.get('history_entry'),
        })

    monkeypatch.setattr(assassin_msg, 'should_reply', decide)
    monkeypatch.setattr(assassin_msg, 'reply_to_group', reply)
    event = FakeEvent(
        group_id='123456',
        user_id='20002',
        target_id='20003',
        sender={'nickname': '测试群友'},
    )

    assassin_main.Event.poke(event, None)

    entries = list(assassin_msg._get_history(BOT_HASH, '123456') or [])
    assert decisions == [False]
    assert replies == [{
        'group_id': '123456',
        'message': '戳一戳：测试群友戳了戳用户20003',
        'history_entry': entries[0],
    }]
    assert entries[0]['message'] == '戳一戳：测试群友戳了戳用户20003'
    assert entries[0]['target_is_self'] is False
    assert event.sent == []


# 群戳其他人是否回复受响应开关控制。
def test_group_poke_targeting_other_obeys_response_switch(monkeypatch, inbound_config):
    inbound_config['event_poke_enabled'] = True
    monkeypatch.setattr(
        assassin_msg,
        'should_reply',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not decide reply')),
    )
    event = FakeEvent(
        group_id='123456',
        user_id='20002',
        target_id='20003',
        sender={'nickname': '测试群友'},
    )

    assassin_main.Event.poke(event, None)

    entries = list(assassin_msg._get_history(BOT_HASH, '123456') or [])
    assert len(entries) == 1
    assert entries[0]['message'] == '戳一戳：测试群友戳了戳用户20003'
    assert entries[0]['target_is_self'] is False
    assert event.sent == []


# 群戳机器人也会复用现有回复链路。
def test_group_poke_targeting_bot_reuses_existing_reply_path(monkeypatch, inbound_config):
    inbound_config.update({
        'event_poke_enabled': True,
        'interaction_response_enabled': True,
    })
    decisions = []
    replies = []

    def decide(*args, **kwargs):
        decisions.append(kwargs.get('is_addressed'))
        return True

    def reply(plugin_event, group_id, message, **kwargs):
        replies.append({
            'group_id': group_id,
            'message': message,
            'history_entry': kwargs.get('history_entry'),
        })

    monkeypatch.setattr(assassin_msg, 'should_reply', decide)
    monkeypatch.setattr(assassin_msg, 'reply_to_group', reply)
    event = FakeEvent(
        group_id='123456',
        user_id='20002',
        target_id='10001',
        sender={'nickname': '测试群友'},
    )

    assassin_main.Event.poke(event, None)

    entries = list(assassin_msg._get_history(BOT_HASH, '123456') or [])
    assert decisions == [True]
    assert replies == [{
        'group_id': '123456',
        'message': '戳一戳：测试群友戳了戳你',
        'history_entry': entries[0],
    }]
    assert event.sent == []


# 自己戳自己或私聊戳不会进入群历史。
def test_self_and_private_pokes_do_not_enter_group_history(inbound_config):
    inbound_config.update({
        'event_poke_enabled': True,
        'interaction_response_enabled': True,
    })
    self_event = FakeEvent(group_id='123456', user_id='10001', target_id='20002')
    private_event = FakeEvent(group_id=None, user_id='20002', target_id='10001')

    assassin_main.Event.poke(self_event, None)
    assassin_main.Event.poke(private_event, None)

    assert data.gMessageHistory == {}
    events = eventInbox.snapshot(BOT_HASH)['events']
    assert [event['scene'] for event in events] == ['private', 'group']
    assert self_event.sent == []
    assert private_event.sent == []


# 连续戳一戳使用固定两秒的历史窗口。
def test_consecutive_pokes_use_a_fixed_two_second_history_window(
    monkeypatch,
    inbound_config,
):
    inbound_config['event_poke_enabled'] = True
    clock = [100.0]
    monkeypatch.setattr(assassin_msg.time, 'monotonic', lambda: clock[0])
    event = FakeEvent(
        group_id='123456',
        user_id='20002',
        target_id='10001',
        sender={'nickname': '测试群友'},
    )

    assassin_main.Event.poke(event, None)
    clock[0] = 101.5
    assassin_main.Event.poke(event, None)
    assert len(list(assassin_msg._get_history(BOT_HASH, '123456') or [])) == 1

    clock[0] = 102.5
    assassin_main.Event.poke(event, None)
    entries = list(assassin_msg._get_history(BOT_HASH, '123456') or [])
    assert len(entries) == 2
    assert eventInbox.snapshot(BOT_HASH)['event_count'] == 3


# 新的历史事件会打断戳一戳去重。
def test_new_history_event_breaks_poke_deduplication(inbound_config):
    inbound_config['event_poke_enabled'] = True
    event = FakeEvent(
        group_id='123456',
        user_id='20002',
        target_id='10001',
        sender={'nickname': '测试群友'},
    )

    assassin_main.Event.poke(event, None)
    assassin_msg.add_message_to_history(
        '123456',
        '中间的新消息',
        '20003',
        '另一位群友',
        bot_hash=BOT_HASH,
        role='user',
    )
    assassin_main.Event.poke(event, None)

    entries = list(assassin_msg._get_history(BOT_HASH, '123456') or [])
    assert [entry['message'] for entry in entries] == [
        '戳一戳：测试群友戳了戳你',
        '中间的新消息',
        '戳一戳：测试群友戳了戳你',
    ]


# 戳一戳历史不可变，且不参与知识检索。
def test_poke_history_is_immutable_and_excluded_from_retrieval(inbound_config):
    inbound_config['event_poke_enabled'] = True
    existing = {
        'time': '2026-10-30T12:00:00+08:00',
        'user_id': '20002',
        'nickname': '测试群友',
        'message': '旧消息',
        'role': 'user',
    }
    assassin_msg._ensure_history(BOT_HASH, '123456')
    assassin_msg.add_message_to_history(
        '123456',
        existing['message'],
        existing['user_id'],
        existing['nickname'],
        bot_hash=BOT_HASH,
        role='user',
    )
    history = assassin_msg._get_history(BOT_HASH, '123456')
    original_entry = history[0]
    rendered_before = assassin_msg._render_history_entry(original_entry, True)
    event = FakeEvent(
        group_id='123456',
        user_id='20002',
        target_id='10001',
        sender={'nickname': '测试群友'},
    )

    assassin_main.Event.poke(event, None)

    assert history[0] is original_entry
    assert assassin_msg._render_history_entry(history[0], True) == rendered_before
    poke_rendered = assassin_msg._render_history_entry(history[1], True)
    poke_payload = json.loads(poke_rendered['content'])
    assert list(poke_payload) == [
        'time',
        'user_id',
        'nickname',
        'message',
        'message_type',
        'target_id',
        'target_is_self',
    ]
    assert poke_payload['message_type'] == 'poke'
    assert [
        entry['message'] for entry in assassin_msg._history_for_retrieval(list(history))
    ] == ['旧消息']


# 清空入站箱会重置戳一戳去重状态。
def test_event_inbox_clear_resets_poke_deduplication(inbound_config):
    inbound_config['event_poke_enabled'] = True
    event = FakeEvent(
        group_id='123456',
        user_id='20002',
        target_id='10001',
        sender={'nickname': '测试群友'},
    )
    assassin_main.Event.poke(event, None)
    assert data.gPokeDedupe

    eventInbox.clear_all()

    assert data.gPokeDedupe == {}
