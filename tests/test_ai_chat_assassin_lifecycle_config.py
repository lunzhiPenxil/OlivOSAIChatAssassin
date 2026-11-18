"""插件生命周期、静态知识库与配置持久化的离线覆盖率测试。"""

import copy
import json
import os
import sys
import time
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
from OlivOSAIChatAssassin import load  # noqa: E402


BOT_HASH = 'bot-config'


class RecordingManager:
    def __init__(self):
        self.loaded = []
        self.saved = []

    def load_bot(self, bot_hash):
        self.loaded.append(bot_hash)

    def save_bot_memory(self, bot_hash):
        self.saved.append(bot_hash)


def make_manager(tmp_path):
    return load.DataManager(
        config_dir=str(tmp_path / 'config'),
        memory_dir=str(tmp_path / 'memory'),
        default_config=copy.deepcopy(OlivOSAIChatAssassin.data.configDefault),
    )


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')


# 插件初始化会按固定顺序清空运行态并加载配置与静态知识。
def test_main_init_after_runs_lifecycle_in_order(monkeypatch):
    calls = []
    proc = SimpleNamespace(
        Proc_data={'bot_info_dict': {'bot-b': object(), 'bot-a': object()}}
    )
    monkeypatch.setattr(OlivOSAIChatAssassin.activity, 'clear_all', lambda: calls.append('activity'))
    monkeypatch.setattr(OlivOSAIChatAssassin.interaction, 'clear_all', lambda: calls.append('interaction'))
    monkeypatch.setattr(OlivOSAIChatAssassin.eventInbox, 'clear_all', lambda: calls.append('eventInbox'))
    monkeypatch.setattr(OlivOSAIChatAssassin.msg, 'clear_poke_state', lambda: calls.append('poke'))
    monkeypatch.setattr(
        OlivOSAIChatAssassin.data.gHistoryWindowGuard,
        'clear_all',
        lambda: calls.append('history-window'),
    )
    monkeypatch.setattr(load, 'load_init_after', lambda: calls.append('load-init'))
    monkeypatch.setattr(load, 'load_logger', lambda value: calls.append(('logger', value)))
    monkeypatch.setattr(OlivOSAIChatAssassin.manager, 'reset', lambda: calls.append('manager-reset'))
    monkeypatch.setattr(OlivOSAIChatAssassin.manager, 'reload', lambda: calls.append('manager-reload'))
    monkeypatch.setattr(
        OlivOSAIChatAssassin.webUI,
        'restore_webui_assets',
        lambda value: calls.append(('assets', value)),
    )
    monkeypatch.setattr(load, 'load_bot', lambda values: calls.append(('bots', values)))
    monkeypatch.setattr(load, 'load_staticKnowledge', lambda: calls.append('static-knowledge'))

    assert assassin_main.Event.init_after(None, proc) is None

    assert calls == [
        'activity',
        'interaction',
        'eventInbox',
        'poke',
        'history-window',
        'load-init',
        ('logger', proc),
        'manager-reset',
        'manager-reload',
        ('assets', proc),
        ('bots', ['bot-b', 'bot-a']),
        'static-knowledge',
    ]


# 空初始化回调不产生副作用。
def test_main_init_is_a_noop():
    assert assassin_main.Event.init(None, None) is None


# WebUI 已处理菜单时主入口直接返回。
def test_main_menu_returns_after_webui_handles_request(monkeypatch):
    event = SimpleNamespace(data=SimpleNamespace(namespace='OlivOSAIChatAssassin'))
    monkeypatch.setattr(OlivOSAIChatAssassin.webUI, 'handle_menu', lambda _event, _proc: True)

    assert assassin_main.Event.menu(event, None) is None


# 原生菜单事件由主入口识别后静默结束。
def test_main_menu_accepts_native_plugin_menu_event(monkeypatch):
    event = SimpleNamespace(
        data=SimpleNamespace(
            namespace='OlivOSAIChatAssassin',
            event='OlivOSAIChatAssassin_Menu',
        )
    )
    monkeypatch.setattr(OlivOSAIChatAssassin.webUI, 'handle_menu', lambda _event, _proc: False)

    assert assassin_main.Event.menu(event, None) is None


# 静态知识库只合并对象文件，并跳过类型错误与损坏 JSON。
def test_load_static_knowledge_merges_valid_files_and_skips_invalid(tmp_path, monkeypatch):
    knowledge_dir = tmp_path / 'Knowledge'
    knowledge_dir.mkdir()
    write_json(knowledge_dir / 'a.json', {'alpha': 'A'})
    write_json(knowledge_dir / 'b.json', {'beta': 'B'})
    write_json(knowledge_dir / 'list.json', ['ignored'])
    (knowledge_dir / 'broken.json').write_text('{broken', encoding='utf-8')
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gStaticKnowledgeDir', str(knowledge_dir))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gStaticKnowledge', {'old': 'keep'})

    load.load_staticKnowledge()

    assert OlivOSAIChatAssassin.data.gStaticKnowledge == {'alpha': 'A', 'beta': 'B'}


# 静态知识目录无法创建时保持空知识库。
def test_load_static_knowledge_handles_directory_failure(monkeypatch):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gStaticKnowledge', {'old': 'value'})

    def fail_makedirs(*args, **kwargs):
        raise OSError('permission denied')

    monkeypatch.setattr(load.os, 'makedirs', fail_makedirs)

    load.load_staticKnowledge()

    assert OlivOSAIChatAssassin.data.gStaticKnowledge == {}


# 数据管理器缺失时批量加载只记录失败，不抛出异常。
def test_load_bot_without_data_manager_is_safe(monkeypatch):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', None)

    assert load.load_bot(['bot-a']) is None


# 批量加载会把每个 bot 交给数据管理器。
def test_load_bot_delegates_each_bot(monkeypatch):
    manager = RecordingManager()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)

    load.load_bot(['bot-a', 'bot-b'])

    assert manager.loaded == ['bot-a', 'bot-b']


# 数据管理器缺失时记忆写入只记录失败，不抛出异常。
def test_write_memory_without_data_manager_is_safe(monkeypatch):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', None)

    assert load.write_memory(BOT_HASH) is None


# 记忆写入会委托给数据管理器。
def test_write_memory_delegates_to_data_manager(monkeypatch):
    manager = RecordingManager()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)

    load.write_memory(BOT_HASH)

    assert manager.saved == [BOT_HASH]


# 已加载 bot 的配置保存会写回专属文件并刷新 revision。
def test_save_bot_config_writes_loaded_bot_file(tmp_path):
    manager = make_manager(tmp_path)
    manager.load_bot(BOT_HASH)
    manager.config[BOT_HASH]['model'] = 'updated-model'

    manager.save_bot_config(BOT_HASH)

    path = tmp_path / 'config' / f'config_{BOT_HASH}.json'
    assert json.loads(path.read_text(encoding='utf-8'))['model'] == 'updated-model'
    assert manager._config_revision[BOT_HASH] == manager._revision_from_path(str(path))


# 未加载 bot 的配置保存会被拒绝。
def test_save_bot_config_rejects_unloaded_bot(tmp_path):
    manager = make_manager(tmp_path)

    with pytest.raises(KeyError, match='配置未加载'):
        manager.save_bot_config('missing-bot')


# 缺少已记录路径时配置保存会回退到默认专属文件名。
def test_save_bot_config_uses_default_path_when_path_is_missing(tmp_path):
    manager = make_manager(tmp_path)
    manager.config[BOT_HASH] = {'model': 'manual'}

    manager.save_bot_config(BOT_HASH)

    path = tmp_path / 'config' / f'config_{BOT_HASH}.json'
    assert json.loads(path.read_text(encoding='utf-8')) == {'model': 'manual'}


# 已加载 bot 的记忆保存会写回专属文件并刷新 revision。
def test_save_bot_memory_writes_loaded_bot_file(tmp_path):
    manager = make_manager(tmp_path)
    manager.load_bot(BOT_HASH)
    manager.memory[BOT_HASH]['group-1'] = '新记忆'

    manager.save_bot_memory(BOT_HASH)

    path = tmp_path / 'memory' / f'memory_{BOT_HASH}.json'
    assert json.loads(path.read_text(encoding='utf-8'))['group-1'] == '新记忆'
    assert manager._memory_revision[BOT_HASH] == manager._revision_from_path(str(path))


# 未加载 bot 的记忆保存会被拒绝。
def test_save_bot_memory_rejects_unloaded_bot(tmp_path):
    manager = make_manager(tmp_path)

    with pytest.raises(KeyError, match='记忆未加载'):
        manager.save_bot_memory('missing-bot')


# force 重载会无条件重新加载指定 bot。
def test_data_manager_reload_force_reloads_bot(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    calls = []
    monkeypatch.setattr(manager, 'load_bot', lambda bot_hash, force=False: calls.append(bot_hash))

    manager.reload(BOT_HASH, force=True)

    assert calls == [BOT_HASH]


# 配置文件时间更新后会触发热重载。
def test_data_manager_reload_detects_config_change(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    manager.load_bot(BOT_HASH)
    calls = []
    monkeypatch.setattr(manager, 'load_bot', lambda bot_hash, force=False: calls.append(bot_hash))
    future = time.time() + 10
    os.utime(manager._config_path[BOT_HASH], (future, future))

    manager.reload()

    assert calls == [BOT_HASH]


# 记忆文件时间更新后会触发热重载。
def test_data_manager_reload_detects_memory_change(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    manager.load_bot(BOT_HASH)
    calls = []
    monkeypatch.setattr(manager, 'load_bot', lambda bot_hash, force=False: calls.append(bot_hash))
    future = time.time() + 10
    os.utime(manager._memory_path[BOT_HASH], (future, future))

    manager.reload()

    assert calls == [BOT_HASH]


# 配置文件消失后会触发热重载。
def test_data_manager_reload_detects_missing_config(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    manager.load_bot(BOT_HASH)
    calls = []
    monkeypatch.setattr(manager, 'load_bot', lambda bot_hash, force=False: calls.append(bot_hash))
    os.remove(manager._config_path[BOT_HASH])

    manager.reload(BOT_HASH)

    assert calls == [BOT_HASH]


# 记忆文件消失后会触发热重载。
def test_data_manager_reload_detects_missing_memory(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    manager.load_bot(BOT_HASH)
    calls = []
    monkeypatch.setattr(manager, 'load_bot', lambda bot_hash, force=False: calls.append(bot_hash))
    os.remove(manager._memory_path[BOT_HASH])

    manager.reload(BOT_HASH)

    assert calls == [BOT_HASH]


# 只加载了配置容器时会补齐记忆并触发热重载。
def test_data_manager_reload_loads_partially_loaded_bot(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    manager.config[BOT_HASH] = {}
    calls = []
    monkeypatch.setattr(manager, 'load_bot', lambda bot_hash, force=False: calls.append(bot_hash))

    manager.reload(BOT_HASH)

    assert calls == [BOT_HASH]


# 配置和记忆都未变化时不会重复加载。
def test_data_manager_reload_skips_unchanged_bot(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    manager.load_bot(BOT_HASH)
    calls = []
    monkeypatch.setattr(manager, 'load_bot', lambda bot_hash, force=False: calls.append(bot_hash))

    manager.reload(BOT_HASH)

    assert calls == []


# 缺失文件的 revision 为空字符串。
def test_revision_from_missing_path_returns_empty(tmp_path):
    assert load.DataManager._revision_from_path(str(tmp_path / 'missing.json')) == ''
