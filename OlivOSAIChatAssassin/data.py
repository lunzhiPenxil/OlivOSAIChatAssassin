import threading
from collections import deque

import OlivOS
import OlivOSAIChatAssassin

gProc: 'OlivOS.pluginAPI.shallow|None' = None
gPluginName = '群聊刺客'
gMessageHistory: 'dict[tuple[str, str], OlivOSAIChatAssassin.tools.DynamicQueue]' = {}

gData: 'OlivOSAIChatAssassin.load.DataManager|None' = None

gConfigDir = './plugin/data/OlivOSAIChatAssassin'
gConfigPath = './plugin/data/OlivOSAIChatAssassin/config.json'

gGroupLock: 'dict[tuple[str, str], OlivOSAIChatAssassin.tools.SlackableFairLock]' = {}
gGroupKnowledgeCounter: 'dict[tuple[str, str], int]' = {}
gGroupKnowledgeCounterLimit: int = int(4 * 4)

gInteractionLock = threading.Lock()
gInteractionLastAttempt: 'dict[tuple[str, str], float]' = {}
gInteractionFailureUntil: 'dict[tuple[str, str, str], float]' = {}

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
gImageCache: 'dict[tuple[str, str], deque]' = {}

gPeakUpCache: 'dict[str, dict[str, dict[tuple, dict]]]' = {}

gThinkTS: 'dict[str, dict[str, float]]' = {}


def state_key(bot_hash: str, group_id: str) -> tuple:
    """返回运行期群状态键；所有状态都按 bot 与群共同隔离。"""
    return (str(bot_hash), str(group_id))


configDefault = {
    'api_key': '',
    'api_base': 'https://api.deepseek.com/v1',
    'model': 'deepseek-v4-flash',
    'max_tokens': 2048,
    'api_timeout': 120,
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
    'context_budget_chars': 0,
    'knowledge_top_k': 0,
    'slack_time': 5,
    'slack_cooldown_time': 30,
    'reply_probability': 1,
    'reply_keywords': [],
    'mention_reply': True,
    'ignore_prefixes': [],
    'max_message_length': 2048,
    'interaction_enabled': False,
    'interaction_actions': [],
    'interaction_reactions': [],
    'interaction_cooldown': 30,
    'ocr_api': {
        'api_key': '',
        'api_base': 'https://api.siliconflow.cn/v1',
        'model': 'Pro/moonshotai/Kimi-K2.5',
        'mode': 'base64',
        'enable': False,
        "queue_size": 8
    }
}
