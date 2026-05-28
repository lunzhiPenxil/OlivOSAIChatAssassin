import time
import threading
from typing import Optional

import OlivOSAIChatAssassin


# 礼貌节律公平锁
# 本锁实现以下功能
#   - 按照获取顺序依次授予锁
#   - 可以通过 slack 接口进行松弛等待
#   - 当出现竞争者时立即中断松弛，令松弛失败
#   - 松弛失败时加速下次松弛的进度
class SlackableFairLock:
    def __init__(self, slack_time: float, cooldown_time: float):
        self._lock: threading.Lock = threading.Lock()
        self._cond_acquire: threading.Condition = threading.Condition(self._lock)
        self._cond_slack: threading.Condition = threading.Condition(self._lock)
        self._next_ticket: int = 0
        self._serving: int = 0
        self._held: bool = False
        self._count: int = 0
        self._first_timestamp: 'float|None' = None
        self._slack_count: int = 1
        self._slack_time: float = slack_time
        self._cooldown_time: float = cooldown_time

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def acquire(self):
        with self._lock:
            if self._first_timestamp is None:
                self._first_timestamp = time.perf_counter()
            my_ticket = self._next_ticket
            self._next_ticket += 1
            self._count += 1
            self._cond_slack.notify_all()
            while my_ticket != self._serving:
                self._cond_acquire.wait()
            self._held = True

    def release(self):
        with self._lock:
            if not self._held:
                raise RuntimeError("release unlocked lock")
            self._held = False
            self._serving += 1
            self._count -= 1
            self._tryReset()
            self._cond_acquire.notify_all()

    def slack(self):
        with self._lock:
            if not self._held:
                raise RuntimeError("slack unlocked lock")
            remaining = self._getRemaining()
            if remaining > 0:
                self._cond_slack.wait(timeout=remaining)
            res = self._isLast()
            if not res:
                self._slack_count *= 2
            return res

    def _tryReset(self):
        if 0 == self._count:
            self._next_ticket = 0
            self._serving = 0
            self._first_timestamp = None
            self._slack_count = 1

    def _locked(self):
        return self._held

    def locked(self):
        with self._lock:
            return self._held

    def _isLast(self):
        return self._count <= 1

    def isLast(self):
        with self._lock:
            return self._isLast()

    def _getRemaining(self):
        now_timestamp = time.perf_counter()
        return float(
            min(
                max(0, self._slack_time - (now_timestamp - self._first_timestamp)),
                max(0, self._cooldown_time - (now_timestamp - self._first_timestamp)) / self._slack_count,
            )
        )

    def getRemaining(self):
        with self._lock:
            return self._getRemaining()


def sleep(sleep_time: float):
    OlivOSAIChatAssassin.logger.log(f"WAIT - {sleep_time:.2f} s")
    time.sleep(sleep_time)


def get_recommendRank(word1_in: str, word2_in: str, gate_rank: int = 1000, rate: float = 0.1):
    word1 = word1_in.lower()
    word2 = word2_in.lower()

    # 空字符串处理
    if not word1 or not word2:
        return gate_rank + 1

    # 保证 word1 为较短的字符串（提高 DP 效率），原始逻辑会自动交换
    if len(word1) > len(word2):
        return gate_rank + 2

    len1 = len(word1)
    len2 = len(word2)

    # 子串快速匹配（此时 len1 <= len2）
    if word2.find(word1) != -1:
        return 0   # 完全匹配时原始逻辑计算后也是 0

    # 滚动数组：prev_lcs 上一行 LCS，prev_ed 上一行编辑距离
    prev_lcs = [0] * (len1 + 1)
    prev_ed = list(range(len1 + 1))   # 第一行：空字符串 -> word1 的编辑距离

    for j in range(1, len2 + 1):
        ch2 = word2[j - 1]
        cur_lcs = [0] * (len1 + 1)
        cur_ed = [0] * (len1 + 1)
        cur_ed[0] = j   # 第一列：空字符串 -> word2 前缀的编辑距离

        for i in range(1, len1 + 1):
            if word1[i - 1] == ch2:
                cur_lcs[i] = prev_lcs[i - 1] + 1
                cur_ed[i] = prev_ed[i - 1]
            else:
                cur_lcs[i] = max(prev_lcs[i], cur_lcs[i - 1])
                cur_ed[i] = min(prev_ed[i - 1], prev_ed[i], cur_ed[i - 1]) + 1

        prev_lcs = cur_lcs
        prev_ed = cur_ed

    iRank_1 = prev_lcs[len1]   # LCS 长度
    iRank_2 = prev_ed[len1]    # 编辑距离

    # 原始计算公式（此时 find_flag = 1，因为未子串匹配）
    iRank = len2 * (len1 - iRank_1) + iRank_2 + 1
    iRank = (iRank * iRank) // len1 // len2

    # 阈值判断（使用 rate 和 gate_rank，原始 rate=1.0）
    if iRank >= int(len1 * len2 * rate):
        iRank += gate_rank

    return iRank


def get_recommendMatch(rank, gate_rank: int = 1000):
    res = False
    if rank < gate_rank:
        res = True
    return res


def peak_up_recommendMatch(
    target: str, dictMap: dict, dictName: str, ageing: int,
    rate: float = 1.0,
    matchedList: 'list|None' = None,
    father: 'str|None' = None
):
    timestamp = int(time.perf_counter())
    res = {}
    res_key_list = []
    matchedList_this = matchedList if type(matchedList) is list else []
    if dictName not in OlivOSAIChatAssassin.data.gPeakUpCache:
        OlivOSAIChatAssassin.data.gPeakUpCache[dictName] = {}
    cache_key_list = list(OlivOSAIChatAssassin.data.gPeakUpCache[dictName].keys())
    for k in cache_key_list:
        if timestamp - (
            OlivOSAIChatAssassin.data.gPeakUpCache.get(dictName, {}).get(k, {}).get('timestamp', 0)
        ) >= ageing:
            OlivOSAIChatAssassin.data.gPeakUpCache[dictName].pop(k, None)
    if type(dictMap) is dict:
        dictMap_key_list = list(dictMap.keys())
        if target in OlivOSAIChatAssassin.data.gPeakUpCache[dictName]:
            res_key_list = OlivOSAIChatAssassin.data.gPeakUpCache.get(dictName, {}).get(target, {}).get('keylist', None)
            if type(res_key_list) is list:
                for k in res_key_list:
                    k_str = k if father is None else f'{father} -> {k}'
                    OlivOSAIChatAssassin.logger.log(f'PEAK UP - [{dictName}] {k_str} (cached)')
        else:
            for k in dictMap_key_list:
                if k not in matchedList_this:
                    rank = OlivOSAIChatAssassin.tools.get_recommendRank(k, target, rate=rate)
                    if OlivOSAIChatAssassin.tools.get_recommendMatch(rank):
                        res_key_list.append(k)
                        k_str = k if father is None else f'{father} -> {k}'
                        OlivOSAIChatAssassin.logger.log(f'PEAK UP - [{dictName}] {k_str} ({rank})')
        if type(res_key_list) is not list:
            res_key_list = []
        else:
            OlivOSAIChatAssassin.data.gPeakUpCache[dictName][target] = {
                'timestamp': timestamp,
                'keylist': res_key_list
            }
        for k in res_key_list:
            if k in dictMap:
                res[k] = dictMap[k]
    return res


def get_copy_data(data: dict):
    res = data.copy()
    return res


class DynamicQueue:
    """动态队列：增长至 max_grow，达到后的下一次追加时保留最新 keep 个元素，然后继续增长至 max_grow，如此循环。"""

    def __init__(self, keep, max_grow):
        self.max_grow = max_grow   # 最大增长长度
        self.keep = keep           # 触发修剪后保留的元素个数
        self.queue = []            # 用列表存储队列元素

    def append(self, item):
        """向队列追加一个元素，自动执行增长/修剪逻辑。"""
        # 如果当前长度已经达到 max_grow，下一次追加时先修剪
        if len(self.queue) == self.max_grow:
            # 保留最后 keep-1 个元素（因为接下来还要追加一个新元素）
            self.queue = self.queue[-(self.keep - 1):]
        # 追加新元素
        self.queue.append(item)

    def __len__(self):
        return len(self.queue)

    def __repr__(self):
        return f"DynamicQueue({self.queue})"

    def __iter__(self):
        return iter(self.queue)

    def __getitem__(self, index):
        return self.queue[index]


def opcode_parse_params(typeKey: str, markup: str) -> dict:
    """从 '[OP:typeKey,key1=val1,key2=val2]' 提取参数为字典"""
    # 去掉前缀 '[OP:typeKey,' 和后缀 ']'
    inner = markup[len(f'[OP:{typeKey},'):-1]
    params = {}
    for part in inner.split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            params[k] = v
    return params


def imgcode_format(data: Optional[dict] = None):
    res = '[图片：未识别成功，不应回复；意图：不明；类型：不明]'
    if (
        type(data) is dict
        and 'content' in data
        and 'intent' in data
        and 'type' in data
    ):
        res = (
            f"[图片：{data.get('content', '未识别成功')}"
            f"；意图：{data.get('intent', '不明')}"
            f"；类型：{data.get('type', '不明')}]"
        )
    return res
