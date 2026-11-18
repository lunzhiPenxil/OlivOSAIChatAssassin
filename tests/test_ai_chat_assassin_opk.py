'''Build the real OlivOSAIChatAssassin plugin as an OPK and exercise its WebUI.'''

import json
import os
import queue
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from OlivOS import pluginAPI
from webui_test_support import make_host


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / 'OlivOSAIChatAssassin'
NAMESPACE = 'OlivOSAIChatAssassin'
PACKAGE_IGNORED_PARTS = {'.codex', '__pycache__'}
PACKAGE_IGNORED_SUFFIXES = {'.pyc', '.pyo'}

if not (PLUGIN_ROOT / 'app.json').is_file():
    pytest.skip('OlivOSAIChatAssassin is not installed', allow_module_level=True)


def build_opk(destination):
    files = []
    for path in sorted(PLUGIN_ROOT.rglob('*')):
        relative = path.relative_to(PLUGIN_ROOT)
        if not path.is_file():
            continue
        if any(part in PACKAGE_IGNORED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in PACKAGE_IGNORED_SUFFIXES:
            continue
        files.append((path, relative.as_posix()))
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files:
            archive.write(path, relative)
    return files


@pytest.fixture
def opk_installation(tmp_path, monkeypatch):
    app_dir = tmp_path / 'plugin' / 'app'
    tmp_dir = tmp_path / 'plugin' / 'tmp'
    app_dir.mkdir(parents=True)
    tmp_dir.mkdir(parents=True)
    monkeypatch.setattr(pluginAPI, 'plugin_path', str(app_dir) + os.sep)
    monkeypatch.setattr(pluginAPI, 'plugin_path_tmp', str(tmp_dir) + os.sep)
    monkeypatch.syspath_prepend(str(tmp_dir))
    monkeypatch.syspath_prepend(str(app_dir))

    package = app_dir / f'{NAMESPACE}.opk'
    files = build_opk(package)
    host = make_host(
        root_path=tmp_path,
        rx_queue=queue.Queue(),
        control_queue=queue.Queue(),
        server_conf={'port': 0, 'host': '127.0.0.1'},
    )
    loader = pluginAPI.shallow(control_queue=host.Proc_info.control_queue)
    loader.database = SimpleNamespace(_init_namespace=lambda namespace: None)

    previous = {
        name: module
        for name, module in sys.modules.items()
        if name == NAMESPACE or name.startswith(NAMESPACE + '.')
    }
    for name in previous:
        sys.modules.pop(name)
    try:
        yield host, loader, package, files
    finally:
        host.on_terminate()
        for name in list(sys.modules):
            if name == NAMESPACE or name.startswith(NAMESPACE + '.'):
                sys.modules.pop(name)
        sys.modules.update(previous)


# 真实 OPK 可以被加载，并提供单页 WebUI。
def test_real_plugin_opk_loads_and_serves_single_webui_page(opk_installation):
    host, loader, package, packaged_files = opk_installation
    packaged_names = {relative for _, relative in packaged_files}
    with zipfile.ZipFile(package) as archive:
        archive_names = set(archive.namelist())
    assert archive_names == packaged_names
    assert 'app.json' in archive_names
    assert 'webui/index.html' in archive_names
    assert not any(name.startswith(NAMESPACE + '/') for name in archive_names)
    assert not any(name.startswith('.codex/') for name in archive_names)

    loader.load_plugin_list()
    plugin = loader.plugin_models_dict[NAMESPACE]
    module = plugin['model']
    webui_root = Path(plugin['webui_root'])
    expected_page = (PLUGIN_ROOT / 'webui' / 'index.html').read_bytes()
    cached_page = webui_root / 'webui' / 'index.html'

    assert Path(module.__file__).parts[-2] == NAMESPACE
    assert not (host.root / 'plugin' / 'tmp' / NAMESPACE).exists()
    assert webui_root.parent == host.root / 'plugin' / 'cache' / 'webui' / NAMESPACE
    assert {
        path.relative_to(webui_root).as_posix()
        for path in webui_root.rglob('*')
        if path.is_file()
    } == {'webui/index.html'}
    assert cached_page.read_bytes() == expected_page
    assert not (webui_root / 'index.html').exists()
    assert not (webui_root / 'assets').exists()

    # Older hosts can lose the generated page cache before init_after; the import-time
    # snapshot must recreate the declared single-file page without recreating OPK files.
    cached_page.unlink()
    module.main.Event.init_after(None, loader)
    assert cached_page.read_bytes() == expected_page

    loader.sendPluginList(ready=True)
    while not host.Proc_info.control_queue.empty():
        host.consume(host.Proc_info.control_queue.get_nowait())

    client = host.app.test_client()
    assert client.get(f'/plugin/{NAMESPACE}/webui/index.html').status_code == 401
    login = client.post('/api/login', headers={'X-Auth-Token': host.test_token})
    assert login.status_code == 200
    ticket = None
    if hasattr(host, 'new_plugin_ticket') and hasattr(host, 'plugin_ticket_session'):
        session = login.json['session']
        ticket_response = client.post(
            '/api/plugin_ticket',
            headers={'X-Auth-Token': host.test_token},
            json={'session': session, 'namespace': NAMESPACE},
        )
        assert ticket_response.status_code == 200
        ticket = ticket_response.json['ticket']

        # The iframe is an opaque origin, so its document must work with a path ticket
        # instead of relying on a host cookie.
        opaque_client = host.app.test_client()
        page = opaque_client.get(f'/plugin/{NAMESPACE}/webui/index.html?plugin_ticket={ticket}')
    else:
        # Released OlivOS hosts may not expose path tickets yet. Keep the OPK/page
        # contract covered through the cookie fallback until the host capability lands.
        page = client.get(f'/plugin/{NAMESPACE}/webui/index.html')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    if ticket is not None:
        assert f'/plugin_asset/{ticket}/{NAMESPACE}/webui/' in html
    for marker in (
        'memorySectionList',
        'group_index',
        '\u8fd4\u56de\u7fa4\u5217\u8868',
        'id="interactionResponseEnabled"',
        'data-field="interaction_response_enabled"',
        '<style>',
        '<script>',
    ):
        assert marker in html
    assert './assets/' not in html
    assert module.data.configDefault['event_poke_enabled'] is False
    assert module.data.configDefault['interaction_response_enabled'] is False
    assert client.get(
        f'/plugin/{NAMESPACE}/index.html',
        follow_redirects=True,
    ).status_code == 404

    manifest = json.loads((PLUGIN_ROOT / 'app.json').read_text(encoding='utf-8'))
    assert host.plugin_pages == [
        {
            'title': manifest['webui_config'][0]['title'],
            'type': 'iframe',
            'path': 'webui/index.html',
            'namespace': NAMESPACE,
        }
    ]


# 真实 OPK 中的戳一戳复用共享历史与回复链路。
def test_real_plugin_opk_routes_poke_through_shared_history_and_reply_path(
    opk_installation,
    tmp_path,
    monkeypatch,
):
    host, loader, package, packaged_files = opk_installation
    loader.load_plugin_list()
    module = loader.plugin_models_dict[NAMESPACE]['model']
    bot_hash = 'opk-poke-bot'
    group_id = '123456789'
    manager = module.load.DataManager(
        config_dir=str(tmp_path / 'poke-config'),
        memory_dir=str(tmp_path / 'poke-memory'),
        default_config=dict(module.data.configDefault),
    )
    manager.load_bot(bot_hash)
    manager.config[bot_hash].update({
        'event_poke_enabled': True,
        'interaction_response_enabled': True,
        'enabled_groups': [group_id],
    })
    manager.save_bot_config(bot_hash)

    monkeypatch.setattr(module.data, 'gData', manager)
    monkeypatch.setattr(module.data, 'gMessageHistory', {})
    monkeypatch.setattr(module.data, 'gGroupLock', {})
    module.data.gHistoryWindowGuard.clear_all()
    module.eventInbox.clear_all()
    module.msg.clear_poke_state()

    decisions = []
    replies = []

    def decide(*args, **kwargs):
        decisions.append(kwargs.get('is_addressed'))
        return True

    def reply(plugin_event, target_group_id, message, **kwargs):
        replies.append({
            'group_id': target_group_id,
            'message': message,
            'history_entry': kwargs.get('history_entry'),
        })

    monkeypatch.setattr(module.msg, 'should_reply', decide)
    monkeypatch.setattr(module.msg, 'reply_to_group', reply)
    event = SimpleNamespace(
        bot_info=SimpleNamespace(hash=bot_hash),
        base_info={'self_id': 10001},
        data=SimpleNamespace(
            group_id=group_id,
            user_id='20002',
            target_id='10001',
            sender={'nickname': 'OPK群友'},
        ),
    )

    try:
        module.main.Event.poke(event, None)

        entries = list(module.msg._get_history(bot_hash, group_id) or [])
        assert len(entries) == 1
        assert entries[0]['role'] == 'user'
        assert entries[0]['nickname'] == 'OPK群友'
        assert entries[0]['message'] == '戳一戳：OPK群友戳了戳你'
        assert entries[0]['message_type'] == 'poke'
        assert entries[0]['target_id'] == '10001'
        assert entries[0]['target_is_self'] is True
        assert decisions == [True]
        assert replies == [{
            'group_id': group_id,
            'message': '戳一戳：OPK群友戳了戳你',
            'history_entry': entries[0],
        }]
        assert module.eventInbox.snapshot(bot_hash)['event_count'] == 1

        with (tmp_path / 'poke-config' / f'config_{bot_hash}.json').open(
            encoding='utf-8',
        ) as file_obj:
            persisted = json.load(file_obj)
        assert persisted['event_poke_enabled'] is True
        assert persisted['interaction_response_enabled'] is True
    finally:
        module.data.gHistoryWindowGuard.clear_all()
        module.eventInbox.clear_all()
        module.msg.clear_poke_state()
