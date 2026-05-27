import time
import requests
import base64
import os
from pathlib import Path

import OlivOSAIChatAssassin


def call_ai(
    lConfig,
    messages,
    temperature_override: 'float|None' = None,
    flag_thinking_override: 'bool|None' = None,
    reasoning_effort_override: 'str|None' = None,
    response_format_override: 'dict|None' = None
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
    if response_format_override is not None:
        payload.update({
            'response_format': response_format_override
        })
    OlivOSAIChatAssassin.logger.log("CALL AI - START")
    start = time.perf_counter()
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    end = time.perf_counter()
    OlivOSAIChatAssassin.logger.log(f"CALL AI - DONE {(end - start):.2f} s")
    if response.status_code == 200:
        result: dict = response.json()
        res = result['choices'][0]['message']['content'].strip()
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


def call_ai_ocr(
    lConfig,
    prompt: str,
    image_url: str
):
    # 调用 API
    res = None
    api_key = lConfig['ocr_api']['api_key']
    api_base = lConfig['ocr_api']['api_base']
    model = lConfig['ocr_api']['model']
    max_tokens = 2048
    url = f"{api_base}/chat/completions"
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
    OlivOSAIChatAssassin.logger.log("CALL AI OCR - START")
    start = time.perf_counter()
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    end = time.perf_counter()
    OlivOSAIChatAssassin.logger.log(f"CALL AI OCR - DONE {(end - start):.2f} s")
    if response.status_code == 200:
        result: dict = response.json()
        res = result['choices'][0]['message']['content'].strip()
        log_reasoning_content(OlivOSAIChatAssassin.tools.get_copy_data(result.get('choices', {})))
        log_usage(OlivOSAIChatAssassin.tools.get_copy_data(result.get('usage', {})))
    else:
        OlivOSAIChatAssassin.logger.warn(f'API ERR: {response.status_code} {response.text}')
    return res


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
