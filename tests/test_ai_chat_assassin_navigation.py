import copy
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_APP = REPO_ROOT
PLUGIN_ROOT = PLUGIN_APP / 'OlivOSAIChatAssassin'
WEBUI_HTML = (PLUGIN_ROOT / 'webui' / 'index.html').read_text(encoding='utf-8')
if not (PLUGIN_ROOT / 'app.json').is_file():
    pytest.skip('OlivOSAIChatAssassin 未安装，跳过插件专项测试', allow_module_level=True)
if str(PLUGIN_APP) not in sys.path:
    sys.path.insert(0, str(PLUGIN_APP))

import OlivOSAIChatAssassin  # noqa: E402
from OlivOSAIChatAssassin import load, webUI  # noqa: E402


class FakeEvent:
    def __init__(self, event_name, payload=None, session='private-session', request_id='nav-1'):
        webui_context = {'request_id': request_id, 'payload': payload}
        if session is not None:
            webui_context['session'] = session
        self.data = SimpleNamespace(
            namespace=webUI.NAMESPACE,
            event=event_name,
            payload=payload,
            webui=webui_context,
        )
        self.bot_info = None
        self.sent = []

    def send(self, send_type, target_id, message):
        self.sent.append((send_type, target_id, message))
        return True


def call_bridge(event_name, payload, session='private-session'):
    event = FakeEvent(event_name, payload, session=session)
    proc = SimpleNamespace(Proc_data={'bot_info_dict': {}})
    assert webUI.handle_menu(event, proc) is True
    assert len(event.sent) == 1
    send_type, request_id, response = event.sent[0]
    assert send_type == 'webui'
    assert request_id == 'nav-1'
    return response


def frontend_function(name, next_name):
    start = WEBUI_HTML.index(f'function {name}(')
    end = WEBUI_HTML.index(f'function {next_name}(', start + 1)
    return WEBUI_HTML[start:end]


def api_payload(**values):
    payload = {'api_version': 1}
    payload.update(values)
    return payload


def navigation_snapshot(client_updated_at=1, active_tab='memory', scope='group_index'):
    section = '用户侧写' if scope == 'global' else None
    return {
        'version': 1,
        'client_updated_at': client_updated_at,
        'bot_hash': 'bot-hash-001',
        'active_tab': active_tab,
        'overview': {
            'activity_fullscreen': False,
            'inbound_fullscreen': False,
            'inbound_page': 0,
        },
        'memory': {
            'scope': scope,
            'section': section,
            'group_index_offset': 50,
        },
    }


@pytest.fixture
def navigation_path(tmp_path, monkeypatch):
    path = tmp_path / 'ui_navigation.json'
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gNavigationPath', str(path))
    return path


def test_navigation_round_trips_for_same_webui_session(navigation_path):
    snapshot = navigation_snapshot(active_tab='overview')
    snapshot['overview'] = {
        'activity_fullscreen': True,
        'inbound_fullscreen': False,
        'inbound_page': 3,
    }

    saved = call_bridge(webUI.EVENT_SAVE_NAVIGATION, api_payload(snapshot=snapshot))
    assert saved['ok'] is True
    assert saved['data']['saved'] is True

    loaded = call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload())
    assert loaded['ok'] is True
    assert loaded['data']['navigation'] == snapshot


def test_navigation_is_isolated_between_webui_sessions(navigation_path):
    first = navigation_snapshot(client_updated_at=10)
    second = navigation_snapshot(client_updated_at=20, scope='global')

    assert call_bridge(
        webUI.EVENT_SAVE_NAVIGATION,
        api_payload(snapshot=first),
        session='session-a',
    )['data']['saved'] is True
    assert call_bridge(
        webUI.EVENT_SAVE_NAVIGATION,
        api_payload(snapshot=second),
        session='session-b',
    )['data']['saved'] is True

    first_result = call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload(), session='session-a')
    second_result = call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload(), session='session-b')
    third_result = call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload(), session='session-c')

    assert first_result['data']['navigation'] == first
    assert second_result['data']['navigation'] == second
    assert third_result['data']['navigation'] is None


def test_navigation_stores_only_session_digest_and_whitelisted_fields(navigation_path):
    snapshot = navigation_snapshot(client_updated_at=11)
    snapshot['search'] = '不会落盘的搜索词'
    snapshot['overview']['message'] = '不会落盘的消息正文'
    snapshot['memory']['secret'] = '不会落盘的记忆正文'

    result = call_bridge(webUI.EVENT_SAVE_NAVIGATION, api_payload(snapshot=snapshot))
    assert result['data']['saved'] is True

    raw = navigation_path.read_text(encoding='utf-8')
    document = json.loads(raw)
    session_key = hashlib.sha256(b'private-session').hexdigest()[:32]
    assert 'private-session' not in raw
    assert session_key in document['sessions']
    stored = document['sessions'][session_key]['snapshot']
    assert stored['client_updated_at'] == 11
    assert 'search' not in stored
    assert 'message' not in stored['overview']
    assert 'secret' not in stored['memory']


def test_navigation_rejects_older_client_snapshot(navigation_path):
    newest = navigation_snapshot(client_updated_at=50, active_tab='settings')
    older = navigation_snapshot(client_updated_at=49, active_tab='about')

    assert call_bridge(
        webUI.EVENT_SAVE_NAVIGATION,
        api_payload(snapshot=newest),
    )['data']['saved'] is True
    rejected = call_bridge(webUI.EVENT_SAVE_NAVIGATION, api_payload(snapshot=older))
    assert rejected['ok'] is True
    assert rejected['data']['saved'] is False

    loaded = call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload())
    assert loaded['data']['navigation']['active_tab'] == 'settings'


@pytest.mark.parametrize(
    'mutate',
    (
        lambda value: value.pop('active_tab'),
        lambda value: value['overview'].update(activity_fullscreen='yes'),
        lambda value: value['overview'].update(inbound_page=-1),
        lambda value: value['memory'].update(scope='group'),
        lambda value: value['memory'].update(scope='global', section='x' * 257),
        lambda value: value.update(client_updated_at=True),
    ),
)
def test_navigation_rejects_invalid_snapshots(navigation_path, mutate):
    snapshot = navigation_snapshot()
    mutate(snapshot)

    response = call_bridge(webUI.EVENT_SAVE_NAVIGATION, api_payload(snapshot=snapshot))
    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'
    assert not navigation_path.exists()


@pytest.mark.parametrize(
    'document',
    (
        'not-json',
        json.dumps({'version': 99, 'sessions': {}}),
        json.dumps({'version': 1, 'sessions': []}),
        json.dumps({'version': 1, 'sessions': {'broken': {'snapshot': {}}}}),
    ),
)
def test_navigation_corrupt_file_falls_back_to_empty(navigation_path, document):
    navigation_path.write_text(document, encoding='utf-8')
    response = call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload())
    assert response['ok'] is True
    assert response['data']['navigation'] is None


def test_navigation_store_prunes_expired_and_caps_sessions(navigation_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(webUI.time, 'time', lambda: clock[0])
    store = webUI.NavigationStore(str(navigation_path))

    assert store.save('expired-session', navigation_snapshot(client_updated_at=1)) is True
    clock[0] += webUI.NAVIGATION_TTL_SECONDS + 1
    assert store.get('expired-session') is None

    for index in range(webUI.NAVIGATION_SESSION_LIMIT + 1):
        clock[0] += 1
        snapshot = navigation_snapshot(client_updated_at=index + 2)
        assert store.save(f'session-{index}', snapshot) is True

    document = json.loads(navigation_path.read_text(encoding='utf-8'))
    assert len(document['sessions']) == webUI.NAVIGATION_SESSION_LIMIT
    assert hashlib.sha256(b'expired-session').hexdigest()[:32] not in document['sessions']


def test_navigation_save_failure_is_silent(navigation_path, monkeypatch):
    def fail_write(_path, _data):
        raise OSError('disk unavailable')

    monkeypatch.setattr(load.DataManager, '_write_json_atomic', fail_write)
    response = call_bridge(
        webUI.EVENT_SAVE_NAVIGATION,
        api_payload(snapshot=navigation_snapshot(client_updated_at=5)),
    )
    assert response['ok'] is True
    assert response['data']['saved'] is False
    assert not navigation_path.exists()


def test_navigation_without_webui_session_is_ignored(navigation_path):
    saved = call_bridge(
        webUI.EVENT_SAVE_NAVIGATION,
        api_payload(snapshot=navigation_snapshot(client_updated_at=5)),
        session=None,
    )
    loaded = call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload(), session=None)

    assert saved['ok'] is True
    assert saved['data']['saved'] is False
    assert loaded['ok'] is True
    assert loaded['data']['navigation'] is None
    assert not navigation_path.exists()


def test_navigation_snapshot_deep_copy_does_not_alias_store(navigation_path):
    snapshot = navigation_snapshot(client_updated_at=7, scope='global')
    assert call_bridge(webUI.EVENT_SAVE_NAVIGATION, api_payload(snapshot=snapshot))['ok'] is True
    loaded = call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload())['data']['navigation']
    assert loaded == snapshot
    loaded['memory']['section'] = '被修改'
    assert copy.deepcopy(snapshot)['memory']['section'] == '用户侧写'
    assert call_bridge(webUI.EVENT_GET_NAVIGATION, api_payload())['data']['navigation'] == snapshot


def test_navigation_store_instances_share_write_lock(navigation_path, monkeypatch):
    original_write = load.DataManager._write_json_atomic

    def slow_write(path, document):
        time.sleep(0.01)
        original_write(path, document)

    monkeypatch.setattr(load.DataManager, '_write_json_atomic', slow_write)
    session_count = 12
    barrier = threading.Barrier(session_count)

    def save(index):
        barrier.wait(timeout=5)
        store = webUI.NavigationStore(str(navigation_path))
        return store.save(
            f'parallel-session-{index}',
            navigation_snapshot(client_updated_at=index + 1),
        )

    with ThreadPoolExecutor(max_workers=session_count) as executor:
        results = list(executor.map(save, range(session_count)))

    assert all(results)
    document = json.loads(navigation_path.read_text(encoding='utf-8'))
    for index in range(session_count):
        key = hashlib.sha256(f'parallel-session-{index}'.encode('utf-8')).hexdigest()[:32]
        assert key in document['sessions']


def test_navigation_frontend_loads_snapshot_before_bootstrap_and_restores_without_save():
    initial = frontend_function('loadInitialState', 'loadBootstrap')
    assert initial.index('await loadNavigationSnapshot();') < initial.index('await loadBootstrap();')
    assert 'state.navigationRestoring = true;' in initial
    assert 'state.navigationReady = true;' in initial

    scheduler = frontend_function('scheduleNavigationSave', 'flushNavigationSaveQueue')
    assert 'state.navigationRestoring || !state.navigationReady || !state.ready' in scheduler


def test_navigation_frontend_snapshot_keeps_only_navigation_state():
    snapshot = frontend_function('buildNavigationSnapshot', 'scheduleNavigationSave')
    for key in ('bot_hash', 'active_tab', 'overview', 'memory', 'group_index_offset'):
        assert key in snapshot
    for forbidden in ('search:', 'image_types', 'draft:', 'content:', 'message:', 'reasoning:'):
        assert forbidden not in snapshot


def test_navigation_frontend_persists_tab_fullscreen_pages_memory_and_bot():
    expected_calls = (
        ('selectBot', 'loadBotData'),
        ('activateTab', 'showBanner'),
        ('changeInboundPage', 'openInboundFullscreen'),
        ('openInboundFullscreen', 'closeInboundFullscreen'),
        ('closeInboundFullscreen', 'handleInboundFullscreenKeydown'),
        ('openActivityFullscreen', 'closeActivityFullscreen'),
        ('closeActivityFullscreen', 'handleActivityFullscreenKeydown'),
        ('loadMemorySection', 'groupManagerView'),
    )
    for name, next_name in expected_calls:
        assert 'scheduleNavigationSave();' in frontend_function(name, next_name)

    reset_activity = frontend_function('resetActivityState', 'loadActivity')
    reset_inbound = frontend_function('resetInboundState', 'loadInboundEvents')
    assert 'closeActivityFullscreen(false, {persist: false});' in reset_activity
    assert 'closeInboundFullscreen(false, {persist: false});' in reset_inbound


def test_navigation_frontend_memory_fallback_and_group_page_clamping():
    restore = frontend_function('restoreMemoryNavigation', 'restoreNavigationSnapshot')
    assert 'if (loaded) {' in restore
    assert 'loadMemorySection(\'global\', saved.section, 0, null)' in restore
    assert 'loadMemorySection(\'group_index\', null, offset, null)' in restore
    assert 'saved.scope === \'group\'' not in restore

    clamp = frontend_function('clampGroupMemoryOffset', 'restoreMemoryNavigation')
    assert 'Math.floor((safeTotal - 1) / MEMORY_PAGE_SIZE) * MEMORY_PAGE_SIZE' in clamp
    assert 'Math.min(requested - (requested % MEMORY_PAGE_SIZE), maxOffset)' in clamp


def test_navigation_frontend_restores_bot_and_fullscreen_without_new_saves():
    bootstrap = frontend_function('loadBootstrap', 'renderBotOptions')
    assert 'navigationTargetBotHash()' in bootstrap
    assert 'bot.hash === savedBotHash' in bootstrap
    assert '|| targetBot' in bootstrap

    restore = frontend_function('restoreOverviewNavigation', 'globalMemorySectionAvailable')
    assert 'page: Math.max(0, Number(overview.inbound_page) || 0)' in restore
    assert 'persist: false' in restore
    assert 'focus: false' in restore


def test_navigation_webui_keeps_a_single_frontend_entry():
    assert sorted(path.name for path in (PLUGIN_ROOT / 'webui').iterdir()) == ['index.html']
    assert not (PLUGIN_ROOT / 'index.html').exists()
