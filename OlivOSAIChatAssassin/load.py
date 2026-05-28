import os
import json
import threading
from enum import Enum
from typing import Dict, Optional

import OlivOSAIChatAssassin


def load_logger(Proc):
    OlivOSAIChatAssassin.data.gProc = Proc


def load_init_after():
    OlivOSAIChatAssassin.data.gMessageHistory = {}
    OlivOSAIChatAssassin.data.gData = OlivOSAIChatAssassin.load.DataManager(
        config_dir=OlivOSAIChatAssassin.data.gConfigDir,
        memory_dir=OlivOSAIChatAssassin.data.gMemoryDir,
        default_config=OlivOSAIChatAssassin.data.configDefault
    )


def load_bot(bot_hash_list: str):
    pass
    if OlivOSAIChatAssassin.data.gData is None:
        OlivOSAIChatAssassin.logger.warn('加载数据失败')
    else:
        for bot_hash in bot_hash_list:
            OlivOSAIChatAssassin.data.gData.load_bot(bot_hash)


def write_memory(bot_hash: str):
    if OlivOSAIChatAssassin.data.gData is None:
        OlivOSAIChatAssassin.logger.warn('写入记忆失败')
    else:
        OlivOSAIChatAssassin.data.gData.save_bot_memory(bot_hash)


def load_staticKnowledge():
    OlivOSAIChatAssassin.data.gStaticKnowledge = {}
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
                        OlivOSAIChatAssassin.data.gStaticKnowledge.update(**f_obj)
                        OlivOSAIChatAssassin.logger.log(f'已加载知识库[{i}]')
            except Exception as e:
                OlivOSAIChatAssassin.logger.warn(f'加载知识库[{i}]失败: {e}')
        OlivOSAIChatAssassin.logger.log(f'已加载知识库共[{len(OlivOSAIChatAssassin.data.gStaticKnowledge)}]条')
    except Exception as e:
        OlivOSAIChatAssassin.logger.warn(f'加载知识库完全失败: {e}')


class DataManagerType(Enum):
    CONFIG = 'CONFIG'
    MEMORY = 'MEMORY'


class DataManager:
    def __init__(self, config_dir: str, memory_dir: str, default_config: dict):
        """
        :param config_dir:    存放配置文件的目录（对应 gConfigDir）
        :param memory_dir:    存放记忆文件的目录（对应 gMemoryDir）
        :param default_config: 默认配置字典（对应 gMemoryDefault 或 configDefault）
        """
        self.config_dir = config_dir
        self.memory_dir = memory_dir
        self.default_config = default_config

        # botHash -> 配置字典
        self.config: Dict[str, dict] = {}
        # botHash -> 记忆字典
        self.memory: Dict[str, dict] = {}

        # 用于 get 方法的分发映射
        self._data_mapping = {
            DataManagerType.CONFIG: self.config,
            DataManagerType.MEMORY: self.memory,
        }

        # 记录每个 bot 最终使用的文件路径
        self._config_path: Dict[str, str] = {}
        self._memory_path: Dict[str, str] = {}

        # 记录文件最后修改时间
        self._config_mtime: Dict[str, float] = {}
        self._memory_mtime: Dict[str, float] = {}

        # 读写锁
        self._config_lock = threading.Lock()
        self._memory_lock = OlivOSAIChatAssassin.data.gMemoryLock

    # ==================== 核心加载逻辑 ====================

    def _load_bot_config(self, bot_hash: str) -> dict:
        """
        加载指定 bot 的配置，支持回退：
        1. 优先使用 config_{botHash}.json
        2. 不存在则使用 config.json
        3. 仍不存在则使用默认配置并写入 config_{botHash}.json
        """
        flag_need_read = True
        flag_need_write = False

        os.makedirs(self.config_dir, exist_ok=True)

        config = {}
        specific_path = os.path.join(self.config_dir, f"config_{bot_hash}.json")
        common_path = os.path.join(self.config_dir, "config.json")
        target_path = None

        # 决定使用哪个文件
        if os.path.exists(specific_path):
            target_path = specific_path
        elif os.path.exists(common_path):
            target_path = common_path
            flag_need_write = True
        else:
            # 两个都不存在：用默认配置写入专属文件
            target_path = specific_path
            flag_need_read = False
            flag_need_write = True

        # 读取文件
        if flag_need_read:
            with self._config_lock:
                with open(target_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)

        # 补全缺失的默认字段
        for key, value in self.default_config.items():
            if key not in config:
                config[key] = value

        if flag_need_write:
            target_path = specific_path
            with self._config_lock:
                with open(target_path, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=4)

        self._config_path[bot_hash] = target_path
        self._config_mtime[bot_hash] = os.path.getmtime(target_path)
        return config

    def _load_bot_memory(self, bot_hash: str) -> dict:
        """
        加载指定 bot 的记忆，逻辑与配置类似：
        memory_{botHash}.json -> memory.json -> 默认空字典并写入
        """
        flag_need_read = True
        flag_need_write = False

        os.makedirs(self.memory_dir, exist_ok=True)

        memory = {}
        specific_path = os.path.join(self.memory_dir, f"memory_{bot_hash}.json")
        common_path = os.path.join(self.memory_dir, "memory.json")
        target_path = None

        if os.path.exists(specific_path):
            target_path = specific_path
        elif os.path.exists(common_path):
            target_path = common_path
            flag_need_write = True
        else:
            target_path = specific_path
            # 默认记忆为空字典，也可自定义默认记忆内容
            flag_need_read = False
            flag_need_write = True

        if flag_need_read:
            with self._memory_lock:
                with open(target_path, 'r', encoding='utf-8') as f:
                    memory = json.load(f)

        if flag_need_write:
            target_path = specific_path
            with self._memory_lock:
                with open(target_path, 'w', encoding='utf-8') as f:
                    json.dump(memory, f, ensure_ascii=False, indent=4)

        self._memory_path[bot_hash] = target_path
        self._memory_mtime[bot_hash] = os.path.getmtime(target_path)
        return memory

    def load_bot(self, bot_hash: str, force: bool = False):
        """
        加载指定 bot 的配置和记忆。
        :param force: 如果为 True，强制重新加载（即使未检测到变化也会重新读取文件）。
        """
        try:
            # 如果 force=False，可以保留当前数据，但 load_bot 本身总是重新加载
            # 这里 force 参数留给 reload 方法控制是否强制
            self.config[bot_hash] = self._load_bot_config(bot_hash)
            self.memory[bot_hash] = self._load_bot_memory(bot_hash)
        except Exception as e:
            OlivOSAIChatAssassin.logger.warn(f"加载 bot {bot_hash} 失败: {e}")
            raise

    # ==================== 热加载 ====================

    def reload(self, bot_hash: Optional[str] = None, force: bool = False):
        """
        重新加载配置/记忆，支持热加载（检测外部修改）。
        - 若指定 bot_hash，则只处理该 bot；
        - 若为 None，则处理所有已加载的 bot。
        - 若 force=True，则无论文件是否修改都强制重新加载（调用 load_bot）；
        - 若 force=False，则只对文件发生变化的 bot 进行重载。
        """
        if bot_hash is not None:
            targets = [bot_hash]
        else:
            # 取所有已加载 bot 的并集（配置和记忆都可能存在）
            loaded = set(self.config.keys()) | set(self.memory.keys())
            targets = list(loaded)

        for bh in targets:
            if force:
                self.load_bot(bh)
            else:
                # 热加载：仅当文件变化时重载
                # 注意：配置和记忆需要分别检查，但 load_bot 会同时加载两者
                # 为了效率，可以分别检查，只要任一变化就调用 load_bot
                need_reload = False
                # 检查配置是否已加载且变化
                if bh in self._config_path:
                    cfg_path = self._config_path[bh]
                    if os.path.exists(cfg_path):
                        new_mtime = os.path.getmtime(cfg_path)
                        if new_mtime > self._config_mtime.get(bh, 0):
                            need_reload = True
                    else:
                        need_reload = True
                # 检查记忆是否已加载且变化
                if not need_reload and bh in self._memory_path:
                    mem_path = self._memory_path[bh]
                    if os.path.exists(mem_path):
                        new_mtime = os.path.getmtime(mem_path)
                        if new_mtime > self._memory_mtime.get(bh, 0):
                            need_reload = True
                    else:
                        need_reload = True
                # 如果尚未加载过，但用户指定了 reload，也重新加载
                if bh not in self.config or bh not in self.memory:
                    need_reload = True

                if need_reload:
                    self.load_bot(bh)

    # ==================== 对外接口 ====================

    def get(self, data_type: DataManagerType, bot_hash: str) -> dict:
        container = self._data_mapping.get(data_type)
        if container is None:
            raise ValueError(f"Unknown data type: {data_type}")

        # 自动加载未加载的 bot
        if bot_hash not in container:
            self.load_bot(bot_hash)

        return container[bot_hash]

    def getConfig(self, bot_hash: str) -> dict:
        return self.get(DataManagerType.CONFIG, bot_hash)

    def getMemory(self, bot_hash: str) -> dict:
        return self.get(DataManagerType.MEMORY, bot_hash)

    # ==================== 写入 / 持久化 ====================

    def save_bot_config(self, bot_hash: str):
        """将当前内存中的配置写回文件（覆盖专属文件）"""
        if bot_hash not in self.config:
            raise KeyError(f"Bot {bot_hash} 配置未加载")
        path = self._config_path.get(bot_hash)
        if not path:
            path = os.path.join(self.config_dir, f"config_{bot_hash}.json")
        with self._config_lock:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.config[bot_hash], f, ensure_ascii=False, indent=4)
            self._config_mtime[bot_hash] = os.path.getmtime(path)

    def save_bot_memory(self, bot_hash: str):
        if bot_hash not in self.memory:
            raise KeyError(f"Bot {bot_hash} 记忆未加载")
        path = self._memory_path.get(bot_hash)
        if not path:
            path = os.path.join(self.memory_dir, f"memory_{bot_hash}.json")
        with self._memory_lock:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.memory[bot_hash], f, ensure_ascii=False, indent=4)
            self._memory_mtime[bot_hash] = os.path.getmtime(path)
