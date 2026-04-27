import time
import requests

import OlivOSAIChatAssassin


def call_ai(
    lConfig,
    messages,
    temperature_override: 'float|None' = None,
    json_mode: bool = True,
    flag_thinking_override: 'bool|None' = None,
    reasoning_effort_override: 'str|None' = None
):
    # 调用 API
    res = None
    api_key = lConfig['api_key']
    api_base = lConfig['api_base']
    model = lConfig['model']
    max_tokens = lConfig.get('max_tokens', 1024)
    temperature = lConfig.get('temperature', 0.7)
    thinking = lConfig.get('thinking', {'type': 'disabled'})
    if not (
        type(thinking) is dict
        and type(thinking.get('type', None)) is str
    ):
        thinking = {'type': 'disabled'}
    if flag_thinking_override is not None:
        thinking = {'type': 'enabled' if flag_thinking_override else 'disabled'}
    reasoning_effort = lConfig.get('reasoning_effort', 'max')
    if reasoning_effort_override is not None:
        reasoning_effort = reasoning_effort_override
    url = f"{api_base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature if temperature_override is None else temperature_override,
        "thinking": thinking,
        "stream": False
    }
    if thinking.get('type', 'disabled') == 'enabled':
        payload.update({
            'reasoning_effort': reasoning_effort
        })
    OlivOSAIChatAssassin.logger.log("CALL AI - START")
    start = time.perf_counter()
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    end = time.perf_counter()
    OlivOSAIChatAssassin.logger.log(f"CALL AI - DONE {(end - start):.2f} s")
    if response.status_code == 200:
        result: dict = response.json()
        res = result['choices'][0]['message']['content'].strip()
        res = OlivOSAIChatAssassin.msg.get_message(res, json_mode=json_mode)
        log_reasoning_content(OlivOSAIChatAssassin.tools.get_copy_data(result.get('choices', {})))
        log_usage(OlivOSAIChatAssassin.tools.get_copy_data(result.get('usage', {})))
    else:
        OlivOSAIChatAssassin.logger.warn(f'API ERR: {response.status_code} {response.text}')
    return res


def log_reasoning_content(choices: list):
    if type(choices) is list:
        if (
            len(choices) >= 1
            and type(choices[0]) is dict
            and 'message' in choices[0]
            and type(choices[0]['message']) is dict
            and 'reasoning_content' in choices[0]['message']
            and type(choices[0]['message']['reasoning_content']) is str
        ):
            OlivOSAIChatAssassin.logger.log(
                "MSG - REASON - "
                f"{choices[0]['message']['reasoning_content']}"
            )


def log_usage(usage_data: dict):
    if type(usage_data) is dict:
        if (
            'prompt_tokens' in usage_data
            and type(usage_data['prompt_tokens']) is int
            and 'completion_tokens' in usage_data
            and type(usage_data['completion_tokens']) is int
            and 'total_tokens' in usage_data
            and type(usage_data['total_tokens']) is int
        ):
            OlivOSAIChatAssassin.logger.log(
                "USAGE - TOKEN - "
                f"{usage_data['total_tokens']} ({usage_data['prompt_tokens']}/{usage_data['completion_tokens']})"
            )
        if (
            'prompt_cache_hit_tokens' in usage_data
            and type(usage_data['prompt_cache_hit_tokens']) is int
            and 'prompt_cache_miss_tokens' in usage_data
            and type(usage_data['prompt_cache_miss_tokens']) is int
        ):
            cache_hit = (
                (
                    usage_data['prompt_cache_hit_tokens']
                    / (
                        usage_data['prompt_cache_hit_tokens'] + usage_data['prompt_cache_miss_tokens']
                    )
                )
                * 100
            )
            OlivOSAIChatAssassin.logger.log(
                "USAGE - CACHE - "
                f"{cache_hit:.2f} %"
            )
