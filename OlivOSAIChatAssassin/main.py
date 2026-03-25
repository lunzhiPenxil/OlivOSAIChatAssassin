import OlivOS
import OlivOSAIChatAssassin  # noqa: F401
import json
import random
import time
import requests
from collections import deque
import os
import threading
import re
from datetime import datetime

gProc = None
gPluginName = '群聊刺客'
gConfig = None
gMessageHistory = {}  # group_id -> deque of messages
gConfigDir = './plugin/data/OlivOSAIChatAssassin'
gConfigPath = './plugin/data/OlivOSAIChatAssassin/config.json'

gSkipStr = '【SKIP】'

gGroupLock: 'dict[str, threading.Lock]' = {}

gMemoryDir = './plugin/data/OlivOSAIChatAssassin'
gMemoryPath = './plugin/data/OlivOSAIChatAssassin/memory.json'
gMemory = {
    '全局': {
        '常识': [],
        '知识搜索': {},
        '人物关系': {}
    }
}
gMemoryLock = threading.Lock()
gMemoryDefaultStr = "择机加入对话"

configDefault = {
    'api_key': '',
    'api_base': 'https://api.deepseek.com/v1',
    'model': 'deepseek-chat',
    'max_tokens': 512,
    'temperature': 0.7,
    'personality': '你是一个伪装成人类的自豪的新锐AI，你择机加入聊天，避免过多尬聊。别让他们发现你的真实身份！！！',
    'enabled_groups': [
        'all'
    ],
    'history_size': 10,
    'reply_probability': 1,
    'reply_keywords': [],
    'mention_reply': True,
    'ignore_prefixes': [],
    'max_message_length': 2000
}


class Event(object):
    def init(plugin_event, Proc):
        # 初始化流程
        load_config()
        load_memory()
        # 初始化消息历史
        global gMessageHistory
        gMessageHistory = {}
        # 如果配置中启用了群组，初始化对应的历史队列
        if gConfig and 'enabled_groups' in gConfig:
            for group_id in gConfig['enabled_groups']:
                gMessageHistory[group_id] = deque(
                    maxlen=gConfig.get('history_size', configDefault['history_size'])
                )

    def init_after(plugin_event, Proc):
        # 初始化后处理流程
        global gProc
        gProc = Proc

    def private_message(plugin_event, Proc):
        # 私聊消息事件入口
        pass  # 本插件仅处理群聊

    def group_message(plugin_event, Proc):
        # 群消息事件入口
        group_id = str(plugin_event.data.group_id)
        gGroupLock.setdefault(group_id, threading.Lock())
        missed = gGroupLock[group_id].locked()
        gGroupLock[group_id].acquire()
        unity_group_message(plugin_event, Proc, missed)
        gGroupLock[group_id].release()

    def poke(plugin_event, Proc):
        # 戳一戳事件入口
        pass

    def save(plugin_event, Proc):
        # 插件卸载时执行的保存流程
        pass

    def menu(plugin_event, Proc):
        # 插件菜单事件监听
        if plugin_event.data.namespace == 'OlivOSAIChatAssassin':
            if plugin_event.data.event == 'OlivOSAIChatAssassin_Menu_Config':
                log('配置：请编辑插件数据目录下的config.json文件，并重启插件。')
            elif plugin_event.data.event == 'OlivOSAIChatAssassin_Menu_Status':
                status = get_status()
                log(status)


def load_config():
    global gConfig
    try:
        os.makedirs(gConfigDir, exist_ok=True)
        if os.path.exists(gConfigPath):
            with open(gConfigPath, 'r', encoding='utf-8') as f:
                gConfig = json.load(f)
                # 设置默认值
                defaults = configDefault
                for key, value in defaults.items():
                    if key not in gConfig:
                        gConfig[key] = value
        else:
            # 如果配置文件不存在，使用示例配置但不启用任何群组
            gConfig = configDefault
            # 创建示例配置文件
            with open(gConfigPath, 'w', encoding='utf-8') as f:
                json.dump(gConfig, f, ensure_ascii=False, indent=4)
    except Exception as e:
        log(f'加载配置失败: {e}')
        gConfig = None


def load_memory():
    global gMemory
    try:
        os.makedirs(gMemoryDir, exist_ok=True)
        if os.path.exists(gMemoryPath):
            with open(gMemoryPath, 'r', encoding='utf-8') as f:
                gMemory = json.load(f)
        else:
            gMemory = {}
            write_memory()
    except Exception as e:
        log(f'加载记忆失败: {e}')
        gMemory = None


def write_memory():
    gMemoryLock.acquire()
    try:
        os.makedirs(gMemoryDir, exist_ok=True)
        with open(gMemoryPath, 'w', encoding='utf-8') as f:
            json.dump(gMemory, f, ensure_ascii=False, indent=4)
    except Exception as e:
        log(f'写入记忆失败: {e}')
    gMemoryLock.release()


def unity_group_message(plugin_event, Proc, missed: bool = False):
    group_id = str(plugin_event.data.group_id)
    load_config()
    load_memory()
    if not gConfig:
        return
    # 检查是否在启用群组列表中
    if (
        'enabled_groups' in gConfig
        and (
            group_id not in gConfig['enabled_groups']
            and 'all' not in gConfig['enabled_groups']
        )
    ):
        return
    # 忽略前缀消息
    message = plugin_event.data.message
    message = msg_wash(message)
    if should_ignore(message):
        log('IGNORE')
        return
    # 添加消息到历史
    if group_id not in gMessageHistory:
        gMessageHistory[group_id] = deque(
            maxlen=gConfig.get('history_size', configDefault['history_size'])
        )
    add_message_to_history(
        group_id, message, plugin_event.data.user_id, plugin_event.data.sender.get('nickname', '用户')
    )
    # 决定是否回复
    if missed:
        log('MISSED')
    elif not should_reply(group_id, message, plugin_event):
        log('SHOULD NOT')
    else:
        reply_to_group(plugin_event, group_id)


def should_ignore(message):
    if not gConfig:
        return False
    if len(message) <= 0:
        return True
    ignore_prefixes = gConfig.get('ignore_prefixes', [])
    for prefix in ignore_prefixes:
        if message.startswith(prefix):
            return True
    return False


def add_message_to_history(group_id, message, user_id, nickname):
    if group_id not in gMessageHistory:
        return
    timestamp = time.time()
    msg_entry = {
        'timestamp': timestamp,
        'time': datetime.now().astimezone().replace(microsecond=0).isoformat(),
        'user_id': user_id,
        'nickname': nickname,
        'message': message
    }
    gMessageHistory[group_id].append(msg_entry)


def should_reply(group_id, message, plugin_event):
    if not gConfig:
        return False
    # 检查是否被@
    self_id = plugin_event.base_info['self_id']
    mention_str = f'[OP:at,id={self_id}]'
    if gConfig.get('mention_reply', True) and mention_str in message:
        return True
    # 检查是否包含关键词
    keywords = gConfig.get('reply_keywords', [])
    for kw in keywords:
        if kw in message:
            return True
    # 随机概率回复
    prob = gConfig.get('reply_probability', configDefault["reply_probability"])
    if random.random() < prob:
        return True
    return False


def reply_to_group(plugin_event, group_id):
    if not gConfig or not gConfig.get('api_key'):
        return
    # 构建对话历史
    history: 'list[dict]' = list(gMessageHistory.get(group_id, deque()))
    if not history:
        return
    elif len(history) <= 5:
        log('HISTORY TOO SHORT')
        return
    self_id = plugin_event.base_info['self_id']
    mention_str = f'[OP:at,id={self_id}]'
    personality = gConfig.get('personality', '')
    contentDefault = f'''
# 规则
- 你是一个人类，这是你日常水群的群聊，你会根据你的人格设定自行决定要不要参与群聊对话
- "[OP:at,id=【QQ号】]"表示你被@，这是群聊中@的格式，你也可以使用这种格式
- 谨记你是在进行聊天，所以不要把括号之类的内容发出来，不需要你描述自己的动作或者心理活动，这只会让人起疑

# 人格设定
- {personality}

# 已知信息
- 现在的系统时间是：{datetime.now().astimezone().replace(microsecond=0).isoformat()}
- 你的QQ号是：{self_id}，所以你被@时是：{mention_str}
- 本群群号是：{group_id}
'''

    # 生成记忆
    def set_memory():
        history = list(gMessageHistory.get(group_id, deque()))
        # 设置任务
        content = f'''
# 当前记忆
- {gMemory.get(group_id, gMemoryDefaultStr)}

# 当前任务
- 对聊天记录进行总结
- 杜绝流水账，请每次都决定自己需要长期记住什么东西
- 仅输出需要记忆的信息
- 不要遗忘别的群的记忆
- 最终长度限制在100字以内
'''
        # 格式化历史为OpenAI消息格式
        messages = get_ai_context(gConfig, history, content, flagMerge=True)
        # 调用 API
        try:
            gMemory[group_id] = call_ai(gConfig, messages, temperature_override=0.7, json_mode=False)
            write_memory()
            log(f'[本群记忆]\n{gMemory[group_id]}')
        except Exception as e:
            log(f'API FATAL: {e}')

    # 设置任务
    thisMemoryG = {}
    for k, v in gMemory.get('全局', {}).items():
        if k not in (
            '人物关系',
            '知识搜索',
        ):
            thisMemoryG[k] = v
    thisMemoryG['知识搜索'] = {}
    thisMemoryM = gMemory.get('全局', {'知识搜索': {}}).get('知识搜索', {})
    if type(thisMemoryM) is dict:
        for k, v in thisMemoryM.items():
            flagHit = False
            for j in history:
                if k in j.get('message', ''):
                    flagHit = True
            if flagHit:
                log(f'PEAK UP - [知识搜索] {k}')
                thisMemoryG['知识搜索'][k] = v
    thisMemoryG['人物关系'] = {}
    thisMemoryP = gMemory.get('全局', {'人物关系': {}}).get('人物关系', {})
    if type(thisMemoryP) is dict:
        for k, v in thisMemoryP.items():
            flagHit = False
            flagHit_str = None
            for j in history:
                if k == j.get('user_id', None):
                    flagHit = True
                    flagHit_str = k
                    break
                if (
                    type(v) is list
                    and len(v) >= 1
                ):
                    if type(v[0]) is str:
                        if v[0] in j.get('message', '').lower():
                            flagHit = True
                            flagHit_str = v[0]
                            break
                    elif type(v[0]) is list:
                        for n in v[0]:
                            if n in j.get('message', '').lower():
                                flagHit = True
                                flagHit_str = n
                                break
            if flagHit:
                log(f'PEAK UP - [人物关系] {flagHit_str}')
                thisMemoryG['人物关系'][k] = v
    thisMemory = {
        '全局': thisMemoryG,
        group_id: gMemory.get(group_id, gMemoryDefaultStr)
    }
    content = f'''{contentDefault}
# 当前记忆
- {json.dumps(thisMemory, ensure_ascii=False)}

# 当前任务
- 当你不想参与对话时，你会回复"{gSkipStr}"，这是你必须遵守的规则，你不需要每句话都回复，你需要按照你的心情来，但是当有人找你时尽量回复
- 判断是否应该加入聊天进行回复
- 如果应该回复，就直接输出你的回复内容
'''
    # 格式化历史为OpenAI消息格式
    messages = get_ai_context(gConfig, history, content)
    # 调用 API
    reply_text = None
    try:
        reply_text = call_ai(gConfig, messages)
    except Exception as e:
        log(f'API FATAL: {e}')
    # 发送回复
    if reply_text is not None:
        # 限制消息长度
        max_len = gConfig.get('max_message_length', 2000)
        if len(reply_text) > max_len:
            reply_text = reply_text[:max_len] + '...'
        if reply_text != gSkipStr:
            reply_list = reply_split(reply_wash(reply_text))
            log(f'REPLY - {reply_list}')
            for i in reply_list:
                if len(i) > 0:
                    add_message_to_history(group_id, i, None, None)
            t_set_memory = threading.Thread(target=set_memory)
            t_set_memory.start()
            sleep(1 + (random.random() * 2 - 1) * 0.95)
            reply(plugin_event, reply_list)
            t_set_memory.join()
        else:
            log('SKIP')
    else:
        log('NONE')


def get_ai_context(
    lConfig,
    history,
    content,
    flagMerge: bool = False,
    prefix: bool = "总结如下记录："
):
    # 格式化历史为OpenAI消息格式
    messages = []
    # 添加系统提示
    messages.append(
        {
            "role": "system",
            "content": content
        }
    )
    # 添加最近的历史消息，限制数量
    max_history = lConfig.get('history_size', 20)
    if flagMerge:
        chat_content = '\n'.join([
            f'{entry["time"]} [{entry["nickname"]}]({entry["user_id"]}) 说: "{entry["message"]}"'
            if entry['nickname'] is not None
            else f'{entry["time"]} [我]() 说: "{entry["message"]}"'
            for entry in list(history)[-max_history:]
        ])
        messages.append(
            {
                "role": "user",
                "content": f"{prefix}\n" + chat_content
            }
        )
    else:
        for entry in list(history)[-max_history:]:
            if entry['nickname'] is None:
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"{entry['message']}"
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(entry, ensure_ascii=False)
                    }
                )
    return messages


def call_ai(
    lConfig,
    messages,
    temperature_override: 'float|None' = None,
    json_mode: bool = True
):
    # 调用 API
    res = None
    api_key = lConfig['api_key']
    api_base = lConfig['api_base']
    model = lConfig['model']
    max_tokens = lConfig.get('max_tokens', 1024)
    temperature = lConfig.get('temperature', 0.7)
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
        "stream": False
    }
    start = time.perf_counter()
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    end = time.perf_counter()
    log(f"CALL AI - {(end - start):.2f} s")
    if response.status_code == 200:
        result: dict = response.json()
        res = result['choices'][0]['message']['content'].strip()
        res = get_message(res, json_mode=json_mode)
        log_usage(get_usage(result.get('usage', {})))
    else:
        log(f'API ERR: {response.status_code} {response.text}')
    return res


def get_message(data_str: str, json_mode: bool):
    res = data_str
    if not json_mode:
        log('DATA TYPE - STR OUT')
    elif res == gSkipStr:
        log('DATA TYPE - STR SKIP')
    else:
        res = get_json_message(res)
    return res


def get_json_message(data_str: str):
    res = None
    data_str = data_str.replace('\r', '')
    data_list = data_str.split('\n')
    res_list = []
    for i in data_list:
        i_2 = i
        i_2 = i_2.strip()
        if (
            i_2.startswith('{')
            and i_2.endswith('}')
        ):
            try:
                data_dict = json.loads(i_2)
                if (
                    type(data_dict) is dict
                    and 'message' in data_dict
                    and type(data_dict['message']) is str
                ):
                    res_list.append(data_dict['message'])
                    log('DATA TYPE - JSON')
                else:
                    log(f'DATA ERR: {i}')
            except Exception:
                log(f'DATA ERR: {i}')
        else:
            res_list.append(i)
            log('DATA TYPE - STR')
    if len(res_list) > 0:
        res = '\n'.join(res_list)
    return res


def get_usage(usage_data: dict):
    res = usage_data.copy()
    return res


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
            log(
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
            log(
                "USAGE - CACHE - "
                f"{cache_hit:.2f} %"
            )


def get_status():
    status_lines = []
    status_lines.append('状态')
    if gConfig:
        status_lines.append(f'已加载配置: {len(gConfig.get("enabled_groups", []))} 个启用群组')
        status_lines.append(f'API密钥: {"已设置" if gConfig.get("api_key") else "未设置"}')
        status_lines.append(f'历史记录大小: {gConfig.get("history_size", configDefault["history_size"])}')
        status_lines.append(f'回复概率: {gConfig.get("reply_probability", configDefault["reply_probability"])}')
        for group_id, history in gMessageHistory.items():
            status_lines.append(f'群 {group_id}: {len(history)} 条历史消息')
    else:
        status_lines.append('配置未加载')
    return '\n'.join(status_lines)


# 主动发送消息示例实现（参考模板）
def send_message_force(botHash, send_type, target_id, message):
    Proc = gProc
    if (
        Proc is not None
        and botHash in Proc.Proc_data['bot_info_dict']
    ):
        pluginName = gPluginName
        plugin_event = OlivOS.API.Event(
            OlivOS.contentAPI.fake_sdk_event(
                bot_info=Proc.Proc_data['bot_info_dict'][botHash],
                fakename=pluginName
            ),
            Proc.log
        )
        plugin_event.send(send_type, target_id, message)


def log(msg: str):
    if gProc is not None:
        gProc.log(2, msg, [
            (gPluginName, 'default')
        ])


def reply(plugin_event, msg: list):
    for i in msg:
        len_i = len(i)
        if len_i > 0:
            sleep_time = sum([
                0.3 + (random.random() * 2 - 1) * 0.25
                for _ in range(len_i)
            ])
            sleep(sleep_time)
            plugin_event.reply(i)


def reply_wash(msg: str):
    res = msg
    res = res.replace('\r', '')
    res = res.strip('\n')
    res = res.rstrip('。')
    res = re.sub(r'\(.+\)', '', res)
    res = re.sub(r'（.+）', '', res)
    return res


def reply_split(msg: str):
    res = msg
    res = res.split('\n')
    return res


def msg_wash(msg: str):
    res = msg
    res = re.sub(r'\[OP:image.+\]', '', res)
    return res


def sleep(sleep_time: float):
    log(f"WAIT - {sleep_time:.2f} s")
    time.sleep(sleep_time)
