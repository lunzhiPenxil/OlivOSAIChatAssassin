"""群聊刺客历史队列持久化测试。"""

import json
import os
import sys
import threading
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
from OlivOSAIChatAssassin import data, load  # noqa: E402
from OlivOSAIChatAssassin import main as assassin_main  # noqa: E402
from OlivOSAIChatAssassin import msg  # noqa: E402


BOT_HASH = 'history-bot'
GROUP_ID = '123456789'


def make_config(**overrides):
    config = {
        'history_size': 2,
        'history_dynamic': True,
        'history_dynamic_size': 4,
        'history_size_min': 1,
        'max_message_length': 2048,
    }
    config.update(overrides)
    return config


class FakeData:
    def __init__(self, config=None):
        self.config = config or make_config()

    def reload(self, _bot_hash):
        return None

    def getConfig(self, _bot_hash):
        return self.config

    def getMemory(self, _bot_hash):
        return {'全局': {}}


@pytest.fixture(autouse=True)
def quiet_logger(monkeypatch):
    events = []
    logger = SimpleNamespace(
        log=lambda message: events.append(('log', message)),
        warn=lambda message: events.append(('warn', message)),
        logRaw=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(OlivOSAIChatAssassin, 'logger', logger)
    return events


@pytest.fixture
def history_env(tmp_path, monkeypatch):
    store = load.HistoryStore(str(tmp_path / 'history'))
    monkeypatch.setattr(data, 'gHistoryStore', store)
    monkeypatch.setattr(data, 'gMessageHistory', {})
    monkeypatch.setattr(data, 'gGroupLock', {})
    monkeypatch.setattr(data, 'gData', FakeData())
    return store


def make_entry(index, message=None, role='user', **extra):
    entry = {
        'timestamp': 1700000000 + index,
        'time': f'2026-11-03T12:00:{index:02d}+08:00',
        'user_id': f'user-{index}',
        'nickname': f'群友{index}',
        'message': message if message is not None else f'消息{index}',
        'role': role,
    }
    entry.update(extra)
    return entry


def history_messages(history):
    return [entry['message'] for entry in history]


# 历史目录名使用首字母大写的 History。
def test_history_directory_uses_capitalized_name():
    assert data.gHistoryDir == './plugin/data/OlivOSAIChatAssassin/History'


# 历史文件不存在时不会主动创建。
def test_missing_history_file_is_not_created(history_env):
    assert history_env.load(BOT_HASH, GROUP_ID) is None
    assert not Path(history_env.history_dir).exists()


# 历史读写往返会保留约定的字段。
def test_history_round_trip_preserves_contract_fields(history_env):
    entries = [
        make_entry(1, message_id='message-1'),
        make_entry(
            2,
            message='戳一戳：群友2戳了戳你',
            message_type='poke',
            target_id='10001',
            target_is_self=True,
        ),
        make_entry(
            3,
            message='回复内容',
            role='assistant',
            user_id=None,
            nickname=None,
            replies=['回复内容', '第二段'],
            reply_to={'message_id': 'message-2', 'user_id': 'user-2'},
            unknown='drop',
        ),
    ]

    assert history_env.save(BOT_HASH, GROUP_ID, entries) is True
    reloaded_store = load.HistoryStore(history_env.history_dir)
    loaded = reloaded_store.load(BOT_HASH, GROUP_ID)

    assert loaded[0] == entries[0]
    assert loaded[1] == entries[1]
    assert loaded[2]['replies'] == ['回复内容', '第二段']
    assert loaded[2]['reply_to'] == {'message_id': 'message-2', 'user_id': 'user-2'}
    assert 'unknown' not in loaded[2]


# 缺少 role 的旧记录会推断为用户消息。
@pytest.mark.parametrize(
    'entry, expected_role',
    (
        ({'message': '旧用户消息', 'nickname': '旧群友'}, 'user'),
        ({'message': '旧回复', 'nickname': None}, 'assistant'),
    ),
)
def test_legacy_entry_without_role_is_inferred(history_env, entry, expected_role):
    assert history_env.save(BOT_HASH, GROUP_ID, [entry]) is True

    loaded = history_env.load(BOT_HASH, GROUP_ID)

    assert loaded[0]['role'] == expected_role


# 无效历史文档会被忽略。
@pytest.mark.parametrize(
    'payload',
    (
        '{',
        json.dumps({'version': 2, 'bot_hash': BOT_HASH, 'group_id': GROUP_ID, 'entries': []}),
        json.dumps({'version': 1, 'bot_hash': 'other', 'group_id': GROUP_ID, 'entries': []}),
        json.dumps({'version': 1, 'bot_hash': BOT_HASH, 'group_id': GROUP_ID, 'entries': 'bad'}),
        json.dumps({'version': 1, 'bot_hash': BOT_HASH, 'group_id': GROUP_ID, 'entries': [1, {}]}),
    ),
)
def test_invalid_history_documents_are_ignored(history_env, payload):
    path = Path(history_env.history_path(BOT_HASH, GROUP_ID))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding='utf-8')

    assert history_env.load(BOT_HASH, GROUP_ID) == []


# 延迟恢复时会裁剪到当前上下文窗口。
def test_lazy_restore_trims_to_current_window(history_env, monkeypatch):
    monkeypatch.setattr(
        data,
        'gData',
        FakeData(make_config(history_size=2, history_dynamic_size=4)),
    )
    history_env.save(
        BOT_HASH,
        GROUP_ID,
        [make_entry(index) for index in range(1, 7)],
    )

    history = msg._ensure_history(BOT_HASH, GROUP_ID)

    assert history_messages(history) == ['消息3', '消息4', '消息5', '消息6']
    msg.add_message_to_history(
        GROUP_ID,
        '消息7',
        'user-7',
        '群友7',
        bot_hash=BOT_HASH,
    )
    assert history_messages(history) == ['消息6', '消息7']


# 同群内不同 bot 的历史互相隔离。
def test_lazy_restore_separates_bots_for_same_group(history_env):
    history_env.save('bot-a', GROUP_ID, [make_entry(1, message='A')])
    history_env.save('bot-b', GROUP_ID, [make_entry(2, message='B')])

    history_a = msg._ensure_history('bot-a', GROUP_ID)
    history_b = msg._ensure_history('bot-b', GROUP_ID)

    assert history_a is not history_b
    assert history_messages(history_a) == ['A']
    assert history_messages(history_b) == ['B']


# 记忆重置不会删除用户历史。
def test_user_history_survives_memory_reset(history_env, monkeypatch):
    monkeypatch.setattr(
        data,
        'gData',
        FakeData(make_config(history_size=8, history_dynamic_size=16)),
    )
    msg._ensure_history(BOT_HASH, GROUP_ID)
    for index in range(1, 7):
        msg.add_message_to_history(
            GROUP_ID,
            f'消息{index}',
            f'user-{index}',
            f'群友{index}',
            bot_hash=BOT_HASH,
        )

    monkeypatch.setattr(data, 'gMessageHistory', {})
    restored = msg._ensure_history(BOT_HASH, GROUP_ID)

    assert len(restored) == 6
    assert history_messages(restored) == [f'消息{index}' for index in range(1, 7)]


# assistant 消息只有显式提交后才会持久化。
def test_assistant_is_only_persisted_after_explicit_commit(history_env):
    msg._ensure_history(BOT_HASH, GROUP_ID)
    msg.add_message_to_history(
        GROUP_ID,
        '用户消息',
        'user-1',
        '群友1',
        bot_hash=BOT_HASH,
    )
    msg.add_message_to_history(
        GROUP_ID,
        '尚未发送的回复',
        None,
        None,
        bot_hash=BOT_HASH,
        role='assistant',
        replies=['尚未发送的回复'],
        persist=False,
    )

    assert [entry['role'] for entry in history_env.load(BOT_HASH, GROUP_ID)] == ['user']

    assert msg._persist_history(BOT_HASH, GROUP_ID) is True
    assert [entry['role'] for entry in history_env.load(BOT_HASH, GROUP_ID)] == [
        'user',
        'assistant',
    ]


# 丢弃 assistant 消息后会重写持久化历史。
def test_discard_assistant_rewrites_persisted_history(history_env):
    history = msg._ensure_history(BOT_HASH, GROUP_ID)
    msg.add_message_to_history(
        GROUP_ID,
        '用户消息',
        'user-1',
        '群友1',
        bot_hash=BOT_HASH,
    )
    assistant_entry = msg.add_message_to_history(
        GROUP_ID,
        '不应保留',
        None,
        None,
        bot_hash=BOT_HASH,
        role='assistant',
        replies=['不应保留'],
        persist=False,
    )
    msg._persist_history(BOT_HASH, GROUP_ID, history)

    assert msg._discard_history_entry(
        history,
        assistant_entry,
        bot_hash=BOT_HASH,
        group_id=GROUP_ID,
    ) is True

    loaded = history_env.load(BOT_HASH, GROUP_ID)
    assert [entry['role'] for entry in loaded] == ['user']


# 写入失败时保留内存状态，并在刷新时重试。
def test_write_failure_keeps_memory_and_flush_retries(history_env, monkeypatch):
    original_write = load.DataManager._write_json_atomic
    state = {'fail': True}

    def flaky_write(path, document):
        if state['fail']:
            state['fail'] = False
            raise OSError('disk full')
        return original_write(path, document)

    monkeypatch.setattr(
        load.DataManager,
        '_write_json_atomic',
        staticmethod(flaky_write),
    )
    msg._ensure_history(BOT_HASH, GROUP_ID)
    entry = msg.add_message_to_history(
        GROUP_ID,
        '仍应进入内存',
        'user-1',
        '群友1',
        bot_hash=BOT_HASH,
    )

    assert entry is not None
    assert history_messages(msg._get_history(BOT_HASH, GROUP_ID)) == ['仍应进入内存']
    assert history_env.pending_keys() == [(BOT_HASH, GROUP_ID)]

    assert load.flush_history() == 1
    assert history_env.pending_keys() == []
    assert history_messages(history_env.load(BOT_HASH, GROUP_ID)) == ['仍应进入内存']


# 恶意会话 ID 不会逃逸历史目录。
def test_history_path_is_safe_for_hostile_ids(history_env):
    path = history_env.history_path('bot/../x', '../../群 : 1')
    base = os.path.abspath(history_env.history_dir)

    assert os.path.commonpath([base, path]) == base
    assert path.startswith(base + os.sep)


# 并发保存仍会生成合法 JSON。
def test_concurrent_saves_keep_valid_json(history_env):
    errors = []

    def save(index):
        try:
            history_env.save(BOT_HASH, GROUP_ID, [make_entry(index, message=f'并发{index}')])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2.0)

    assert errors == []
    path = Path(history_env.history_path(BOT_HASH, GROUP_ID))
    document = json.loads(path.read_text(encoding='utf-8'))
    assert document['version'] == 1
    assert len(document['entries']) == 1


# 主流程保存会先刷新历史再清理内存。
def test_main_save_flushes_history_before_clearing(history_env, monkeypatch):
    calls = []
    monkeypatch.setattr(load, 'flush_history', lambda: calls.append('history'))
    monkeypatch.setattr(OlivOSAIChatAssassin.activity, 'clear_all', lambda: calls.append('activity'))
    monkeypatch.setattr(OlivOSAIChatAssassin.interaction, 'clear_all', lambda: calls.append('interaction'))
    monkeypatch.setattr(OlivOSAIChatAssassin.eventInbox, 'clear_all', lambda: calls.append('inbox'))
    monkeypatch.setattr(msg, 'clear_poke_state', lambda: calls.append('poke'))
    monkeypatch.setattr(data.gHistoryWindowGuard, 'clear_all', lambda: calls.append('guard'))

    assassin_main.Event.save(None, None)

    assert calls[0] == 'history'
    assert calls[1:] == ['activity', 'interaction', 'inbox', 'poke', 'guard']


# 插件初始化时只初始化历史存储，不扫描已有历史。
def test_load_init_after_initializes_store_without_scanning(tmp_path, monkeypatch):
    history_dir = tmp_path / 'history-store'
    monkeypatch.setattr(data, 'gHistoryDir', str(history_dir))
    monkeypatch.setattr(data, 'gHistoryStore', None)
    monkeypatch.setattr(data, 'gData', None)

    load.load_init_after()

    assert isinstance(data.gHistoryStore, load.HistoryStore)
    assert data.gHistoryStore.history_dir == str(history_dir)
    assert not history_dir.exists()
