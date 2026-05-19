import json
import random
import time
import threading
import re
import os
from datetime import datetime

import OlivOS
import OlivOSAIChatAssassin


def unity_group_message(plugin_event: OlivOS.API.Event, Proc):
    # 群消息事件入口
    group_id = str(plugin_event.data.group_id)
    OlivOSAIChatAssassin.data.gGroupLock.setdefault(
        group_id,
        OlivOSAIChatAssassin.tools.SlackableFairLock(
            slack_time=OlivOSAIChatAssassin.data.gConfig.get(
                'slack_time',
                OlivOSAIChatAssassin.data.configDefault['slack_time']
            ),
            cooldown_time=OlivOSAIChatAssassin.data.gConfig.get(
                'slack_cooldown_time',
                OlivOSAIChatAssassin.data.configDefault['slack_cooldown_time']
            )
        )
    )
    with OlivOSAIChatAssassin.data.gGroupLock[group_id]:
        OlivOSAIChatAssassin.msg.unity_group_message_router(plugin_event, Proc)


# 配置文件修改时间缓存，避免每次消息都重新加载
_gConfigMtime: float = 0.0
_gMemoryMtime: float = 0.0


def unity_group_message_router(plugin_event: OlivOS.API.Event, Proc):
    global _gConfigMtime, _gMemoryMtime
    group_id = str(plugin_event.data.group_id)

    # 仅在文件变化时重新加载配置和记忆（避免高频磁盘 I/O）
    try:
        config_mtime = os.path.getmtime(OlivOSAIChatAssassin.data.gConfigPath)
        if config_mtime > _gConfigMtime:
            _gConfigMtime = config_mtime
            OlivOSAIChatAssassin.load.load_config()
    except OSError:
        OlivOSAIChatAssassin.load.load_config()
    try:
        memory_mtime = os.path.getmtime(OlivOSAIChatAssassin.data.gMemoryPath)
        if memory_mtime > _gMemoryMtime:
            _gMemoryMtime = memory_mtime
            OlivOSAIChatAssassin.load.load_memory()
    except OSError:
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
    message = msg_trans(message)
    message = msg_wash(message)
    if should_ignore(message):
        OlivOSAIChatAssassin.logger.log('IGNORE')
        return
    # 添加消息到历史
    if group_id not in OlivOSAIChatAssassin.data.gMessageHistory:
        OlivOSAIChatAssassin.data.gMessageHistory[group_id] = OlivOSAIChatAssassin.tools.DynamicQueue(
            keep=OlivOSAIChatAssassin.data.gConfig.get(
                'history_size', OlivOSAIChatAssassin.data.configDefault['history_size']
            ),
            max_grow=(
                OlivOSAIChatAssassin.data.gConfig.get(
                    'history_dynamic_size', OlivOSAIChatAssassin.data.configDefault['history_dynamic_size'],
                )
                if OlivOSAIChatAssassin.data.gConfig.get(
                    'history_dynamic', OlivOSAIChatAssassin.data.configDefault['history_dynamic'],
                ) is True else
                OlivOSAIChatAssassin.data.gConfig.get(
                    'history_size', OlivOSAIChatAssassin.data.configDefault['history_size'],
                )
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
    if not should_reply(group_id, message, plugin_event):
        OlivOSAIChatAssassin.logger.log('SHOULD NOT')
    else:
        reply_to_group(plugin_event, group_id, message)


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
    max_len = OlivOSAIChatAssassin.data.gConfig.get(
        'max_message_length',
        OlivOSAIChatAssassin.data.configDefault['max_message_length']
    )
    if len(message_new) > max_len:
        message_new = message_new[:max_len] + '...'
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


def reply_to_group(plugin_event: OlivOS.API.Event, group_id: str, message: str):
    total_start = time.perf_counter()
    if not OlivOSAIChatAssassin.data.gConfig or not OlivOSAIChatAssassin.data.gConfig.get('api_key'):
        return
    # 构建对话历史
    history: 'list[dict]' = list(OlivOSAIChatAssassin.data.gMessageHistory.get(group_id, []))
    if not history:
        return
    elif len(history) <= OlivOSAIChatAssassin.data.gConfig.get(
        'history_size_min', OlivOSAIChatAssassin.data.configDefault['history_size_min']
    ):
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
- 越新的消息越重要，不要重复去回复已经回复过的消息，除非有必要
- 谨记你是在进行聊天，所以不要把括号之类的内容发出来，不需要你描述自己的动作或者心理活动，这只会让人起疑

# 人格设定
- {personality}

# 已知信息
- 你的QQ号是：{self_id}，所以你被@时是：{mention_str}
- 本群群号是：{group_id}，
- 最后一条消息总是最新的，时间可以视为当前本地时间
'''

    # 生成记忆
    def set_memory(t_thisMemory: dict):
        history = list(OlivOSAIChatAssassin.data.gMessageHistory.get(group_id, []))
        # 设置任务
        examples_knowledge = {
            "k": {
                "中国": "五千年文明古国，幅员辽阔，正全面推进民族复兴，坚持和平发展。"
            },
            "g": "我们刚刚聊到了中国"
        }
        content = '''
# 信息
前情提要附加在最末尾

# 当前任务
'''
        if record_knowledge:
            content += '''
## 分析知识点，将结果输出至k键的值中
- 分析当前聊天记录，提炼需要记住的知识点，注意不是对于现状的记录，只记录常识性的知识
- 每条知识点长度限制在32字以内
- 每条知识带有一个介于2至8字之间的关键词，被用于作为子字符串进行搜索
- 知识点以Json对象的格式输出，知识点的关键词为键，内容为值
'''
        content += '''
## 总结本群聊天记录，将结果作为字符串输出至g的值中
- 对聊天记录进行总结
- 杜绝流水账，请每次都决定自己需要记住什么东西
- 最终长度限制在128字以内
'''
        content += f'''
# 参考输出
{json.dumps(examples_knowledge, ensure_ascii=False)}
'''
        # 格式化历史为OpenAI消息格式
        messages = get_ai_context(
            OlivOSAIChatAssassin.data.gConfig, history, content, flagMerge=True,
            prefix='现在提炼如下对话中的重要知识点：',
            patch=f'前情提要：{OlivOSAIChatAssassin.data.gMemory.get(group_id, OlivOSAIChatAssassin.data.gMemoryDefaultStr)}'
        )
        # 调用 API
        try:
            call_ai_res = OlivOSAIChatAssassin.webTools.call_ai(
                OlivOSAIChatAssassin.data.gConfig, messages,
                temperature_override=0.7,
                flag_thinking_override=False,
                reasoning_effort_override="max",
                response_format_override={"type": "json_object"}
            )
            call_ai_data = json.loads(call_ai_res)
            knowledge_data: 'dict|None' = None
            group_memory_data: 'str|None' = None
            with OlivOSAIChatAssassin.data.gMemoryLock:
                if (
                    'k' in call_ai_data
                    and type(call_ai_data['k']) is dict
                ):
                    knowledge_data = call_ai_data['k']
                if '全局' not in OlivOSAIChatAssassin.data.gMemory:
                    OlivOSAIChatAssassin.data.gMemory['全局'] = {}
                if '知识缓存' not in OlivOSAIChatAssassin.data.gMemory['全局']:
                    OlivOSAIChatAssassin.data.gMemory['全局']['知识缓存'] = {}
                for k, v in knowledge_data.items():
                    if (
                        type(k) is str
                        and type(v) is str
                    ):
                        OlivOSAIChatAssassin.data.gMemory['全局']['知识缓存'][k] = v
                        OlivOSAIChatAssassin.logger.log(f'[更新知识] - {k}\n{v}')
                if (
                    'g' in call_ai_data
                    and type(call_ai_data['g']) is str
                ):
                    group_memory_data = call_ai_data['g']
                OlivOSAIChatAssassin.data.gMemory[group_id] = group_memory_data
                OlivOSAIChatAssassin.logger.log(f'[本群记忆]\n{OlivOSAIChatAssassin.data.gMemory[group_id]}')
            OlivOSAIChatAssassin.load.write_memory()
        except Exception as e:
            OlivOSAIChatAssassin.logger.warn(f'API FATAL: {e}')

    # 设置任务
    thisMemoryC = {}
    thisMemoryG = {}
    with OlivOSAIChatAssassin.data.gMemoryLock:
        for k, v in OlivOSAIChatAssassin.data.gMemory.get('全局', {}).items():
            if k not in (
                '人物关系',
                '知识缓存',
                '知识搜索',
            ):
                thisMemoryC[k] = v
    key_gMemory_const = '知识搜索'
    key_staticKnowledge = '知识库'
    thisMemoryG[key_gMemory_const] = {}
    thisMemoryG_patch = {}
    for key_gMemory in (
        '知识缓存',
        '知识库',
        '知识搜索',
    ):
        start = time.perf_counter()
        thisMemoryM = OlivOSAIChatAssassin.data.gMemory.get('全局', {key_gMemory: {}}).get(key_gMemory, {})
        rate_this = 0.1
        thisMemoryG_patch = {}
        if key_gMemory == key_staticKnowledge:
            thisMemoryM = OlivOSAIChatAssassin.data.gStaticKnowledge
            rate_this = 0.15
        for j in history:
            target_str = j.get('message', '')
            if j.get('nickname', '') is None:
                target_str = f"我()：{j.get('message', '')}"
            else:
                target_str = f"{j.get('nickname', '')}({j.get('user_id', '')})：{j.get('message', '')}"
            thisMemoryG_patch.update(
                OlivOSAIChatAssassin.tools.peak_up_recommendMatch(
                    target=target_str,
                    dictMap=thisMemoryM,
                    dictName=key_gMemory,
                    ageing=OlivOSAIChatAssassin.data.gConfig.get(
                        'search_ageing',
                        OlivOSAIChatAssassin.data.configDefault['search_ageing']
                    ),
                    rate=rate_this,
                    matchedList=list(thisMemoryG_patch.keys())
                )
            )
        thisMemoryG[key_gMemory_const].update(thisMemoryG_patch)
        end = time.perf_counter()
        OlivOSAIChatAssassin.logger.log(f"CALL PEAK UP - [{key_gMemory}] - DONE {(end - start):.2f} s")
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
    if not OlivOSAIChatAssassin.data.gGroupLock[group_id].slack():
        OlivOSAIChatAssassin.logger.log(
            f'NEXT - {time.perf_counter() - total_start:.2f}'
            f'/{OlivOSAIChatAssassin.data.gGroupLock[group_id].getRemaining():.2f} s - {message}'
        )
        return
    else:
        OlivOSAIChatAssassin.logger.log(
            f'HIT - {time.perf_counter() - total_start:.2f}'
            f'/{OlivOSAIChatAssassin.data.gGroupLock[group_id].getRemaining():.2f} s - {message}'
        )
    examples_reply = {
        'r': ['好的']
    }
    content = f'''{contentDefault}
# 信息
- 最新的消息中附带当前的记忆信息
- 越新的消息越重要

# 固定记忆
- {json.dumps(thisMemoryC, ensure_ascii=False)}

# 当前任务
## 将回复内容输出至r的值中
- 即便思考也要保证Json格式输出的完整，任何时候都要保证Json格式输出的完整
- 当你不想参与对话时，你会令r的值的列表为空，这是你必须遵守的规则，你不需要每句话都回复，你需要按照你的心情来，但是当有人找你时尽量回复
- 判断是否应该加入聊天进行回复
- 根据自己已经回复过的消息，避免重复已经回应过的话题，避免重复自己说过的话
- 如果应该回复，将回复内容追加至r的值的列表中，多条消息需要分开

# 参考输出，以严格的Json格式输出
{json.dumps(examples_reply, ensure_ascii=False)}
'''
    # 格式化历史为OpenAI消息格式
    history_size_max_print = (
        OlivOSAIChatAssassin.data.gConfig.get(
            'history_dynamic_size', OlivOSAIChatAssassin.data.configDefault['history_dynamic_size'],
        )
        if OlivOSAIChatAssassin.data.gConfig.get(
            'history_dynamic', OlivOSAIChatAssassin.data.configDefault['history_dynamic'],
        ) is True else
        OlivOSAIChatAssassin.data.gConfig.get(
            'history_size', OlivOSAIChatAssassin.data.configDefault['history_size'],
        )
    )
    OlivOSAIChatAssassin.logger.log(f"HISTORY - SIZE [{len(history)}/{history_size_max_print}]")
    messages = get_ai_context(
        OlivOSAIChatAssassin.data.gConfig, history, content,
        patch={'当前记忆': thisMemory}
    )
    # 调用 API
    reply_list = None
    reply_count = 0
    retry_count = OlivOSAIChatAssassin.data.gConfig.get(
        "retry_count", OlivOSAIChatAssassin.data.configDefault["retry_count"]
    )
    try:
        while (
            reply_list is None
            and reply_count < retry_count
        ):
            reply_count += 1
            OlivOSAIChatAssassin.logger.log(f"CALL AI - TRY [{reply_count}/{retry_count}]")
            reply_list = get_json_message(
                OlivOSAIChatAssassin.webTools.call_ai(
                    OlivOSAIChatAssassin.data.gConfig, messages,
                    response_format_override={"type": "json_object"}
                )
            )
    except Exception as e:
        OlivOSAIChatAssassin.logger.warn(f'API FATAL: {e}')
    # 发送回复
    if reply_list is None:
        OlivOSAIChatAssassin.logger.log('NONE')
    else:
        if len(reply_list) <= 0:
            OlivOSAIChatAssassin.logger.log('SKIP')
        else:
            reply_list = reply_wash(reply_list)
            OlivOSAIChatAssassin.logger.log(f'REPLY - {reply_list}')
            add_message_to_history(group_id, ''.join(reply_list), None, None)
            t_set_memory = threading.Thread(
                target=set_memory,
                args=(thisMemory, )
            )
            t_set_memory.start()
            OlivOSAIChatAssassin.tools.sleep(1 + (random.random() * 2 - 1) * 0.95)
            reply(
                plugin_event, reply_list,
                total_time_past=time.perf_counter() - total_start
            )
            t_set_memory.join()


def get_ai_context(
    lConfig,
    history,
    content,
    flagMerge: bool = False,
    prefix: str = "总结如下记录：",
    patch: 'dict|None' = None
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
    max_history_this = len(history)
    if flagMerge:
        chat_content = '\n'.join([
            f'{entry["time"]} [{entry["nickname"]}]({entry["user_id"]}) 说: "{entry["message"]}"'
            if entry['nickname'] is not None
            else f'{entry["time"]} [我]() 说: "{entry["message"]}"'
            for entry in list(history)[-max_history_this:]
        ])
        messages.append(
            {
                "role": "user",
                "content": f"{prefix}\n{chat_content}\n\n{patch}"
            }
        )
    else:
        count = 0
        for entry in list(history)[-max_history_this:]:
            count += 1
            if entry['nickname'] is None:
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"{entry['message']}"
                    }
                )
            else:
                entry_this = {}
                entry_this.update(entry)
                if count == max_history_this:
                    entry_this.update(patch)
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(entry_this, ensure_ascii=False)
                    }
                )
    return messages


def get_json_message(data_str: str):
    res_list = []
    try:
        data_dict = json.loads(data_str)
        if (
            type(data_dict) is dict
            and 'r' in data_dict
            and type(data_dict['r']) is list
        ):
            for i in data_dict['r']:
                res_list.append(i)
            OlivOSAIChatAssassin.logger.log('DATA TYPE - JSON')
        else:
            res_list = None
            OlivOSAIChatAssassin.logger.warn(f'DATA TYPE ERR: {data_str}')
    except Exception:
        res_list = None
        OlivOSAIChatAssassin.logger.warn(f'DATA ERR: {data_str}')
    return res_list


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


def reply(plugin_event, msg: list, total_time_past: float = 0.0):
    flag_first = True
    for i in msg:
        len_i = len(i)
        if len_i <= 0:
            OlivOSAIChatAssassin.logger.log('SKIP - REPLY NONE')
        else:
            sleep_time = sum([
                0.2 + (random.random() * 2 - 1) * 0.15
                for _ in range(len_i)
            ])
            if flag_first:
                flag_first = False
                if sleep_time > total_time_past:
                    sleep_time -= total_time_past
            if sleep_time > 30:
                sleep_time /= 2
            OlivOSAIChatAssassin.tools.sleep(sleep_time)
            plugin_event.reply(i)


def reply_wash(msg: list):
    res = []
    # 限制消息长度
    max_len = OlivOSAIChatAssassin.data.gConfig.get(
        'max_message_length',
        OlivOSAIChatAssassin.data.configDefault['max_message_length']
    )
    for i in msg:
        res_i = i
        if type(res_i) is str:
            res_i = res_i.replace('\r', '')
            res_i = res_i.strip('\n')
            res_i = res_i.rstrip('。')
            res_i = re.sub(r'\(.+\)', '', res_i)
            res_i = re.sub(r'（.+）', '', res_i)
            if len(res_i) > max_len:
                res_i = res_i[:max_len]
            res.append(res_i)
    return res


def msg_trans(msg: str):
    res = msg
    return res


def msg_wash(msg: str):
    res = msg
    res = re.sub(r'\[OP:image.+\]', '', res)
    res = re.sub(r'\[OP:record.+\]', '', res)
    res = re.sub(r'\[OP:video.+\]', '', res)
    return res
