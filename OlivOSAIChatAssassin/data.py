import threading
from collections import deque

import OlivOS
import OlivOSAIChatAssassin

gProc: 'OlivOS.pluginAPI.shallow|None' = None
gPluginName = '群聊刺客'
gConfig = None
gMessageHistory: 'dict[str, deque]' = {}
gConfigDir = './plugin/data/OlivOSAIChatAssassin'
gConfigPath = './plugin/data/OlivOSAIChatAssassin/config.json'

gSkipStr = '【SKIP】'

gGroupLock: 'dict[str, OlivOSAIChatAssassin.tools.FairLock]' = {}
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
    'model': 'deepseek-v4-flash',
    'max_tokens': 2048,
    'temperature': 0.7,
    "thinking": {"type": "disabled"},
    "reasoning_effort": "max",
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
