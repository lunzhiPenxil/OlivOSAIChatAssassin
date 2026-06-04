import threading
from collections import deque

import OlivOS
import OlivOSAIChatAssassin

gProc: 'OlivOS.pluginAPI.shallow|None' = None
gPluginName = '群聊刺客'
gMessageHistory: 'dict[str, OlivOSAIChatAssassin.tools.DynamicQueue]' = {}

gData: 'OlivOSAIChatAssassin.load.DataManager|None' = None

gConfigDir = './plugin/data/OlivOSAIChatAssassin'
gConfigPath = './plugin/data/OlivOSAIChatAssassin/config.json'

gGroupLock: 'dict[str, OlivOSAIChatAssassin.tools.SlackableFairLock]' = {}
gGroupKnowledgeCounter: 'dict[str, int]' = {}
gGroupKnowledgeCounterLimit: int = int(4 * 4)

gMemoryLock = threading.Lock()
gMemoryDir = './plugin/data/OlivOSAIChatAssassin'
gMemoryPath = './plugin/data/OlivOSAIChatAssassin/memory.json'
gMemoryDefault = {
    '全局': {
        '知识搜索': {},
        '知识缓存': {},
        '人物关系': {},
        '用户侧写': {},
        '图片缓存': {}
    }
}
gMemoryDefaultStr = "择机加入对话"

gStaticKnowledgeDir = './plugin/data/OlivOSAIChatAssassin/Knowledge'
gStaticKnowledge = {}

gImageDir = './plugin/data/OlivOSAIChatAssassin/Image'
gImageCache: 'dict[str, deque]' = {}

gPeakUpCache: 'dict[str, dict[str, dict[str, int|list]]]' = {}

gThinkTS: 'dict[str, dict[str, float]]' = {}

configDefault = {
    'api_key': '',
    'api_base': 'https://api.deepseek.com/v1',
    'model': 'deepseek-v4-flash',
    'max_tokens': 2048,
    'temperature': 0.7,
    'thinking': {'type': 'disabled'},
    'reasoning_effort': 'max',
    'first_thinking': False,
    'first_thinking_cooldown': 60,
    'personality': '你是一个伪装成人类的自豪的新锐AI，你择机加入聊天，避免过多尬聊。别让他们发现你的真实身份！！！',
    'record_knowledge': True,
    'search_knowledge_deepin': 1,
    'retry_count': 3,
    'enabled_groups': [
        'all'
    ],
    'search_ageing': 900,
    'history_size_min': 5,
    'history_size': 8,
    'history_dynamic': False,
    'history_dynamic_size': 16,
    'slack_time': 5,
    'slack_cooldown_time': 30,
    'reply_probability': 1,
    'reply_keywords': [],
    'mention_reply': True,
    'ignore_prefixes': [],
    'max_message_length': 2048,
    'ocr_api': {
        'api_key': '',
        'api_base': 'https://api.siliconflow.cn/v1',
        'model': 'Pro/moonshotai/Kimi-K2.5',
        'mode': 'base64',
        'enable': False,
        "queue_size": 8
    }
}
