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

gGroupLock: 'dict[str, FairLock]' = {}
gGroupKnowledgeCounter: 'dict[str, int]' = {}
gGroupKnowledgeCounterLimit: int = int(4 * 4)

gMemoryDir = './plugin/data/OlivOSAIChatAssassin'
gMemoryPath = './plugin/data/OlivOSAIChatAssassin/memory.json'
gMemory = {
    '全局': {
        '常识': [],
        '知识搜索': {},
        '知识缓存': {},
        '人物关系': {}
    }
}
gMemoryLock = threading.Lock()
gMemoryDefaultStr = "择机加入对话"

gStaticKnowledgeDir = './plugin/data/OlivOSAIChatAssassin/Knowledge'
gStaticKnowledge = {}

configDefault = {
    'api_key': '',
    'api_base': 'https://api.deepseek.com/v1',
    'model': 'deepseek-chat',
    'max_tokens': 512,
    'temperature': 0.7,
    'personality': '你是一个伪装成人类的自豪的新锐AI，你择机加入聊天，避免过多尬聊。别让他们发现你的真实身份！！！',
    'record_knowledge': True,
    'enabled_groups': [
        'all'
    ],
    'history_size': 12,
    'reply_probability': 1,
    'reply_keywords': [],
    'mention_reply': True,
    'ignore_prefixes': [],
    'max_message_length': 2000
}


class Event:
    def init(plugin_event, Proc):
        # 初始化流程
        pass

    def init_after(plugin_event, Proc):
        # 初始化后处理流程
        global gProc
        global gMessageHistory
        gProc = Proc
        load_config()
        load_staticKnowledge()
        load_memory()
        # 初始化消息历史
        gMessageHistory = {}
        # 如果配置中启用了群组，初始化对应的历史队列
        if gConfig and 'enabled_groups' in gConfig:
            for group_id in gConfig['enabled_groups']:
                gMessageHistory[group_id] = deque(
                    maxlen=gConfig.get('history_size', configDefault['history_size'])
                )

    def private_message(plugin_event, Proc):
        # 私聊消息事件入口
        pass  # 本插件仅处理群聊

    def group_message(plugin_event, Proc):
        # 群消息事件入口
        group_id = str(plugin_event.data.group_id)
        gGroupLock.setdefault(group_id, FairLock())
        missed = gGroupLock[group_id].locked()
        with gGroupLock[group_id]:
            if (
                gGroupLock[group_id].isBusy()
                and gGroupLock[group_id].isLast()
            ):
                missed = False
            unity_group_message(plugin_event, Proc, missed)

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


# 公平锁
class FairLock:
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._next_ticket = 0
        self._serving = 0
        self._held = False  # 是否被持有
        self._busy_gate = 3
        self._busy = False
        self._count = 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def acquire(self):
        with self._lock:
            my_ticket = self._next_ticket
            self._next_ticket += 1
            self._count += 1
            self._try_refresh()
            while my_ticket != self._serving:
                self._cond.wait()
            self._held = True

    def release(self):
        with self._lock:
            if not self._held:
                raise RuntimeError("release unlocked lock")
            self._held = False
            self._serving += 1
            self._count -= 1
            self._try_reset()
            self._cond.notify_all()

    def _try_reset(self):
        if 0 == self._count:
            self._next_ticket = 0
            self._serving = 0
            self._busy = False

    def locked(self):
        with self._lock:
            return self._held

    def isLast(self):
        with self._lock:
            return self._count == 1

    def _try_refresh(self):
        if self._count >= self._busy_gate:
            self._busy = True

    def setBusyGate(self, gate: int):
        with self._lock:
            self._busy_gate = gate

    def isBusy(self, gate: int = 3):
        with self._lock:
            return self._busy


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
        warn(f'加载配置失败: {e}')
        gConfig = None


def load_staticKnowledge():
    global gStaticKnowledge
    gStaticKnowledge = {}
    try:
        os.makedirs(gStaticKnowledgeDir, exist_ok=True)
        for i in os.listdir(gStaticKnowledgeDir):
            f_name = f'{gStaticKnowledgeDir}/{i}'
            try:
                with open(f_name, 'r', encoding='utf-8') as f:
                    f_obj = json.loads(f.read())
                    if type(f_obj) is not dict:
                        warn(f'加载知识库[{i}]失败: 类型错误[{type(f_obj)}]')
                    else:
                        gStaticKnowledge.update(**f_obj)
                        log(f'已加载知识库[{i}]')
            except Exception as e:
                warn(f'加载知识库[{i}]失败: {e}')
        log(f'已加载知识库共[{len(gStaticKnowledge)}]条')
    except Exception as e:
        warn(f'加载知识库完全失败: {e}')


def load_memory():
    global gMemory
    with gMemoryLock:
        try:
            os.makedirs(gMemoryDir, exist_ok=True)
            if os.path.exists(gMemoryPath):
                with open(gMemoryPath, 'r', encoding='utf-8') as f:
                    gMemory = json.load(f)
            else:
                gMemory = {}
                write_memory()
        except Exception as e:
            warn(f'加载记忆失败: {e}')
            gMemory = None


def write_memory():
    with gMemoryLock:
        try:
            os.makedirs(gMemoryDir, exist_ok=True)
            with open(gMemoryPath, 'w', encoding='utf-8') as f:
                json.dump(gMemory, f, ensure_ascii=False, indent=4)
        except Exception as e:
            warn(f'写入记忆失败: {e}')


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
    message_id = plugin_event.data.message_id
    if -1 == message_id:
        message_id = None
    add_message_to_history(
        group_id, message, plugin_event.data.user_id, plugin_event.data.sender.get('nickname', '用户'),
        message_id=message_id
    )
    # 决定是否回复
    if missed:
        log(f'MISSED - {message}')
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


def add_message_to_history(group_id, message, user_id, nickname, message_id: 'str|None' = None):
    if group_id not in gMessageHistory:
        return
    timestamp = time.time()
    message_new = message
    if len(message_new) > 100:
        message_new = message_new[:100] + '...'
    msg_entry = {
        'timestamp': timestamp,
        'time': datetime.now().astimezone().replace(microsecond=0).isoformat(),
        'user_id': user_id,
        'nickname': nickname,
        'message': message_new
    }
    if message_id is not None:
        msg_entry['message_id'] = str(message_id)
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
    record_knowledge = gConfig.get('record_knowledge', True)
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
- 最终长度限制在128字以内
'''
        # 格式化历史为OpenAI消息格式
        messages = get_ai_context(gConfig, history, content, flagMerge=True)
        # 调用 API
        try:
            group_memory = call_ai(gConfig, messages, temperature_override=0.7, json_mode=False)
            with gMemoryLock:
                gMemory[group_id] = group_memory
            write_memory()
            log(f'[本群记忆]\n{gMemory[group_id]}')
        except Exception as e:
            warn(f'API FATAL: {e}')

    # 生成长期记忆
    def set_knowledge(t_thisMemory: dict):
        history = list(gMessageHistory.get(group_id, deque()))
        # 设置任务
        examples_knowledge = {
            "中国": "五千年文明古国，幅员辽阔，正全面推进民族复兴，坚持和平发展。"
        }
        content = f'''
# 当前任务
- 分析当前聊天记录，提炼需要记住的知识点，注意不是对于现状的记录，只记录常识性的知识
- 每条知识点长度限制在32字以内
- 每条知识带有一个介于2至8字之间的关键词，被用于作为子字符串进行搜索
- 知识点以Json对象的格式输出，知识点的关键词为键，内容为值

# 参考输出
{json.dumps(examples_knowledge, ensure_ascii=False)}
'''
        # 格式化历史为OpenAI消息格式
        messages = get_ai_context(
            gConfig, history, content, flagMerge=True,
            prefix=f'前情提要：{gMemory.get(group_id, gMemoryDefaultStr)}\n\n现在提炼如下对话中的重要知识点：'
        )
        # 调用 API
        try:
            knowledge_data_str = call_ai(gConfig, messages, temperature_override=0.7, json_mode=False)
            knowledge_data_str = knowledge_data_str.lstrip("```json")
            knowledge_data_str = knowledge_data_str.lstrip("```")
            knowledge_data_str = knowledge_data_str.rstrip("```")
            knowledge_data_str = knowledge_data_str.replace("\r", "")
            knowledge_data = {}
            flag_knowledge_err = False
            flag_knowledge_update = False
            try:
                knowledge_data = json.loads(knowledge_data_str)
            except Exception as e:
                warn(f'API JSON DATA FATAL: {e}\n{knowledge_data_str}')
                knowledge_data = {}
                flag_knowledge_err = True
            if type(knowledge_data) is not dict:
                warn(f'API DATA TYPE FATAL: \n{knowledge_data_str}')
                knowledge_data = {}
                flag_knowledge_err = True
            if flag_knowledge_err:
                for knowledge_data_str_i in knowledge_data_str.split('\n'):
                    try:
                        knowledge_data_i = json.loads(knowledge_data_str_i)
                        if type(knowledge_data_i) is dict:
                            knowledge_data.update(**knowledge_data_i)
                    except Exception:
                        pass
            with gMemoryLock:
                if '全局' not in gMemory:
                    gMemory['全局'] = {}
                if '知识缓存' not in gMemory['全局']:
                    gMemory['全局']['知识缓存'] = {}
                for k, v in knowledge_data.items():
                    flag_knowledge_update = True
                    if (
                        type(k) is str
                        and type(v) is str
                    ):
                        gMemory['全局']['知识缓存'][k] = v
                        log(f'[更新知识] - {k}\n{v}')
            if flag_knowledge_update:
                write_memory()
        except Exception as e:
            warn(f'API FATAL: {e}')

    # 设置任务
    thisMemoryG = {}
    with gMemoryLock:
        for k, v in gMemory.get('全局', {}).items():
            if k not in (
                '人物关系',
                '知识缓存',
                '知识搜索',
            ):
                thisMemoryG[k] = v
    key_gMemory_const = '知识搜索'
    key_staticKnowledge = '知识库'
    thisMemoryG[key_gMemory_const] = {}
    for key_gMemory in (
        '知识缓存',
        '知识库',
        '知识搜索',
    ):
        thisMemoryM = gMemory.get('全局', {key_gMemory: {}}).get(key_gMemory, {})
        if key_gMemory == key_staticKnowledge:
            thisMemoryM = gStaticKnowledge
        if type(thisMemoryM) is dict:
            for k, v in thisMemoryM.items():
                flagHit = False
                rank = None
                for j in history:
                    if key_gMemory == key_staticKnowledge:
                        rank = get_recommendRank(k, j.get('message', ''), rate=0.15)
                    else:
                        rank = get_recommendRank(k, j.get('message', ''))
                    if get_recommendMatch(rank):
                        flagHit = True
                        break
                if flagHit:
                    log(f'PEAK UP - [{key_gMemory}] {k} ({rank})')
                    thisMemoryG[key_gMemory_const][k] = v

    key_gMemory_const = '人物关系'
    thisMemoryG[key_gMemory_const] = {}
    for key_gMemory in (
        '人物关系',
    ):
        thisMemoryP = gMemory.get('全局', {key_gMemory: {}}).get(key_gMemory, {})
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
                    log(f'PEAK UP - [{key_gMemory}] {flagHit_str}')
                    thisMemoryG[key_gMemory_const][k] = v
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
        warn(f'API FATAL: {e}')
    # 发送回复
    if reply_text is None:
        get_gGroupKnowledgeCounter(str(group_id), False)
        log('NONE')
    else:
        # 限制消息长度
        max_len = gConfig.get('max_message_length', 2000)
        if len(reply_text) > max_len:
            reply_text = reply_text[:max_len] + '...'
        if reply_text == gSkipStr:
            get_gGroupKnowledgeCounter(str(group_id), False)
            log('SKIP')
        else:
            flag_needKnowledge = get_gGroupKnowledgeCounter(str(group_id), True)
            if record_knowledge is not True:
                flag_needKnowledge = False
            reply_list = reply_split(reply_wash(reply_text))
            log(f'REPLY - {reply_list}')
            for i in reply_list:
                if len(i) > 0:
                    add_message_to_history(group_id, i, None, None)
            t_set_memory = threading.Thread(target=set_memory)
            t_set_memory.start()
            if flag_needKnowledge:
                t_set_knowledge = threading.Thread(
                    target=set_knowledge,
                    args=(thisMemory, )
                )
                t_set_knowledge.start()
            sleep(1 + (random.random() * 2 - 1) * 0.95)
            reply(plugin_event, reply_list)
            t_set_memory.join()
            if flag_needKnowledge:
                t_set_knowledge.join()


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
        warn(f'API ERR: {response.status_code} {response.text}')
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
                    warn(f'DATA ERR: {i}')
            except Exception:
                warn(f'DATA ERR: {i}')
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


def logRaw(level: int, msg: str):
    if gProc is not None:
        gProc.log(level, msg, [
            (gPluginName, 'default')
        ])


def log(msg: str):
    logRaw(2, msg)


def warn(msg: str):
    logRaw(3, msg)


def reply(plugin_event, msg: list):
    for i in msg:
        len_i = len(i)
        if len_i > 0:
            sleep_time = sum([
                0.2 + (random.random() * 2 - 1) * 0.15
                for _ in range(len_i)
            ])
            if sleep_time > 30:
                sleep_time /= 2
            sleep(sleep_time)
            plugin_event.reply(i)


def reply_wash(msg: str):
    res = msg
    res = res.replace('\r', '')
    res = res.strip('\n')
    res = res.rstrip('。')
    res = re.sub(r'\(.+\)', '', res)
    res = re.sub(r'（.+）', '', res)
    res = res.replace('[SKIP]', '【SKIP】')
    return res


def reply_split(msg: str):
    res = msg
    res = res.split('\n')
    return res


def msg_wash(msg: str):
    res = msg
    res = re.sub(r'\[OP:image.+\]', '', res)
    res = re.sub(r'\[OP:record.+\]', '', res)
    res = re.sub(r'\[OP:video.+\]', '', res)
    return res


def sleep(sleep_time: float):
    log(f"WAIT - {sleep_time:.2f} s")
    time.sleep(sleep_time)


def get_gGroupKnowledgeCounter(group_id: str, flag_busy: bool, rate: int = 4):
    res = False
    if group_id not in gGroupKnowledgeCounter:
        gGroupKnowledgeCounter[group_id] = gGroupKnowledgeCounterLimit
    if flag_busy:
        gGroupKnowledgeCounter[group_id] += int(rate)
    else:
        gGroupKnowledgeCounter[group_id] += 1
    if (
        flag_busy
        and gGroupKnowledgeCounter[group_id] >= gGroupKnowledgeCounterLimit
    ):
        gGroupKnowledgeCounter[group_id] = 0
        res = True
    if not res:
        log(f'KNOWLEDGE [{group_id}] - {gGroupKnowledgeCounter[group_id]} / {gGroupKnowledgeCounterLimit}')
    else:
        log(f'KNOWLEDGE [{group_id}] - HIT')
    return res


def get_recommendRank(word1_in: str, word2_in: str, gate_rank: int = 1000, rate: float = 0.1):
    iRank = 1
    find_flag = 1
    word1 = word1_in.lower()
    word2 = word2_in.lower()
    if not word1 or not word2:
        return gate_rank + 1  # 返回一个高数值，表示完全不匹配，因为有些键值可能因为误设置从而为空字符串
    # word1 为短字符串，此场景不进行对调
    # if len(word1) > len(word2):
    #     [word1, word2] = [word2, word1]
    if len(word1) > len(word2):
        return gate_rank + 2
    word1_len = len(word1)
    word2_len = len(word2)

    if word2.find(word1) != -1:
        find_flag = 0

    # LCS
    dp1 = []
    dp1_first = [0]
    for word1_this in word1:
        dp1_first.append(0)
    dp1.append(dp1_first)
    for word2_this in word2:
        dp1.append([0] + [0] * word1_len)
    tmp_i_list = range(1, word1_len + 1)
    tmp_j_list = range(1, word2_len + 1)
    for i in tmp_i_list:
        for j in tmp_j_list:
            if word1[i - 1] == word2[j - 1]:
                dp1[j][i] = dp1[j - 1][i - 1] + 1
            else:
                dp1[j][i] = max(dp1[j - 1][i], dp1[j][i - 1])
    iRank_1 = dp1[word2_len][word1_len]

    # minDistance
    dp2 = []
    dp2_first = [0]
    tmp_counter = 1
    for word1_this in word1:
        dp2_first.append(tmp_counter)
        tmp_counter += 1
    dp2.append(dp2_first)
    tmp_counter = 1
    for word2_this in word2:
        dp2.append([tmp_counter] + [0] * word1_len)
        tmp_counter += 1
    tmp_i_list = range(1, word1_len + 1)
    tmp_j_list = range(1, word2_len + 1)
    for i in tmp_i_list:
        for j in tmp_j_list:
            if word1[i - 1] == word2[j - 1]:
                dp2[j][i] = dp2[j - 1][i - 1]
            else:
                dp2[j][i] = min(dp2[j - 1][i - 1], min(dp2[j - 1][i], dp2[j][i - 1])) + 1
    iRank_2 = dp2[word2_len][word1_len]

    iRank = (find_flag) * (word2_len * (word1_len - iRank_1) + iRank_2 + 1)
    iRank = int(int((iRank * iRank) / word1_len) / word2_len)

    if iRank >= int(word1_len * word2_len * rate):
        iRank += gate_rank

    return iRank


def get_recommendMatch(rank, gate_rank: int = 1000):
    res = False
    if rank < gate_rank:
        res = True
    return res
