import json
import random
import time
import threading
import re
from collections import deque
from datetime import datetime

import OlivOS
import OlivOSAIChatAssassin


def unity_group_message(plugin_event: OlivOS.API.Event, Proc, missed: bool = False):
    group_id = str(plugin_event.data.group_id)
    OlivOSAIChatAssassin.load.load_config()
    OlivOSAIChatAssassin.load.load_memory()
    if not OlivOSAIChatAssassin.data.gConfig:
        return
    # 检查是否在启用群组列表中
    if (
        'enabled_groups' in OlivOSAIChatAssassin.data.gConfig
        and (
            group_id not in OlivOSAIChatAssassin.data.gConfig['enabled_groups']
            and 'all' not in OlivOSAIChatAssassin.data.gConfig['enabled_groups']
        )
    ):
        return
    # 忽略前缀消息
    message = plugin_event.data.message
    message = msg_wash(message)
    if should_ignore(message):
        OlivOSAIChatAssassin.logger.log('IGNORE')
        return
    # 添加消息到历史
    if group_id not in OlivOSAIChatAssassin.data.gMessageHistory:
        OlivOSAIChatAssassin.data.gMessageHistory[group_id] = deque(
            maxlen=OlivOSAIChatAssassin.data.gConfig.get(
                'history_size', OlivOSAIChatAssassin.data.configDefault['history_size']
            )
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
        OlivOSAIChatAssassin.logger.log(f'MISSED - {message}')
    elif not should_reply(group_id, message, plugin_event):
        OlivOSAIChatAssassin.logger.log('SHOULD NOT')
    else:
        reply_to_group(plugin_event, group_id)


def should_ignore(message):
    if not OlivOSAIChatAssassin.data.gConfig:
        return False
    if len(message) <= 0:
        return True
    ignore_prefixes = OlivOSAIChatAssassin.data.gConfig.get('ignore_prefixes', [])
    for prefix in ignore_prefixes:
        if message.startswith(prefix):
            return True
    return False


def add_message_to_history(group_id, message, user_id, nickname, message_id: 'str|None' = None):
    if group_id not in OlivOSAIChatAssassin.data.gMessageHistory:
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
    OlivOSAIChatAssassin.data.gMessageHistory[group_id].append(msg_entry)


def should_reply(group_id, message, plugin_event):
    if not OlivOSAIChatAssassin.data.gConfig:
        return False
    # 检查是否被@
    self_id = plugin_event.base_info['self_id']
    mention_str = f'[OP:at,id={self_id}]'
    if OlivOSAIChatAssassin.data.gConfig.get('mention_reply', True) and mention_str in message:
        return True
    # 检查是否包含关键词
    keywords = OlivOSAIChatAssassin.data.gConfig.get('reply_keywords', [])
    for kw in keywords:
        if kw in message:
            return True
    # 随机概率回复
    prob = OlivOSAIChatAssassin.data.gConfig.get(
        'reply_probability', OlivOSAIChatAssassin.data.configDefault["reply_probability"]
    )
    if random.random() < prob:
        return True
    return False


def reply_to_group(plugin_event, group_id):
    if not OlivOSAIChatAssassin.data.gConfig or not OlivOSAIChatAssassin.data.gConfig.get('api_key'):
        return
    # 构建对话历史
    history: 'list[dict]' = list(OlivOSAIChatAssassin.data.gMessageHistory.get(group_id, deque()))
    if not history:
        return
    elif len(history) <= 5:
        OlivOSAIChatAssassin.logger.log('HISTORY TOO SHORT')
        return
    self_id = plugin_event.base_info['self_id']
    mention_str = f'[OP:at,id={self_id}]'
    personality = OlivOSAIChatAssassin.data.gConfig.get('personality', '')
    record_knowledge = OlivOSAIChatAssassin.data.gConfig.get('record_knowledge', True)
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
        history = list(OlivOSAIChatAssassin.data.gMessageHistory.get(group_id, deque()))
        # 设置任务
        content = f'''
# 当前记忆
- {OlivOSAIChatAssassin.data.gMemory.get(group_id, OlivOSAIChatAssassin.data.gMemoryDefaultStr)}

# 当前任务
- 对聊天记录进行总结
- 杜绝流水账，请每次都决定自己需要长期记住什么东西
- 仅输出需要记忆的信息
- 不要遗忘别的群的记忆
- 最终长度限制在128字以内
'''
        # 格式化历史为OpenAI消息格式
        messages = get_ai_context(OlivOSAIChatAssassin.data.gConfig, history, content, flagMerge=True)
        # 调用 API
        try:
            group_memory = OlivOSAIChatAssassin.webTools.call_ai(
                OlivOSAIChatAssassin.data.gConfig, messages,
                temperature_override=0.7,
                json_mode=False,
                flag_thinking_override=False,
                reasoning_effort_override="max"
            )
            with OlivOSAIChatAssassin.data.gMemoryLock:
                OlivOSAIChatAssassin.data.gMemory[group_id] = group_memory
            OlivOSAIChatAssassin.load.write_memory()
            OlivOSAIChatAssassin.logger.log(f'[本群记忆]\n{OlivOSAIChatAssassin.data.gMemory[group_id]}')
        except Exception as e:
            OlivOSAIChatAssassin.logger.warn(f'API FATAL: {e}')

    # 生成长期记忆
    def set_knowledge(t_thisMemory: dict):
        history = list(OlivOSAIChatAssassin.data.gMessageHistory.get(group_id, deque()))
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
            OlivOSAIChatAssassin.data.gConfig, history, content, flagMerge=True,
            prefix=(
                f'前情提要：{OlivOSAIChatAssassin.data.gMemory.get(group_id, OlivOSAIChatAssassin.data.gMemoryDefaultStr)}'
                f'\n\n现在提炼如下对话中的重要知识点：'
            )
        )
        # 调用 API
        try:
            knowledge_data_str = OlivOSAIChatAssassin.webTools.call_ai(
                OlivOSAIChatAssassin.data.gConfig, messages,
                temperature_override=0.7,
                json_mode=False,
                flag_thinking_override=False,
                reasoning_effort_override="max"
            )
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
                OlivOSAIChatAssassin.logger.warn(f'API JSON DATA FATAL: {e}\n{knowledge_data_str}')
                knowledge_data = {}
                flag_knowledge_err = True
            if type(knowledge_data) is not dict:
                OlivOSAIChatAssassin.logger.warn(f'API DATA TYPE FATAL: \n{knowledge_data_str}')
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
            with OlivOSAIChatAssassin.data.gMemoryLock:
                if '全局' not in OlivOSAIChatAssassin.data.gMemory:
                    OlivOSAIChatAssassin.data.gMemory['全局'] = {}
                if '知识缓存' not in OlivOSAIChatAssassin.data.gMemory['全局']:
                    OlivOSAIChatAssassin.data.gMemory['全局']['知识缓存'] = {}
                for k, v in knowledge_data.items():
                    flag_knowledge_update = True
                    if (
                        type(k) is str
                        and type(v) is str
                    ):
                        OlivOSAIChatAssassin.data.gMemory['全局']['知识缓存'][k] = v
                        OlivOSAIChatAssassin.logger.log(f'[更新知识] - {k}\n{v}')
            if flag_knowledge_update:
                OlivOSAIChatAssassin.load.write_memory()
        except Exception as e:
            OlivOSAIChatAssassin.logger.warn(f'API FATAL: {e}')

    # 设置任务
    thisMemoryG = {}
    with OlivOSAIChatAssassin.data.gMemoryLock:
        for k, v in OlivOSAIChatAssassin.data.gMemory.get('全局', {}).items():
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
        thisMemoryM = OlivOSAIChatAssassin.data.gMemory.get('全局', {key_gMemory: {}}).get(key_gMemory, {})
        if key_gMemory == key_staticKnowledge:
            thisMemoryM = OlivOSAIChatAssassin.data.gStaticKnowledge
        if type(thisMemoryM) is dict:
            for k, v in thisMemoryM.items():
                flagHit = False
                rank = None
                for j in history:
                    if key_gMemory == key_staticKnowledge:
                        rank = OlivOSAIChatAssassin.tools.get_recommendRank(k, j.get('message', ''), rate=0.15)
                    else:
                        rank = OlivOSAIChatAssassin.tools.get_recommendRank(k, j.get('message', ''))
                    if OlivOSAIChatAssassin.tools.get_recommendMatch(rank):
                        flagHit = True
                        break
                if flagHit:
                    OlivOSAIChatAssassin.logger.log(f'PEAK UP - [{key_gMemory}] {k} ({rank})')
                    thisMemoryG[key_gMemory_const][k] = v

    key_gMemory_const = '人物关系'
    thisMemoryG[key_gMemory_const] = {}
    for key_gMemory in (
        '人物关系',
    ):
        thisMemoryP = OlivOSAIChatAssassin.data.gMemory.get('全局', {key_gMemory: {}}).get(key_gMemory, {})
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
                    OlivOSAIChatAssassin.logger.log(f'PEAK UP - [{key_gMemory}] {flagHit_str}')
                    thisMemoryG[key_gMemory_const][k] = v
    thisMemory = {
        '全局': thisMemoryG,
        group_id: OlivOSAIChatAssassin.data.gMemory.get(group_id, OlivOSAIChatAssassin.data.gMemoryDefaultStr)
    }
    content = f'''{contentDefault}
# 当前记忆
- {json.dumps(thisMemory, ensure_ascii=False)}

# 当前任务
- 当你不想参与对话时，你会回复"{OlivOSAIChatAssassin.data.gSkipStr}"，这是你必须遵守的规则，你不需要每句话都回复，你需要按照你的心情来，但是当有人找你时尽量回复
- 判断是否应该加入聊天进行回复
- 如果应该回复，就直接输出你的回复内容
'''
    # 格式化历史为OpenAI消息格式
    messages = get_ai_context(OlivOSAIChatAssassin.data.gConfig, history, content)
    # 调用 API
    reply_text = None
    try:
        reply_text = OlivOSAIChatAssassin.webTools.call_ai(OlivOSAIChatAssassin.data.gConfig, messages)
    except Exception as e:
        OlivOSAIChatAssassin.logger.warn(f'API FATAL: {e}')
    # 发送回复
    if reply_text is None:
        get_gGroupKnowledgeCounter(str(group_id), False)
        OlivOSAIChatAssassin.logger.log('NONE')
    else:
        # 限制消息长度
        max_len = OlivOSAIChatAssassin.data.gConfig.get('max_message_length', 2000)
        if len(reply_text) > max_len:
            reply_text = reply_text[:max_len] + '...'
        if reply_text == OlivOSAIChatAssassin.data.gSkipStr:
            get_gGroupKnowledgeCounter(str(group_id), False)
            OlivOSAIChatAssassin.logger.log('SKIP')
        else:
            flag_needKnowledge = get_gGroupKnowledgeCounter(str(group_id), True)
            if record_knowledge is not True:
                flag_needKnowledge = False
            reply_list = reply_split(reply_wash(reply_text))
            OlivOSAIChatAssassin.logger.log(f'REPLY - {reply_list}')
            add_message_to_history(group_id, ''.join(reply_list), None, None)
            t_set_memory = threading.Thread(target=set_memory)
            t_set_memory.start()
            if flag_needKnowledge:
                t_set_knowledge = threading.Thread(
                    target=set_knowledge,
                    args=(thisMemory, )
                )
                t_set_knowledge.start()
            OlivOSAIChatAssassin.tools.sleep(1 + (random.random() * 2 - 1) * 0.95)
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


def get_message(data_str: str, json_mode: bool):
    res = data_str
    if not json_mode:
        OlivOSAIChatAssassin.logger.log('DATA TYPE - STR OUT')
    elif res == OlivOSAIChatAssassin.data.gSkipStr:
        OlivOSAIChatAssassin.logger.log('DATA TYPE - STR SKIP')
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
                    OlivOSAIChatAssassin.logger.log('DATA TYPE - JSON')
                else:
                    OlivOSAIChatAssassin.logger.warn(f'DATA ERR: {i}')
            except Exception:
                OlivOSAIChatAssassin.logger.warn(f'DATA ERR: {i}')
        else:
            res_list.append(i)
            OlivOSAIChatAssassin.logger.log('DATA TYPE - STR')
    if len(res_list) > 0:
        res = '\n'.join(res_list)
    return res


def get_status():
    status_lines = []
    status_lines.append('状态')
    if OlivOSAIChatAssassin.data.gConfig:
        status_lines.append(f'已加载配置: {len(OlivOSAIChatAssassin.data.gConfig.get("enabled_groups", []))} 个启用群组')
        status_lines.append(f'API密钥: {"已设置" if OlivOSAIChatAssassin.data.gConfig.get("api_key") else "未设置"}')
        history_size = OlivOSAIChatAssassin.data.gConfig.get(
            "history_size", OlivOSAIChatAssassin.data.configDefault["history_size"]
        )
        status_lines.append(f'历史记录大小: {history_size}')
        reply_probability = OlivOSAIChatAssassin.data.gConfig.get(
            "reply_probability", OlivOSAIChatAssassin.data.configDefault["reply_probability"]
        )
        status_lines.append(f'回复概率: {reply_probability}')
        for group_id, history in OlivOSAIChatAssassin.data.gMessageHistory.items():
            status_lines.append(f'群 {group_id}: {len(history)} 条历史消息')
    else:
        status_lines.append('配置未加载')
    return '\n'.join(status_lines)


def send_message_force(botHash, send_type, target_id, message):
    Proc = OlivOSAIChatAssassin.data.gProc
    if (
        Proc is not None
        and botHash in Proc.Proc_data['bot_info_dict']
    ):
        pluginName = OlivOSAIChatAssassin.data.gPluginName
        plugin_event = OlivOS.API.Event(
            OlivOS.contentAPI.fake_sdk_event(
                bot_info=Proc.Proc_data['bot_info_dict'][botHash],
                fakename=pluginName
            ),
            Proc.log
        )
        plugin_event.send(send_type, target_id, message)


def reply(plugin_event, msg: list):
    for i in msg:
        if OlivOSAIChatAssassin.data.gSkipStr in i:
            OlivOSAIChatAssassin.logger.log('SKIP - REPLY STR')
            return
    for i in msg:
        len_i = len(i)
        if len_i <= 0:
            OlivOSAIChatAssassin.logger.log('SKIP - REPLY NONE')
        else:
            sleep_time = sum([
                0.2 + (random.random() * 2 - 1) * 0.15
                for _ in range(len_i)
            ])
            if sleep_time > 30:
                sleep_time /= 2
            OlivOSAIChatAssassin.tools.sleep(sleep_time)
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


def get_gGroupKnowledgeCounter(group_id: str, flag_busy: bool, rate: int = 4):
    res = False
    if group_id not in OlivOSAIChatAssassin.data.gGroupKnowledgeCounter:
        OlivOSAIChatAssassin.data.gGroupKnowledgeCounter[group_id] = (
            OlivOSAIChatAssassin.data.gGroupKnowledgeCounterLimit
        )
    if flag_busy:
        OlivOSAIChatAssassin.data.gGroupKnowledgeCounter[group_id] += int(rate)
    else:
        OlivOSAIChatAssassin.data.gGroupKnowledgeCounter[group_id] += 1
    if (
        flag_busy
        and (
            OlivOSAIChatAssassin.data.gGroupKnowledgeCounter[group_id]
            >= OlivOSAIChatAssassin.data.gGroupKnowledgeCounterLimit
        )
    ):
        OlivOSAIChatAssassin.data.gGroupKnowledgeCounter[group_id] = 0
        res = True
    if not res:
        OlivOSAIChatAssassin.logger.log(
            f'KNOWLEDGE [{group_id}]'
            f' - {OlivOSAIChatAssassin.data.gGroupKnowledgeCounter[group_id]}'
            f' / {OlivOSAIChatAssassin.data.gGroupKnowledgeCounterLimit}'
        )
    else:
        OlivOSAIChatAssassin.logger.log(f'KNOWLEDGE [{group_id}] - HIT')
    return res
