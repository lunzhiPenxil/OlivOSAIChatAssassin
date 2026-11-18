"""AI 请求重试与记忆提炼容错的离线回归测试。"""

import json
import sys
import threading
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
from OlivOSAIChatAssassin import msg as assassin_msg  # noqa: E402
from OlivOSAIChatAssassin import tools as assassin_tools  # noqa: E402
from OlivOSAIChatAssassin import webTools  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def make_config(**overrides):
    config = {
        'api_key': 'test-key',
        'api_base': 'https://api.example.test/v1',
        'model': 'test-model',
        'max_tokens': 4096,
        'temperature': 0.7,
        'thinking': {'type': 'disabled'},
        'reasoning_effort': 'max',
        'first_thinking': False,
        'retry_count': 2,
        'ocr_api': {
            'api_key': 'ocr-test-key',
            'api_base': 'https://ocr.example.test/v1',
            'model': 'ocr-test-model',
            'mode': 'base64',
            'enable': False,
            'queue_size': 8,
        },
    }
    config.update(overrides)
    return config


def chat_payload(content='ok', finish_reason='stop', reasoning_content=None):
    message = {'content': content}
    if reasoning_content is not None:
        message['reasoning_content'] = reasoning_content
    return {
        'choices': [
            {
                'message': message,
                'finish_reason': finish_reason,
            }
        ],
        'usage': {
            'prompt_tokens': 10,
            'completion_tokens': 2,
            'total_tokens': 12,
        },
    }


@pytest.fixture(autouse=True)
def quiet_logger(monkeypatch):
    events = []
    logger = SimpleNamespace(
        log=lambda message: events.append(('log', message)),
        warn=lambda message: events.append(('warn', message)),
    )
    monkeypatch.setattr(OlivOSAIChatAssassin, 'logger', logger)
    return events


def fake_post(monkeypatch, response=None, exc=None):
    captured = {}

    def post(url, headers, json, timeout):
        captured.update({
            'url': url,
            'headers': headers,
            'payload': json,
            'timeout': timeout,
        })
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(webTools.requests, 'post', post)
    return captured


def fake_post_sequence(monkeypatch, responses):
    captured = []

    def post(url, headers, json, timeout):
        captured.append({
            'url': url,
            'headers': headers,
            'payload': json,
            'timeout': timeout,
        })
        if len(captured) > len(responses):
            raise AssertionError('unexpected extra AI request')
        return responses[len(captured) - 1]

    monkeypatch.setattr(webTools.requests, 'post', post)
    return captured


# call_ai 会规范化 base_url 并返回内容。
def test_call_ai_normalizes_base_url_and_returns_content(monkeypatch):
    config = make_config(api_base='https://api.example.test/v1/')
    response = FakeResponse(payload=chat_payload('回答'))
    captured = fake_post(monkeypatch, response=response)

    assert webTools.call_ai(config, [{'role': 'user', 'content': 'hi'}]) == '回答'
    assert captured['url'] == 'https://api.example.test/v1/chat/completions'
    assert captured['timeout'] == (10.0, 120.0)


# call_ai 使用配置中的超时时间。
def test_call_ai_uses_configured_timeout(monkeypatch):
    config = make_config(api_timeout=45)
    response = FakeResponse(payload=chat_payload('回答'))
    captured = fake_post(monkeypatch, response=response)

    assert webTools.call_ai(config, [{'role': 'user', 'content': 'hi'}]) == '回答'
    assert captured['timeout'] == (10.0, 45.0)


# 空内容会带上 finish_reason 进入可重试失败。
def test_empty_content_is_retryable_with_finish_reason(monkeypatch):
    response = FakeResponse(payload=chat_payload('', reasoning_content='thought'))
    fake_post(monkeypatch, response=response)

    with pytest.raises(webTools.AICallFailure) as error:
        webTools.call_ai(make_config(), [{'role': 'user', 'content': 'hi'}])

    assert error.value.retryable is True
    assert error.value.kind == 'empty_content'
    assert error.value.finish_reason == 'stop'


# 启用推理兜底时，空内容可从 reasoning 中提取 JSON。
def test_empty_content_uses_reasoning_json_when_enabled(monkeypatch):
    response = FakeResponse(
        payload=chat_payload('', reasoning_content='思考过程\n{"r": ["好的"]}')
    )
    fake_post(monkeypatch, response=response)

    result = webTools.call_ai(
        make_config(),
        [{'role': 'user', 'content': 'hi'}],
        flag_reasoning_fallback=True,
    )

    assert result == '思考过程\n{"r": ["好的"]}'


# 空内容的推理兜底也会返回纯文本草稿。
def test_empty_content_reasoning_fallback_returns_plain_draft(monkeypatch):
    response = FakeResponse(payload=chat_payload('', reasoning_content='只有思考没有 json'))
    fake_post(monkeypatch, response=response)

    result = webTools.call_ai(
        make_config(),
        [{'role': 'user', 'content': 'hi'}],
        flag_reasoning_fallback=True,
    )

    assert result == '只有思考没有 json'


# content_filter 结束原因不会重试。
def test_content_filter_is_not_retried(monkeypatch):
    response = FakeResponse(payload=chat_payload('', finish_reason='content_filter'))
    fake_post(monkeypatch, response=response)

    with pytest.raises(webTools.AICallFailure) as error:
        webTools.call_ai(make_config(), [{'role': 'user', 'content': 'hi'}])

    assert error.value.retryable is False
    assert error.value.kind == 'empty_content'


# 不完整的结束原因会按参数化场景重试。
@pytest.mark.parametrize('finish_reason', ['length', 'insufficient_system_resource'])
def test_incomplete_finish_reasons_are_retryable(monkeypatch, finish_reason):
    response = FakeResponse(
        payload=chat_payload('{"r": [', finish_reason=finish_reason, reasoning_content='thought')
    )
    fake_post(monkeypatch, response=response)

    with pytest.raises(webTools.AICallFailure) as error:
        webTools.call_ai(make_config(), [{'role': 'user', 'content': 'hi'}])

    assert error.value.retryable is True
    assert error.value.kind == 'incomplete'
    assert error.value.finish_reason == finish_reason


# 限流错误保留 Retry-After。
def test_rate_limit_keeps_retry_after(monkeypatch):
    response = FakeResponse(
        status_code=429,
        headers={'Retry-After': '7', 'x-request-id': 'request-429'},
        text='limited',
    )
    fake_post(monkeypatch, response=response)

    with pytest.raises(webTools.AICallFailure) as error:
        webTools.call_ai(make_config(), [{'role': 'user', 'content': 'hi'}])

    assert error.value.retryable is True
    assert error.value.kind == 'http'
    assert error.value.status_code == 429
    assert error.value.retry_after == 7.0


# 鉴权错误不可重试。
def test_authentication_error_is_not_retryable(monkeypatch):
    response = FakeResponse(status_code=401, text='unauthorized')
    fake_post(monkeypatch, response=response)

    with pytest.raises(webTools.AICallFailure) as error:
        webTools.call_ai(make_config(), [{'role': 'user', 'content': 'hi'}])

    assert error.value.retryable is False
    assert error.value.kind == 'http'


# 用量日志支持 OpenAI 缓存 token 明细。
def test_log_usage_supports_openai_cached_token_details(quiet_logger):
    webTools.log_usage({
        'prompt_tokens': 100,
        'completion_tokens': 10,
        'total_tokens': 110,
        'prompt_tokens_details': {'cached_tokens': 75},
    })

    messages = [message for _level, message in quiet_logger]
    assert 'USAGE - CACHE - 75.00 % (hit=75, miss=25)' in messages


# 网络错误可重试。
def test_network_error_is_retryable(monkeypatch):
    fake_post(monkeypatch, exc=webTools.requests.Timeout('timed out'))

    with pytest.raises(webTools.AICallFailure) as error:
        webTools.call_ai(make_config(), [{'role': 'user', 'content': 'hi'}])

    assert error.value.retryable is True
    assert error.value.kind == 'network'


# OCR 会规范化 URL 并关闭思考。
def test_ocr_normalizes_url_and_disables_thinking(monkeypatch):
    config = make_config()
    config['ocr_api']['api_base'] = 'https://ocr.example.test/v1/'
    response = FakeResponse(payload=chat_payload('{}'))
    captured = fake_post(monkeypatch, response=response)

    assert webTools.call_ai_ocr(config, 'prompt', 'data:image/png;base64,AA') == '{}'
    assert captured['url'] == 'https://ocr.example.test/v1/chat/completions'
    assert captured['payload']['thinking'] == {'type': 'disabled'}


# 重试延迟有界，并尊重 Retry-After。
def test_retry_delay_is_bounded_and_respects_retry_after(monkeypatch):
    monkeypatch.setattr(webTools.random, 'uniform', lambda low, high: 1.0)

    assert webTools.get_retry_delay(1) == 1.0
    assert webTools.get_retry_delay(10) == 30.0
    assert webTools.get_retry_delay(1, retry_after=12) == 12.0
    assert webTools.get_retry_delay(1, retry_after=120) == 30.0


class FakeData:
    def __init__(self, config, memory=None):
        self.config = config
        self.memory = memory if memory is not None else {'全局': {}}

    def reload(self, bot_hash):
        return None

    def getConfig(self, bot_hash):
        return self.config

    def getMemory(self, bot_hash):
        return self.memory


# 空内容后回复重试会关闭思考模式。
def test_reply_retry_disables_thinking_after_empty_content(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=2)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    calls = []
    sleeps = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise webTools.AICallFailure(
                'empty response content',
                retryable=True,
                finish_reason='length',
                kind='empty_content',
            )
        return '{"r": ["好的"]}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: sleeps.append(value))

    result = assassin_msg.get_reply_list_with_retry('bot', 'group', [], [])

    assert result == ['好的']
    assert len(calls) == 2
    assert 'flag_thinking_override' not in calls[0]
    assert calls[1]['flag_thinking_override'] is False
    assert len(sleeps) == 1


# 回复重试会处理畸形回复 JSON。
def test_reply_retry_handles_malformed_reply_json(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=2)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return '{"r": ['
        return '{"r": ["好的"]}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: None)

    result = assassin_msg.get_reply_list_with_retry('bot', 'group', [], [])

    assert result == ['好的']
    assert len(calls) == 2
    assert calls[1]['flag_thinking_override'] is False


# 提取器可从包裹文本中恢复 JSON。
@pytest.mark.parametrize(
    'raw',
    [
        '```json\n{"r": ["好的"]}\n```',
        '格式如下：\n{"r": ["好的"]}\n请查收。',
    ],
)
def test_get_json_message_recovers_wrapped_json(raw):
    payload = assassin_msg.get_json_message(raw)

    assert payload == ['好的']


# 提取器拒绝纯文本。
def test_get_json_message_rejects_plain_text():
    text = '[发图片:example.jpg] 带我们玩就买了呗，我这周末有空！'
    assert assassin_msg.get_json_message(text) is None


# 提取器拒绝带前缀的畸形 JSON。
def test_get_json_message_rejects_prefixed_malformed_json():
    assert assassin_msg.get_json_message('结果如下：{"r": [') is None


# 提取器拒绝非字符串回复项。
def test_get_json_message_rejects_non_string_reply_items():
    assert assassin_msg.get_json_message('{"r": ["好的", 1]}') is None


# 提取器拒绝形似 JSON 的畸形输出。
def test_get_json_message_rejects_malformed_json_like_output():
    assert assassin_msg.get_json_message('{"r": [') is None


# 提取器拒绝纯文本控制词。
@pytest.mark.parametrize('raw', ['NEXT', 'SKIP'])
def test_get_json_message_rejects_plain_text_control_tokens(raw):
    assert assassin_msg.get_json_message(raw) is None


# 回复遇到纯文本输出会重试。
def test_reply_retries_plain_text_output(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=2)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    calls = []
    text = '[发图片:example.jpg] 带我们玩就买了呗，我这周末有空！'

    def call_ai(lConfig, messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            return text
        return '{"r": ["好的"]}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: None)

    result = assassin_msg.get_reply_list_with_retry('bot', 'group', [], [])

    assert result == ['好的']
    assert len(calls) == 2
    assert calls[1][1]['flag_thinking_override'] is False
    assert calls[1][1]['temperature_override'] == 0.0
    assert calls[1][1]['flag_reasoning_fallback'] is False
    assert calls[1][0][0]['role'] == 'system'
    assert '格式修复器' in calls[1][0][0]['content']
    assert json.loads(calls[1][0][1]['content']) == {'draft': text}


# 重试预算耗尽后不会发送纯文本。
def test_reply_does_not_send_plain_text_after_retry_budget(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=8)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    calls = []
    text = '[发图片:example.jpg] 带我们玩就买了呗，我这周末有空！'

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        return text

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: None)

    result = assassin_msg.get_reply_list_with_retry('bot', 'group', [], [])

    assert result is None
    assert len(calls) == 3


# 修复失败时使用纠正后的生成结果。
def test_reply_repair_failure_uses_corrected_generation(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=3)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    calls = []
    text = '直接输出的纯文本'

    def call_ai(lConfig, messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) <= 2:
            return text
        return '{"r": ["修复成功"]}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: None)

    result = assassin_msg.get_reply_list_with_retry('bot', 'group', [], [])

    assert result == ['修复成功']
    assert len(calls) == 3
    retry_messages = calls[2][0]
    assert retry_messages[0]['role'] == 'system'
    assert '唯一合法结构' in retry_messages[0]['content']
    assert all(message['role'] != 'system' for message in retry_messages[1:])


# 内容为空时会修复推理草稿。
def test_reply_repairs_reasoning_draft_when_content_is_empty(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=3)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            raise webTools.AICallFailure(
                'empty response content',
                retryable=True,
                finish_reason='stop',
                kind='empty_content',
                content='',
                reasoning_content='这是被写进思考通道的群聊回复',
            )
        return '{"r": ["这是被写进思考通道的群聊回复"]}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: None)

    result = assassin_msg.get_reply_list_with_retry('bot', 'group', [], [])

    assert result == ['这是被写进思考通道的群聊回复']
    assert len(calls) == 2
    assert json.loads(calls[1][0][1]['content']) == {
        'draft': '这是被写进思考通道的群聊回复'
    }
    assert calls[1][1]['flag_thinking_override'] is False


# 连续空内容后停止重试。
def test_reply_stops_after_repeated_empty_content(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=8)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        raise webTools.AICallFailure(
            'empty response content',
            retryable=True,
            finish_reason='stop',
            kind='empty_content',
            content='',
            reasoning_content='',
        )

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: None)

    assert assassin_msg.get_reply_list_with_retry('bot', 'group', [], []) is None
    assert len(calls) == 2
    assert calls[0].get('flag_thinking_override') is None
    assert calls[1]['flag_thinking_override'] is False


# JSON 重试消息扩展首个 system，且不留下尾部 system。
def test_json_retry_messages_extend_first_system_without_trailing_system():
    messages = [
        {'role': 'system', 'content': '系统提示'},
        {'role': 'user', 'content': '用户消息'},
    ]

    retry_messages = assassin_msg._get_json_retry_messages(messages)

    assert retry_messages[0]['role'] == 'system'
    assert retry_messages[0]['content'].startswith('系统提示')
    assert '唯一合法结构' in retry_messages[0]['content']
    assert retry_messages[-1] == {'role': 'user', 'content': '用户消息'}
    assert messages[0]['content'] == '系统提示'


# 多个候选时优先最后一个契约对象。
def test_get_json_message_prefers_last_contract_object():
    raw = '示例：{"r": ["示例"]}\n最终：{"r": ["最终"]}'

    assert assassin_msg.get_json_message(raw) == ['最终']


# 回复与动作必须来自同一个 JSON 对象。
def test_get_json_envelope_reads_reply_and_actions_from_same_object():
    raw = '{"r": ["好的"], "a": [{"t": "reaction", "v": "like"}], "ignored": true}'

    assert assassin_msg.get_json_envelope(raw) == {
        'r': ['好的'],
        'a': [{'t': 'reaction', 'v': 'like'}],
    }
    assert assassin_msg.get_json_message(raw) == ['好的']


# 使用最终选中候选对象的动作。
def test_get_json_envelope_uses_actions_from_selected_last_candidate():
    raw = (
        '示例：{"r": ["旧"], "a": [{"t": "poke"}]}\n'
        '最终：{"r": ["新"], "a": [{"t": "reaction", "v": "doge"}]}'
    )

    envelope = assassin_msg.get_json_envelope(raw)

    assert envelope == {
        'r': ['新'],
        'a': [{'t': 'reaction', 'v': 'doge'}],
    }


# 非列表动作会被规范化为空。
@pytest.mark.parametrize('actions', [None, 'reaction', {'t': 'reaction'}])
def test_get_json_envelope_normalizes_non_list_actions_to_empty(actions):
    payload = {'r': ['好的']}
    if actions is not None:
        payload['a'] = actions

    assert assassin_msg.get_json_envelope(json.dumps(payload, ensure_ascii=False)) == {
        'r': ['好的'],
        'a': [],
    }


# 回复提示词定义严格的 JSON 契约。
def test_reply_prompt_defines_strict_json_contract():
    rules = assassin_msg.REPLY_FORMAT_RULES

    assert '本节是 json 输出协议' in rules
    assert '只输出一个合法 JSON 对象' in rules
    assert '禁止输出 Markdown 代码块、解释、前后缀或纯文本回复' in rules
    assert '{"r": ["消息一", "消息二"]}' in rules
    assert '{"r": []}' in rules
    assert '纯文本、解释或格式不完整的输出会被丢弃并重试' in rules


# 主回复历史会把 assistant 消息包装成 JSON。
def test_main_reply_history_wraps_assistant_messages_as_json():
    history = [
        {
            'time': '2026-10-26T10:00:00+08:00',
            'nickname': None,
            'user_id': None,
            'message': '上一条回复',
        },
        {
            'time': '2026-10-26T10:00:01+08:00',
            'nickname': '小明',
            'user_id': '123',
            'message': '你好',
        },
    ]

    messages = assassin_msg.get_ai_context(
        make_config(), history, 'system', assistant_json_reply=True
    )

    assert json.loads(messages[1]['content']) == {'r': ['上一条回复']}


# 首轮思考历史保留纯文本 assistant 消息。
def test_first_thinking_history_keeps_plain_assistant_messages():
    history = [
        {
            'time': '2026-10-26T10:00:00+08:00',
            'nickname': None,
            'user_id': None,
            'message': '上一条回复',
        },
    ]

    messages = assassin_msg.get_ai_context(make_config(), history, 'system')

    assert messages[1]['content'] == '上一条回复'


# 动态上下文只改变提示词尾部。
def test_dynamic_context_only_changes_the_prompt_tail():
    history = [
        {
            'time': '2026-10-26T10:00:00+08:00',
            'nickname': '小明',
            'user_id': '123',
            'message': '上一条消息',
        },
        {
            'time': '2026-10-26T10:00:01+08:00',
            'nickname': '小红',
            'user_id': '456',
            'message': '你好',
        },
    ]
    first = assassin_msg.get_ai_context(
        make_config(),
        history,
        'system',
        patch={'当前记忆': {'固定记忆': {'偏好': '一'}}},
        assistant_json_reply=True,
    )
    second = assassin_msg.get_ai_context(
        make_config(),
        history,
        'system',
        patch={'当前记忆': {'固定记忆': {'偏好': '二'}}},
        assistant_json_reply=True,
    )

    assert first[:-2] == second[:-2]
    assert first[-1] == second[-1]
    assert json.loads(first[-1]['content'])['message'] == '你好'
    assert '<current_context source="system">' in first[-2]['content']
    assert first[-2]['content'] != second[-2]['content']


# 上下文块紧凑，并省略空记忆区。
def test_context_block_is_compact_and_omits_empty_memory_sections():
    history = [{
        'time': '2026-10-26T10:00:01+08:00',
        'nickname': '小明',
        'user_id': '123',
        'message': '你好',
    }]

    messages = assassin_msg.get_ai_context(
        make_config(),
        history,
        'system',
        patch={
            '当前记忆': {
                '固定记忆': {},
                '全局': {
                    '知识缓存': {},
                    '知识搜索': {'q1': '知识一'},
                    '人物关系': {'u1': '甲'},
                },
                'group-1': '群摘要',
            },
            '互动目标': {'p1': {'name': ''}},
        },
        assistant_json_reply=True,
    )

    block = messages[-2]['content']
    context_line = block.splitlines()[2]
    context = json.loads(context_line)
    assert context == {
        '当前记忆': {
            '全局': {
                '知识搜索': {'q1': '知识一'},
                '人物关系': {'u1': '甲'},
            },
            'group-1': '群摘要',
        },
        '当前互动目标': {'p1': {'name': ''}},
    }
    assert context_line == json.dumps(
        context, ensure_ascii=False, separators=(',', ':')
    )
    assert json.dumps('固定记忆', ensure_ascii=False) not in context_line
    assert json.dumps('知识缓存', ensure_ascii=False) not in context_line
    assert list(context['当前记忆']['全局']) == ['知识搜索', '人物关系']


# 所有记忆区为空时省略上下文块。
def test_context_block_is_omitted_when_all_memory_sections_are_empty():
    history = [{
        'time': '2026-10-26T10:00:01+08:00',
        'nickname': '小明',
        'user_id': '123',
        'message': '你好',
    }]

    messages = assassin_msg.get_ai_context(
        make_config(),
        history,
        'system',
        patch={'当前记忆': {'固定记忆': {}, '全局': {}}},
        assistant_json_reply=True,
    )

    assert all('<current_context' not in message['content'] for message in messages)


# 上下文块位于最新用户消息之前。
def test_context_block_precedes_the_latest_user_message():
    history = [
        {
            'time': '2026-10-26T10:00:00+08:00',
            'nickname': '小明',
            'user_id': '123',
            'message': '上一条群消息',
        },
        {
            'time': '2026-10-26T10:00:01+08:00',
            'nickname': '小红',
            'user_id': '456',
            'message': '当前群消息',
            'message_id': 'msg-2',
        },
    ]

    messages = assassin_msg.get_ai_context(
        make_config(),
        history,
        'system',
        patch={'当前记忆': {'群记忆': '测试'}},
        assistant_json_reply=True,
    )

    assert messages[-2]['role'] == 'user'
    assert '<current_context source="system">' in messages[-2]['content']
    assert json.loads(messages[-1]['content']) == {
        'time': '2026-10-26T10:00:01+08:00',
        'user_id': '456',
        'nickname': '小红',
        'message': '当前群消息',
        'message_id': 'msg-2',
    }


# 戳一戳目标去重、有界，并隐藏原始 ID。
def test_poke_targets_are_deduplicated_bounded_and_hide_raw_ids():
    history = [
        {'role': 'user', 'user_id': '101', 'nickname': '甲', 'message': '一'},
        {'role': 'assistant', 'user_id': None, 'nickname': None, 'message': '回复'},
        {'role': 'user', 'user_id': '202', 'nickname': '乙', 'message': '二'},
        {'role': 'user', 'user_id': '101', 'nickname': '甲的新昵称', 'message': '三'},
        {'role': 'user', 'user_id': '10001', 'nickname': '机器人', 'message': '自消息'},
        {'role': 'user', 'user_id': '', 'nickname': '无号用户', 'message': '四'},
    ]

    public_targets, backend_targets = assassin_msg._build_poke_targets(
        history,
        '10001',
    )

    assert public_targets == {
        'p1': {'name': '甲的新昵称'},
        'p2': {'name': '乙'},
    }
    assert backend_targets == {
        'p1': {'user_id': '101', 'name': '甲的新昵称'},
        'p2': {'user_id': '202', 'name': '乙'},
    }


# 回复重试覆盖首轮思考与主请求。
def test_reply_retry_covers_first_thinking_and_main_request(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, first_thinking=True, retry_count=2)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'get_think', lambda bot_hash, group_id: True)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: None)
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return 'NEXT'
        if len(calls) == 2:
            raise webTools.AICallFailure(
                'empty response content',
                retryable=True,
                finish_reason='length',
                kind='empty_content',
            )
        return '{"r": ["好的"]}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)

    result = assassin_msg.get_reply_list_with_retry('bot', 'group', [], [])

    assert result == ['好的']
    assert len(calls) == 3
    assert calls[0]['flag_thinking_override'] is False
    assert calls[1]['response_format_override'] == {'type': 'json_object'}
    assert 'flag_thinking_override' not in calls[1]
    assert calls[2]['flag_thinking_override'] is False


# 遇到不可重试失败时停止回复重试。
def test_reply_retry_stops_on_non_retryable_failure(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=3)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    calls = []
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    token = activity.begin('bot', 'group', '群友', '不可重试失败')
    activity.activate(token)

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        raise webTools.AICallFailure(
            'empty response content',
            retryable=False,
            finish_reason='content_filter',
            kind='empty_content',
        )

    monkeypatch.setattr(webTools, 'call_ai', call_ai)

    try:
        assert assassin_msg.get_reply_list_with_retry(
            'bot', 'group', [], [], activity_token=token
        ) is None
        assert len(calls) == 1
        snapshot = activity.snapshot('bot')
        assert snapshot['terminal_count'] == 1
        assert snapshot['failed_count'] == 1
        session = snapshot['sessions'][0]
        assert session['state'] == activity.STATE_FAILED
        assert session['error_code'] == 'model_failure'
        assert 0 < session['expires_in_ms'] <= activity.ACTIVITY_TERMINAL_TTL_MS
    finally:
        activity.clear_all()


# 可松弛公平锁会因竞争者唤醒重试等待。
def test_slackable_fair_lock_wakes_retry_wait_for_competitor():
    group_lock = assassin_tools.SlackableFairLock(slack_time=1, cooldown_time=30)
    group_lock.acquire()
    waiter_result = []

    def acquire_and_release():
        group_lock.acquire()
        group_lock.release()

    competitor_thread = threading.Thread(target=acquire_and_release)
    waiter = threading.Thread(target=lambda: waiter_result.append(group_lock.wait_for_competitor(1.0)))
    try:
        assert group_lock.wait_for_competitor(0.01) is False
        waiter.start()
        time.sleep(0.02)
        competitor_thread.start()
        waiter.join(1.0)
        assert waiter_result == [True]
    finally:
        group_lock.release()
        competitor_thread.join(1.0)
    assert competitor_thread.is_alive() is False


# 新消息会取消重试退避。
def test_retry_backoff_is_canceled_by_new_message(monkeypatch, quiet_logger):
    config = make_config(thinking={'type': 'enabled'}, retry_count=3)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    wait_calls = []
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    token = activity.begin('bot', 'cancel-group', '群友', '等待重试')
    activity.activate(token)
    next_token = activity.begin('bot', 'cancel-group', '下一位', '新消息已经到达')

    class FakeGroupLock:
        def wait_for_competitor(self, timeout):
            wait_calls.append(timeout)
            return True

    monkeypatch.setitem(OlivOSAIChatAssassin.data.gGroupLock, 'cancel-group', FakeGroupLock())
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        raise webTools.AICallFailure('network timeout', retryable=True, kind='network')

    monkeypatch.setattr(webTools, 'call_ai', call_ai)

    try:
        assert assassin_msg.get_reply_list_with_retry(
            'bot', 'cancel-group', [], [], activity_token=token
        ) is None
        assert len(calls) == 1
        assert len(wait_calls) == 1
        assert any('RETRY CANCELED' in message for level, message in quiet_logger)
        assert not any(
            level == 'warn' and message.startswith('API FATAL:')
            for level, message in quiet_logger
        )
        snapshot = activity.snapshot('bot')
        assert snapshot['session_count'] == 1
        assert snapshot['terminal_count'] == 0
        assert snapshot['superseded_count'] == 0
        session = snapshot['sessions'][0]
        assert session['state'] == activity.STATE_QUEUED
        assert session['message_preview'] == '新消息已经到达'
        assert session['sender_name'] == '下一位'
        assert session['expires_in_ms'] == 0
        activity.finish(next_token)
    finally:
        activity.clear_all()


# 记忆更新容忍缺失字段。
def test_memory_update_tolerates_missing_fields(quiet_logger):
    memory = {
        '全局': {'知识缓存': {'old': 'value'}},
        'group-1': '旧记忆',
    }

    changed = assassin_msg._update_memory_from_ai_data(
        memory, 'group-1', {'k': {'new': 'value'}}
    )

    assert changed is True
    assert memory['全局']['知识缓存'] == {'old': 'value', 'new': 'value'}
    assert memory['group-1'] == '旧记忆'
    assert ('warn', 'MEMORY - NO VALID UPDATE') not in quiet_logger


# 无有效群标识时保留原群记忆。
def test_memory_update_preserves_group_memory_without_valid_g():
    memory = {
        '全局': {},
        'group-1': '旧记忆',
    }

    changed = assassin_msg._update_memory_from_ai_data(
        memory, 'group-1', {'k': None, 'u': None, 'g': None}
    )

    assert changed is False
    assert memory['group-1'] == '旧记忆'
    assert memory['全局'] == {}


# 合法群记忆会被接受。
def test_memory_update_accepts_valid_group_memory():
    memory = {'全局': {}, 'group-1': '旧记忆'}

    changed = assassin_msg._update_memory_from_ai_data(memory, 'group-1', {'g': '新记忆'})

    assert changed is True
    assert memory['group-1'] == '新记忆'


# 记忆 JSON 畸形时只修复一次，失败则保留旧值。
def test_memory_malformed_json_one_repair_then_preserve_old_values(monkeypatch, quiet_logger):
    config = make_config()
    memory = {
        '全局': {'知识缓存': {'old': 'value'}},
        'group-1': '旧记忆',
    }
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config, memory))
    monkeypatch.setattr(
        OlivOSAIChatAssassin.data,
        'gMessageHistory',
        {'group-1': [{'role': 'user', 'message': '需要提炼的消息'}]},
    )
    responses = iter(['不是 JSON', '仍然不是 JSON'])
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(
        OlivOSAIChatAssassin.load,
        'write_memory',
        lambda **kwargs: (_ for _ in ()).throw(AssertionError('不应写入无效记忆')),
    )

    assassin_msg._run_memory_extraction('bot-a', 'group-1')

    assert len(calls) == 2
    assert calls[1]['temperature_override'] == 0.1
    assert memory == {
        '全局': {'知识缓存': {'old': 'value'}},
        'group-1': '旧记忆',
    }
    assert ('warn', 'MEMORY FORMAT ERR - one repair attempt') in quiet_logger
    assert ('warn', 'MEMORY FORMAT ERR - preserve old values') in quiet_logger


# 记忆任务使用提交时的历史快照。
def test_memory_worker_uses_submit_time_history_snapshot(monkeypatch):
    finished = threading.Event()
    captured = {}
    history = [{'role': 'user', 'message': '提交时的消息'}]

    def run_extraction(bot_hash, group_id, activity_token=None, history_snapshot=None):
        captured['snapshot'] = history_snapshot
        finished.set()

    monkeypatch.setattr(assassin_msg, '_run_memory_extraction', run_extraction)
    monkeypatch.setattr(assassin_msg, '_get_history', lambda *args: history)
    monkeypatch.setattr(assassin_msg, '_memory_worker', assassin_msg._MemoryTaskWorker())

    assassin_msg._submit_memory_extraction('bot-a', 'group-1')
    history.append({'role': 'user', 'message': '提交后的消息'})

    assert finished.wait(1.0)
    assert captured['snapshot'] == [{'role': 'user', 'message': '提交时的消息'}]


# 记忆提炼在构建 AI 上下文前过滤戳一戳。
def test_memory_extraction_filters_poke_before_building_ai_context(monkeypatch):
    config = make_config()
    memory = {'全局': {}, 'group-1': '旧记忆'}
    history = [
        {'role': 'user', 'message': '普通消息一'},
        {
            'role': 'user',
            'message': '戳一戳：群友戳了戳你',
            'message_type': assassin_msg.POKE_MESSAGE_TYPE,
            'target_id': '10001',
            'target_is_self': True,
        },
        {'role': 'assistant', 'message': '自己的回复', 'replies': ['自己的回复']},
    ]
    captured = {}

    def capture_context(lConfig, history_snapshot, *args, **kwargs):
        captured['history'] = list(history_snapshot)
        return []

    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config, memory))
    monkeypatch.setattr(assassin_msg, '_get_history', lambda *args: list(history))
    monkeypatch.setattr(assassin_msg, 'get_ai_context', capture_context)
    monkeypatch.setattr(
        webTools,
        'call_ai',
        lambda *args, **kwargs: '{"k": {}, "u": {}, "g": "新记忆"}',
    )
    monkeypatch.setattr(assassin_msg, '_apply_memory_data', lambda *args, **kwargs: True)

    result = assassin_msg._run_memory_extraction('bot-a', 'group-1')

    assert result == OlivOSAIChatAssassin.activity.MEMORY_STATE_DONE
    assert captured['history'] == [history[0], history[2]]


# 记忆任务并行完成时会更新活动卡。
def test_memory_worker_reports_parallel_completion_to_the_activity_card(monkeypatch):
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    token = activity.begin('bot-a', 'group-1', '群友', '并行任务')
    activity.activate(token)
    activity.update(token, stage=activity.STAGE_SENDING)
    activity.memory_started(token)
    started = threading.Event()

    def run_extraction(bot_hash, group_id, activity_token=None, history_snapshot=None):
        started.set()
        return activity.MEMORY_STATE_DONE

    monkeypatch.setattr(assassin_msg, '_run_memory_extraction', run_extraction)
    worker = assassin_msg._MemoryTaskWorker()
    try:
        worker.submit('bot-a', 'group-1', token, history_snapshot=[{'message': '材料'}])
        assert started.wait(1.0)
        deadline = time.monotonic() + 1.0
        memory_task = None
        while time.monotonic() < deadline:
            memory_task = activity.snapshot('bot-a')['sessions'][0]['memory_task']
            if memory_task and memory_task['state'] == activity.MEMORY_STATE_DONE:
                break
            time.sleep(0.01)
        assert memory_task['state'] == activity.MEMORY_STATE_DONE
        assert memory_task['detail'] == activity.MEMORY_DETAIL_DONE
    finally:
        activity.finish(token)
        activity.clear_all()


class NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def join(self):
        pass


def prepare_group_message(
    monkeypatch,
    *,
    message_id='msg-1',
    model='napcat_default',
    sender=None,
    config_overrides=None,
):
    bot_hash = 'test-bot'
    group_id = '12345'
    replies = []
    config_arguments = dict(
        thinking={'type': 'enabled'},
        first_thinking=False,
        retry_count=3,
        enabled_groups=['all'],
        reply_probability=1,
        history_size_min=5,
        history_size=8,
        slack_time=0,
        slack_cooldown_time=0,
    )
    if config_overrides:
        config_arguments.update(config_overrides)
    config = make_config(**config_arguments)
    memory = {
        '全局': {
            '偏好': {'表达': '自然简短'},
            '知识搜索': {},
            '知识缓存': {},
            '人物关系': {},
            '用户侧写': {},
            '图片缓存': {},
        },
        group_id: '测试群记忆',
    }
    history = [
        {
            'timestamp': 100.0 + index,
            'time': f'2026-10-26T10:00:0{index}+08:00',
            'user_id': f'user-{index}',
            'nickname': f'群友{index}',
            'message': f'历史消息{index}',
        }
        for index in range(5)
    ]
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config, memory))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMessageHistory', {group_id: history})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gGroupLock', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageCache', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gStaticKnowledge', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gPeakUpCache', {})
    monkeypatch.setattr(assassin_tools, 'sleep', lambda value: None)
    monkeypatch.setattr(assassin_tools, 'set_think', lambda bot_hash, group_id: None)
    monkeypatch.setattr(assassin_tools, 'get_think', lambda bot_hash, group_id: False)
    monkeypatch.setattr(assassin_msg, 'threading', SimpleNamespace(Thread=NoopThread))
    order = []
    actions = []

    def record_reply(text):
        replies.append(text)
        order.append(('reply', text))

    def set_msg_emoji_like(message_id_value, emoji_id, is_set=True, group_id=None):
        actions.append(('reaction', message_id_value, emoji_id, is_set, group_id))
        order.append(('reaction', emoji_id))

    def group_poke(group_id_value, user_id_value):
        actions.append(('poke', group_id_value, user_id_value))
        order.append(('poke', user_id_value))

    plugin_event = SimpleNamespace(
        platform={'sdk': 'onebot', 'platform': 'qq', 'model': model},
        bot_info=SimpleNamespace(hash=bot_hash),
        base_info={'self_id': '10001'},
        data=SimpleNamespace(
            group_id=group_id,
            user_id='42',
            message='今晚有人一起开黑吗',
            message_id=message_id,
            sender=sender if sender is not None else {'nickname': '测试用户'},
        ),
        reply=record_reply,
        set_msg_emoji_like=set_msg_emoji_like,
        group_poke=group_poke,
        order=order,
        actions=actions,
    )
    return plugin_event, group_id, replies


# 群消息发送前会修复纯文本输出。
def test_group_message_repairs_plain_text_before_sending(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(monkeypatch)
    plain_text = '这是一段没有 JSON 包装的直接回复'
    captured = fake_post_sequence(
        monkeypatch,
        [
            FakeResponse(payload=chat_payload(plain_text)),
            FakeResponse(payload=chat_payload('{"r": ["修复后的回复"]}')),
        ],
    )

    OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

    assert replies == ['修复后的回复']
    assert len(captured) == 2
    assert captured[0]['payload']['response_format'] == {'type': 'json_object'}
    assert json.loads(captured[1]['payload']['messages'][1]['content']) == {
        'draft': plain_text
    }
    assert captured[1]['payload']['thinking'] == {'type': 'disabled'}
    assert OlivOSAIChatAssassin.data.gMessageHistory[group_id][-1]['message'] == '修复后的回复'


# 群消息发送前会修复推理草稿。
def test_group_message_repairs_reasoning_draft_before_sending(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(monkeypatch)
    draft = '这是被写进思考通道的群聊回复'
    captured = fake_post_sequence(
        monkeypatch,
        [
            FakeResponse(
                payload=chat_payload(
                    '',
                    finish_reason='stop',
                    reasoning_content=draft,
                )
            ),
            FakeResponse(payload=chat_payload('{"r": ["这是被写进思考通道的群聊回复"]}')),
        ],
    )

    OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

    assert replies == [draft]
    assert len(captured) == 2
    assert captured[0]['payload']['thinking'] == {'type': 'enabled'}
    assert json.loads(captured[1]['payload']['messages'][1]['content']) == {'draft': draft}
    assert captured[1]['payload']['thinking'] == {'type': 'disabled'}
    assert OlivOSAIChatAssassin.data.gMessageHistory[group_id][-1]['message'] == draft


# 上下文之后仍保留当前群消息。
def test_group_message_keeps_current_group_message_after_context(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(monkeypatch)
    captured = fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": []}'))],
    )

    OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

    assert replies == []
    messages = captured[0]['payload']['messages']
    assert '<current_context source="system">' in messages[-2]['content']
    current_message = json.loads(messages[-1]['content'])
    assert current_message['message'] == '今晚有人一起开黑吗'
    assert current_message['message_id'] == 'msg-1'


# 提示词鼓励自然的群聊参与。
def test_group_message_prompt_prefers_natural_participation(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(monkeypatch)
    captured = fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": []}'))],
    )

    OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

    assert replies == []
    system_prompt = captured[0]['payload']['messages'][0]['content']
    assert '普通群友，不是客服、助手或话题主持人' in system_prompt
    assert '人格兴趣、你和群友的熟悉感、现场氛围与时机' in system_prompt
    assert '话题正合你的兴趣' in system_prompt
    assert '自然优先于参与' in system_prompt
    assert '但是当有人找你时尽量回复' not in system_prompt


# 群消息会报告受控阶段与完成结果。
def test_group_message_reports_controlled_stages_and_completed_result(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(monkeypatch)
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    updates = []
    original_update = activity.update

    def record_update(token, stage=None, **kwargs):
        if stage:
            updates.append(stage)
        return original_update(token, stage=stage, **kwargs)

    monkeypatch.setattr(activity, 'update', record_update)
    plain_text = '仍然必须经过 JSON 修复'
    fake_post_sequence(
        monkeypatch,
        [
            FakeResponse(payload=chat_payload(plain_text)),
            FakeResponse(payload=chat_payload('{"r": ["修复后的活动回复"]}')),
        ],
    )

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
        assert replies == ['修复后的活动回复']
        assert {
            activity.STAGE_PREPARING,
            activity.STAGE_RECALLING,
            activity.STAGE_PACING,
            activity.STAGE_MODEL,
            activity.STAGE_REPAIRING,
            activity.STAGE_SENDING,
        }.issubset(set(updates))
        snapshot = activity.snapshot('test-bot')
        assert snapshot['completed_count'] == 1
        session = snapshot['sessions'][0]
        assert session['state'] == activity.STATE_FINISHED
        assert 0 < session['expires_in_ms'] <= activity.ACTIVITY_TERMINAL_TTL_MS
        assert OlivOSAIChatAssassin.data.gMessageHistory[group_id][-1]['message'] == '修复后的活动回复'
    finally:
        activity.clear_all()


# 群消息会报告每条发送内容与位置。
def test_group_message_reports_each_outgoing_message_and_send_position(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(monkeypatch)
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    observed = []

    def capture_reply(text):
        replies.append(text)
        session = activity.snapshot('test-bot')['sessions'][0]
        observed.append({
            'message': text,
            'sending_index': session['sending_index'],
            'sent_count': session['sent_count'],
            'outgoing_messages': list(session['outgoing_messages']),
            'outgoing_total': session['outgoing_total'],
        })

    plugin_event.reply = capture_reply
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": ["第一条", "第二条"]}'))],
    )

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
        assert replies == ['第一条', '第二条']
        assert observed == [
            {
                'message': '第一条',
                'sending_index': 0,
                'sent_count': 0,
                'outgoing_messages': ['第一条', '第二条'],
                'outgoing_total': 2,
            },
            {
                'message': '第二条',
                'sending_index': 1,
                'sent_count': 1,
                'outgoing_messages': ['第一条', '第二条'],
                'outgoing_total': 2,
            },
        ]
        session = activity.snapshot('test-bot')['sessions'][0]
        assert session['state'] == activity.STATE_FINISHED
        assert session['sending_index'] == -1
        assert session['sent_count'] == 2
        assert session['outgoing_messages'] == ['第一条', '第二条']
        sending_steps = [
            entry for entry in session['trace']
            if entry['stage'] == activity.STAGE_SENDING
        ]
        assert sending_steps[0]['detail'] == '第 1/2 条'
        assert sending_steps[0]['status'] == activity.TRACE_DONE
        assert sending_steps[1]['detail'] == '第 2/2 条'
        assert sending_steps[1]['status'] == activity.TRACE_DONE
    finally:
        activity.clear_all()


def interaction_config(**overrides):
    config = {
        'interaction_enabled': True,
        'interaction_actions': ['reaction', 'poke'],
        'interaction_reactions': ['like'],
        'interaction_cooldown': 0,
    }
    config.update(overrides)
    return config


# 仅表情动作不写文本历史，也不启动记忆。
def test_group_message_reaction_only_has_no_text_history_or_memory(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": [], "a": [{"t": "reaction", "v": "like"}]}'))],
    )
    memory_submissions = []
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    OlivOSAIChatAssassin.interaction.clear_all()
    monkeypatch.setattr(
        assassin_msg,
        '_submit_memory_extraction',
        lambda *args, **kwargs: memory_submissions.append(True),
    )

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == []
        assert plugin_event.actions == [('reaction', '10001', 76, True, '12345')]
        assert memory_submissions == []
        assert all(
            entry.get('role') != 'assistant'
            for entry in OlivOSAIChatAssassin.data.gMessageHistory[group_id]
        )
        session = activity.snapshot('test-bot')['sessions'][0]
        assert session['state'] == activity.STATE_FINISHED
        assert session['error_code'] == 'interaction_only'
        assert any(
            entry['stage'] == activity.STAGE_INTERACTION
            for entry in session['trace']
        )
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()
        activity.clear_all()


# 动作通过通用事件 API 执行，不做平台门槛判断。
def test_group_message_action_uses_generic_event_api_without_platform_gate(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='opaque-message',
        model='unknown_model',
        config_overrides=interaction_config(),
    )
    del plugin_event.platform
    plugin_event.data.user_id = 'opaque-user'
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": [], "a": [{"t": "poke"}]}'))],
    )
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == []
        assert plugin_event.actions == [('poke', '12345', 'opaque-user')]
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 戳一戳别名从当前上下文解析。
def test_group_message_poke_alias_resolves_from_current_context(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    captured = fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": [], "a": [{"t": "poke", "v": "p2"}]}'))],
    )
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == []
        assert plugin_event.actions == [('poke', group_id, 'user-1')]
        context = captured[0]['payload']['messages'][-2]['content']
        assert '当前互动目标' in context
        assert '"p1"' in context
        assert '"p2"' in context
        assert 'user-0' not in context
        assert 'user-1' not in context
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 空回复且无动作仍视为 SKIP。
def test_group_message_empty_reply_without_action_remains_skip(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": []}'))],
    )
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == []
        assert plugin_event.actions == []
        session = activity.snapshot('test-bot')['sessions'][0]
        assert session['state'] == activity.STATE_SKIPPED
        assert session['error_code'] == 'no_reply'
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()
        activity.clear_all()


# 仅动作失败只报告一次。
def test_group_message_action_only_failure_is_reported_once(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": [], "a": [{"t": "reaction", "v": "like"}]}'))],
    )
    plugin_event.set_msg_emoji_like = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError('offline')
    )
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == []
        session = activity.snapshot('test-bot')['sessions'][0]
        assert session['state'] == activity.STATE_FAILED
        assert session['error_code'] == 'interaction_failed'
        assert any(
            entry['stage'] == activity.STAGE_INTERACTION
            and '失败' in (entry.get('detail') or '')
            for entry in session['trace']
        )
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()
        activity.clear_all()


# 第一个动作无效时不会回退到第二个。
def test_group_message_invalid_first_action_does_not_fallback_to_second(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    fake_post_sequence(
        monkeypatch,
        [
            FakeResponse(
                payload=chat_payload(
                    '{"r": [], "a": [{"t": "unknown"}, {"t": "reaction", "v": "like"}]}'
                )
            )
        ],
    )
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == []
        assert plugin_event.actions == []
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 动作无效不会阻塞文本发送。
def test_group_message_invalid_action_does_not_block_text(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": ["正常回复"], "a": [{"t": "unknown"}]}'))],
    )
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == ['正常回复']
        assert plugin_event.actions == []
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 文本先于独立动作发送。
def test_group_message_sends_text_before_independent_action(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": ["收到"], "a": [{"t": "reaction", "v": "like"}]}'))],
    )
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == ['收到']
        assert plugin_event.order == [('reply', '收到'), ('reaction', 76)]
        assert OlivOSAIChatAssassin.data.gMessageHistory[group_id][-1]['replies'] == ['收到']
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 文本失败时仍执行独立动作。
def test_group_message_text_failure_still_executes_independent_action(monkeypatch):
    plugin_event, _group_id, _replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": ["收到"], "a": [{"t": "poke"}]}'))],
    )
    monkeypatch.setattr(assassin_msg, '_submit_memory_extraction', lambda *args, **kwargs: 1)

    def fail_reply(text):
        raise RuntimeError('send failed')

    plugin_event.reply = fail_reply
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        with pytest.raises(RuntimeError, match='send failed'):
            OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
        assert plugin_event.actions == [('poke', '12345', '42')]
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 已移除的文本动作会被忽略。
@pytest.mark.parametrize(
    'action',
    [
        {'t': 'quote'},
        {'t': 'mention'},
    ],
)
def test_group_message_removed_text_actions_are_ignored(monkeypatch, action):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )
    payload = {'r': ['', '第一', '第二'], 'a': [action]}
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload(json.dumps(payload, ensure_ascii=False)))],
    )
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
        assert replies == ['第一', '第二']
        assert plugin_event.actions == []
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# JSON 纠正重试不会恢复动作。
def test_json_correction_retry_never_restores_actions(monkeypatch):
    config = make_config(thinking={'type': 'enabled'}, retry_count=2)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(assassin_msg, '_repair_json_reply', lambda *args, **kwargs: None)
    monkeypatch.setattr(OlivOSAIChatAssassin.tools, 'sleep', lambda value: None)
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return '{"r": ['
        return '{"r": ["恢复文本"], "a": [{"t": "reaction", "v": "like"}]}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)

    envelope = assassin_msg.get_reply_envelope_with_retry('bot', 'group', [], [])

    assert envelope == {'r': ['恢复文本'], 'a': []}
    assert len(calls) == 2


# 首轮思考 SKIP 时零互动动作。
def test_first_thinking_skip_has_zero_interaction_actions(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(first_thinking=True),
    )
    monkeypatch.setattr(assassin_tools, 'get_think', lambda bot_hash, group_id: True)
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        return 'SKIP'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert len(calls) == 1
        assert replies == []
        assert plugin_event.actions == []
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 松弛取消时零互动动作。
def test_slack_cancel_has_zero_interaction_actions(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(),
    )

    class RejectingLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def slack(self):
            return False

        def getRemaining(self):
            return 0.0

    monkeypatch.setattr(assassin_msg, '_get_group_lock', lambda bot_hash, group_id: RejectingLock())
    monkeypatch.setattr(
        webTools,
        'call_ai',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('AI must not be called')),
    )
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == []
        assert plugin_event.actions == []
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 重试取消时零互动动作。
def test_retry_cancel_has_zero_interaction_actions(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(retry_count=3),
    )

    class CancelableLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def slack(self):
            return True

        def getRemaining(self):
            return 0.0

        def wait_for_competitor(self, timeout):
            return True

    monkeypatch.setattr(assassin_msg, '_get_group_lock', lambda bot_hash, group_id: CancelableLock())
    monkeypatch.setattr(
        webTools,
        'call_ai',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            webTools.AICallFailure('network timeout', retryable=True, kind='network')
        ),
    )
    OlivOSAIChatAssassin.interaction.clear_all()

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

        assert replies == []
        assert plugin_event.actions == []
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()


# 发送前并行启动记忆提炼，并包含自身回复。
def test_memory_extraction_starts_before_send_with_assistant_reply(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(monkeypatch)
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": ["一", "二"]}'))],
    )
    order = []
    snapshots = []
    parallel_states = []

    def submit_memory(bot_hash, group_id, activity_token=None, history_snapshot=None):
        order.append('memory_submit')
        snapshots.append(list(history_snapshot or []))
        return 1

    def record_sleep(value):
        order.append('sleep')
        session = OlivOSAIChatAssassin.activity.snapshot('test-bot')['sessions'][0]
        parallel_states.append((session['stage'], session['memory_task']['state']))

    def record_reply(plugin_event, message_list, total_time_past=0.0, activity_token=None):
        order.append('send')
        replies.extend(message_list)

    monkeypatch.setattr(assassin_msg, '_submit_memory_extraction', submit_memory)
    monkeypatch.setattr(assassin_tools, 'sleep', record_sleep)
    monkeypatch.setattr(assassin_msg, 'reply', record_reply)

    OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

    assert order == ['memory_submit', 'sleep', 'send']
    assert replies == ['一', '二']
    assert snapshots[-1][-1]['role'] == 'assistant'
    assert snapshots[-1][-1]['replies'] == ['一', '二']
    assert parallel_states == [('sending', 'running')]


# 记忆提交失败不阻塞发送。
def test_memory_submit_failure_does_not_block_sending(monkeypatch, quiet_logger):
    plugin_event, _group_id, replies = prepare_group_message(monkeypatch)
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": ["正常回复"]}'))],
    )

    def fail_submit(*args, **kwargs):
        raise RuntimeError('worker unavailable')

    monkeypatch.setattr(assassin_msg, '_submit_memory_extraction', fail_submit)

    OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

    assert replies == ['正常回复']
    assert any(
        'MEMORY WORKER - SUBMIT ERR [RuntimeError]' in message
        for level, message in quiet_logger
    )
    session = OlivOSAIChatAssassin.activity.snapshot('test-bot')['sessions'][0]
    assert session['memory_task']['state'] == OlivOSAIChatAssassin.activity.MEMORY_STATE_FAILED


# 跳过路径保留短期活动卡，且不调用 AI。
def test_group_message_skipped_path_keeps_short_lived_activity_without_calling_ai(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(monkeypatch)
    activity = OlivOSAIChatAssassin.activity
    activity.clear_all()
    OlivOSAIChatAssassin.data.gData.config['reply_probability'] = 0
    finishes = []
    original_finish = activity.finish

    def record_finish(token, state='finished', error_code=None):
        finishes.append((state, error_code))
        return original_finish(token, state, error_code)

    monkeypatch.setattr(activity, 'finish', record_finish)
    monkeypatch.setattr(
        webTools,
        'call_ai',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('AI must not be called')),
    )

    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
        assert replies == []
        assert (activity.STATE_SKIPPED, 'not_selected') in finishes
        snapshot = activity.snapshot('test-bot')
        assert snapshot['skipped_count'] == 1
        assert snapshot['completed_count'] == 0
        session = snapshot['sessions'][0]
        assert session['state'] == activity.STATE_SKIPPED
        assert session['error_code'] == 'not_selected'
        assert 0 < session['expires_in_ms'] <= activity.ACTIVITY_TERMINAL_TTL_MS
    finally:
        activity.clear_all()


# assistant 历史保留多条回复。
def test_assistant_history_preserves_multiple_replies(monkeypatch):
    config = make_config()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMessageHistory', {})
    assassin_msg._ensure_history('test-bot', 'group-1')
    assassin_msg.add_message_to_history(
        'group-1',
        '一',
        None,
        None,
        bot_hash='test-bot',
        role='assistant',
        replies=['一', '二', '三'],
    )
    history = list(assassin_msg._get_history('test-bot', 'group-1'))
    messages = assassin_msg.get_ai_context(
        config, history, 'system', assistant_json_reply=True
    )

    assert json.loads(messages[1]['content']) == {'r': ['一', '二', '三']}


# 空昵称用户消息不会被误判为 assistant。
def test_user_message_with_empty_nickname_is_not_assistant(monkeypatch):
    config = make_config()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMessageHistory', {})
    assassin_msg._ensure_history('test-bot', 'group-1')
    assassin_msg.add_message_to_history(
        'group-1', '匿名用户消息', '42', '', bot_hash='test-bot', role='user'
    )
    entry = list(assassin_msg._get_history('test-bot', 'group-1'))[-1]

    assert assassin_msg._get_history_role(entry) == 'user'
    assert entry['role'] == 'user'


# 无 role 的旧历史保持兼容。
def test_legacy_history_without_role_remains_compatible():
    assert assassin_msg._get_history_role({'nickname': '小明', 'message': 'hi'}) == 'user'
    assert assassin_msg._get_history_role({'nickname': None, 'message': '旧回复'}) == 'assistant'


# assistant 历史保留回复目标元数据。
def test_assistant_history_keeps_reply_target_metadata(monkeypatch):
    config = make_config()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMessageHistory', {})
    assassin_msg._ensure_history('test-bot', 'group-1')
    assassin_msg.add_message_to_history(
        'group-1',
        '回复内容',
        None,
        None,
        bot_hash='test-bot',
        role='assistant',
        reply_to={'message_id': 'msg-9', 'user_id': '42'},
    )
    entry = list(assassin_msg._get_history('test-bot', 'group-1'))[-1]

    assert entry['reply_to'] == {'message_id': 'msg-9', 'user_id': '42'}
    assert entry['replies'] == ['回复内容']


# 历史清洗保留图片文件名。
def test_reply_history_wash_keeps_image_file_names():
    assert assassin_msg.reply_history_wash(['[发图片:cat.png]', '[图片：描述]']) == [
        '[发图片:cat.png]',
        '',
    ]


# 首轮思考空内容不会进入 JSON 修复。
def test_first_thinking_empty_content_never_enters_json_repair(monkeypatch):
    config = make_config(first_thinking=True, retry_count=2)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(assassin_tools, 'get_think', lambda bot_hash, group_id: True)
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise webTools.AICallFailure(
                'empty response content',
                retryable=True,
                kind='empty_content',
                content='预判草稿',
            )
        return '{"r": ["正文回复"]}'

    def fail_repair(*args, **kwargs):
        raise AssertionError('first thinking must not use JSON repair')

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    monkeypatch.setattr(assassin_msg, '_repair_json_reply', fail_repair)

    assert assassin_msg.get_reply_list_with_retry(
        'bot', 'group', [{'role': 'user', 'content': 'hi'}], []
    ) == ['正文回复']
    assert len(calls) == 2
    assert calls[0]['max_tokens_override'] == assassin_msg.FIRST_THINKING_MAX_TOKENS
    assert calls[0]['temperature_override'] == assassin_msg.FIRST_THINKING_TEMPERATURE


# 首轮思考严格解析 NEXT/SKIP 决策。
def test_first_thinking_strict_decision_parsing():
    assert assassin_msg._parse_first_thinking_decision(' NEXT \n') == 'NEXT'
    assert assassin_msg._parse_first_thinking_decision('SKIP') == 'SKIP'
    assert assassin_msg._parse_first_thinking_decision('NEXT please') is None
    assert assassin_msg._parse_first_thinking_decision('我会 SKIP') is None


# 首轮思考拒绝单字符决策。
@pytest.mark.parametrize('value', ['N', 'S', '1', '0', '是', '？'])
def test_first_thinking_single_character_is_rejected(value):
    assert assassin_msg._parse_first_thinking_decision(value) is None


# 首轮思考非法文本回退到主请求。
def test_first_thinking_invalid_text_falls_back_to_main_request(monkeypatch):
    config = make_config(first_thinking=True, retry_count=2)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(assassin_tools, 'get_think', lambda bot_hash, group_id: True)
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return '我不确定，也许 NEXT？'
        return '{"r": ["继续"]}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)

    assert assassin_msg.get_reply_list_with_retry('bot', 'group', [], []) == ['继续']
    assert len(calls) == 2


# 非法单字符会记录短预览并回退。
def test_first_thinking_invalid_single_character_logs_preview_and_falls_back(monkeypatch, quiet_logger):
    config = make_config(first_thinking=True, retry_count=2)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(assassin_tools, 'get_think', lambda bot_hash, group_id: True)
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return 'N'
        return json.dumps({'r': ['继续']}, ensure_ascii=False)

    monkeypatch.setattr(webTools, 'call_ai', call_ai)

    assert assassin_msg.get_reply_list_with_retry('bot', 'group', [], []) == ['继续']
    assert len(calls) == 2
    assert any('FIRST THINK - INVALID [1] preview=' in message for _level, message in quiet_logger)


# 上下文预算保留最新消息的时间顺序。
def test_context_budget_keeps_latest_messages_in_chronological_order():
    history = [
        {
            'time': f'2026-10-26T10:00:0{index}+08:00',
            'user_id': str(index),
            'nickname': f'群友{index}',
            'message': f'消息{index}',
            'role': 'user',
        }
        for index in range(5)
    ]
    filtered = assassin_msg._apply_history_budget(
        history,
        make_config(context_budget_chars=130),
        False,
    )

    assert filtered
    assert filtered[-1]['message'] == '消息4'
    assert [entry['time'] for entry in filtered] == sorted(entry['time'] for entry in filtered)


# 预算为 0 时保留原历史。
def test_context_budget_zero_preserves_existing_history():
    history = [
        {'nickname': '小明', 'user_id': '1', 'message': '一'},
        {'nickname': '小红', 'user_id': '2', 'message': '二'},
    ]
    assert assassin_msg._apply_history_budget(history, make_config(context_budget_chars=0), False) == history


# 长消息截断后仍保持 JSON 合法。
def test_context_budget_truncates_long_message_without_breaking_json():
    history = [{
        'time': '2026-10-26T10:00:00+08:00',
        'user_id': '42',
        'nickname': '很长的昵称',
        'message': '很长的消息' * 100,
        'role': 'user',
    }]
    filtered = assassin_msg._apply_history_budget(
        history,
        make_config(context_budget_chars=60),
        False,
    )

    assert len(filtered) == 1
    payload = json.loads(assassin_msg._render_history_entry(filtered[0], False)['content'])
    assert payload['message']
    assert len(payload['message']) < len(history[0]['message'])


# 上下文预算日志只记录元数据，不记录正文。
def test_context_budget_logs_metadata_without_message_body(quiet_logger):
    secret = '不得出现在日志里的完整正文'
    history = [{
        'time': '2026-10-26T10:00:00+08:00',
        'user_id': '42',
        'nickname': '群友',
        'message': secret * 20,
        'role': 'user',
    }]

    assassin_msg._apply_history_budget(
        history,
        make_config(context_budget_chars=80),
        False,
    )

    messages = [message for _level, message in quiet_logger]
    assert any(message.startswith('CONTEXT - CHARS') for message in messages)
    assert any(message.startswith('CONTEXT - BUDGET') for message in messages)
    assert any(message.startswith('CONTEXT - TRUNCATED') for message in messages)
    assert all(secret not in message for message in messages)


# 同群两个 bot 的运行时状态互相隔离。
def test_two_bots_in_same_group_have_separate_runtime_state(monkeypatch):
    config = make_config()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMessageHistory', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gGroupLock', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gImageCache', {})
    history_a = assassin_msg._ensure_history('bot-a', 'group-1')
    history_b = assassin_msg._ensure_history('bot-b', 'group-1')
    lock_a = assassin_msg._get_group_lock('bot-a', 'group-1')
    lock_b = assassin_msg._get_group_lock('bot-b', 'group-1')
    cache_a = assassin_msg._ensure_image_cache('bot-a', 'group-1')
    cache_b = assassin_msg._ensure_image_cache('bot-b', 'group-1')

    assert history_a is not history_b
    assert lock_a is not lock_b
    assert cache_a is not cache_b
    assert assassin_msg._history_key('bot-a', 'group-1') != assassin_msg._history_key('bot-b', 'group-1')


# 动态队列窗口投影与实际追加一致。
@pytest.mark.parametrize(
    'keep,max_grow,initial_count,target_index,append_count',
    [
        (8, 16, 14, 13, 8),
        (4, 18, 14, 13, 8),
        (5, 13, 8, 7, 8),
        (4, 10, 3, 2, 20),
    ],
)
def test_dynamic_queue_window_projection_matches_actual_append(
    keep,
    max_grow,
    initial_count,
    target_index,
    append_count,
):
    history = assassin_tools.DynamicQueue(keep=keep, max_grow=max_grow)
    items = [object() for _ in range(initial_count)]
    target = items[target_index]
    for item in items:
        history.append(item)
    projected = history.would_retain(target_index, append_count)
    for _ in range(append_count):
        history.append(object())

    actual = any(item is target for item in history)

    assert projected is actual


# 动态队列投影拒绝非法状态。
def test_dynamic_queue_window_projection_rejects_invalid_state():
    history = assassin_tools.DynamicQueue(keep=1, max_grow=4)
    for index in range(4):
        history.append(index)

    assert history.would_retain(3, 1) is None


# 历史窗口守卫只统计匹配且确定到达的消息。
def test_history_window_guard_counts_only_matching_guaranteed_arrivals():
    guard = assassin_tools.HistoryWindowGuard()
    key = ('bot', 'group')

    first = guard.register(key, True, ('config-a',))
    guard.register(key, True, ('config-a',))
    guard.register(key, True, ('config-b',))
    guard.register(key, False, ('config-a',))

    assert guard.pending_guaranteed_count(key, ('config-a',)) == 2
    guard.mark_appended(first)
    assert guard.pending_guaranteed_count(key, ('config-a',)) == 1
    guard.discard(first)
    assert guard.pending_guaranteed_count(key, ('config-a',)) == 1


# 历史窗口守卫按 bot 与群隔离。
def test_history_window_guard_is_isolated_by_bot_and_group():
    guard = assassin_tools.HistoryWindowGuard()
    key_a = assassin_msg._history_key('bot-a', 'group-1')
    key_b = assassin_msg._history_key('bot-b', 'group-1')

    guard.register(key_a, True, ('config',))
    guard.register(key_b, True, ('config',))

    assert guard.pending_guaranteed_count(key_a, ('config',)) == 1
    assert guard.pending_guaranteed_count(key_b, ('config',)) == 1
    assert guard.pending_guaranteed_count(('bot-c', 'group-1'), ('config',)) == 0


# 历史准入探测区分确定、拒绝与未知。
def test_history_admission_probe_separates_guaranteed_rejected_and_unknown(monkeypatch):
    config = make_config(
        enabled_groups=['all'],
        ignore_prefixes=['/skip'],
    )
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))

    def event(message, group_id='group-1'):
        return SimpleNamespace(data=SimpleNamespace(message=message, group_id=group_id))

    assert assassin_msg._probe_history_admission(
        event('普通文本'), 'bot', 'group-1'
    )[0] == assassin_msg.HISTORY_ARRIVAL_GUARANTEED
    assert assassin_msg._probe_history_admission(
        event('/skip 忽略'), 'bot', 'group-1'
    )[0] == assassin_msg.HISTORY_ARRIVAL_REJECTED
    assert assassin_msg._probe_history_admission(
        event('   '), 'bot', 'group-1'
    )[0] == assassin_msg.HISTORY_ARRIVAL_GUARANTEED
    assert assassin_msg._probe_history_admission(
        event(''), 'bot', 'group-1'
    )[0] == assassin_msg.HISTORY_ARRIVAL_REJECTED

    config['ignore_prefixes'] = []
    assert assassin_msg._probe_history_admission(
        event('[OP:image,file=a.png,url=https://example.test/a.png]'),
        'bot',
        'group-1',
    )[0] == assassin_msg.HISTORY_ARRIVAL_UNKNOWN

    config['enabled_groups'] = ['other-group']
    assert assassin_msg._probe_history_admission(
        event('普通文本'), 'bot', 'group-1'
    )[0] == assassin_msg.HISTORY_ARRIVAL_REJECTED


# 默认历史窗口分块以提升提示词缓存命中。
def test_default_history_window_is_chunked_for_prompt_cache(monkeypatch):
    config = make_config()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))

    history = assassin_msg._new_history('test-bot')

    assert history.keep == OlivOSAIChatAssassin.data.configDefault['history_size']
    assert history.max_grow == OlivOSAIChatAssassin.data.configDefault['history_dynamic_size']
    assert history.max_grow > history.keep


# 旧群历史会复制给其它 bot。
def test_legacy_group_history_is_copied_for_other_bots(monkeypatch):
    config = make_config()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config))
    legacy = OlivOSAIChatAssassin.tools.DynamicQueue(keep=10, max_grow=10)
    legacy.append({
        'role': 'user',
        'user_id': '1',
        'nickname': '旧群友',
        'message': '旧消息',
    })
    monkeypatch.setattr(
        OlivOSAIChatAssassin.data,
        'gMessageHistory',
        {'group-1': legacy},
    )

    history_a = assassin_msg._get_history('bot-a', 'group-1')
    history_b = assassin_msg._get_history('bot-b', 'group-1')
    assassin_msg.add_message_to_history(
        'group-1', 'A 的新消息', '2', '甲', bot_hash='bot-a', role='user'
    )
    assassin_msg.add_message_to_history(
        'group-1', 'B 的新消息', '3', '乙', bot_hash='bot-b', role='user'
    )

    assert history_a is legacy
    assert history_b is not history_a
    assert [entry['message'] for entry in history_a] == ['旧消息', 'A 的新消息']
    assert [entry['message'] for entry in history_b] == ['旧消息', 'B 的新消息']


# 提示词包含边界与共享记忆范围。
def test_group_message_prompts_include_boundary_and_shared_memory_scope(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(monkeypatch)
    config = OlivOSAIChatAssassin.data.gData.getConfig('test-bot')
    config['first_thinking'] = True
    monkeypatch.setattr(assassin_tools, 'get_think', lambda bot_hash, group_id: True)
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return 'NEXT'
        return '{"r": []}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

    assert replies == []
    assert len(calls) == 2
    boundary_prefix = '# 输入边界\n' + assassin_msg.INPUT_BOUNDARY_RULES
    for messages in calls:
        assert messages[0]['content'].startswith(boundary_prefix)
        assert assassin_msg.INPUT_BOUNDARY_RULES in messages[0]['content']
        assert assassin_msg.REPLY_DECISION_RULES in messages[0]['content']
        assert '<current_context source="system">' in messages[-2]['content']
        assert json.loads(messages[-1]['content'])['message'] == '今晚有人一起开黑吗'
        assert '固定记忆' not in messages[0]['content']
    assert '固定记忆' in calls[0][-2]['content']
    assert '固定记忆' in calls[1][-2]['content']
    main_system = calls[1][0]['content']
    assert main_system.index('# 当前任务') < main_system.index('# 已知信息')


# 首轮思考提示词只保留决策关键上下文。
def test_first_thinking_prompt_keeps_only_decision_critical_context(monkeypatch):
    plugin_event, _group_id, replies = prepare_group_message(
        monkeypatch,
        message_id='10001',
        config_overrides=interaction_config(
            first_thinking=True,
            personality='喜欢接梗，但对客服式问答没有兴趣',
        ),
    )
    monkeypatch.setattr(assassin_tools, 'get_think', lambda bot_hash, group_id: True)
    calls = []

    def call_ai(lConfig, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return 'NEXT'
        return '{"r": []}'

    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    OlivOSAIChatAssassin.interaction.clear_all()
    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
    finally:
        OlivOSAIChatAssassin.interaction.clear_all()

    assert replies == []
    assert len(calls) == 2
    first_system = calls[0][0]['content']
    main_system = calls[1][0]['content']
    assert '喜欢接梗，但对客服式问答没有兴趣' in first_system
    assert '[OP:at,id=10001]' in first_system
    assert '表情回应、戳一戳' in first_system
    assert '拿不准' in first_system
    assert '# 输出协议（最高优先级）' in first_system
    assert '只能完整输出大写单词 `NEXT` 或 `SKIP`' in first_system
    assert '禁止输出单字母' in first_system
    assert assassin_msg.REPLY_FORMAT_RULES not in first_system
    assert '# 参考输出' not in first_system
    assert '发图片' not in first_system
    assert len(first_system) < len(main_system)


# 群消息历史保留回复边界。
def test_group_message_history_preserves_reply_boundaries(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(monkeypatch)
    fake_post_sequence(
        monkeypatch,
        [FakeResponse(payload=chat_payload('{"r": ["一", "二", "三"]}'))],
    )

    OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)

    assert replies == ['一', '二', '三']
    history = list(assassin_msg._get_history('test-bot', group_id))
    assert history[-1]['role'] == 'assistant'
    assert history[-1]['replies'] == ['一', '二', '三']
    assert history[-1]['reply_to'] == {'message_id': 'msg-1', 'user_id': '42'}


def install_history_window_case(
    monkeypatch,
    *,
    keep,
    max_grow,
    initial_count=13,
    config_overrides=None,
):
    window_config = {
        'history_size': keep,
        'history_dynamic': True,
        'history_dynamic_size': max_grow,
        'history_size_min': 1,
        'reply_probability': 1,
        'slack_time': 0,
        'slack_cooldown_time': 0,
    }
    if config_overrides:
        window_config.update(config_overrides)
    plugin_event, group_id, replies = prepare_group_message(
        monkeypatch,
        config_overrides=window_config,
    )
    history = assassin_tools.DynamicQueue(keep=keep, max_grow=max_grow)
    for index in range(initial_count):
        history.append({
            'timestamp': 1000.0 + index,
            'time': f'2026-10-29T10:00:{index:02d}+08:00',
            'user_id': f'history-{index}',
            'nickname': f'历史群友{index}',
            'message': f'历史消息{index}',
            'role': 'user',
        })
    key = assassin_msg._history_key('test-bot', group_id)
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gMessageHistory', {key: history})
    guard = assassin_tools.HistoryWindowGuard()
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gHistoryWindowGuard', guard)
    signature = assassin_msg._history_admission_signature(
        OlivOSAIChatAssassin.data.gData.getConfig('test-bot')
    )
    return plugin_event, group_id, replies, history, key, guard, signature


def window_followup_event(base_event, group_id: str, message: str, index: int):
    return SimpleNamespace(
        platform=base_event.platform,
        bot_info=base_event.bot_info,
        base_info=base_event.base_info,
        data=SimpleNamespace(
            group_id=group_id,
            user_id=f'follow-user-{index}',
            message=message,
            message_id=f'follow-message-{index}',
            sender={'nickname': f'后续群友{index}'},
        ),
        reply=lambda text: None,
    )


# 检索使用已确认的未来窗口。
@pytest.mark.parametrize(
    'keep,max_grow,pending_count,expect_cancel',
    [
        (4, 18, 8, True),
        (4, 18, 4, False),
        (8, 16, 8, False),
    ],
)
def test_group_message_retrieval_uses_confirmed_future_window(
    monkeypatch,
    quiet_logger,
    keep,
    max_grow,
    pending_count,
    expect_cancel,
):
    plugin_event, group_id, replies, history, key, guard, signature = (
        install_history_window_case(
            monkeypatch,
            keep=keep,
            max_grow=max_grow,
        )
    )
    # 本用例只验证检索窗口让位；排除既有节奏锁在竞争者出现时的正常让位。
    group_lock = assassin_msg._get_group_lock('test-bot', group_id)
    monkeypatch.setattr(group_lock, 'slack', lambda: True)
    current_message = plugin_event.data.message
    recall_started = threading.Event()
    release_recall = threading.Event()
    thread_errors = []
    ai_calls = []

    monkeypatch.setattr(
        assassin_msg,
        'should_reply',
        lambda group_id, message, plugin_event, bot_hash=None: message == current_message,
    )

    def recall(bot_hash, history_snapshot, cancel_check=None):
        recall_started.set()
        assert release_recall.wait(2.0)
        if cancel_check is not None:
            cancel_check('test_retrieval')
        return {}, 0

    def call_ai(*args, **kwargs):
        ai_calls.append(kwargs)
        return '{"r": ["收到"]}'

    monkeypatch.setattr(assassin_msg, '_recall_knowledge', recall)
    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    OlivOSAIChatAssassin.activity.clear_all()

    def run_event(event):
        try:
            OlivOSAIChatAssassin.main.Event.group_message(event, None)
        except BaseException as exc:
            thread_errors.append(exc)

    current_thread = threading.Thread(target=run_event, args=(plugin_event,))
    followup_threads = []
    try:
        current_thread.start()
        assert recall_started.wait(2.0)
        for index in range(pending_count):
            event = window_followup_event(
                plugin_event,
                group_id,
                f'后续消息{index}',
                index,
            )
            followup_threads.append(
                threading.Thread(target=run_event, args=(event,))
            )
        for thread in followup_threads:
            thread.start()
        deadline = time.monotonic() + 2.0
        while guard.pending_guaranteed_count(key, signature) < pending_count:
            if time.monotonic() >= deadline:
                pytest.fail('后续消息未在超时前完成待到达登记')
            time.sleep(0.005)
        release_recall.set()
        current_thread.join(3.0)
        for thread in followup_threads:
            thread.join(3.0)
    finally:
        release_recall.set()
        current_thread.join(3.0)
        for thread in followup_threads:
            thread.join(3.0)
        OlivOSAIChatAssassin.activity.clear_all()

    assert current_thread.is_alive() is False
    assert all(thread.is_alive() is False for thread in followup_threads)
    assert thread_errors == []
    assert replies == ([] if expect_cancel else ['收到'])
    assert bool(ai_calls) is (not expect_cancel)
    messages = [message for _level, message in quiet_logger]
    out_of_window = any(
        'RECALL - CANCEL [reason=out_of_window' in message
        for message in messages
    )
    assert out_of_window is expect_cancel
    if expect_cancel:
        assert not any(
            entry.get('message') == current_message
            for entry in history
        )


# 预检索会跳过已被挤出窗口的消息。
def test_group_message_pre_recall_skips_already_evicted_message(
    monkeypatch,
    quiet_logger,
):
    plugin_event, group_id, replies, history, key, guard, signature = (
        install_history_window_case(
            monkeypatch,
            keep=4,
            max_grow=18,
        )
    )
    tokens = [
        guard.register(key, True, signature)
        for _ in range(8)
    ]
    sampled = []
    monkeypatch.setattr(
        assassin_msg,
        'should_reply',
        lambda *args, **kwargs: sampled.append(True) or True,
    )
    monkeypatch.setattr(
        assassin_msg,
        '_recall_knowledge',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('retrieval must not start for an evicted message')
        ),
    )
    OlivOSAIChatAssassin.activity.clear_all()
    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
    finally:
        for token in tokens:
            guard.discard(token)
        OlivOSAIChatAssassin.activity.clear_all()

    assert sampled == []
    assert replies == []
    assert any(
        'RECALL - CANCEL [reason=out_of_window' in message
        for _level, message in quiet_logger
    )
    assert any(
        entry.get('message') == plugin_event.data.message
        for entry in history
    )


# 未确认到达的消息不会触发检索取消。
def test_unconfirmed_arrivals_do_not_trigger_retrieval_cancellation(
    monkeypatch,
    quiet_logger,
):
    plugin_event, group_id, replies, _history, _key, guard, _signature = (
        install_history_window_case(
            monkeypatch,
            keep=4,
            max_grow=18,
            config_overrides={'ignore_prefixes': ['/skip']},
        )
    )
    ignored_event = window_followup_event(
        plugin_event,
        group_id,
        '/skip 后续消息',
        1,
    )
    unknown_event = window_followup_event(
        plugin_event,
        group_id,
        '[OP:image,file=a.png,url=https://example.test/a.png]',
        2,
    )
    tokens = [
        assassin_msg._register_history_arrival(ignored_event, 'test-bot', group_id),
        assassin_msg._register_history_arrival(unknown_event, 'test-bot', group_id),
    ]
    ai_calls = []

    def recall(bot_hash, history_snapshot, cancel_check=None):
        if cancel_check is not None:
            cancel_check('test_retrieval')
        return {}, 0

    def call_ai(*args, **kwargs):
        ai_calls.append(kwargs)
        return '{"r": ["收到"]}'

    monkeypatch.setattr(assassin_msg, '_recall_knowledge', recall)
    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    OlivOSAIChatAssassin.activity.clear_all()
    try:
        assert [token.guaranteed for token in tokens] == [False, False]
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
    finally:
        for token in tokens:
            guard.discard(token)
        OlivOSAIChatAssassin.activity.clear_all()

    assert replies == ['收到']
    assert ai_calls
    assert not any(
        'RECALL - CANCEL [reason=out_of_window' in message
        for _level, message in quiet_logger
    )


# 其它群的消息不会取消本群检索。
def test_other_group_arrivals_do_not_cancel_local_retrieval(
    monkeypatch,
    quiet_logger,
):
    plugin_event, group_id, replies, _history, _key, guard, signature = (
        install_history_window_case(
            monkeypatch,
            keep=4,
            max_grow=18,
        )
    )
    other_key = assassin_msg._history_key('other-bot', group_id)
    tokens = [
        guard.register(other_key, True, signature)
        for _ in range(8)
    ]
    ai_calls = []

    def recall(bot_hash, history_snapshot, cancel_check=None):
        if cancel_check is not None:
            cancel_check('test_retrieval')
        return {}, 0

    def call_ai(*args, **kwargs):
        ai_calls.append(kwargs)
        return '{"r": ["收到"]}'

    monkeypatch.setattr(assassin_msg, '_recall_knowledge', recall)
    monkeypatch.setattr(webTools, 'call_ai', call_ai)
    OlivOSAIChatAssassin.activity.clear_all()
    try:
        OlivOSAIChatAssassin.main.Event.group_message(plugin_event, None)
    finally:
        for token in tokens:
            guard.discard(token)
        OlivOSAIChatAssassin.activity.clear_all()

    assert replies == ['收到']
    assert ai_calls
    assert not any(
        'RECALL - CANCEL [reason=out_of_window' in message
        for _level, message in quiet_logger
    )


# 准入配置变化时禁用历史窗口取消。
def test_history_window_cancel_is_disabled_when_admission_config_changes(monkeypatch):
    plugin_event, group_id, _replies, history, key, guard, signature = (
        install_history_window_case(
            monkeypatch,
            keep=4,
            max_grow=18,
        )
    )
    history_entry = assassin_msg.add_message_to_history(
        group_id,
        plugin_event.data.message,
        plugin_event.data.user_id,
        '测试用户',
        bot_hash='test-bot',
        role='user',
    )
    tokens = [
        guard.register(key, True, signature)
        for _ in range(8)
    ]
    try:
        state = assassin_msg._history_window_state(
            'test-bot',
            group_id,
            history_entry,
            signature,
        )
        assert state is not None
        assert state['retained'] is False

        OlivOSAIChatAssassin.data.gData.getConfig('test-bot')['ignore_prefixes'] = [
            '/changed'
        ]
        assert assassin_msg._history_window_state(
            'test-bot',
            group_id,
            history_entry,
            signature,
        ) is None
        assert len(history) == 14
    finally:
        for token in tokens:
            guard.discard(token)


# 回忆在节奏控制前执行，取消后跳过 AI。
def test_recall_runs_before_pacing_and_cancel_skips_ai(monkeypatch):
    plugin_event, group_id, replies = prepare_group_message(monkeypatch)
    OlivOSAIChatAssassin.data.gData.getConfig('test-bot')['history_size_min'] = 1
    order = []

    class FakeGroupLock:
        def slack(self):
            order.append('pacing')
            return False

        def getRemaining(self):
            return 1.0

    monkeypatch.setattr(assassin_msg, '_get_group_lock', lambda bot_hash, group_id: FakeGroupLock())

    def recall(*args, **kwargs):
        order.append('recall')
        return {}, 0

    monkeypatch.setattr(
        assassin_msg,
        '_recall_knowledge',
        recall,
    )
    monkeypatch.setattr(
        webTools,
        'call_ai',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('AI must not be called')),
    )

    assassin_msg.reply_to_group(plugin_event, group_id, '下一条消息')

    assert replies == []
    assert order == ['recall', 'pacing']


# 记忆提示词标记卡片数据不可信，并在关闭时隐藏知识。
def test_memory_prompt_marks_card_data_as_untrusted_and_hides_k_when_disabled():
    enabled_prompt = assassin_msg._build_memory_prompt(record_knowledge=True)
    disabled_prompt = assassin_msg._build_memory_prompt(record_knowledge=False)

    assert enabled_prompt.startswith('# 输入边界\n' + assassin_msg.INPUT_BOUNDARY_RULES)
    assert assassin_msg.INPUT_BOUNDARY_RULES in enabled_prompt
    assert '卡片内容同样来自用户输入' in enabled_prompt
    assert '\n## 分析知识点' not in disabled_prompt
    assert '"k"' not in disabled_prompt


# 知识缓存命中时会过滤匹配列表。
def test_peak_up_cache_filters_matched_list_on_hit(monkeypatch):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gPeakUpCache', {})
    first = assassin_tools.peak_up_recommendMatch(
        'hello world',
        {'hello': '一', 'hell': '二'},
        '知识缓存',
        900,
        rate=1.0,
        bot_hash='bot-a',
    )
    second = assassin_tools.peak_up_recommendMatch(
        'hello world',
        {'hello': '一', 'hell': '二'},
        '知识缓存',
        900,
        rate=1.0,
        matchedList=['hello'],
        bot_hash='bot-a',
    )

    assert set(first) == {'hello', 'hell'}
    assert second == {'hell': '二'}


# 检索取消时不会创建知识缓存。
def test_peak_up_cache_is_not_created_when_retrieval_is_cancelled(monkeypatch):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gPeakUpCache', {})

    class RetrievalCancelled(Exception):
        pass

    def cancel_check(phase):
        if phase == 'knowledge_before_cache_write':
            raise RetrievalCancelled(phase)

    with pytest.raises(RetrievalCancelled):
        assassin_tools.peak_up_recommendMatch(
            'hello',
            {'hello': '一', 'hell': '二'},
            '知识缓存',
            900,
            rate=0.1,
            bot_hash='bot-a',
            cancel_check=cancel_check,
        )

    assert OlivOSAIChatAssassin.data.gPeakUpCache == {}


# 知识缓存按 bot 与速率隔离。
def test_peak_up_cache_separates_bots_and_rates(monkeypatch):
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gPeakUpCache', {})
    assassin_tools.peak_up_recommendMatch(
        'hello', {'hello': '一'}, '知识缓存', 900, rate=0.1, bot_hash='bot-a'
    )
    assassin_tools.peak_up_recommendMatch(
        'hello', {'hello': '一'}, '知识缓存', 900, rate=0.2, bot_hash='bot-a'
    )
    assassin_tools.peak_up_recommendMatch(
        'hello', {'hello': '一'}, '知识缓存', 900, rate=0.1, bot_hash='bot-b'
    )

    assert set(OlivOSAIChatAssassin.data.gPeakUpCache) == {'bot-a', 'bot-b'}
    assert len(OlivOSAIChatAssassin.data.gPeakUpCache['bot-a']['知识缓存']) == 2


# 知识检索默认排除 assistant 消息。
def test_knowledge_lookup_excludes_assistant_messages_by_default(monkeypatch):
    config = make_config(knowledge_top_k=0)
    memory = {
        '全局': {
            '知识缓存': {'alpha': '知识缓存内容'},
            '知识搜索': {'beta': '知识搜索内容'},
            '人物关系': {},
            '用户侧写': {},
            '图片缓存': {},
        }
    }
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config, memory))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gStaticKnowledge', {'gamma': '静态知识'})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gPeakUpCache', {})
    history = [
        {'role': 'assistant', 'message': 'alpha beta gamma'},
        {'role': 'user', 'message': '完全无关'},
    ]

    result, _candidate_count = assassin_msg._recall_knowledge('bot-a', history)

    assert result == {}


# 知识检索结果保留来源标签。
def test_knowledge_results_keep_source_labels(monkeypatch):
    config = make_config(knowledge_top_k=0)
    memory = {
        '全局': {
            '知识缓存': {'alpha': '知识缓存内容'},
            '知识搜索': {'beta': '知识搜索内容'},
            '人物关系': {},
            '用户侧写': {},
            '图片缓存': {},
        }
    }
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config, memory))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gStaticKnowledge', {'gamma': '静态知识'})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gPeakUpCache', {})
    history = [{'role': 'user', 'message': 'alpha beta gamma'}]

    result, _candidate_count = assassin_msg._recall_knowledge('bot-a', history)

    assert set(result) == {'知识缓存', '知识库', '知识搜索'}
    assert result['知识缓存']['alpha'] == '知识缓存内容'
    assert result['知识库']['gamma'] == '静态知识'
    assert result['知识搜索']['beta'] == '知识搜索内容'


# 深度知识检索遵守 top_k。
def test_deep_knowledge_search_respects_top_k(monkeypatch):
    config = make_config(knowledge_top_k=1, search_knowledge_deepin=2)
    memory = {
        '全局': {
            '知识缓存': {
                'alpha': 'alpha 指向 beta',
                'bravo': 'bravo 的独立内容',
                'charlie': 'charlie 的独立内容',
            },
            '知识搜索': {},
            '人物关系': {},
            '用户侧写': {},
            '图片缓存': {},
        }
    }
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config, memory))
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gStaticKnowledge', {})
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gPeakUpCache', {})
    history = [{'role': 'user', 'message': 'alpha bravo charlie'}]

    result, _candidate_count = assassin_msg._recall_knowledge('bot-a', history)

    assert len(result['知识缓存']) == 1
    assert next(iter(result['知识缓存'])) == 'alpha'


# 人物关系匹配支持 WebUI 的多种值形态。
def test_person_relation_matching_supports_webui_value_shapes(monkeypatch):
    config = make_config()
    memory = {
        '全局': {
            '人物关系': {
                '1': {'name': '小明', 'aliases': ['明明', '小明同学']},
                '2': ['小红', ['红红']],
                '3': '小刚，别名刚哥',
            },
            '用户侧写': {'1': '喜欢猫'},
        }
    }
    monkeypatch.setattr(OlivOSAIChatAssassin.data, 'gData', FakeData(config, memory))
    history = [
        {'role': 'user', 'user_id': '1', 'message': '明明来了'},
        {'role': 'user', 'user_id': '9', 'message': '小刚，别名刚哥最近怎么样'},
        {'role': 'user', 'user_id': '9', 'message': '红红也来了'},
    ]

    result = assassin_msg._recall_people('bot-a', history)

    assert '1' in result['人物关系']
    assert '2' in result['人物关系']
    assert '3' in result['人物关系']
    assert result['用户侧写'] == {'1': '喜欢猫'}
