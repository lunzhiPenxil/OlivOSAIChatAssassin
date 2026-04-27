import os
import json

import OlivOSAIChatAssassin


def load_config():
    try:
        os.makedirs(OlivOSAIChatAssassin.data.gConfigDir, exist_ok=True)
        if os.path.exists(OlivOSAIChatAssassin.data.gConfigPath):
            with open(OlivOSAIChatAssassin.data.gConfigPath, 'r', encoding='utf-8') as f:
                OlivOSAIChatAssassin.data.gConfig = json.load(f)
                # 设置默认值
                defaults = OlivOSAIChatAssassin.data.configDefault
                for key, value in defaults.items():
                    if key not in OlivOSAIChatAssassin.data.gConfig:
                        OlivOSAIChatAssassin.data.gConfig[key] = value
        else:
            # 如果配置文件不存在，使用示例配置但不启用任何群组
            OlivOSAIChatAssassin.data.gConfig = OlivOSAIChatAssassin.data.configDefault
            # 创建示例配置文件
            with open(OlivOSAIChatAssassin.data.gConfigPath, 'w', encoding='utf-8') as f:
                json.dump(OlivOSAIChatAssassin.data.gConfig, f, ensure_ascii=False, indent=4)
    except Exception as e:
        OlivOSAIChatAssassin.logger.warn(f'加载配置失败: {e}')
        OlivOSAIChatAssassin.data.gConfig = None


def load_staticKnowledge():
    global gStaticKnowledge
    gStaticKnowledge = {}
    try:
        os.makedirs(OlivOSAIChatAssassin.data.gStaticKnowledgeDir, exist_ok=True)
        for i in os.listdir(OlivOSAIChatAssassin.data.gStaticKnowledgeDir):
            f_name = f'{OlivOSAIChatAssassin.data.gStaticKnowledgeDir}/{i}'
            try:
                with open(f_name, 'r', encoding='utf-8') as f:
                    f_obj = json.loads(f.read())
                    if type(f_obj) is not dict:
                        OlivOSAIChatAssassin.logger.warn(f'加载知识库[{i}]失败: 类型错误[{type(f_obj)}]')
                    else:
                        gStaticKnowledge.update(**f_obj)
                        OlivOSAIChatAssassin.logger.log(f'已加载知识库[{i}]')
            except Exception as e:
                OlivOSAIChatAssassin.logger.warn(f'加载知识库[{i}]失败: {e}')
        OlivOSAIChatAssassin.logger.log(f'已加载知识库共[{len(gStaticKnowledge)}]条')
    except Exception as e:
        OlivOSAIChatAssassin.logger.warn(f'加载知识库完全失败: {e}')


def load_memory():
    global gMemory
    try:
        os.makedirs(OlivOSAIChatAssassin.data.gMemoryDir, exist_ok=True)
        if os.path.exists(OlivOSAIChatAssassin.data.gMemoryPath):
            with OlivOSAIChatAssassin.data.gMemoryLock:
                with open(OlivOSAIChatAssassin.data.gMemoryPath, 'r', encoding='utf-8') as f:
                    gMemory = json.load(f)
        else:
            gMemory = {}
            write_memory()
    except Exception as e:
        OlivOSAIChatAssassin.logger.warn(f'加载记忆失败: {e}')
        gMemory = None


def write_memory():
    with OlivOSAIChatAssassin.data.gMemoryLock:
        try:
            os.makedirs(OlivOSAIChatAssassin.data.gMemoryDir, exist_ok=True)
            with open(OlivOSAIChatAssassin.data.gMemoryPath, 'w', encoding='utf-8') as f:
                json.dump(gMemory, f, ensure_ascii=False, indent=4)
        except Exception as e:
            OlivOSAIChatAssassin.logger.warn(f'写入记忆失败: {e}')
