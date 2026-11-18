"""WebUI bridge contract for the OlivOSAIChatAssassin plugin."""

import copy
import json
import os
import queue
import re
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

import OlivOS  # noqa: E402
import OlivOSAIChatAssassin  # noqa: E402
import OlivOSAIChatAssassin.main as assassin_main  # noqa: E402
from OlivOSAIChatAssassin import interaction  # noqa: E402
from webui_test_support import make_host  # noqa: E402


BOT_HASH = 'bot-hash-001'


class FakeEvent:
    def __init__(self, event_name, payload=None, namespace=None, request_id='request-1', webui=True):
        namespace = namespace or OlivOSAIChatAssassin.webUI.NAMESPACE
        data = {
            'namespace': namespace,
            'event': event_name,
            'payload': payload,
        }
        if webui:
            data['webui'] = {
                'session': 'private-session',
                'request_id': request_id,
                'payload': payload,
            }
        self.data = SimpleNamespace(**data)
        self.bot_info = None
        self.sent = []

    def send(self, send_type, target_id, message):
        self.sent.append((send_type, target_id, message))
        return True


@pytest.fixture
def plugin_api(tmp_path, monkeypatch):
    bot = SimpleNamespace(
        id=10001,
        hash=BOT_HASH,
        platform={'sdk': 'onebot', 'platform': 'qq', 'model': 'default'},
        enable=True,
        password='bot-password',
        post_info=SimpleNamespace(access_token='access-token', host='127.0.0.1', port=5700),
    )
    proc = SimpleNamespace(Proc_data={'bot_info_dict': {BOT_HASH: bot}})
    manager = OlivOSAIChatAssassin.load.DataManager(
        config_dir=str(tmp_path / 'config'),
        memory_dir=str(tmp_path / 'memory'),
        default_config=copy.deepcopy(OlivOSAIChatAssassin.data.configDefault),
    )
    manager.load_bot(BOT_HASH)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', manager)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gProc', None)
    monkeypatch.setattr(
        OlivOSAIChatAssassin.data,
        'gPowerPath',
        str(tmp_path / 'power_overrides.json'),
    )
    OlivOSAIChatAssassin.manager.reset()
    yield SimpleNamespace(manager=manager, proc=proc, bot=bot)
    OlivOSAIChatAssassin.manager.reset()


@pytest.fixture
def bridge_host(tmp_path):
    host = make_host(root_path=tmp_path, rx_queue=queue.Queue(), control_queue=queue.Queue())
    yield host
    host.on_terminate()


@pytest.fixture
def image_cache_dir(tmp_path, monkeypatch):
    path = tmp_path / 'image-cache'
    path.mkdir()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageDir', str(path))
    state = OlivOSAIChatAssassin.webUI._image_cache_index_state
    with OlivOSAIChatAssassin.webUI._image_cache_index_lock:
        state.update(directory='', signature=None, built_at=0.0, index={})
    yield path
    with OlivOSAIChatAssassin.webUI._image_cache_index_lock:
        state.update(directory='', signature=None, built_at=0.0, index={})


def write_test_png(path, size=(800, 600)):
    from PIL import Image
    image = Image.new('RGB', size, (18, 52, 86))
    image.save(path, format='PNG')


def call_bridge(plugin_api, event_name, payload):
    event = FakeEvent(event_name, payload)
    handled = OlivOSAIChatAssassin.webUI.handle_menu(event, plugin_api.proc)
    assert handled is True
    assert len(event.sent) == 1
    send_type, request_id, response = event.sent[0]
    assert send_type == 'webui'
    assert request_id == 'request-1'
    return response


def api_payload(**values):
    payload = {'api_version': 1}
    payload.update(values)
    return payload


def test_webui_manifest_registers_single_file_page():
    manifest_path = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'app.json'
    raw_manifest = manifest_path.read_bytes()
    assert not raw_manifest.startswith(b'\xef\xbb\xbf')
    manifest = json.loads(raw_manifest.decode('utf-8'))
    assert manifest['webui_config'] == [
        {
            'title': '群聊刺客',
            'type': 'iframe',
            'path': 'webui/index.html',
        }
    ]
    page_root = manifest_path.parent / 'webui'
    assert (page_root / 'index.html').is_file()
    assert not (manifest_path.parent / 'index.html').exists()
    assert not (manifest_path.parent / 'assets').exists()
    assert not (manifest_path.parent / 'app.js').exists()
    assert not (manifest_path.parent / 'style.css').exists()
    assert not (manifest_path.parent / 'README.md').exists()
    assert (manifest_path.parent / '.codex' / 'AGENT.md').is_file()
    assert sorted(path.name for path in page_root.iterdir()) == ['index.html']
    assert not (page_root / 'assets').exists()
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert '<style>' in html
    assert '<script>' in html
    assert './assets/' not in html


def test_webui_activity_endpoint_returns_only_selected_bot_memory_snapshot(plugin_api, monkeypatch):
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    try:
        token = activity.begin(
            BOT_HASH,
            '123456',
            '测试用户',
            '[OP:at,id=10001]\n今晚有人一起开黑吗',
        )
        activity.activate(token)
        activity.update(token, stage=activity.STAGE_MODEL, attempt=1)
        other = activity.begin('other-bot', '123456', '另一个用户', '不应返回')
        activity.activate(other)

        def fail_memory_read(*args, **kwargs):
            raise AssertionError('activity endpoint must not read memory')

        monkeypatch.setattr(plugin_api.manager, 'snapshot_memory', fail_memory_read)
        assert OlivOSAIChatAssassin.webUI._HANDLERS[
            OlivOSAIChatAssassin.webUI.EVENT_ACTIVITY
        ] is OlivOSAIChatAssassin.webUI._handle_activity
        response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_ACTIVITY,
            api_payload(bot_hash=BOT_HASH),
        )
        assert response['ok'] is True
        assert 'revision' not in response
        data = response['data']
        assert data['session_count'] == 1
        assert data['running_count'] == 1
        assert len(data['sessions']) == 1
        session = data['sessions'][0]
        assert session['group_id'] == '123456'
        assert session['stage'] == activity.STAGE_MODEL
        assert session['message_preview'] == '今晚有人一起开黑吗'
        assert 'OP:' not in session['message_preview']

        activity.finish(token)
        completed_response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_ACTIVITY,
            api_payload(bot_hash=BOT_HASH),
        )
        completed_data = completed_response['data']
        assert completed_data['completed_count'] == 1
        completed_session = completed_data['sessions'][0]
        assert completed_session['state'] == activity.STATE_FINISHED
        assert 0 < completed_session['expires_in_ms'] <= activity.ACTIVITY_TERMINAL_TTL_MS

        skipped_token = activity.begin(BOT_HASH, '654321', '跳过用户', '无需回复')
        activity.activate(skipped_token)
        activity.finish(skipped_token, activity.STATE_SKIPPED, 'not_selected')
        skipped_response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_ACTIVITY,
            api_payload(bot_hash=BOT_HASH),
        )
        skipped_data = skipped_response['data']
        assert skipped_data['completed_count'] == 1
        assert skipped_data['skipped_count'] == 1
        skipped_session = next(
            session for session in skipped_data['sessions']
            if session['group_id'] == '654321'
        )
        assert skipped_session['state'] == activity.STATE_SKIPPED
        assert skipped_session['error_code'] == 'not_selected'
        assert 0 < skipped_session['expires_in_ms'] <= activity.ACTIVITY_TERMINAL_TTL_MS

        failed_token = activity.begin(BOT_HASH, '777777', '失败用户', '模型失败')
        activity.activate(failed_token)
        activity.finish(failed_token, activity.STATE_FAILED, 'model_failure')
        superseded_token = activity.begin(BOT_HASH, '888888', '接管用户', '退避中的消息')
        activity.activate(superseded_token)
        next_token = activity.begin(BOT_HASH, '888888', '下一位用户', '已经到达的新消息')
        activity.finish(superseded_token, activity.STATE_SUPERSEDED, 'new_message')
        terminal_response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_ACTIVITY,
            api_payload(bot_hash=BOT_HASH),
        )
        terminal_data = terminal_response['data']
        assert terminal_data['session_count'] == 4
        assert terminal_data['terminal_count'] == 3
        assert terminal_data['completed_count'] == 1
        assert terminal_data['skipped_count'] == 1
        assert terminal_data['failed_count'] == 1
        assert terminal_data['superseded_count'] == 0
        terminal_sessions = {
            session['group_id']: session for session in terminal_data['sessions']
        }
        for group_id, state, error_code in (
            ('123456', activity.STATE_FINISHED, ''),
            ('654321', activity.STATE_SKIPPED, 'not_selected'),
            ('777777', activity.STATE_FAILED, 'model_failure'),
        ):
            terminal_session = terminal_sessions[group_id]
            assert terminal_session['state'] == state
            assert terminal_session['error_code'] == error_code
            assert 0 < terminal_session['expires_in_ms'] <= activity.ACTIVITY_TERMINAL_TTL_MS
        queued_session = terminal_sessions['888888']
        assert queued_session['state'] == activity.STATE_QUEUED
        assert queued_session['message_preview'] == '已经到达的新消息'
        assert queued_session['sender_name'] == '下一位用户'
        assert queued_session['expires_in_ms'] == 0
        activity.finish(next_token)
    finally:
        activity.clear_all()


def test_webui_activity_frontend_polls_only_visible_overview_and_preserves_content():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    for marker in (
        "activity: 'OlivOSAIChatAssassin_WebUI_GetActivity'",
        'const ACTIVITY_POLL_INTERVAL = 2000;',
        'const ACTIVITY_TIMER_INTERVAL = 1000;',
        'const ACTIVITY_REQUEST_TIMEOUT = 5000;',
        'const ACTIVITY_DETAIL_LIMIT = 3;',
        '持续刷新中',
        '刷新异常，仍在重试',
        'state.activityTimingAnchor = performance.now();',
        'function tickActivityTimings()',
        'Number(session.elapsed_ms || 0) + sessionOffset',
        'function expireTerminalActivitySessions()',
        'function activityTerminalRemainingMs',
        'const ACTIVITY_TERMINAL_META = {',
        'function activityTerminalMeta(stateName)',
        'const ACTIVITY_TERMINAL_COUNT_KEYS = [',
        "countKey: 'failed_count',",
        "countKey: 'superseded_count',",
        "parts.push('skipped:' + String(data.skipped_count || 0));",
        "parts.push('failed:' + String(data.failed_count || 0));",
        "parts.push('superseded:' + String(data.superseded_count || 0));",
        "'activity-countdown ' + terminalMeta.className",
        "label: '已完成',",
        "label: '已跳过',",
        "label: '失败',",
        "label: '已取代',",
        'const ACTIVITY_MEMORY_META = {',
        "running: {label: '与回复并行', className: 'is-running'},",
        "countdown.textContent = remainingSeconds + ' 秒后消失'",
        'function activityTerminalCountdownText(session)',
        'text.textContent !== message',
        'state.visible && !document.hidden',
        "state.activeTab === 'overview' && state.selectedBot",
        'document.addEventListener(\'visibilitychange\'',
        'state.activityError = errorMessage(error);',
        'state.activityLoading = false;',
        'elements.overviewActivityList.replaceChildren();',
        'elements.overviewActivityFullscreenList.replaceChildren();',
        'function openActivityFullscreen(options)',
        'function closeActivityFullscreen(restoreFocus, options)',
        'function handleActivityFullscreenKeydown(event)',
        'state.activityFullscreenOpen',
        'body.activity-fullscreen-open',
        'overviewActivityFullscreenButton',
        'activity-fullscreen-icon',
        'overviewActivityFullscreenSummary',
        'overviewActivityFullscreenBackButton.focus',
        'pageShell.inert = true;',
        'fullscreenBody.scrollTop = fullscreenScrollTop;',
        'updateActivityTimings(sessions);',
        'prefers-reduced-motion: reduce',
    ):
        assert marker in html
    assert 'activity-more-list' not in html
    assert 'activity-more-row' not in html
    assert 'buildActivityMoreRow' not in html
    assert 'activityMoreExpanded' not in html
    assert 'overviewActivityFullscreenLauncher.hidden' not in html
    assert 'overviewActivityFullscreenButton.hidden' not in html
    fullscreen_append = 'elements.overviewActivityFullscreenList.appendChild(buildActivitySession(session));'
    append_index = html.index(fullscreen_append)
    loop_start = html.rfind('sessions.forEach(function (session) {', 0, append_index)
    assert loop_start >= 0
    fullscreen_loop = html[loop_start:html.index('});', append_index) + 3]
    assert 'buildActivitySession(session)' in fullscreen_loop
    assert 'slice(' not in fullscreen_loop
    assert 'activityLastUpdated' not in html
    assert 'activityClockText' not in html
    assert '更新于 ' not in html
    assert 'innerHTML' not in html
    assert "session.state === 'finished' || session.state === 'skipped'" not in html
    assert "skipped ? 'activity-countdown is-skipped' : 'activity-countdown'" not in html
    settings = html.split('<section id="panel-settings"', 1)[1].split(
        '<section id="panel-memory"',
        1,
    )[0]
    assert 'activity' not in settings
    assert 'OlivOSAIChatAssassin_WebUI_GetActivity' in html


def test_webui_activity_frontend_renders_trace_and_outgoing_progress_compactly():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    for marker in (
        'const ACTIVITY_TRACE_STATUS_META = {',
        'function activityTraceStatusMeta(statusName)',
        'function activityTraceStageText(stageName)',
        'function buildActivityTrace(session)',
        'function buildActivityOutgoing(session)',
        'function buildActivityStream(session)',
        'function buildActivityMemory(session)',
            'function scrollActivityStreams(root)',
        "list.setAttribute('aria-label', '受控执行轨迹');",
        "'span', 'activity-trace-status', statusMeta.label",
        "'span', 'activity-parallel-label', '并行任务'",
        'trace.map(function (entry)',
        'outgoing.map(function (message)',
        "memoryTask.state || '', memoryTask.detail || ''",
        'memoryElapsed: parallelTask ? parallelTask.elapsed : null',
        'node.memoryElapsed.textContent = activityDurationText(',
        "stateText = '发送中';",
        "stateText = '已发送';",
        "stateText = '待发送';",
        "'第 ' + String(index + 1) + '/' + String(total) + ' 条'",
        "'另有 ' + String(total - messages.length) + ' 条未展开'",
        'session.outgoing_total || 0,',
        'session.sent_count || 0,',
        '.activity-stream {',
        '.activity-parallel-task {',
        '.activity-parallel-task.is-running .activity-parallel-dot {',
        'max-height: 96px;',
        'overflow-y: auto;',
            'scrollActivityStreams(elements.overviewActivityList);',
    ):
        assert marker in html
    assert 'reasoning_content' not in html
    assert 'innerHTML' not in html


def test_webui_settings_exposes_api_timeout_field():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    timeout_input = re.search(r'<input[^>]*id="apiTimeout"[^>]*>', html)
    assert timeout_input is not None
    assert 'data-field="api_timeout"' in timeout_input.group(0)
    assert 'min="1"' in timeout_input.group(0)
    assert 'max=' not in timeout_input.group(0)


def test_webui_settings_exposes_context_budget_and_knowledge_top_k():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')

    assert 'id="contextBudgetChars" data-field="context_budget_chars"' in html
    assert 'id="knowledgeTopK" data-field="knowledge_top_k"' in html
    assert '0 表示关闭上下文预算' in html
    assert '0 表示不限制知识检索条数' in html
    assert "context_budget_chars: function () { return readInteger('contextBudgetChars'); }" in html
    assert "knowledge_top_k: function () { return readInteger('knowledgeTopK'); }" in html
    assert "'maxTokens', 'apiTimeout', 'temperature'" in html
    assert "setValue('apiTimeout', config.api_timeout);" in html
    assert "api_timeout: function () { return readNumber('apiTimeout'); }," in html


def test_webui_settings_places_thinking_toggle_before_reasoning_effort():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')

    assert html.index('id="thinkingEnabled"') < html.index('id="reasoningEffort"')


def test_webui_settings_exposes_interaction_controls():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')

    assert '<legend>群内互动</legend>' in html
    assert 'id="overviewInteraction"' not in html
    assert '插件不判断 sdk、协议或模型，也不使用平台专有的 inde 接口' in html
    for control_id in (
        'interactionEnabled',
        'interactionActions',
        'interactionReactions',
        'interactionCooldown',
    ):
        assert f'id="{control_id}"' in html
    assert 'id="interactionActionQuote"' not in html
    assert 'id="interactionActionMention"' not in html
    assert "const INTERACTION_ACTION_OPTIONS = [" in html
    assert "const INTERACTION_REACTION_OPTIONS = [" in html
    for reaction in ('tears', 'applause', 'cheer', 'speechless', 'watermelon', 'slack'):
        assert f"['{reaction}'," in html
    assert 'function renderChoiceGroup(root, options)' in html
    assert "setChoiceValues('interactionActions', interactionActions);" in html
    assert "setChoiceValues('interactionReactions', interactionReactions);" in html
    cooldown_input = re.search(r'<input[^>]*id="interactionCooldown"[^>]*>', html)
    assert cooldown_input is not None
    assert 'data-field="interaction_cooldown"' in cooldown_input.group(0)
    assert 'min="0"' in cooldown_input.group(0)
    assert 'max=' not in cooldown_input.group(0)
    assert "setValue('interactionCooldown', config.interaction_cooldown);" in html
    assert "interaction_actions: function () { return readChoiceValues('interactionActions'); }" in html
    assert "interaction_reactions: function () { return readChoiceValues('interactionReactions'); }" in html
    assert "interaction_cooldown: function () { return readNumber('interactionCooldown'); }," in html
    assert "interaction: '互动动作'," in html


def test_webui_settings_uses_tag_editors_for_string_lists():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')

    for editor_id, field in (
        ('enabledGroups', 'enabled_groups'),
        ('replyKeywords', 'reply_keywords'),
        ('ignorePrefixes', 'ignore_prefixes'),
    ):
        assert f'data-list-editor="{editor_id}"' in html
        assert f'id="{editor_id}" data-field="{field}" type="hidden"' in html
        assert f"setListValues('{editor_id}', config.{field});" in html
        assert f"{field}: function () {{ return readListValues('{editor_id}'); }}," in html
    assert '每行一个' not in html
    assert 'data-list-input' in html
    assert 'data-list-add' in html
    assert 'function enhanceListEditors()' in html
    assert 'function appendListValues(id, text)' in html
    assert 'function removeListValue(id, index)' in html
    assert 'commitAllListInputs();' in html
    assert "textContent = '尚未添加';" in html


def test_webui_choice_options_match_backend_catalog():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    reaction_block = re.search(
        r"const INTERACTION_REACTION_OPTIONS = \[(.*?)\n    \];",
        html,
        re.S,
    )
    assert reaction_block is not None
    options = re.findall(r"\['([^']+)', '([^']+)'\]", reaction_block.group(1))

    assert options == [
        (name, label)
        for name, label, _face_id in interaction.REACTION_CATALOG
    ]


def test_webui_settings_exposes_extra_config_field_editor():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert '<legend>额外字段</legend>' in html
    assert 'id="extraConfigFields"' in html
    assert 'id="extraConfigAddButton"' in html
    assert "{value: 'string', label: '字符串'}" in html
    assert "{value: 'number', label: '数字'}" in html
    assert "{value: 'boolean', label: '布尔值'}" in html
    assert "{value: 'object', label: '对象'}" in html
    assert "{value: 'array', label: '数组'}" in html
    assert "{value: 'null', label: '空值'}" in html
    assert 'function collectExtraConfigFields(patch)' in html
    assert 'function setExtraConfigPatchValue(patch, name, value)' in html
    assert 'remove_fields: requestData.remove_fields' in html


def test_webui_about_panel_contains_manifesto_without_old_notices():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    about = html.split('<section id="panel-about"', 1)[1].split('</section>', 1)[0]
    expected_paragraphs = [
        '潜入群聊的AI聊天工作流插件',
        '快捷访问',
        'GitHub 页面',
        '论坛页面',
        '作者：lunzhiPenxil仑质',
        'href="https://github.com/lunzhiPenxil/OlivOSAIChatAssassin"',
        'href="https://forum.olivos.run/d/902"',
        'href="https://github.com/lunzhiPenxil" target="_blank" rel="noopener noreferrer"',
        'target="_blank" rel="noopener noreferrer"',
        '《群聊刺客》',
        '——论社交的最小系统与图灵测试的终结',
        '原教旨主义图灵测试通常设定为一位人类裁判明确知道有一个机器并试图从诸多一对一场景中识别它，'
        '但这对当今的互联网现实而言是否太狭隘而不具有实际意义了？',
        '当我意识到这一点时，我便开始观察网友们的行为模式。最终，我得出了一个略带讽刺却符合直觉的结论：'
        '在充斥着碎片化信息的互联网浅社交中，大多数人的发言呈现出 “低信息密度、低组织成本、低参与意识与高行为重复” 的特征。'
        '在这样的语境下，人们分辨对话者是AI还是人类的警惕性本就极为淡薄，甚至可以说是主动放弃了鉴别。'
        '在这种低质量的信息交互中，AI甚至不需要刻意隐藏，因为它输出的内容与人类输出的内容，已经没有显著区别'
        '——无论是在信息熵上，还是价值观上。',
        '这意味着，我完全可以转变思路：不再以制造人类的通用仿品为目标去搭建一个AI Agent，'
        '而是将目标限定在“多人即时通讯群聊”这一特定场景中——一个账号信息没有特别标注、且言行与设定高度一致的AI账号。',
        '虽然，对于那些将互联网浅社交视为水与空气的人而言这有些冒犯，但……',
        '不妨设想一下：若是你在每天水群的群聊中，看到一个顶着常见二次元或抽象文化头像、用着大众脸昵称的群友，'
        '在你说话时时不时附和几句，有时反对你几句——你或许根本就不会多想。这个项目甚至不需要有大语言模型的参与。'
        '相比之下，大语言模型只是极大拓展了其上限，并略微抬高了其下限，而或许，也已远远超过其真正所需。',
        '正如一千个读者有一千个哈姆雷特，但AI不需要逐一实现那一千个平行世界中的希斯克里夫'
        '——它只需要人类以为它是“某个世界的凯瑟琳”，这就足够了，因为，在人际交互的最小系统里，存在感可以是一种集体幻觉。',
        '<strong>所以我写了一个OlivOS插件，并在过去的一段时间内于几十余个群内开展了测试。</strong>',
        '从结论上来说，实验结果符合预期。它指向了未来AI与人类共存的常态：AI不会以完美的形态出现——因为它不需要；'
        '它悄无声息地融入我们的日常场景，重塑我们的社交习惯。',
        '这或许预示着，真正的图灵测试已经变成了一个社会学问题，而非技术问题。AI试着证明自己是人已经成为了过去式。'
        '人是否还愿意、以及能够表现得不像AI——这才是需要付出额外努力的事情。',
        '或许从此以后，一切都将改变。',
    ]
    for paragraph in expected_paragraphs:
        assert paragraph in about
    assert '2026-03-16<br>仑质' in about
    for removed in (
        '页面通过 OlivOS 插件消息桥访问本地插件数据。',
        '安全边界',
        '协议',
        'WebUI API 版本',
    ):
        assert removed not in about


def test_webui_manifest_is_cached_before_first_request(plugin_api, tmp_path, monkeypatch):
    manifest = json.loads((PLUGIN_ROOT / 'app.json').read_text(encoding='utf-8'))
    assert OlivOSAIChatAssassin.webUI._manifest_cache is not None
    monkeypatch.setattr(
        OlivOSAIChatAssassin.webUI,
        '__file__',
        str(tmp_path / 'removed-opk' / 'webUI.py'),
    )
    response = call_bridge(plugin_api, OlivOSAIChatAssassin.webUI.EVENT_BOOTSTRAP, api_payload())
    assert response['ok'] is True
    assert response['data']['plugin']['version'] == manifest['version']
    assert response['data']['plugin']['compatible_svn'] == manifest['compatible_svn']


def test_webui_opk_assets_are_restored_from_import_cache(tmp_path):
    cache = OlivOSAIChatAssassin.webUI._webui_asset_cache
    assert set(cache) == {'webui/index.html'}
    root = tmp_path / 'restored-plugin'
    logs = []
    proc = SimpleNamespace(
        plugin_models_dict={
            OlivOSAIChatAssassin.webUI.NAMESPACE: {'webui_root': str(root)},
        },
        log=lambda level, message: logs.append((level, message)),
    )
    assert OlivOSAIChatAssassin.webUI.restore_webui_assets(proc) is True
    for name, data in cache.items():
        assert (root / Path(name)).read_bytes() == data
    assert (root / 'webui' / 'index.html').is_file()
    assert not (root / 'index.html').exists()
    assert logs and logs[0][0] == 2
    assert OlivOSAIChatAssassin.webUI.restore_webui_assets(proc) is False


def test_webui_frontend_stays_inside_sandbox_contract():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    combined = html
    for forbidden in ('fetch(', 'XMLHttpRequest', 'new WebSocket', 'localStorage', 'innerHTML'):
        assert forbidden not in combined
    assert 'window.parent.postMessage' in combined
    assert 'olivos:plugin_reply' in combined
    assert 'olivos:plugin_visibility' in combined
    assert 'MAX_REQUEST_BYTES' in combined
    assert 'new Blob' in combined
    assert 'id="configWarnings"' in html


def test_webui_selects_use_theme_aware_custom_listboxes():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    for marker in (
        'appearance: none;',
        '-webkit-appearance: none;',
        '.select-shell {',
        '.select-shell > select {',
        '.field > span:not(.select-shell) {',
        '.memory-list-control > span:not(.select-shell) {',
        '.select-chevron {',
        '.select-chevron::before {',
        'select.select-menu-open + .select-chevron::before {',
        'transform: rotate(225deg);',
        'transition: transform 180ms cubic-bezier(0.2, 0.8, 0.2, 1);',
        'select:hover:not(:disabled)',
        'select:active:not(:disabled)',
        'select:focus {',
        'box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 22%, transparent);',
        'select:disabled {',
        'select option,',
        'select optgroup {',
        'select option:checked {',
        'select option:disabled {',
        '.memory-list-control select {',
        'padding-right: 32px;',
        '--popover:',
        '--select-option-selected:',
        '--menu-shadow:',
        '.select-menu {',
        '.select-menu.opens-above {',
        '.select-menu-option-label {',
        '.select-menu-option[aria-selected="true"]::after {',
        'background: var(--popover);',
        'box-shadow: var(--menu-shadow);',
        'right: 12px;',
        'cursor: pointer;',
        'const SELECT_MENU_GAP = 4;',
        '.memory-other-arrow::before {',
        '.memory-other-toggle.expanded .memory-other-arrow::before {',
        '@keyframes select-menu-in {',
        '@media (forced-colors: active)',
        'function enhanceCustomSelect(select) {',
        'function handleSelectKeydown(event) {',
        'function openSelectMenu(select) {',
        'function typeaheadSelectItem(character) {',
        'function ensureSelectShell(select) {',
        "shell.className = 'select-shell';",
        "chevron.className = 'select-chevron';",
        "chevron.setAttribute('aria-hidden', 'true');",
        'function handleSelectViewportScroll(event) {',
        'function handleSelectWindowBlur() {',
        "menu.setAttribute('role', 'listbox');",
        "select.setAttribute('aria-activedescendant', item.element.id);",
        "select.setAttribute('aria-expanded', 'true');",
        'event.stopPropagation();',
        'new MutationObserver(function () {',
    ):
        assert marker in html
    assert re.search(
        r'\.select-menu\s*\{[^}]*display:\s*flex;[^}]*'
        r'flex-direction:\s*column;[^}]*row-gap:\s*3px;',
        html,
        re.S,
    )
    assert '--select-chevron:' not in html
    assert html.count('--popover:') == 2


def test_webui_editor_and_memory_boxes_expand_instead_of_scrolling():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert 'AUTO_GROW_MAX_HEIGHT' in html
    assert 'function resizeTextarea' in html
    assert "textarea.addEventListener('input'" in html
    assert "window.addEventListener('resize', scheduleTextareaResize)" in html
    assert not re.search(r'\.memory-chip-list\s*\{[^}]*max-height', html)
    assert not re.search(r'\.memory-chip-list\s*\{[^}]*overflow:\s*auto', html)
    assert not re.search(r'\.memory-detail pre\s*\{[^}]*max-height', html)
    assert not re.search(r'\.memory-detail pre\s*\{[^}]*overflow:\s*auto', html)


def test_webui_overview_uses_image_cache_count_without_activation_notice():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert '<span class="card-label">图片缓存</span>' in html
    assert 'id="overviewImageCache"' in html
    assert 'id="overviewSource"' not in html
    assert '配置来源' not in html
    assert '生效提示' not in html
    assert '历史窗口、松弛时间和已存在群的图片队列长度需要重载插件后完整生效。' not in html
    assert "return section.name === IMAGE_CACHE_SECTION;" in html
    assert 'elements.overviewImageCache.textContent = state.overview' in html
    assert "String(imageCacheCount) + ' 条'" in html
    assert 'function configRevisionTimeText(revision)' in html
    assert "const timestamp = raw.split(':', 1)[0];" in html
    assert 'new Date(Number(timestamp) / 1000000)' in html
    assert 'elements.overviewRevision.textContent = configRevisionTimeText(state.revision);' in html


def test_webui_settings_do_not_define_upper_limits():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    settings = html.split('<section id="panel-settings"', 1)[1].split(
        '<section id="panel-memory"',
        1,
    )[0]
    assert not re.search(r'\bmax\s*=', settings)
    assert not re.search(r'\bmaxlength\s*=', settings)


def test_webui_group_memory_is_browsed_from_unified_memory_categories():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert 'id="memorySectionList"' in html
    assert 'id="groupMemoryList"' not in html
    assert 'memory-group-list' not in html
    assert "groupButton.textContent = '群记忆'" in html
    assert "groupButton.dataset.memoryScope = 'group_index'" in html
    assert 'function showGroupMemoryIndex' in html
    assert 'function renderGroupMemoryIndex' in html
    assert "loadMemorySection('group'" in html
    assert '返回群列表' in html
    assert re.search(
        r"const MEMORY_SECTION_ORDER = \[[^\]]*'用户侧写'.*?'人物关系'.*?'知识缓存'"
        r".*?KNOWLEDGE_SEARCH_SECTION.*?IMAGE_CACHE_SECTION",
        html,
        re.S,
    )
    assert 'function orderedGlobalMemorySections' in html
    assert 'function isSpecialMemorySection' in html
    assert 'function appendMemorySectionButton' in html
    assert 'function appendMemoryOtherMenu' in html
    assert "label.textContent = '其它'" in html
    assert 'data-memory-other-toggle' in html
    assert "'memoryOtherSectionList'" in html
    assert 'memoryOtherExpanded' in html
    assert '.memory-other-list[hidden]' in html
    assert 'function memorySectionDetailText(section)' in html
    assert "return '列表 · ' + String(count) + ' 项';" in html
    assert "prioritized.get(name) || {name: name, kind: 'dict', count: 0}" in html
    assert re.search(
        r'const loadDefaultGroupIndex = !state\.memoryQuery;\s*'
        r'if \(loadDefaultGroupIndex\) \{\s*'
        r"state\.memoryQuery = \{scope: 'group_index', offset: 0, limit: MEMORY_PAGE_SIZE\};",
        html,
    )
    assert "loadMemorySection('group_index', null, 0, null)" in html
    assert 'memoryGroupIndexOffset' in html
    assert 'function renderGroupMemoryIndex(data, query)' in html
    assert 'const entries = Array.isArray(data.entries)' in html
    assert "next.textContent = '下一页'" in html


def test_webui_relationship_view_renders_structured_people_entries():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    for marker in (
        '.relationship-name',
        '.relationship-user-id',
        '.relationship-description',
        'function relationshipFormatText(relationship)',
        'function createRelationshipEntry(entry, query)',
        "single_alias: '单昵称'",
        "multiple_aliases: '多昵称'",
        "description_only: '仅关系描述'",
        "return '非常规写法';",
        "recognized ? '未记录昵称' : '未识别的条目写法'",
        "description || '未记录关系描述'",
        "userId.textContent = '用户 ID · ' + entry.key;",
        "summary.textContent = '查看原始 JSON';",
        'relationship.aliases_truncated || relationship.description_truncated',
        'createMemoryEditButton(entry.value || ',
        'if (data.relationship) {',
        'list.appendChild(createRelationshipEntry(entry, query));',
        '按用户 ID 展示；兼容单昵称、多昵称、无昵称和关系描述缺省等写法。',
        'id="memoryRelationshipFields"',
        'id="memoryRelationshipAliasMode"',
        '<option value="single">单昵称（字符串）</option>',
        '<option value="multiple">多昵称（数组）</option>',
        '<option value="null">无昵称（null）</option>',
        '<option value="empty_list">空昵称数组（[]）</option>',
        'function collectRelationshipEditorValue(context)',
        "mode: recognized ? 'relationship' : 'text'",
        '使用结构化表单编辑昵称和关系描述；附加扩展数据在末尾单独展开。',
        '该条目不是标准人物关系写法，仍使用原始内容编辑。',
    ):
        assert marker in html


def test_webui_all_memory_list_pagers_are_visible_for_empty_and_single_page_views():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    group_renderer = html.split('function renderGroupMemoryIndex', 1)[1].split(
        'function appendEmpty',
        1,
    )[0]
    assert "if (total === 0) {" in group_renderer
    assert "appendEmpty(elements.memoryDetail, '没有群记忆');" in group_renderer
    assert 'if (total > 0) {' not in group_renderer
    assert "const pager = document.createElement('div');" in group_renderer
    assert 'next.disabled = !hasMore;' in group_renderer

    section_renderer = html.split('function renderMemorySection', 1)[1].split(
        'function parseMemoryEditorValue',
        1,
    )[0]
    assert "typeof data.total === 'number' && data.total > 0" not in section_renderer
    assert "const total = typeof data.total === 'number' ? data.total : entries.length;" in section_renderer
    assert "const pager = document.createElement('div');" in section_renderer
    assert "next.disabled = data.has_more !== true;" in section_renderer
    assert 'function memoryPagerRangeText(offset, entryCount, total)' in html
    assert "return String(start) + ' - ' + String(end) + ' / ' + String(safeTotal);" in html
    assert html.count(
        'status.textContent = memoryPagerRangeText(offset, entries.length, total);'
    ) == 2


def test_webui_memory_reload_keeps_content_and_shows_bounded_loading_state():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    for marker in (
        'memory-loading-indicator',
        'memory-loading-spinner',
        'aria-busy',
        'memoryViewKey',
        'memoryLoadingMessage',
        'memoryActiveTicket',
        'memoryQueuedLoad',
        'function startQueuedMemoryLoad',
        'const retained = hasRenderedMemoryContent();',
        'function drainMemoryImageQueue',
        'state.memoryImageQueue.push',
        'timeout: MEMORY_IMAGE_REQUEST_TIMEOUT',
        'state.busy || state.memoryLoading || state.memoryEditorOpen',
        '!state.memoryEditorOpen || !context || state.busy || state.memoryLoading',
        '!state.selectedBot || state.busy || state.memoryLoading',
    ):
        assert marker in html
    memory_timeout = re.search(r'const MEMORY_REQUEST_TIMEOUT = (\d+);', html)
    image_timeout = re.search(r'const MEMORY_IMAGE_REQUEST_TIMEOUT = (\d+);', html)
    image_concurrency = re.search(r'const MEMORY_IMAGE_CONCURRENCY = (\d+);', html)
    assert memory_timeout and int(memory_timeout.group(1)) >= 90000
    assert image_timeout and int(image_timeout.group(1)) >= 60000
    assert image_concurrency and 1 <= int(image_concurrency.group(1)) <= 8
    assert re.search(
        r"if \(!retained\) \{\s*elements\.memoryDetail\.replaceChildren\(\);",
        html,
    )
    assert '暂时只读' not in html
    assert re.search(
        r'\.memory-loading-indicator\s*\{[^}]*position:\s*absolute',
        html,
    )
    assert re.search(
        r'\.memory-detail\.memory-is-loading \.memory-edit-button\s*\{[^}]*opacity:\s*0',
        html,
    )
    assert '}, {timeout: 30000});' not in html


def test_webui_memory_loading_and_delete_controls_do_not_shift_adjacent_buttons():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert 'padding-right: 180px' not in html
    assert re.search(
        r'\.memory-heading-row\s*\{[^}]*position:\s*relative',
        html,
    )
    assert re.search(
        r'\.memory-heading-row > \.memory-loading-indicator\s*\{[^}]*top:\s*calc\(100% \+ 6px\)',
        html,
    )
    assert (
        "const indicatorHost = elements.memoryDetail.querySelector('.memory-heading-row') ||"
        in html
    )
    assert 'indicatorHost.appendChild(indicator);' in html
    assert re.search(r'\.memory-delete-button\s*\{[^}]*width:\s*88px', html)
    assert re.search(
        r'\.memory-edit-button\.has-delete\s*\{[^}]*width:\s*88px[^}]*right:\s*104px',
        html,
    )
    assert re.search(
        r'\.memory-value-card\.has-delete\.editable > pre\s*\{[^}]*padding-right:\s*200px',
        html,
    )
    assert re.search(
        r'\.relationship-entry\.deletable\.editable \.memory-entry-header\s*\{[^}]*padding-right:\s*200px',
        html,
    )
    assert "row.classList.add('editable');" in html


def test_webui_memory_entry_kind_labels_follow_hover_and_selection_state():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert re.search(r'\.memory-entry-kind\s*\{[^}]*opacity:\s*0', html)
    for selector in (
        '.memory-entry:hover .memory-entry-kind',
        '.memory-entry:focus-within .memory-entry-kind',
        '.memory-entry.selected .memory-entry-kind',
    ):
        assert selector in html
    assert re.search(
        r'\.memory-detail\.memory-is-loading \.memory-entry-kind\s*\{[^}]*opacity:\s*0',
        html,
    )
    assert not re.search(r'@media \(hover: none\)\s*\{', html)
    assert "const owner = card.closest('.memory-entry');" in html
    assert 'owner.classList.add(\'selected\');' in html


def test_webui_narrow_memory_actions_overlay_text_with_gradient_scrim():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    narrow = html.split('@media (max-width: 760px) {', 1)[1].split(
        '@media (max-width: 760px) and (hover: none)',
        1,
    )[0]
    assert 'padding-right: 12px;' in narrow
    assert '.relationship-entry.deletable.editable .memory-entry-header' in narrow
    assert '.memory-image-entry .memory-entry-header' in narrow
    assert 'padding-right: 0;' in narrow
    assert 'width: min(220px, 100%);' in narrow
    assert 'height: 52px;' in narrow
    assert 'background: linear-gradient(to left, var(--surface-soft), transparent);' in narrow
    assert 'background: linear-gradient(to left, var(--surface), transparent);' in narrow
    assert 'background: linear-gradient(to left, var(--accent-soft), transparent);' in narrow
    for selector in (
        '.memory-value-card.actionable:hover::after',
        '.memory-value-card.actionable:focus-within::after',
        '.memory-value-card.actionable.selected::after',
        '.memory-entry.actionable:hover::after',
        '.memory-entry.actionable:focus-within::after',
        '.memory-entry.actionable.selected::after',
    ):
        assert selector in narrow
    assert '.memory-detail.memory-is-loading .memory-value-card.actionable::after' in narrow
    assert '.memory-detail.memory-is-loading .memory-entry.actionable::after' in narrow
    assert '@media (max-width: 760px) and (hover: none)' in html
    assert html.count("row.classList.add('actionable');") == 2


def test_webui_mobile_layout_uses_safe_touch_targets_and_bottom_sheet():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert 'content="width=device-width, initial-scale=1, viewport-fit=cover"' in html
    assert '-webkit-text-size-adjust: 100%;' in html

    wide = html.split('@media (max-width: 760px) {', 1)[0]
    narrow = html.split('@media (max-width: 760px) {', 1)[1].split(
        '@media (max-width: 760px) and (hover: none)',
        1,
    )[0]
    assert re.search(
        r'\.account-tools\s*\{[^}]*display:\s*grid;[^}]*'
        r'grid-template-columns:\s*minmax\(0, 1fr\) auto;',
        narrow,
        re.S,
    )
    tabs_rule = re.search(r'\.tabs\s*\{(.*?)\}', narrow, re.S)
    assert tabs_rule is not None
    assert 'grid-template-columns: repeat(4, minmax(0, 1fr));' in tabs_rule.group(1)
    assert 'position: sticky' not in tabs_rule.group(1)
    assert re.search(
        r'input:not\(\[type="checkbox"\]\):not\(\[type="radio"\]\),\s*'
        r'select,\s*textarea\s*\{[^}]*min-height:\s*44px;[^}]*font-size:\s*16px;',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.sticky-heading\s*\{[^}]*position:\s*sticky;[^}]*'
        r'top:\s*env\(safe-area-inset-top, 0px\);',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.memory-chip-list\s*\{[^}]*grid-template-columns:\s*'
        r'repeat\(2, minmax\(0, 1fr\)\);',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.memory-image-type-options\s*\{[^}]*grid-template-columns:\s*'
        r'repeat\(2, minmax\(0, 1fr\)\);',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.memory-image-type-option\s*\{[^}]*min-height:\s*46px;[^}]*'
        r'border-radius:\s*13px;',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.memory-image-type-option\s*\{[^}]*min-height:\s*38px;[^}]*'
        r'transition:\s*background-color 180ms ease',
        wide,
        re.S,
    )
    assert '.memory-image-type-option::before {' in wide
    assert '.memory-image-type-option.selected::before {' in wide
    assert 'animation: memory-check-in 180ms' in wide
    assert '.memory-image-type-option:active {' in wide
    assert '@supports selector(:has(*))' in wide
    assert '.check-field:has(input[type="checkbox"]:checked)' in wide
    assert re.search(
        r'\.check-field:has\(input\[type="checkbox"\]:checked\)::before\s*\{'
        r'[^}]*animation:\s*memory-check-in 180ms',
        wide,
        re.S,
    )
    assert '@media (prefers-reduced-motion: reduce)' in html
    assert re.search(
        r'\.pager\s*\{[^}]*display:\s*grid;[^}]*'
        r'grid-template-columns:\s*minmax\(0, 1fr\) auto minmax\(0, 1fr\);',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.pager \.button\s*\{[^}]*min-height:\s*44px;',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.select-menu-option\s*\{[^}]*min-height:\s*44px;',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.toast\s*\{[^}]*bottom:\s*calc\(12px \+ env\(safe-area-inset-bottom, 0px\)\);',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.memory-modal-layer\s*\{[^}]*align-items:\s*end;',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.memory-modal-card\s*\{[^}]*max-height:\s*calc\(100dvh - '
        r'env\(safe-area-inset-top, 0px\)\);',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.memory-modal-actions \.button\s*\{[^}]*flex:\s*1 1 0;',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.image-viewer-body img\s*\{[^}]*max-height:\s*calc\(100dvh - 210px - '
        r'env\(safe-area-inset-bottom, 0px\)\);',
        narrow,
        re.S,
    )
    assert re.search(
        r'\.about-link-list\s*\{[^}]*grid-template-columns:\s*1fr;',
        narrow,
        re.S,
    )
    assert 'function revealMemoryDetailOnMobile()' in html
    assert "window.matchMedia('(max-width: 760px)').matches" in html
    assert 'elements.memoryDetail.scrollIntoView({' in html
    assert 'function requestMemoryDetailRevealOnMobile()' in html
    assert 'function revealMemoryDetailAfterRenderOnMobile()' in html
    assert html.count('requestMemoryDetailRevealOnMobile();') >= 7
    assert 'revealMemoryDetailAfterRenderOnMobile();' in html
    touch_narrow = html.split('@media (max-width: 760px) and (hover: none) {', 1)[1].split(
        '@media (forced-colors: active)',
        1,
    )[0]
    assert re.search(
        r'\.relationship-entry \.memory-entry-header,[\s\S]*?'
        r'\.memory-image-entry \.memory-entry-header\s*\{[^}]*flex-wrap:\s*wrap;',
        touch_narrow,
    )
    hidden_action_row = re.search(
        r'\.relationship-entry > \.memory-entry-actions,[\s\S]*?'
        r'\.memory-image-entry > \.memory-entry-actions\s*\{([^}]*)\}',
        touch_narrow,
    )
    assert hidden_action_row is not None
    assert 'display: none;' in hidden_action_row.group(1)
    assert 'justify-content: flex-end;' in hidden_action_row.group(1)
    assert re.search(
        r'\.relationship-entry\.selected > \.memory-entry-actions,[\s\S]*?'
        r'\.memory-image-entry:focus-within > \.memory-entry-actions\s*\{[^}]*display:\s*flex;',
        touch_narrow,
    )
    action_button_rule = re.search(
        r'\.relationship-entry > \.memory-entry-actions \.memory-edit-button,[\s\S]*?'
        r'\.memory-image-entry > \.memory-entry-actions \.memory-edit-button\s*\{([^}]*)\}',
        touch_narrow,
    )
    assert action_button_rule is not None
    assert 'position: static;' in action_button_rule.group(1)
    assert 'width: 88px;' in action_button_rule.group(1)
    assert 'opacity:' not in action_button_rule.group(1)
    assert 'min-height:' not in action_button_rule.group(1)
    assert '.memory-entry-actions {' in html
    assert html.count("actions.className = 'memory-entry-actions';") == 2


def test_webui_memory_editors_use_modal_and_revisioned_save_api():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    for marker in (
        "saveMemory: 'OlivOSAIChatAssassin_WebUI_SaveMemory'",
        'id="memoryModal"',
        'role="dialog" aria-modal="true"',
        'class="memory-modal-backdrop"',
        'backdrop-filter: blur(12px)',
        'function createMemoryValueCard',
        'function createImageCacheEntry',
        'memory-edit-button',
        'memory-value-card.editable:hover .memory-edit-button',
        'function openMemoryEditor',
        'function closeMemoryEditor',
        'function parseMemoryEditorValue',
        'async function saveMemoryEditor',
        'async function saveMemory',
        'expected_revision: state.memoryRevision',
        'state.memoryEditorOpen = true',
        'state.memoryDirty = elements.memoryModalEditor.value !== context.originalValue',
        'const editable = sectionEditable && entry.editable !== false && !entry.truncated;',
        'function isMemorySectionEditable(scope, section)',
        '此分类在 WebUI 中只读；如需修改，请直接编辑记忆 JSON 文件。',
        'entries: [{key: entry.key, value: value}]',
        "const kind = data.value_kind || 'str'",
        'function setMemoryControlsLocked',
        'setMemoryControlsLocked(true)',
        "memoryImage: 'OlivOSAIChatAssassin_WebUI_GetMemoryImage'",
        'id="memoryImageFields"',
        'id="memoryImageEditorThumb"',
        'id="memoryImageEditorThumbImage"',
        'id="imageViewer"',
        'function openImageEditor',
        'function openImageEditorPreview',
        'function resetMemoryImageEditorPreview',
        'function loadMemoryImageEditorThumbnail',
        'async function openImageViewer',
        'function closeImageViewer',
        'data-image-viewer-close',
        'memoryImageCacheKey',
        "variant: variant",
        '缓存已删除',
        'function createMemoryListControls',
        '默认顺序',
        "const IMAGE_CACHE_SECTION = '图片缓存'",
        "const IMAGE_TYPE_FILTERS = ['表情包', '截图', '照片', '其它']",
        "sort: 'default'",
        "order: 'desc'",
        'cached_first: imageCache === true',
        "typeField.dataset.memoryImageTypeFilter = '1'",
        'checkbox.dataset.memoryImageType = typeName',
        "filters.push('类型：' + selectedImageTypes.join('、'))",
        'payload.image_types = Array.isArray(requestedOptions.image_types)',
        "cacheLabel.textContent = '缓存命中优先'",
        "searchInput.dataset.memorySearch = '1'",
        "sortSelect.dataset.memorySort = '1'",
        "orderSelect.dataset.memoryOrder = '1'",
        "cacheFirst.dataset.memoryCachedFirst = '1'",
        "searchButton.dataset.memorySearchSubmit = '1'",
        "searchButton.textContent = '搜索'",
        "searchButton.addEventListener('click', runSearch)",
        'memory-search-row',
        'id="memoryCreateFields"',
        'id="memoryEditorKey"',
        'id="memoryEditorValueType"',
        'function emptyRelationshipDraft',
        'function createMemoryDeleteButton',
        "remove.dataset.memoryDeleteConfirm = '1'",
        '再次点击“确认删除”完成操作。',
        'function openCreateGroupMemory',
        'function openCreateMemoryEntry',
        "action: 'create'",
        "action: 'delete'",
        'data.can_create === true',
        'entry.deletable === true',
        'memory-delete-button',
        '.memory-heading-actions .memory-delete-button',
        "const KNOWLEDGE_SEARCH_SECTION = '知识搜索'",
        "const TEXT_ONLY_CREATE_SECTIONS = ['用户侧写', '知识缓存', KNOWLEDGE_SEARCH_SECTION]",
        'textOnly: true',
        'const textOnly = TEXT_ONLY_CREATE_SECTIONS.indexOf(query.section) >= 0;',
        'elements.memoryEditorValueType.disabled = textOnly',
        '.memory-create-fields.has-single-field',
        "elements.memoryCreateFields.classList.toggle(\n            'has-single-field',",
    ):
        assert marker in html
    assert 'query.section === KNOWLEDGE_SEARCH_SECTION && data.can_delete === true' not in html
    assert "successMessage: '知识搜索已删除'" not in html
    assert re.search(r'\.toast\s*\{[^}]*pointer-events:\s*none;', html, re.S)
    assert '值格式可选择文本或 JSON' not in html
    assert '默认顺序（文件内）' not in html
    assert "searchInput.addEventListener('input'" not in html
    assert 'function createMemoryEditor' not in html
    assert 'memory-editor-actions' not in html
    assert '保存本页' not in html


def test_webui_image_editor_thumbnail_opens_non_saving_original_viewer():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert 'imageEntry: entry' in html
    assert 'openImageViewer(context.imageEntry, elements.memoryImageEditorThumb, {fromEditor: true})' in html
    assert 'state.memoryEditorContext.imageEntry === entry' in html
    assert 'state.memoryEditorOpen && !fromEditor' in html
    assert 'elements.imageViewerEdit.hidden = fromEditor' in html
    assert 'if (!context || context.fromEditor || !context.entry' in html


def test_webui_memory_create_delete_does_not_grant_image_cache_new_entries():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')
    assert 'data.can_create === true' in html
    assert 'data.can_delete === true' in html
    assert "add.textContent = '新增群记忆'" in html
    assert "add.textContent = data.relationship ? '新增人物关系' : '新增条目'" in html
    image_function = re.search(
        r'function createImageCacheEntry\(entry, query\) \{.*?\n    \}\n',
        html,
        re.S,
    )
    assert image_function is not None
    assert 'memory-delete-button' not in image_function.group(0)
    assert 'createMemoryDeleteButton' not in image_function.group(0)
    assert "action: 'create'" not in image_function.group(0)


def test_webui_bootstrap_redacts_bot_secrets(plugin_api):
    response = call_bridge(plugin_api, OlivOSAIChatAssassin.webUI.EVENT_BOOTSTRAP, api_payload())
    assert response['ok'] is True
    assert response['data']['capabilities']['memory_write'] is True
    assert response['data']['capabilities']['group_manager_write'] is True
    assert response['data']['bots'][0]['hash'] == BOT_HASH
    assert response['data']['bots'][0]['platform']['platform'] == 'qq'
    serialized = json.dumps(response, ensure_ascii=False)
    assert 'bot-password' not in serialized
    assert 'access-token' not in serialized
    assert 'private-session' not in serialized


def test_webui_real_menu_bridge_replies_only_to_requesting_session(plugin_api, bridge_host):
    session = bridge_host.new_session()
    other_session = bridge_host.new_session()
    payload = api_payload(bot_hash=BOT_HASH)
    data = {
        'action': 'plugin_menu',
        'namespace': OlivOSAIChatAssassin.webUI.NAMESPACE,
        'event': OlivOSAIChatAssassin.webUI.EVENT_BOOTSTRAP,
        'payload': payload,
        'webui': {
            'session': session,
            'request_id': 'real-request',
            'payload': payload,
        },
    }
    event = OlivOS.API.Event(OlivOS.API.Control.packet('send', {'data': data}))
    event.plugin_info.update(
        namespace=OlivOSAIChatAssassin.webUI.NAMESPACE,
        control_queue=bridge_host.Proc_info.control_queue,
    )
    assert event.active is True
    assert event.bot_info is None

    assert assassin_main.Event.menu(event, plugin_api.proc) is None
    bridge_host.consume(bridge_host.Proc_info.control_queue.get_nowait())

    reply = bridge_host.snapshot('events', session=session)
    assert len(reply) == 1
    assert reply[0]['namespace'] == OlivOSAIChatAssassin.webUI.NAMESPACE
    assert reply[0]['request_id'] == 'real-request'
    assert reply[0]['payload']['ok'] is True
    assert bridge_host.snapshot('events', session=other_session) == []


def test_webui_native_menu_is_not_handled(plugin_api):
    event = FakeEvent('OlivOSAIChatAssassin_Menu', webui=False)
    assert OlivOSAIChatAssassin.webUI.handle_menu(event, plugin_api.proc) is False
    assert event.sent == []


def test_webui_invalid_request_id_is_not_replied(plugin_api):
    event = FakeEvent(OlivOSAIChatAssassin.webUI.EVENT_BOOTSTRAP, api_payload(), request_id='')
    assert OlivOSAIChatAssassin.webUI.handle_menu(event, plugin_api.proc) is False
    assert event.sent == []


def test_webui_unknown_event_returns_error(plugin_api):
    response = call_bridge(plugin_api, 'OlivOSAIChatAssassin_WebUI_Unknown', api_payload())
    assert response['ok'] is False
    assert response['error']['code'] == 'unknown_event'


def test_webui_rejects_oversized_payload(plugin_api):
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_BOOTSTRAP,
        api_payload(padding='x' * OlivOSAIChatAssassin.webUI.MAX_REQUEST_BYTES),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'
    assert 'payload' in response['error']['fields']


def test_webui_config_read_redacts_keys_and_keeps_unknown_fields(plugin_api):
    config = plugin_api.manager.getConfig(BOT_HASH)
    config['api_key'] = 'main-secret'
    config['ocr_api']['api_key'] = 'ocr-secret'
    config['future_field'] = {'keep': True}
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    assert response['ok'] is True
    assert response['data']['config']['api_key'] == ''
    assert response['data']['config']['ocr_api']['api_key'] == ''
    assert response['data']['config']['future_field'] == {'keep': True}
    assert response['data']['secrets']['api_key']['configured'] is True
    assert response['data']['secrets']['ocr_api.api_key']['configured'] is True
    assert response['data']['source'] == 'bot'
    serialized = json.dumps(response, ensure_ascii=False)
    assert 'main-secret' not in serialized
    assert 'ocr-secret' not in serialized
    assert response['revision']


def test_webui_config_read_reports_extra_field_types(plugin_api):
    config = plugin_api.manager.getConfig(BOT_HASH)
    config['future_text'] = 'value'
    config['future_number'] = 12.5
    config['future_bool'] = True
    config['future_object'] = {'keep': True}
    config['future_array'] = ['keep']
    config['future_null'] = None

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )

    assert response['ok'] is True
    extra_fields = {
        item['name']: item['type']
        for item in response['data']['extra_fields']
    }
    assert extra_fields == {
        'future_text': 'string',
        'future_number': 'number',
        'future_bool': 'boolean',
        'future_object': 'object',
        'future_array': 'array',
        'future_null': 'null',
    }
    assert 'api_key' in response['data']['known_fields']
    assert 'future_text' not in response['data']['known_fields']


def test_webui_config_read_warns_about_malformed_nested_structures(plugin_api):
    config = plugin_api.manager.getConfig(BOT_HASH)
    config['thinking'] = 'invalid'
    config['ocr_api'] = 'invalid'
    config['enabled_groups'] = 'all'
    config['interaction_actions'] = 'reaction'
    config['interaction_reactions'] = {'like': True}
    config['interaction_response_enabled'] = 'yes'
    config['power_control_enabled'] = 'yes'
    config['power_default_mode'] = 'auto'
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    assert response['ok'] is True
    warnings = '\n'.join(response['data']['warnings'])
    assert 'thinking' in warnings
    assert 'ocr_api' in warnings
    assert 'enabled_groups' in warnings
    assert 'interaction_actions' in warnings
    assert 'interaction_reactions' in warnings
    assert 'interaction_response_enabled' in warnings
    assert '指令开关（power_control_enabled）不是布尔值' in warnings
    assert 'power_default_mode' in warnings


def test_webui_config_warns_when_power_default_mode_is_missing():
    warnings = '\n'.join(OlivOSAIChatAssassin.webUI._config_warnings({}))

    assert 'power_default_mode' in warnings
    assert '运行期暂按 on 处理' in warnings


def test_webui_config_save_merges_patch_and_preserves_secrets(plugin_api):
    config = plugin_api.manager.getConfig(BOT_HASH)
    config['api_key'] = 'main-secret'
    config['ocr_api']['api_key'] = 'ocr-secret'
    config['future_field'] = ['keep']
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={
                'api_timeout': 45,
                'temperature': 1.1,
                'thinking': {'type': 'enabled'},
                'ocr_api': {'enable': True, 'queue_size': 12},
            },
            clear_secrets=[],
        ),
    )
    assert response['ok'] is True
    saved = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    assert saved['api_timeout'] == 45
    assert saved['temperature'] == 1.1
    assert saved['thinking']['type'] == 'enabled'
    assert saved['ocr_api']['enable'] is True
    assert saved['ocr_api']['queue_size'] == 12
    assert saved['api_key'] == 'main-secret'
    assert saved['ocr_api']['api_key'] == 'ocr-secret'
    assert saved['future_field'] == ['keep']


def test_webui_config_save_persists_interaction_settings_and_clears_bot_cooldown(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    state = OlivOSAIChatAssassin.data
    with state.gInteractionLock:
        state.gInteractionLastAttempt[(BOT_HASH, '12345')] = 1.0
        state.gInteractionFailureUntil[(BOT_HASH, '12345', 'reaction')] = 2.0
        state.gInteractionLastAttempt[('other-bot', '12345')] = 3.0
        state.gInteractionFailureUntil[('other-bot', '12345', 'poke')] = 4.0

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={
                'interaction_enabled': True,
                'interaction_actions': ['reaction', 'poke'],
                'interaction_reactions': ['like', 'doge'],
                'interaction_cooldown': 10 ** 9,
            },
        ),
    )

    assert response['ok'] is True
    saved = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    assert saved['interaction_enabled'] is True
    assert saved['interaction_actions'] == ['reaction', 'poke']
    assert saved['interaction_reactions'] == ['like', 'doge']
    assert saved['interaction_cooldown'] == 10 ** 9
    with state.gInteractionLock:
        assert (BOT_HASH, '12345') not in state.gInteractionLastAttempt
        assert (BOT_HASH, '12345', 'reaction') not in state.gInteractionFailureUntil
        assert state.gInteractionLastAttempt[('other-bot', '12345')] == 3.0
        assert state.gInteractionFailureUntil[('other-bot', '12345', 'poke')] == 4.0
        state.gInteractionLastAttempt.clear()
        state.gInteractionFailureUntil.clear()


def test_webui_config_save_updates_and_removes_extra_fields(plugin_api):
    config = plugin_api.manager.getConfig(BOT_HASH)
    config['legacy_field'] = {'remove': True}
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={
                'future_text': 'updated',
                'future_number': 3.5,
                'future_bool': False,
                'future_object': {'nested': [1, True, None]},
                'future_array': ['a', 2],
                'future_null': None,
            },
            remove_fields=['legacy_field'],
        ),
    )

    assert response['ok'] is True
    saved = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    assert 'legacy_field' not in saved
    assert saved['future_text'] == 'updated'
    assert saved['future_number'] == 3.5
    assert saved['future_bool'] is False
    assert saved['future_object'] == {'nested': [1, True, None]}
    assert saved['future_array'] == ['a', 2]
    assert saved['future_null'] is None
    response_types = {
        item['name']: item['type']
        for item in response['data']['extra_fields']
    }
    assert response_types['future_object'] == 'object'
    assert response_types['future_array'] == 'array'
    assert response_types['future_null'] == 'null'


def test_webui_config_save_replaces_extra_object_value(plugin_api):
    config = plugin_api.manager.getConfig(BOT_HASH)
    config['future_object'] = {'keep': True, 'remove': True}
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'future_object': {'keep': False}},
        ),
    )

    assert response['ok'] is True
    saved = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    assert saved['future_object'] == {'keep': False}


def test_webui_config_save_rejects_removing_known_field(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={},
            remove_fields=['api_key'],
        ),
    )

    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'
    assert 'remove_fields.0' in response['error']['fields']


def test_webui_config_save_rejects_non_finite_extra_values(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'future_number': float('nan')},
        ),
    )

    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'
    assert 'future_number' in response['error']['fields']


def test_webui_config_save_is_visible_to_fresh_data_manager(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'temperature': 0.7, 'max_tokens': 2048, 'api_timeout': 60},
        ),
    )
    assert response['ok'] is True

    reloaded = OlivOSAIChatAssassin.load.DataManager(
        config_dir=plugin_api.manager.config_dir,
        memory_dir=plugin_api.manager.memory_dir,
        default_config=copy.deepcopy(OlivOSAIChatAssassin.data.configDefault),
    )
    reloaded.load_bot(BOT_HASH)
    persisted = reloaded.snapshot_config(BOT_HASH)[0]
    assert persisted['temperature'] == 0.7
    assert persisted['max_tokens'] == 2048
    assert persisted['api_timeout'] == 60


def test_webui_config_save_rejects_stale_revision(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    plugin_api.manager.update_bot_config(
        BOT_HASH,
        {'temperature': 0.5},
        current['revision'],
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'temperature': 1.2},
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'conflict'
    assert response['error']['fields']['current_revision']
    assert plugin_api.manager.snapshot_config(BOT_HASH)[0]['temperature'] == 0.5


def test_webui_config_save_rejects_external_file_change(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    config_path = Path(plugin_api.manager._config_path[BOT_HASH])
    external_config = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    external_config['temperature'] = 0.9
    external_config['external_field'] = {'preserve': True}
    external_bytes = (json.dumps(external_config, ensure_ascii=False, indent=4) + '\n').encode('utf-8')
    config_path.write_bytes(external_bytes)

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'temperature': 1.2},
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'conflict'
    refreshed_revision = response['error']['fields']['current_revision']
    assert refreshed_revision != current['revision']
    assert config_path.read_bytes() == external_bytes

    refreshed = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    assert refreshed['revision'] == refreshed_revision
    assert refreshed['data']['config']['temperature'] == 0.9
    saved = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=refreshed_revision,
            patch={'temperature': 1.2},
        ),
    )
    assert saved['ok'] is True
    persisted = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    assert persisted['temperature'] == 1.2
    assert persisted['external_field'] == {'preserve': True}


@pytest.mark.parametrize(
    'patch, field',
    [
        ({'max_tokens': 0}, 'max_tokens'),
        ({'api_timeout': 0}, 'api_timeout'),
        ({'temperature': -1}, 'temperature'),
        ({'api_base': 'ftp://example.com'}, 'api_base'),
        ({'enabled_groups': ['ok', '']}, 'enabled_groups.1'),
        ({'ocr_api': {'mode': 'file'}}, 'ocr_api.mode'),
        ({'reply_probability': -0.1}, 'reply_probability'),
        ({'context_budget_chars': -1}, 'context_budget_chars'),
        ({'knowledge_top_k': -1}, 'knowledge_top_k'),
        ({'interaction_enabled': 'yes'}, 'interaction_enabled'),
        ({'interaction_actions': ['reaction', 'bad']}, 'interaction_actions.1'),
        ({'interaction_actions': ['reaction', 'quote']}, 'interaction_actions.1'),
        ({'interaction_reactions': ['not_a_reaction']}, 'interaction_reactions.0'),
        ({'interaction_cooldown': -1}, 'interaction_cooldown'),
        ({'power_default_mode': 'auto'}, 'power_default_mode'),
        ({'event_private_message_enabled': 'yes'}, 'event_private_message_enabled'),
        ({'event_poke_enabled': 1}, 'event_poke_enabled'),
        ({'interaction_response_enabled': 'yes'}, 'interaction_response_enabled'),
        ({'event_recall_enabled': None}, 'event_recall_enabled'),
        ({'event_group_advanced_enabled': []}, 'event_group_advanced_enabled'),
    ],
)
def test_webui_config_save_validates_fields(plugin_api, patch, field):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch=patch,
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'
    assert field in response['error']['fields']


def test_webui_config_save_accepts_values_without_upper_limits(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    long_text = 'x' * 100001
    long_model = 'm' * 300
    long_url = 'https://example.com/' + ('p' * 2100)
    many_keywords = [f'keyword-{index}' for index in range(1001)]
    patch = {
        'api_key': 'k' * 4097,
        'api_base': long_url,
        'model': long_model,
        'max_tokens': 1000001,
        'api_timeout': 86401,
        'temperature': 3.5,
        'first_thinking_cooldown': 86401,
        'personality': long_text,
        'reply_probability': 1.5,
        'retry_count': 11,
        'search_knowledge_deepin': 17,
        'search_ageing': 2592001,
        'history_size_min': 1001,
        'history_size': 1001,
        'history_dynamic_size': 10001,
        'context_budget_chars': 1000001,
        'knowledge_top_k': 1001,
        'slack_time': 301,
        'slack_cooldown_time': 3601,
        'max_message_length': 1000001,
        'interaction_cooldown': 10 ** 9,
        'interaction_actions': ['reaction', 'poke'],
        'interaction_reactions': ['like', 'laugh', 'doge', 'surprise', 'tears'],
        'reply_keywords': many_keywords,
        'ocr_api': {
            'api_key': 'o' * 4097,
            'api_base': long_url,
            'model': long_model,
            'queue_size': 129,
        },
    }
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch=patch,
        ),
    )
    assert response['ok'] is True
    saved = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    for field, value in patch.items():
        if field == 'ocr_api':
            for ocr_field, ocr_value in value.items():
                assert saved['ocr_api'][ocr_field] == ocr_value
        else:
            assert saved[field] == value


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_webui_config_save_rejects_non_finite_numbers(plugin_api, value):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'temperature': value},
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'
    assert 'temperature' in response['error']['fields']


def test_webui_config_save_can_clear_secrets(plugin_api):
    config = plugin_api.manager.getConfig(BOT_HASH)
    config['api_key'] = 'main-secret'
    config['ocr_api']['api_key'] = 'ocr-secret'
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={},
            clear_secrets=['api_key', 'ocr_api.api_key'],
        ),
    )
    assert response['ok'] is True
    saved = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    assert saved['api_key'] == ''
    assert saved['ocr_api']['api_key'] == ''
    assert response['data']['secrets']['api_key']['configured'] is False


def test_webui_config_save_rejects_nested_secret_set_and_clear(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'ocr_api': {'api_key': 'replacement-secret'}},
            clear_secrets=['ocr_api.api_key'],
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'
    assert 'clear_secrets.0' in response['error']['fields']


def test_webui_config_save_keeps_memory_when_disk_write_fails(plugin_api, monkeypatch):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    before = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    config_path = Path(plugin_api.manager._config_path[BOT_HASH])
    disk_before = config_path.read_bytes()

    def fail_replace(source, destination):
        raise OSError('disk unavailable')

    monkeypatch.setattr(OlivOSAIChatAssassin.load.os, 'replace', fail_replace)
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'temperature': 1.3},
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'internal_error'
    assert plugin_api.manager.snapshot_config(BOT_HASH)[0] == before
    assert config_path.read_bytes() == disk_before
    assert list(config_path.parent.glob(config_path.name + '.tmp-*')) == []


def test_webui_memory_overview_and_pagination(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})
        memory['全局']['知识缓存'] = {'a': 'one', 'b': 'two'}
        memory['全局']['待办列表'] = ['第一项', '第二项']
        memory['123456'] = '本群摘要'
    overview = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_OVERVIEW,
        api_payload(bot_hash=BOT_HASH),
    )
    assert overview['ok'] is True
    assert overview['data']['group_count'] == 1
    knowledge = next(
        section for section in overview['data']['global_sections']
        if section['name'] == '知识缓存'
    )
    assert knowledge['count'] == 2
    task_list = next(
        section for section in overview['data']['global_sections']
        if section['name'] == '待办列表'
    )
    assert task_list['kind'] == 'list'
    assert task_list['count'] == 2
    assert overview['data']['groups'][0]['group_id'] == '123456'

    list_page = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='待办列表',
        ),
    )
    assert list_page['ok'] is True
    assert list_page['data']['value_kind'] == 'list'
    assert list_page['data']['editable'] is False
    assert json.loads(list_page['data']['value']) == ['第一项', '第二项']

    page = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='知识缓存',
            offset=0,
            limit=1,
        ),
    )
    assert page['ok'] is True
    assert page['data']['kind'] == 'dict'
    assert len(page['data']['entries']) == 1
    assert page['data']['entries'][0]['kind'] == 'str'
    assert page['data']['entries'][0]['editable'] is True
    assert page['data']['entries'][0]['deletable'] is True
    assert page['data']['can_create'] is True
    assert page['data']['can_delete'] is True
    assert page['data']['total'] == 2
    assert page['data']['has_more'] is True

    group = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='group',
            group_id='123456',
        ),
    )
    assert group['ok'] is True
    assert group['data']['value'] == '本群摘要'
    assert group['data']['editable'] is True
    assert group['data']['can_delete'] is True


@pytest.mark.parametrize(
    'section',
    ('用户侧写', '人物关系', '知识缓存', '知识搜索', '图片缓存'),
)
def test_webui_missing_special_memory_section_is_empty(plugin_api, image_cache_dir, section):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {}

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='global', section=section),
    )
    assert response['ok'] is True
    assert response['data']['kind'] == 'dict'
    assert response['data']['entries'] == []
    assert response['data']['total'] == 0
    can_change_entries = section in ('用户侧写', '人物关系', '知识缓存', '知识搜索')
    assert response['data']['can_create'] is can_change_entries
    assert response['data']['can_delete'] is can_change_entries
    if section == '人物关系':
        assert response['data']['relationship'] is True


def test_webui_missing_other_memory_section_is_not_found(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {}

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='global', section='临时分类'),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'not_found'


def test_webui_memory_overview_preserves_global_section_file_order(plugin_api):
    section_order = ['常识', '图片缓存', '用户侧写', '知识缓存', '人物关系']
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory['全局'] = {
            '常识': 'common sense',
            '图片缓存': {},
            '用户侧写': {},
            '知识缓存': {},
            '人物关系': {},
        }
    overview = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_OVERVIEW,
        api_payload(bot_hash=BOT_HASH),
    )
    assert overview['ok'] is True
    assert [section['name'] for section in overview['data']['global_sections']] == section_order


def test_webui_memory_overview_lists_groups_newest_first(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {}
        memory['older-group'] = '较早追加的群'
        memory['newer-group'] = '最新追加的群'
    overview = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_OVERVIEW,
        api_payload(bot_hash=BOT_HASH),
    )
    assert overview['ok'] is True
    assert [group['group_id'] for group in overview['data']['groups']] == [
        'newer-group',
        'older-group',
    ]


def test_webui_group_memory_index_paginates_newest_first(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {}
        for index in range(125):
            memory['group-%03d' % index] = '摘要 %03d' % index

    def request_page(offset):
        response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
            api_payload(
                bot_hash=BOT_HASH,
                scope='group_index',
                offset=offset,
                limit=50,
            ),
        )
        assert response['ok'] is True
        return response['data']

    first = request_page(0)
    assert first['scope'] == 'group_index'
    assert first['total'] == 125
    assert first['offset'] == 0
    assert first['next_offset'] == 50
    assert first['has_more'] is True
    assert len(first['entries']) == 50
    assert first['entries'][0]['group_id'] == 'group-124'
    assert first['entries'][-1]['group_id'] == 'group-075'

    second = request_page(50)
    assert second['total'] == 125
    assert second['next_offset'] == 100
    assert second['has_more'] is True
    assert second['entries'][0]['group_id'] == 'group-074'
    assert second['entries'][-1]['group_id'] == 'group-025'

    last = request_page(100)
    assert last['total'] == 125
    assert last['next_offset'] == 125
    assert last['has_more'] is False
    assert [entry['group_id'] for entry in last['entries']] == [
        'group-%03d' % index for index in range(24, -1, -1)
    ]


def test_webui_group_memory_can_be_edited_and_saved(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory['123456'] = '旧摘要'
        memory.setdefault('全局', {})['用户侧写'] = {'u1': '并发保留'}
    plugin_api.manager.save_bot_memory(BOT_HASH)
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='group', group_id='123456'),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            scope='group',
            group_id='123456',
            value='编辑后的摘要',
        ),
    )
    assert response['ok'] is True
    assert response['revision'] != current['revision']
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['123456'] == '编辑后的摘要'
    assert memory['全局']['用户侧写'] == {'u1': '并发保留'}
    assert revision == response['revision']
    disk = json.loads(Path(plugin_api.manager._memory_path[BOT_HASH]).read_text(encoding='utf-8'))
    assert disk['123456'] == '编辑后的摘要'


def test_webui_group_memory_detail_reports_manager_state(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory['123456'] = '旧摘要'
    plugin_api.manager.save_bot_memory(BOT_HASH)

    detail = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='group', group_id='123456'),
    )

    assert detail['ok'] is True
    assert detail['data']['can_delete'] is True
    assert detail['data']['manager'] == {
        'control_enabled': False,
        'mode': 'on',
        'source': 'default',
        'effective_enabled': True,
        'default_mode': 'on',
    }


def test_webui_group_manager_switch_requires_control_and_writes_override(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory['123456'] = '旧摘要'
    plugin_api.manager.save_bot_memory(BOT_HASH)

    disabled = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SET_GROUP_MANAGER,
        api_payload(bot_hash=BOT_HASH, group_id='123456', enabled=False),
    )
    assert disabled['ok'] is False
    assert disabled['error']['code'] == 'control_disabled'

    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    saved = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'power_control_enabled': True},
        ),
    )
    assert saved['ok'] is True

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SET_GROUP_MANAGER,
        api_payload(bot_hash=BOT_HASH, group_id='123456', enabled=False),
    )
    assert response['ok'] is True
    assert response['data']['manager'] == {
        'control_enabled': True,
        'mode': 'off',
        'source': 'override',
        'effective_enabled': False,
        'default_mode': 'on',
    }
    override_path = Path(OlivOSAIChatAssassin.data.gPowerPath)
    override_document = json.loads(override_path.read_text(encoding='utf-8'))
    assert override_document['bots'][BOT_HASH]['123456'] == 'off'


def test_webui_group_manager_switch_rejects_unknown_group(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'power_control_enabled': True},
        ),
    )

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SET_GROUP_MANAGER,
        api_payload(bot_hash=BOT_HASH, group_id='missing-group', enabled=False),
    )

    assert response['ok'] is False
    assert response['error']['code'] == 'not_found'


def test_webui_group_manager_page_renders_touch_friendly_switch():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')

    assert 'function createGroupManagerCard(data, query)' in html
    assert "setGroupManager: 'OlivOSAIChatAssassin_WebUI_SetGroupManager'" in html
    assert 'callApi(EVENTS.setGroupManager, {' in html
    assert 'switchInput.disabled = currentManager.control_enabled !== true;' in html
    assert "card.dataset.groupManager = '1';" in html
    assert '.group-manager-row {' in html
    assert '.group-manager-switch {' in html
    assert '.group-manager-state[data-state="on"] {' in html
    assert '.group-manager-state[data-state="off"] {' in html
    assert '.group-manager-switch input:focus-visible + .group-manager-track {' in html
    assert 'min-height: 48px;\n        padding-top: 0;' in html


def test_webui_group_memory_can_be_created_and_deleted(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {}
        memory['existing-group'] = '保留的群摘要'
    plugin_api.manager.save_bot_memory(BOT_HASH)

    group_index = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='group_index'),
    )
    assert group_index['ok'] is True
    assert group_index['data']['can_create'] is True

    created = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=group_index['revision'],
            scope='group',
            action='create',
            group_id='new-group',
            value='新群摘要',
        ),
    )
    assert created['ok'] is True
    assert created['data']['action'] == 'create'
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['new-group'] == '新群摘要'
    assert memory['existing-group'] == '保留的群摘要'
    assert revision == created['revision']

    detail = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='group', group_id='new-group'),
    )
    assert detail['ok'] is True
    assert detail['data']['can_delete'] is True

    deleted = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=created['revision'],
            scope='group',
            action='delete',
            group_id='new-group',
        ),
    )
    assert deleted['ok'] is True
    assert deleted['data']['action'] == 'delete'
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert 'new-group' not in memory
    assert memory['existing-group'] == '保留的群摘要'
    assert revision == deleted['revision']
    disk = json.loads(Path(plugin_api.manager._memory_path[BOT_HASH]).read_text(encoding='utf-8'))
    assert 'new-group' not in disk
    assert disk['existing-group'] == '保留的群摘要'


@pytest.mark.parametrize(
    ('section', 'key', 'value'),
    (
        ('用户侧写', 'user-new', '新侧写文本'),
        ('人物关系', '20002', [['新昵称', '别名'], '新关系描述']),
        ('知识缓存', 'knowledge-new', '新知识文本'),
        ('知识搜索', 'search-new', '新搜索文本'),
    ),
)
def test_webui_dict_memory_entries_can_be_created_and_deleted(plugin_api, section, key, value):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
    plugin_api.manager.save_bot_memory(BOT_HASH)

    empty = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='global', section=section),
    )
    assert empty['ok'] is True
    assert empty['data']['kind'] == 'dict'
    assert empty['data']['entries'] == []
    assert empty['data']['can_create'] is True
    assert empty['data']['can_delete'] is True

    created = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=empty['revision'],
            scope='global',
            section=section,
            action='create',
            key=key,
            value=value,
        ),
    )
    assert created['ok'] is True
    assert created['data']['key'] == key
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['全局'][section] == {key: value}
    assert revision == created['revision']

    listed = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='global', section=section),
    )
    assert listed['ok'] is True
    assert listed['data']['total'] == 1
    assert listed['data']['entries'][0]['key'] == key
    assert listed['data']['entries'][0]['deletable'] is True

    deleted = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=created['revision'],
            scope='global',
            section=section,
            action='delete',
            key=key,
        ),
    )
    assert deleted['ok'] is True
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['全局'][section] == {}
    assert revision == deleted['revision']


@pytest.mark.parametrize(
    ('scope', 'section', 'value'),
    (
        ('group', None, {'摘要': '对象值'}),
        ('global', '用户侧写', ['列表值']),
        ('global', '知识缓存', {'question': '对象值'}),
        ('global', '知识搜索', {'question': '对象值'}),
    ),
)
def test_webui_text_only_memory_creation_rejects_non_text(plugin_api, scope, section, value):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {}
    plugin_api.manager.save_bot_memory(BOT_HASH)
    memory_before, revision = plugin_api.manager.snapshot_memory(BOT_HASH)

    payload = api_payload(
        bot_hash=BOT_HASH,
        expected_revision=revision,
        scope=scope,
        action='create',
        value=value,
    )
    if scope == 'group':
        payload['group_id'] = 'new-group'
    else:
        payload['section'] = section
        payload['key'] = 'new-key'
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        payload,
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'
    assert 'value' in response['error']['fields']
    memory_after, revision_after = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory_after == memory_before
    assert revision_after == revision


def test_webui_memory_create_delete_rejects_restricted_and_duplicate_entries(plugin_api, image_cache_dir):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {
            '知识缓存': {'known': 'value'},
            '知识搜索': {'known-search': 'value'},
            '图片缓存': {
                'image.webp': {
                    'content': '内容',
                    'intent': '意图',
                    'type': '截图',
                }
            },
            '临时字典': {'a': 'one'},
        }
        memory['existing-group'] = '摘要'
    plugin_api.manager.save_bot_memory(BOT_HASH)
    memory_before, revision = plugin_api.manager.snapshot_memory(BOT_HASH)

    def save(**values):
        payload = api_payload(bot_hash=BOT_HASH, expected_revision=revision)
        payload.update(values)
        return call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
            payload,
        )

    duplicate_group = save(scope='group', action='create', group_id='existing-group', value='x')
    assert duplicate_group['error']['code'] == 'already_exists'
    missing_group = save(scope='group', action='delete', group_id='missing-group')
    assert missing_group['error']['code'] == 'not_found'
    reserved_group = save(scope='group', action='create', group_id='全局', value={})
    assert reserved_group['error']['code'] == 'validation_error'
    duplicate_entry = save(
        scope='global',
        section='知识缓存',
        action='create',
        key='known',
        value='other',
    )
    assert duplicate_entry['error']['code'] == 'already_exists'
    missing_entry = save(
        scope='global',
        section='知识缓存',
        action='delete',
        key='missing',
    )
    assert missing_entry['error']['code'] == 'not_found'
    image_create = save(
        scope='global',
        section='图片缓存',
        action='create',
        key='new.webp',
        value={'content': '', 'intent': '', 'type': ''},
    )
    assert image_create['error']['code'] == 'unsupported_action'
    image_delete = save(scope='global', section='图片缓存', action='delete', key='image.webp')
    assert image_delete['error']['code'] == 'unsupported_action'
    duplicate_search = save(
        scope='global',
        section='知识搜索',
        action='create',
        key='known-search',
        value='other',
    )
    assert duplicate_search['error']['code'] == 'already_exists'
    missing_search = save(
        scope='global',
        section='知识搜索',
        action='delete',
        key='missing',
    )
    assert missing_search['error']['code'] == 'not_found'
    whole_search_delete = save(scope='global', section='知识搜索', action='delete')
    assert whole_search_delete['error']['code'] == 'validation_error'
    other_create = save(scope='global', section='临时字典', action='create', key='b', value='two')
    assert other_create['error']['code'] == 'read_only'
    other_delete = save(scope='global', section='临时字典', action='delete', key='a')
    assert other_delete['error']['code'] == 'read_only'
    invalid_action = save(scope='group', action='rename', group_id='existing-group', value='x')
    assert invalid_action['error']['code'] == 'validation_error'

    memory_after, revision_after = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory_after == memory_before
    assert revision_after == revision


def test_webui_memory_create_rejects_stale_revision(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {}
    plugin_api.manager.save_bot_memory(BOT_HASH)
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='group_index'),
    )
    with OlivOSAIChatAssassin.data.gMemoryLock:
        plugin_api.manager.getMemory(BOT_HASH)['race-group'] = 'AI 并发写入'
    plugin_api.manager.save_bot_memory(BOT_HASH)
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            scope='group',
            action='create',
            group_id='new-group',
            value='页面旧内容',
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'conflict'
    assert response['error']['fields']['current_revision'] != current['revision']
    memory, _revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert 'new-group' not in memory
    assert memory['race-group'] == 'AI 并发写入'


def test_webui_global_memory_entries_can_be_edited_and_saved(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['知识缓存'] = {
            'a': 'one',
            'b': {'count': 1},
            'keep': 'unchanged',
        }
    plugin_api.manager.save_bot_memory(BOT_HASH)
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='global', section='知识缓存'),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            scope='global',
            section='知识缓存',
            entries=[
                {'key': 'a', 'value': 'changed'},
                {'key': 'b', 'value': {'count': 2}},
            ],
        ),
    )
    assert response['ok'] is True
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['全局']['知识缓存'] == {
        'a': 'changed',
        'b': {'count': 2},
        'keep': 'unchanged',
    }
    assert revision == response['revision']


def test_webui_knowledge_search_is_dictionary_with_editable_entries(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {'知识搜索': {'旧问题': '旧答案', '保留问题': '保留答案'}}
    plugin_api.manager.save_bot_memory(BOT_HASH)
    overview = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_OVERVIEW,
        api_payload(bot_hash=BOT_HASH),
    )
    search_section = next(
        section for section in overview['data']['global_sections']
        if section['name'] == '知识搜索'
    )
    assert search_section['kind'] == 'dict'
    assert search_section['count'] == 2
    assert 'preview' not in search_section
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='global', section='知识搜索'),
    )
    assert current['ok'] is True
    assert current['data']['kind'] == 'dict'
    assert current['data']['total'] == 2
    assert current['data']['can_create'] is True
    assert current['data']['can_delete'] is True
    entries = {entry['key']: entry for entry in current['data']['entries']}
    assert entries['旧问题']['value'] == '旧答案'
    assert entries['旧问题']['kind'] == 'str'
    assert entries['旧问题']['deletable'] is True

    updated = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            scope='global',
            section='知识搜索',
            entries=[{'key': '旧问题', 'value': '更新后的答案'}],
        ),
    )
    assert updated['ok'] is True
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['全局']['知识搜索'] == {'旧问题': '更新后的答案', '保留问题': '保留答案'}
    assert revision == updated['revision']

    deleted = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=updated['revision'],
            scope='global',
            section='知识搜索',
            action='delete',
            key='旧问题',
        ),
    )
    assert deleted['ok'] is True
    assert deleted['data']['key'] == '旧问题'
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['全局']['知识搜索'] == {'保留问题': '保留答案'}
    assert revision == deleted['revision']
    disk = json.loads(Path(plugin_api.manager._memory_path[BOT_HASH]).read_text(encoding='utf-8'))
    assert disk['全局']['知识搜索'] == {'保留问题': '保留答案'}


def test_webui_knowledge_search_rejects_non_dictionary_structure(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.clear()
        memory['全局'] = {'知识搜索': '旧版误存的文本'}
    plugin_api.manager.save_bot_memory(BOT_HASH)

    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='global', section='知识搜索'),
    )
    assert current['ok'] is True
    assert current['data']['kind'] == 'invalid'
    assert current['data']['entries'] == []
    assert current['data']['metadata']['kind'] == 'str'
    assert current['data']['editable'] is False
    assert current['data']['can_create'] is False
    assert current['data']['can_delete'] is False
    assert '应为对象' in current['data']['warning']

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            scope='global',
            section='知识搜索',
            value='不应覆盖异常结构',
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'memory_invalid'
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['全局']['知识搜索'] == '旧版误存的文本'
    assert revision == current['revision']


def test_webui_other_global_memory_sections_are_rendered_read_only(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        global_memory = plugin_api.manager.getMemory(BOT_HASH).setdefault('全局', {})
        global_memory['临时文本'] = '旧文本'
        global_memory['临时字典'] = {'a': 'one'}
        global_memory['临时列表'] = ['第一项', '第二项']

    def request(section):
        response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
            api_payload(bot_hash=BOT_HASH, scope='global', section=section),
        )
        assert response['ok'] is True
        return response['data']

    text_page = request('临时文本')
    assert text_page['value_kind'] == 'str'
    assert text_page['editable'] is False

    list_page = request('临时列表')
    assert list_page['value_kind'] == 'list'
    assert list_page['editable'] is False

    dict_page = request('临时字典')
    assert dict_page['kind'] == 'dict'
    assert dict_page['editable'] is False
    assert dict_page['entries'][0]['editable'] is False


@pytest.mark.parametrize(
    ('section', 'change'),
    (
        ('临时文本', {'value': '不应保存'}),
        ('临时字典', {'entries': [{'key': 'a', 'value': '不应保存'}]}),
    ),
)
def test_webui_other_global_memory_sections_reject_writes(plugin_api, section, change):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        global_memory = plugin_api.manager.getMemory(BOT_HASH).setdefault('全局', {})
        global_memory['临时文本'] = '旧文本'
        global_memory['临时字典'] = {'a': 'one'}
    plugin_api.manager.save_bot_memory(BOT_HASH)
    memory_before, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    payload = api_payload(
        bot_hash=BOT_HASH,
        expected_revision=revision,
        scope='global',
        section=section,
    )
    payload.update(change)
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        payload,
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'read_only'
    memory_after, _revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory_after == memory_before


def test_webui_structured_group_memory_round_trips_without_type_change(plugin_api):
    original = {'summary': '旧摘要', 'tags': ['旧标签']}
    with OlivOSAIChatAssassin.data.gMemoryLock:
        plugin_api.manager.getMemory(BOT_HASH)['654321'] = copy.deepcopy(original)
    plugin_api.manager.save_bot_memory(BOT_HASH)
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='group', group_id='654321'),
    )
    assert current['data']['value_kind'] == 'dict'
    assert json.loads(current['data']['value']) == original
    updated = {'summary': '新摘要', 'tags': ['新标签', '补充']}
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            scope='group',
            group_id='654321',
            value=updated,
        ),
    )
    assert response['ok'] is True
    memory, revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory['654321'] == updated
    assert isinstance(memory['654321'], dict)
    assert revision == response['revision']


def test_webui_memory_save_rejects_stale_revision(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        plugin_api.manager.getMemory(BOT_HASH)['123456'] = '初版'
    plugin_api.manager.save_bot_memory(BOT_HASH)
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(bot_hash=BOT_HASH, scope='group', group_id='123456'),
    )
    with OlivOSAIChatAssassin.data.gMemoryLock:
        plugin_api.manager.getMemory(BOT_HASH)['123456'] = 'AI 刚刚写入的新版本'
    plugin_api.manager.save_bot_memory(BOT_HASH)
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            scope='group',
            group_id='123456',
            value='页面旧内容',
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'conflict'
    assert response['error']['fields']['current_revision'] != current['revision']
    assert plugin_api.manager.snapshot_memory(BOT_HASH)[0]['123456'] == 'AI 刚刚写入的新版本'


def test_webui_memory_save_rejects_invalid_image_fields_and_unknown_entries(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'image.webp': {
                'content': '旧内容',
                'intent': '旧意图',
                'type': '旧类型',
            }
        }
        memory['全局']['知识缓存'] = {'known': 'value'}
    plugin_api.manager.save_bot_memory(BOT_HASH)
    revision = plugin_api.manager.snapshot_memory(BOT_HASH)[1]
    invalid = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=revision,
            scope='global',
            section='图片缓存',
            entries=[{'key': 'image.webp', 'value': {}}],
        ),
    )
    assert invalid['ok'] is False
    assert invalid['error']['code'] == 'validation_error'
    unknown = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=revision,
            scope='global',
            section='知识缓存',
            entries=[{'key': 'missing', 'value': 'x'}],
        ),
    )
    assert unknown['ok'] is False
    assert unknown['error']['code'] == 'not_found'
    assert plugin_api.manager.snapshot_memory(BOT_HASH)[0]['全局']['知识缓存'] == {'known': 'value'}
    assert plugin_api.manager.snapshot_memory(BOT_HASH)[0]['全局']['图片缓存'] == {
        'image.webp': {
            'content': '旧内容',
            'intent': '旧意图',
            'type': '旧类型',
        }
    }


def test_webui_memory_save_keeps_memory_when_disk_write_fails(plugin_api, monkeypatch):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        plugin_api.manager.getMemory(BOT_HASH)['123456'] = '磁盘旧值'
    plugin_api.manager.save_bot_memory(BOT_HASH)
    revision = plugin_api.manager.snapshot_memory(BOT_HASH)[1]
    path = Path(plugin_api.manager._memory_path[BOT_HASH])
    disk_before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError('disk unavailable')

    monkeypatch.setattr(OlivOSAIChatAssassin.load.os, 'replace', fail_replace)
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=revision,
            scope='group',
            group_id='123456',
            value='不应落盘的新值',
        ),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'internal_error'
    assert plugin_api.manager.snapshot_memory(BOT_HASH)[0]['123456'] == '磁盘旧值'
    assert path.read_bytes() == disk_before
    assert list(path.parent.glob(path.name + '.tmp-*')) == []


def test_webui_memory_image_cache_returns_fields_and_file_statuses(plugin_api, image_cache_dir):
    present_path = image_cache_dir / 'present.png'
    write_test_png(present_path)
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})
        memory['全局']['图片缓存'] = {
            'present.png': {
                'content': '一只蓝色小鸟',
                'intent': '表达开心',
                'type': '表情包',
            },
            'deleted.webp': {
                'content': '已删除的缓存',
                'intent': '测试缺失',
                'type': '普通图片',
            },
            'legacy.webp': {
                'content': '旧版内容',
                'intent': '旧版意图',
                'type': '旧版类型',
                'provider': 'LEGACY-PROVIDER-MARKER',
            },
            'legacy-text.webp': '旧版字符串描述',
        }
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='图片缓存',
            offset=0,
            limit=50,
        ),
    )
    assert response['ok'] is True
    data = response['data']
    assert data['image_cache'] is True
    assert data['can_create'] is False
    assert data['can_delete'] is False
    assert data['total'] == 4
    assert data['cache_hit_total'] == 1
    assert data['sort'] == 'default'
    assert data['order'] == 'desc'
    assert data['cached_first'] is True
    entries = {entry['key']: entry for entry in data['entries']}
    present = entries['present.png']
    assert present['fields'] == {
        'content': '一只蓝色小鸟',
        'intent': '表达开心',
        'type': '表情包',
    }
    assert present['editable'] is True
    assert present['deletable'] is False
    assert present['legacy'] is False
    assert present['image']['status'] == 'available'
    assert present['image']['size'] == present_path.stat().st_size
    deleted = entries['deleted.webp']
    assert deleted['editable'] is True
    assert deleted['image']['status'] == 'missing'
    legacy = entries['legacy.webp']
    assert legacy['legacy'] is True
    assert legacy['extra_fields'] == 1
    assert legacy['fields']['content'] == '旧版内容'
    legacy_text = entries['legacy-text.webp']
    assert legacy_text['legacy'] is True
    assert legacy_text['fields'] == {
        'content': '旧版字符串描述',
        'intent': '',
        'type': '',
    }
    serialized = json.dumps(response, ensure_ascii=False)
    assert 'data:image' not in serialized
    assert 'LEGACY-PROVIDER-MARKER' not in serialized


def test_webui_image_cache_directory_index_reuses_scan_until_ttl_expires(image_cache_dir, monkeypatch):
    write_test_png(image_cache_dir / 'photo.png')
    webui_api = OlivOSAIChatAssassin.webUI
    scan_count = 0
    real_scandir = webui_api.os.scandir

    def counting_scandir(path):
        nonlocal scan_count
        scan_count += 1
        return real_scandir(path)

    monkeypatch.setattr(webui_api.os, 'scandir', counting_scandir)
    first = webui_api._image_cache_directory_index()
    second = webui_api._image_cache_directory_index()
    assert scan_count == 1
    assert webui_api._image_cache_name_key('photo.png') in first
    assert second is first

    with webui_api._image_cache_index_lock:
        webui_api._image_cache_index_state['built_at'] -= webui_api.IMAGE_CACHE_INDEX_TTL + 1.0
    refreshed = webui_api._image_cache_directory_index()
    assert scan_count == 2
    assert refreshed is not first


def test_webui_image_cache_directory_index_refreshes_after_directory_changes(image_cache_dir):
    webui_api = OlivOSAIChatAssassin.webUI
    write_test_png(image_cache_dir / 'first.png')
    first = webui_api._image_cache_directory_index()
    assert webui_api._image_cache_name_key('first.png') in first

    write_test_png(image_cache_dir / 'added.png')
    directory_stat = image_cache_dir.stat()
    forced_mtime = directory_stat.st_mtime_ns + 1_000_000_000
    os.utime(image_cache_dir, ns=(forced_mtime, forced_mtime))
    added = webui_api._image_cache_directory_index()
    assert webui_api._image_cache_name_key('added.png') in added

    (image_cache_dir / 'first.png').unlink()
    directory_stat = image_cache_dir.stat()
    forced_mtime = directory_stat.st_mtime_ns + 1_000_000_000
    os.utime(image_cache_dir, ns=(forced_mtime, forced_mtime))
    removed = webui_api._image_cache_directory_index()
    assert webui_api._image_cache_name_key('first.png') not in removed
    assert webui_api._image_cache_name_key('added.png') in removed


def test_webui_image_cache_list_does_not_resolve_each_missing_file(plugin_api, image_cache_dir, monkeypatch):
    webui_api = OlivOSAIChatAssassin.webUI
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            f'missing-{index}.png': {'content': 'deleted', 'intent': '', 'type': '截图'}
            for index in range(40)
        }

    resolved = []

    def reject_path_lookup(filename):
        resolved.append(filename)
        raise AssertionError('列表只应使用目录索引判断缺失文件，不应逐条解析路径')

    monkeypatch.setattr(webui_api, '_safe_image_cache_path', reject_path_lookup)
    response = call_bridge(
        plugin_api,
        webui_api.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='图片缓存',
            offset=0,
            limit=50,
        ),
    )

    assert response['ok'] is True
    assert len(response['data']['entries']) == 40
    assert {entry['image']['status'] for entry in response['data']['entries']} == {'missing'}
    assert resolved == []


def test_webui_memory_image_list_supports_file_name_and_direction_sorting(plugin_api, image_cache_dir):
    def fields(content):
        return {'content': content, 'intent': 'test', 'type': 'image'}

    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'zeta.png': fields('first'),
            'Alpha.png': fields('second'),
            'beta.png': fields('third'),
        }

    def request(**options):
        payload = api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='图片缓存',
            offset=0,
            limit=50,
        )
        payload.update(options)
        response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
            payload,
        )
        assert response['ok'] is True
        return response['data']

    default_order = request()
    assert [entry['key'] for entry in default_order['entries']] == ['beta.png', 'Alpha.png', 'zeta.png']
    assert default_order['sort'] == 'default'
    assert default_order['order'] == 'desc'
    assert default_order['cached_first'] is True

    ascending_name = request(sort='name', order='asc')
    assert [entry['key'] for entry in ascending_name['entries']] == ['Alpha.png', 'beta.png', 'zeta.png']

    descending_name = request(sort='name', order='desc')
    assert [entry['key'] for entry in descending_name['entries']] == ['zeta.png', 'beta.png', 'Alpha.png']

    ascending_file_order = request(sort='default', order='asc')
    assert [entry['key'] for entry in ascending_file_order['entries']] == ['zeta.png', 'Alpha.png', 'beta.png']


def test_webui_memory_image_list_searches_fields_and_prioritizes_cache_hits(plugin_api, image_cache_dir):
    write_test_png(image_cache_dir / 'hit-a.png')
    write_test_png(image_cache_dir / 'Hit-B.PNG')
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'missing.png': {
                'content': '已删除缓存',
                'intent': 'fallback',
                'type': '旧图',
            },
            'hit-a.png': {
                'content': 'Happy blue bird',
                'intent': 'celebrate',
                'type': 'Sticker',
            },
            'hit-b.png': {
                'content': '红色小鸟',
                'intent': '生气',
                'type': '表情包',
            },
        }

    base_payload = api_payload(
        bot_hash=BOT_HASH,
        scope='global',
        section='图片缓存',
        offset=0,
        limit=50,
        sort='default',
        order='asc',
    )

    searched = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        dict(base_payload, search='HAPPY'),
    )
    assert searched['ok'] is True
    searched_data = searched['data']
    assert [entry['key'] for entry in searched_data['entries']] == ['hit-a.png']
    assert searched_data['filtered'] is True
    assert searched_data['total'] == 1
    assert searched_data['unfiltered_total'] == 3
    assert searched_data['cache_hit_total'] == 1

    default_page = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        base_payload,
    )
    assert default_page['ok'] is True
    cached_data = default_page['data']
    assert cached_data['cached_first'] is True
    assert cached_data['cache_hit_total'] == 2
    assert [entry['key'] for entry in cached_data['entries']] == [
        'hit-a.png',
        'hit-b.png',
        'missing.png',
    ]

    file_order = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        dict(base_payload, cached_first=False),
    )
    assert file_order['ok'] is True
    assert file_order['data']['cached_first'] is False
    assert [entry['key'] for entry in file_order['data']['entries']] == [
        'missing.png',
        'hit-a.png',
        'hit-b.png',
    ]


def test_webui_memory_image_list_filters_type_keywords_and_other(plugin_api, image_cache_dir):
    def fields(type_name):
        return {'content': '', 'intent': '', 'type': type_name}

    write_test_png(image_cache_dir / 'sticker.png')
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'sticker.png': fields('表情包'),
            'sticker-long.png': fields('搞笑表情包'),
            'mixed.png': fields('表情包截图'),
            'shot.png': fields('群聊截图'),
            'photo.png': fields('照片/旅行'),
            'other.png': fields('普通图片'),
            'empty.png': fields(''),
        }

    def request(image_types, **options):
        payload = api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='图片缓存',
            offset=0,
            limit=50,
            sort='default',
            order='asc',
            cached_first=False,
            image_types=image_types,
        )
        payload.update(options)
        response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
            payload,
        )
        assert response['ok'] is True
        return response['data']

    stickers = request(['表情包'])
    assert [entry['key'] for entry in stickers['entries']] == [
        'sticker.png',
        'sticker-long.png',
        'mixed.png',
    ]
    assert stickers['image_types'] == ['表情包']
    assert stickers['filtered'] is True
    assert stickers['cache_hit_total'] == 1

    screenshots = request(['截图'])
    assert [entry['key'] for entry in screenshots['entries']] == ['mixed.png', 'shot.png']

    photos = request(['照片'])
    assert [entry['key'] for entry in photos['entries']] == ['photo.png']

    others = request(['其它'])
    assert [entry['key'] for entry in others['entries']] == ['other.png', 'empty.png']

    combined = request(['表情包', '照片'])
    assert [entry['key'] for entry in combined['entries']] == [
        'sticker.png',
        'sticker-long.png',
        'mixed.png',
        'photo.png',
    ]

    others_and_photos = request(['其它', '照片'])
    assert [entry['key'] for entry in others_and_photos['entries']] == [
        'photo.png',
        'other.png',
        'empty.png',
    ]

    combined_with_search = request(['截图'], search='群聊')
    assert [entry['key'] for entry in combined_with_search['entries']] == ['shot.png']


def test_webui_memory_image_list_paginates_after_search_filtering(plugin_api, image_cache_dir):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'z.png': {'content': 'match-z', 'intent': '', 'type': ''},
            'a.png': {'content': 'match-a', 'intent': '', 'type': ''},
            'm.png': {'content': 'other', 'intent': '', 'type': ''},
        }

    first = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='图片缓存',
            offset=0,
            limit=1,
            sort='name',
            order='asc',
            search='MATCH',
        ),
    )
    assert first['ok'] is True
    first_data = first['data']
    assert first_data['total'] == 2
    assert first_data['unfiltered_total'] == 3
    assert first_data['has_more'] is True
    assert first_data['next_offset'] == 1
    assert [entry['key'] for entry in first_data['entries']] == ['a.png']

    second = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='图片缓存',
            offset=first_data['next_offset'],
            limit=1,
            sort='name',
            order='asc',
            search='MATCH',
        ),
    )
    assert second['ok'] is True
    assert second['data']['has_more'] is False
    assert [entry['key'] for entry in second['data']['entries']] == ['z.png']


def test_webui_memory_dict_list_supports_file_order_and_search(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['知识缓存'] = {
            'zeta': 'first entry',
            'Alpha': 'second entry',
            'beta': 'third entry',
        }

    def request(**options):
        payload = api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='知识缓存',
            offset=0,
            limit=50,
        )
        payload.update(options)
        response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
            payload,
        )
        assert response['ok'] is True
        return response['data']

    default_order = request()
    assert default_order['sort'] == 'default'
    assert default_order['order'] == 'desc'
    assert [entry['key'] for entry in default_order['entries']] == ['beta', 'Alpha', 'zeta']

    file_order = request(sort='default', order='asc')
    assert [entry['key'] for entry in file_order['entries']] == ['zeta', 'Alpha', 'beta']

    searched = request(search='SECOND')
    assert searched['filtered'] is True
    assert searched['total'] == 1
    assert [entry['key'] for entry in searched['entries']] == ['Alpha']


def test_webui_person_relationship_view_covers_supported_shapes(plugin_api):
    relationships = {
        'single-id': ['花卷', '关系描述'],
        'multi-id': [['字母君', '小哑巴'], '最近在玩塔2'],
        'description-id': [None, '这是一个AI'],
        'alias-id': ['无言'],
        'empty-alias-id': [[], '仅保留关系描述'],
        'extra-id': [['扩展昵称'], '扩展关系', {'legacy': True}],
        'numeric-description-id': ['数值描述', 42],
        'legacy-text-id': '旧格式文本',
        'legacy-dict-id': {'name': '旧字典'},
        'empty-list-id': [],
    }
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['人物关系'] = relationships

    def request(**options):
        payload = api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='人物关系',
            offset=0,
            limit=50,
            sort='default',
            order='asc',
        )
        payload.update(options)
        response = call_bridge(
            plugin_api,
            OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
            payload,
        )
        assert response['ok'] is True
        return response['data']

    page = request()
    assert page['relationship'] is True
    assert [entry['key'] for entry in page['entries']] == list(relationships)
    entries = {entry['key']: entry for entry in page['entries']}
    assert all(entry['editable'] is True for entry in entries.values())

    assert entries['single-id']['relationship'] == {
        'recognized': True,
        'format': 'single_alias',
        'aliases': ['花卷'],
        'description': '关系描述',
        'description_kind': 'str',
        'extra_fields': 0,
        'aliases_truncated': False,
        'description_truncated': False,
    }
    assert entries['multi-id']['relationship']['format'] == 'multiple_aliases'
    assert entries['multi-id']['relationship']['aliases'] == ['字母君', '小哑巴']
    assert entries['description-id']['relationship']['format'] == 'description_only'
    assert entries['description-id']['relationship']['aliases'] == []
    assert entries['alias-id']['relationship']['description'] == ''
    assert entries['empty-alias-id']['relationship']['format'] == 'description_only'
    assert entries['empty-alias-id']['relationship']['aliases'] == []
    assert entries['extra-id']['relationship']['recognized'] is True
    assert entries['extra-id']['relationship']['extra_fields'] == 1
    assert entries['numeric-description-id']['relationship']['description'] == '42'
    assert entries['numeric-description-id']['relationship']['description_kind'] == 'int'
    for key in ('legacy-text-id', 'legacy-dict-id', 'empty-list-id'):
        assert entries[key]['relationship']['recognized'] is False
        assert entries[key]['relationship']['format'] == 'unrecognized'

    assert [entry['key'] for entry in request(search='小哑巴')['entries']] == ['multi-id']
    assert [entry['key'] for entry in request(search='这是一个AI')['entries']] == [
        'description-id'
    ]


def test_webui_person_relationship_name_sort_uses_alias_names(plugin_api):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['人物关系'] = {
            'user-z': ['Zulu', '关系'],
            'user-a': ['alpha', '关系'],
            'user-m': [['Mike', 'M2'], '关系'],
            'user-n': [None, '无昵称'],
        }
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='人物关系',
            offset=0,
            limit=50,
            sort='name',
            order='asc',
        ),
    )
    assert response['ok'] is True
    assert [entry['key'] for entry in response['data']['entries']] == [
        'user-a',
        'user-m',
        'user-n',
        'user-z',
    ]


@pytest.mark.parametrize(
    'options',
    [
        {'sort': 'size'},
        {'order': 'sideways'},
        {'search': 'x' * 201},
        {'cached_first': 'yes'},
        {'image_types': '表情包'},
        {'image_types': ['未知类型']},
    ],
)
def test_webui_memory_image_list_rejects_invalid_query_options(plugin_api, options):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'image.png': {'content': 'image', 'intent': '', 'type': ''},
        }
    payload = api_payload(
        bot_hash=BOT_HASH,
        scope='global',
        section='图片缓存',
        offset=0,
        limit=50,
    )
    payload.update(options)
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        payload,
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'validation_error'


def test_webui_memory_image_endpoint_generates_thumbnail_and_original(plugin_api, image_cache_dir):
    image_path = image_cache_dir / 'photo.png'
    write_test_png(image_path, size=(800, 600))
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'photo.png': {'content': '照片', 'intent': '展示', 'type': '照片'}
        }
    thumbnail = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_IMAGE,
        api_payload(bot_hash=BOT_HASH, filename='photo.png', variant='thumbnail'),
    )
    assert thumbnail['ok'] is True
    thumbnail_data = thumbnail['data']
    assert thumbnail_data['variant'] == 'thumbnail'
    assert thumbnail_data['mime'] in ('image/jpeg', 'image/png')
    assert thumbnail_data['data_url'].startswith('data:' + thumbnail_data['mime'] + ';base64,')
    assert thumbnail_data['width'] <= 480
    assert thumbnail_data['height'] <= 360
    assert thumbnail_data['source_bytes'] == image_path.stat().st_size
    original = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_IMAGE,
        api_payload(bot_hash=BOT_HASH, filename='photo.png', variant='original'),
    )
    assert original['ok'] is True
    assert original['data']['variant'] == 'original'
    assert original['data']['bytes'] == image_path.stat().st_size


def test_webui_memory_image_endpoint_checks_only_requested_file(plugin_api, image_cache_dir, monkeypatch):
    image_path = image_cache_dir / 'single.png'
    write_test_png(image_path)
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'single.png': {'content': '单图', 'intent': '按需读取', 'type': '普通图片'}
        }

    def unexpected_directory_scan():
        raise AssertionError('单图请求不应扫描整个图片缓存目录')

    monkeypatch.setattr(
        OlivOSAIChatAssassin.webUI,
        '_image_cache_directory_index',
        unexpected_directory_scan,
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_IMAGE,
        api_payload(bot_hash=BOT_HASH, filename='single.png', variant='thumbnail'),
    )
    assert response['ok'] is True
    assert response['data']['source_bytes'] == image_path.stat().st_size


def test_webui_memory_image_endpoint_reports_deleted_cache_file(plugin_api, image_cache_dir):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'deleted.png': {'content': '档案仍在', 'intent': '测试', 'type': '普通图片'}
        }
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_IMAGE,
        api_payload(bot_hash=BOT_HASH, filename='deleted.png', variant='thumbnail'),
    )
    assert response['ok'] is False
    assert response['error']['code'] == 'image_missing'


def test_webui_memory_image_endpoint_rejects_unknown_and_unsafe_keys(
    plugin_api,
    image_cache_dir,
    tmp_path,
):
    outside_path = tmp_path / 'outside.png'
    write_test_png(outside_path)
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            '../outside.png': {'content': '越界', 'intent': '测试', 'type': '普通图片'}
        }
    unknown = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_IMAGE,
        api_payload(bot_hash=BOT_HASH, filename='not-registered.png', variant='original'),
    )
    assert unknown['ok'] is False
    assert unknown['error']['code'] == 'not_found'
    unsafe = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_IMAGE,
        api_payload(bot_hash=BOT_HASH, filename='../outside.png', variant='original'),
    )
    assert unsafe['ok'] is False
    assert unsafe['error']['code'] == 'image_invalid'


def test_webui_memory_image_save_preserves_extra_fields_when_file_is_deleted(
    plugin_api,
    image_cache_dir,
):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {
            'deleted.png': {
                'content': '旧内容',
                'intent': '旧意图',
                'type': '旧类型',
                'provider': 'keep-me',
            }
        }
    plugin_api.manager.save_bot_memory(BOT_HASH)
    revision = plugin_api.manager.snapshot_memory(BOT_HASH)[1]
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=revision,
            scope='global',
            section='图片缓存',
            entries=[{
                'key': 'deleted.png',
                'value': {
                    'content': '新内容',
                    'intent': '新意图',
                    'type': '新类型',
                },
            }],
        ),
    )
    assert response['ok'] is True
    memory_after, _revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory_after['全局']['图片缓存']['deleted.png'] == {
        'content': '新内容',
        'intent': '新意图',
        'type': '新类型',
        'provider': 'keep-me',
    }
    assert not (image_cache_dir / 'deleted.png').exists()


def test_webui_memory_image_save_migrates_legacy_scalar_value(plugin_api, image_cache_dir):
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})['图片缓存'] = {'legacy.png': '旧版描述'}
    plugin_api.manager.save_bot_memory(BOT_HASH)
    revision = plugin_api.manager.snapshot_memory(BOT_HASH)[1]
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_MEMORY,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=revision,
            scope='global',
            section='图片缓存',
            entries=[{
                'key': 'legacy.png',
                'value': {'content': '新描述', 'intent': '新意图', 'type': '新类型'},
            }],
        ),
    )
    assert response['ok'] is True
    memory_after, _revision = plugin_api.manager.snapshot_memory(BOT_HASH)
    assert memory_after['全局']['图片缓存']['legacy.png'] == {
        'content': '新描述',
        'intent': '新意图',
        'type': '新类型',
    }


def test_webui_memory_image_cache_hides_malformed_value(plugin_api):
    marker = 'MALFORMED-BASE64-SECRET-MARKER'
    with OlivOSAIChatAssassin.data.gMemoryLock:
        memory = plugin_api.manager.getMemory(BOT_HASH)
        memory.setdefault('全局', {})
        memory['全局']['图片缓存'] = marker
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_MEMORY_SECTION,
        api_payload(
            bot_hash=BOT_HASH,
            scope='global',
            section='图片缓存',
            offset=0,
            limit=50,
        ),
    )
    assert response['ok'] is True
    assert marker not in json.dumps(response, ensure_ascii=False)
    assert response['data']['image_cache'] is True
    assert response['data']['kind'] == 'invalid'
    assert '结构异常' in response['data']['warning']
    assert response['data']['metadata']['kind'] == 'str'
    assert response['data']['metadata']['length'] == len(marker)
    assert response['data']['entries'] == []


def test_webui_memory_value_preview_uses_bounded_encoding():
    text, truncated = OlivOSAIChatAssassin.webUI._display_memory_value(
        {'content': 'x' * 100000},
        limit=64,
    )
    assert truncated is True
    assert len(text) == 64


def test_webui_settings_exposes_event_receive_controls():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')

    assert '<legend>事件接收</legend>' in html
    for control_id, field in (
        ('eventPrivateMessageEnabled', 'event_private_message_enabled'),
        ('eventPokeEnabled', 'event_poke_enabled'),
        ('interactionResponseEnabled', 'interaction_response_enabled'),
        ('eventRecallEnabled', 'event_recall_enabled'),
        ('eventGroupAdvancedEnabled', 'event_group_advanced_enabled'),
    ):
        assert f'id="{control_id}"' in html
        assert f'data-field="{field}"' in html
        assert f'{field}: function () {{ return elements.{control_id}.checked; }},' in html
    assert '私聊撤回还要求“接收私聊消息”已开启' in html
    assert '含群成员增加、群成员减少、管理员变动、禁言和群文件上传' in html
    assert '连续相同戳击在 2 秒窗口内只保留一条历史' in html
    assert '目标为本人或其他群友都会进入统一群历史' in html
    assert '戳一戳进入正常回复判断' in html
    assert '目标为本人或其他群友都复用普通群消息的判断、节奏和发送链路' in html
    assert 'poke 可用当前上下文里的群友别名指定目标' in html
    assert 'elements.interactionResponseEnabled.checked = config.interaction_response_enabled === true;' in html
    assert "inbound: 'OlivOSAIChatAssassin_WebUI_GetInboundEvents'" in html
    assert 'id="overviewInboundList"' in html
    assert '展示事件日志快照；戳一戳是否进入群历史或触发回复' in html
    assert 'const INBOUND_POLL_INTERVAL = 3000;' in html
    assert 'const INBOUND_DISPLAY_LIMIT = 1;' in html
    assert 'const INBOUND_FULLSCREEN_LIMIT = 200;' in html
    assert 'const INBOUND_PAGE_SIZE = 10;' in html
    assert 'id="overviewInboundFullscreen"' in html
    assert 'id="overviewInboundFullscreenPager"' in html
    assert 'function loadInboundEvents(limitOverride)' in html
    assert 'function renderInboundEvents(force)' in html
    assert 'function renderInboundFullscreenPager(offset, entryCount, total)' in html
    assert 'function changeInboundPage(delta)' in html
    assert 'function openInboundFullscreen(options)' in html
    assert 'function closeInboundFullscreen(restoreFocus, options)' in html


def test_webui_settings_orders_power_control_before_prefixes_and_default_mode():
    page_root = PLUGIN_APP / 'OlivOSAIChatAssassin' / 'webui'
    html = (page_root / 'index.html').read_text(encoding='utf-8')

    assert OlivOSAIChatAssassin.data.configDefault['power_control_enabled'] is False
    assert OlivOSAIChatAssassin.data.configDefault['power_default_mode'] == 'on'
    assert OlivOSAIChatAssassin.data.configDefault['command_prefixes'] == ['.', '。', '/']
    assert '<legend>运行开关</legend>' in html
    assert 'id="powerControlEnabled" data-field="power_control_enabled" type="checkbox"' in html
    assert '<select id="powerDefaultMode" data-field="power_default_mode">' in html
    assert 'data-list-editor="commandPrefixes"' in html
    assert 'id="commandPrefixes" data-field="command_prefixes"' in html
    assert '<option value="on">默认开启</option>' in html
    assert '<option value="off">默认关闭</option>' in html
    assert "setSelectValue('powerDefaultMode', config.power_default_mode, 'on');" in html
    assert "setListValues('commandPrefixes', config.command_prefixes);" in html
    assert 'power_default_mode: function () { return elements.powerDefaultMode.value; },' in html
    assert "command_prefixes: function () { return readListValues('commandPrefixes'); }," in html
    assert 'power on、power off、power clear、power status' in html
    assert html.index('id="powerControlEnabled"') < html.index('data-list-editor="commandPrefixes"')
    assert html.index('data-list-editor="commandPrefixes"') < html.index('id="powerDefaultMode"')
    assert 'check-field span-2 check-field-with-description' in html
    assert '<span class="check-field-copy">' in html
    assert '.check-field-with-description {' in html
    assert '.check-field-copy {' in html
    assert 'background-image: url("data:image/svg+xml,' in html
    assert '.check-field::after {' not in html


def test_webui_config_save_persists_event_receive_switches(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={
                'event_private_message_enabled': True,
                'event_poke_enabled': True,
                'interaction_response_enabled': True,
                'event_recall_enabled': True,
                'event_group_advanced_enabled': True,
            },
        ),
    )

    assert response['ok'] is True
    saved = plugin_api.manager.snapshot_config(BOT_HASH)[0]
    assert saved['event_private_message_enabled'] is True
    assert saved['event_poke_enabled'] is True
    assert saved['interaction_response_enabled'] is True
    assert saved['event_recall_enabled'] is True
    assert saved['event_group_advanced_enabled'] is True


def test_webui_config_save_persists_power_default_mode(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'power_default_mode': 'off'},
        ),
    )

    assert response['ok'] is True
    assert plugin_api.manager.snapshot_config(BOT_HASH)[0]['power_default_mode'] == 'off'


def test_webui_config_save_persists_power_control_switch(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'power_control_enabled': True},
        ),
    )

    assert response['ok'] is True
    assert plugin_api.manager.snapshot_config(BOT_HASH)[0]['power_control_enabled'] is True


def test_webui_config_save_persists_command_prefixes(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'command_prefixes': ['!', '。', '/']},
        ),
    )

    assert response['ok'] is True
    assert plugin_api.manager.snapshot_config(BOT_HASH)[0]['command_prefixes'] == ['!', '。', '/']


def test_webui_inbound_events_endpoint_returns_read_only_snapshot(plugin_api):
    current = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_GET_CONFIG,
        api_payload(bot_hash=BOT_HASH),
    )
    call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_SAVE_CONFIG,
        api_payload(
            bot_hash=BOT_HASH,
            expected_revision=current['revision'],
            patch={'event_poke_enabled': True},
        ),
    )
    incoming = SimpleNamespace(
        bot_info=SimpleNamespace(hash=BOT_HASH),
        base_info={'self_id': 10001},
        data=SimpleNamespace(group_id=None, user_id='20002', target_id='10001'),
    )
    assert OlivOSAIChatAssassin.eventInbox.receive_poke(incoming, None) is True

    response = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_INBOUND,
        api_payload(bot_hash=BOT_HASH),
    )

    assert response['ok'] is True
    assert response['data']['event_count'] == 1
    event = response['data']['events'][0]
    assert event['kind'] == 'poke'
    assert event['scene'] == 'private'
    assert event['summary'] == '戳了戳你'


def test_webui_inbound_events_endpoint_honors_bounded_limit(plugin_api):
    OlivOSAIChatAssassin.eventInbox.clear_bot(BOT_HASH)
    for index in range(35):
        OlivOSAIChatAssassin.eventInbox._record(
            BOT_HASH,
            'poke',
            '第 %s 次戳一戳' % index,
            scene='private',
            user_id='20002',
        )

    latest = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_INBOUND,
        api_payload(bot_hash=BOT_HASH, limit=1),
    )
    fullscreen = call_bridge(
        plugin_api,
        OlivOSAIChatAssassin.webUI.EVENT_INBOUND,
        api_payload(bot_hash=BOT_HASH, limit=200),
    )

    assert latest['ok'] is True
    assert latest['data']['event_count'] == 35
    assert len(latest['data']['events']) == 1
    assert latest['data']['events'][0]['summary'] == '第 34 次戳一戳'
    assert fullscreen['ok'] is True
    assert fullscreen['data']['event_count'] == 35
    assert len(fullscreen['data']['events']) == 35
    assert fullscreen['data']['omitted_count'] == 0
