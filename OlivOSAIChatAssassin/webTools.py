import time
import requests
import base64
import os
import random
from pathlib import Path

import OlivOSAIChatAssassin


class AICallFailure(Exception):
    def __init__(
        self,
        message: str,
        retryable: bool = False,
        status_code: 'int|None' = None,
        retry_after: 'float|None' = None,
        finish_reason: 'str|None' = None,
        kind: str = '',
        content: 'str|None' = None,
        reasoning_content: 'str|None' = None
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.finish_reason = finish_reason
        self.kind = kind
        self.content = content
        self.reasoning_content = reasoning_content


def get_retry_delay(attempt: int, retry_after: 'float|None' = None) -> float:
    """计算下一次重试前的等待时间，避免服务端限流时形成热循环。"""
    try:
        attempt = max(1, int(attempt))
    except (TypeError, ValueError):
        attempt = 1
    delay = min(30.0, 1.6 ** min(attempt - 1, 64))
    if retry_after is not None:
        try:
            delay = min(30.0, max(delay, float(retry_after)))
        except (TypeError, ValueError):
            pass
    return max(0.0, min(30.0, delay * random.uniform(0.9, 1.1)))


def _get_request_timeout(lConfig) -> tuple:
    timeout_value = lConfig.get('api_timeout', 120)
    try:
        timeout_value = float(timeout_value)
    except (TypeError, ValueError):
        timeout_value = 120.0
    if timeout_value <= 0:
        timeout_value = 120.0
    return (10.0, timeout_value)


def _get_retry_after(response) -> 'float|None':
    value = response.headers.get('Retry-After')
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _get_request_id(response) -> str:
    for key in ('x-request-id', 'x-ds-request-id', 'request-id'):
        value = response.headers.get(key)
        if value:
            return str(value)
    return ''


def _get_reasoning_length(message: dict) -> int:
    reasoning_content = message.get('reasoning_content')
    if isinstance(reasoning_content, str):
        return len(reasoning_content)
    return 0


def _post_chat_completion(
    lConfig,
    url: str,
    headers: dict,
    payload: dict,
    log_prefix: str,
    flag_reasoning_fallback: bool = False
) -> str:
    OlivOSAIChatAssassin.logger.log(f'{log_prefix} - START')
    start = time.perf_counter()
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=_get_request_timeout(lConfig)
        )
    except requests.RequestException as exc:
        elapsed = time.perf_counter() - start
        OlivOSAIChatAssassin.logger.warn(
            f'{log_prefix} - NETWORK ERR {elapsed:.2f} s - '
            f'{type(exc).__name__}: {exc}'
        )
        raise AICallFailure(
            f'network error: {type(exc).__name__}: {exc}',
            retryable=True,
            kind='network'
        ) from exc

    elapsed = time.perf_counter() - start
    OlivOSAIChatAssassin.logger.log(f'{log_prefix} - DONE {elapsed:.2f} s')
    request_id = _get_request_id(response)

    if response.status_code != 200:
        retry_after = _get_retry_after(response)
        retryable = (
            response.status_code in (408, 409, 429)
            or response.status_code >= 500
        )
        response_text = response.text or ''
        if len(response_text) > 1000:
            response_text = response_text[:1000] + '...'
        OlivOSAIChatAssassin.logger.warn(
            f'API ERR: {response.status_code} request_id={request_id or "-"} '
            f'retryable={retryable} retry_after={retry_after} {response_text}'
        )
        raise AICallFailure(
            f'http {response.status_code}: {response_text}',
            retryable=retryable,
            status_code=response.status_code,
            retry_after=retry_after,
            kind='http'
        )

    try:
        result = response.json()
    except ValueError as exc:
        OlivOSAIChatAssassin.logger.warn(
            f'{log_prefix} - INVALID JSON RESPONSE request_id={request_id or "-"}'
        )
        raise AICallFailure(
            'invalid response JSON',
            retryable=True,
            status_code=response.status_code,
            kind='response_json'
        ) from exc

    choices = result.get('choices') if isinstance(result, dict) else None
    if (
        not isinstance(choices, list)
        or len(choices) < 1
        or not isinstance(choices[0], dict)
        or not isinstance(choices[0].get('message'), dict)
    ):
        OlivOSAIChatAssassin.logger.warn(
            f'{log_prefix} - INVALID RESPONSE SHAPE request_id={request_id or "-"}'
        )
        raise AICallFailure(
            'invalid response shape',
            retryable=True,
            status_code=response.status_code,
            kind='response_shape'
        )

    message = choices[0]['message']
    content = message.get('content', '')
    if content is None:
        content = ''
    elif not isinstance(content, str):
        content = str(content)
    finish_reason = choices[0].get('finish_reason')
    if not isinstance(finish_reason, str):
        finish_reason = None
    reasoning_content = message.get('reasoning_content')
    if not isinstance(reasoning_content, str):
        reasoning_content = ''

    log_reasoning_content(OlivOSAIChatAssassin.tools.get_copy_data(choices))
    usage = result.get('usage')
    log_usage(OlivOSAIChatAssassin.tools.get_copy_data(usage) if isinstance(usage, dict) else {})
    OlivOSAIChatAssassin.logger.log(
        f'{log_prefix} - RESPONSE request_id={request_id or "-"} '
        f'finish={finish_reason or "-"} content_len={len(content)}'
    )

    if finish_reason == 'insufficient_system_resource':
        OlivOSAIChatAssassin.logger.warn(
            f'{log_prefix} - INSUFFICIENT RESOURCE request_id={request_id or "-"} '
            f'content_len={len(content)} reasoning_len={_get_reasoning_length(message)}'
        )
        raise AICallFailure(
            'response interrupted: insufficient system resource',
            retryable=True,
            status_code=response.status_code,
            finish_reason=finish_reason,
            kind='incomplete',
            content=content,
            reasoning_content=reasoning_content
        )

    if finish_reason == 'length':
        OlivOSAIChatAssassin.logger.warn(
            f'{log_prefix} - RESPONSE TRUNCATED request_id={request_id or "-"} '
            f'content_len={len(content)} reasoning_len={_get_reasoning_length(message)}'
        )
        raise AICallFailure(
            'response truncated: finish_reason=length',
            retryable=True,
            status_code=response.status_code,
            finish_reason=finish_reason,
            kind='incomplete',
            content=content,
            reasoning_content=reasoning_content
        )

    if len(content.strip()) <= 0:
        if (
            flag_reasoning_fallback
            and finish_reason == 'stop'
            and reasoning_content.strip()
        ):
            OlivOSAIChatAssassin.logger.warn(
                f'{log_prefix} - EMPTY CONTENT - REASONING DRAFT '
                f'request_id={request_id or "-"} reasoning_len={len(reasoning_content)}'
            )
            return reasoning_content.strip()
        OlivOSAIChatAssassin.logger.warn(
            f'{log_prefix} - EMPTY CONTENT request_id={request_id or "-"} '
            f'finish={finish_reason or "-"} reasoning_len={_get_reasoning_length(message)}'
        )
        raise AICallFailure(
            'empty response content',
            retryable=finish_reason != 'content_filter',
            status_code=response.status_code,
            finish_reason=finish_reason,
            kind='empty_content',
            content=content,
            reasoning_content=reasoning_content
        )

    return content.strip()


def call_ai(
    lConfig,
    messages,
    temperature_override: 'float|None' = None,
    flag_thinking_override: 'bool|None' = None,
    reasoning_effort_override: 'str|None' = None,
    response_format_override: 'dict|None' = None,
    flag_reasoning_fallback: bool = False,
    max_tokens_override: 'int|None' = None,
):
    # 调用 API
    api_key = lConfig['api_key']
    api_base = lConfig['api_base']
    model = lConfig['model']
    max_tokens = lConfig.get('max_tokens', 1024)
    if max_tokens_override is not None:
        max_tokens = max_tokens_override
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
    url = f"{str(api_base).rstrip('/')}/chat/completions"
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
    if response_format_override is not None:
        payload.update({
            'response_format': response_format_override
        })
    return _post_chat_completion(
        lConfig, url, headers, payload, 'CALL AI',
        flag_reasoning_fallback=flag_reasoning_fallback
    )


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


def _usage_int(value):
    return value if type(value) is int else None


def _usage_cache_tokens(usage_data: dict):
    prompt_tokens = _usage_int(usage_data.get('prompt_tokens'))
    cache_hit = _usage_int(usage_data.get('prompt_cache_hit_tokens'))
    cache_miss = _usage_int(usage_data.get('prompt_cache_miss_tokens'))
    if cache_hit is None:
        details = usage_data.get('prompt_tokens_details')
        if isinstance(details, dict):
            cache_hit = _usage_int(details.get('cached_tokens'))
    if cache_hit is None:
        cache_hit = _usage_int(usage_data.get('cached_tokens'))
    if cache_hit is None:
        cache_hit = _usage_int(usage_data.get('cache_read_input_tokens'))
    if cache_hit is None:
        return None
    if cache_miss is None:
        cache_miss = max(0, prompt_tokens - cache_hit) if prompt_tokens is not None else 0
    return cache_hit, cache_miss


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
        cache_tokens = _usage_cache_tokens(usage_data)
        if cache_tokens is not None:
            cache_hit_tokens, cache_miss_tokens = cache_tokens
            cache_total = cache_hit_tokens + cache_miss_tokens
            if cache_total <= 0:
                return
            cache_hit = (
                (cache_hit_tokens / cache_total)
                * 100
            )
            OlivOSAIChatAssassin.logger.log(
                "USAGE - CACHE - "
                f"{cache_hit:.2f} % (hit={cache_hit_tokens}, miss={cache_miss_tokens})"
            )


def call_ai_ocr(
    lConfig,
    prompt: str,
    image_url: str
):
    # 调用 API
    api_key = lConfig['ocr_api']['api_key']
    api_base = lConfig['ocr_api']['api_base']
    model = lConfig['ocr_api']['model']
    max_tokens = 2048
    url = f"{str(api_base).rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": prompt
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url
                        }
                    }
                ]
            }
        ],
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": {
            "type": "json_object"
        },
        "thinking": {
            "type": "disabled"
        }
    }
    return _post_chat_completion(lConfig, url, headers, payload, 'CALL AI OCR')


def download_image_to_base64(url: str, save_dir: str, filename: str) -> str | None:
    """
    下载图片并转换为 Base64 编码（带 Data URL 前缀）。
    内部使用拆分函数：先下载到本地临时目录，再读取并转换。

    Args:
        url: 图片的网络地址
        delete_temp: 是否在转换后删除临时文件，默认为 True

    Returns:
        成功返回 Data URL 格式的 Base64 字符串，失败返回 None
    """
    try:
        # 1. 下载到临时目录
        local_path = download_image_to_local(url=url, save_dir=save_dir, filename=filename)
        if not local_path:
            return None

        # 2. 将本地图片转为 Base64（包含 data:image 前缀）
        base64_data = image_to_base64(local_path, include_data_url=True)
        return base64_data

    except requests.RequestException as e:
        OlivOSAIChatAssassin.logger.warn(f"OCR IMAGE DOWNLOAD TO BASE64 FAILED: {e}")
        return None


def download_image_to_local(url: str, save_dir: str, filename: str) -> str | None:
    """
    下载图片到本地指定目录。

    Args:
        url: 图片的网络地址
        save_dir: 保存目录路径（如果不存在则自动创建）

    Returns:
        成功时返回保存的绝对路径，失败返回 None
    """
    try:
        # 创建目录（如果不存在）
        Path(save_dir).mkdir(parents=True, exist_ok=True)

        # 发送请求
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()

        # 检查 Content-Type 是否为图片（可选）
        content_type = resp.headers.get('Content-Type', '')
        if not content_type.startswith('image/'):
            return None

        # 保证文件名安全（移除非法字符）
        file_path = os.path.join(save_dir, filename)

        # 写入文件
        with open(file_path, 'wb') as f:
            f.write(resp.content)

        return os.path.abspath(file_path)

    except requests.RequestException as e:
        OlivOSAIChatAssassin.logger.warn(f"OCR IMAGE DOWNLOAD FAILED: {e}")
        return None
    except Exception as e:
        OlivOSAIChatAssassin.logger.warn(f"OCR IMAGE DOWNLOAD FAILED - SAVE ERR: {e}")
        return None


def image_to_base64(image_path: str, include_data_url: bool = True) -> str | None:
    """
    将本地图片文件转换为 Base64 字符串。

    Args:
        image_path: 本地图片文件路径
        include_data_url: 是否包含 Data URL 前缀（如 "data:image/png;base64,"），
                          默认 True，若为 False 则只返回纯 Base64 内容。

    Returns:
        成功返回 Base64 字符串，失败返回 None
    """
    try:
        if not os.path.isfile(image_path):
            OlivOSAIChatAssassin.logger.warn(f"OCR IMAGE TO BASE64 FAILED - NO FILE: {image_path}")
            return None

        # 读取二进制数据
        with open(image_path, 'rb') as f:
            img_data = f.read()

        # Base64 编码
        base64_str = base64.b64encode(img_data).decode('utf-8')

        if include_data_url:
            # 尝试获取 MIME 类型（根据文件扩展名）
            ext = os.path.splitext(image_path)[1].lower()
            mime_map = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif',
                '.webp': 'image/webp', '.bmp': 'image/bmp'
            }
            mime = mime_map.get(ext, 'application/octet-stream')
            return f"data:{mime};base64,{base64_str}"
        else:
            return base64_str

    except Exception as e:
        OlivOSAIChatAssassin.logger.warn(f"OCR IMAGE TO BASE64 FAILED: {e}")
        return None
