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

    def wait_for_competitor(self, timeout: float) -> bool:
        try:
            timeout = max(0.0, float(timeout))
        except (TypeError, ValueError):
            timeout = 0.0
        with self._lock:
            if not self._held:
                raise RuntimeError("wait for competitor on unlocked lock")
            deadline = time.perf_counter() + timeout
            while self._count <= 1:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return False
                self._cond_slack.wait(timeout=remaining)
            self._slack_count *= 2
            return True

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


def invalidate_peak_up_cache(bot_hash: 'str|None' = None) -> None:
    """让记忆编辑后的检索缓存立即失效。"""
    if bot_hash is None:
        OlivOSAIChatAssassin.data.gPeakUpCache.clear()
        return
    OlivOSAIChatAssassin.data.gPeakUpCache.pop(str(bot_hash), None)


def iter_memory_strings(value):
    """递归遍历 WebUI 可能保存的字符串、数组和对象记忆值。"""
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_memory_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_memory_strings(item)


def peak_up_recommendMatch(
    target: str, dictMap: dict, dictName: str, ageing: int,
    rate: float = 1.0,
    matchedList: 'list|None' = None,
    father: 'str|None' = None,
    *,
    bot_hash: 'str|None' = None,
    top_k: int = 0,
):
    timestamp = int(time.perf_counter())
    res = {}
    matchedList_this = matchedList if type(matchedList) is list else []
    matched_set = {str(item) for item in matchedList_this}
    try:
        top_k = max(0, int(top_k))
    except (TypeError, ValueError):
        top_k = 0
    bot_key = str(bot_hash) if bot_hash is not None else ''
    bot_cache = OlivOSAIChatAssassin.data.gPeakUpCache.setdefault(bot_key, {})
    named_cache = bot_cache.setdefault(dictName, {})
    for cache_key in list(named_cache.keys()):
        cached_at = named_cache.get(cache_key, {}).get('timestamp', 0)
        if timestamp - cached_at >= ageing:
            named_cache.pop(cache_key, None)

    if type(dictMap) is not dict:
        return res

    cache_key = (str(target), float(rate), father or '')
    cache_entry = named_cache.get(cache_key)
    if isinstance(cache_entry, dict) and isinstance(cache_entry.get('matches'), list):
        matches = list(cache_entry['matches'])
        cached = True
    else:
        matches = []
        for key in dictMap:
            rank = OlivOSAIChatAssassin.tools.get_recommendRank(key, target, rate=rate)
            if OlivOSAIChatAssassin.tools.get_recommendMatch(rank):
                matches.append({'key': key, 'rank': rank})
        matches.sort(key=lambda item: (item['rank'], str(item['key'])))
        named_cache[cache_key] = {
            'timestamp': timestamp,
            'matches': matches,
        }
        cached = False

    selected = []
    for item in matches:
        key = item.get('key')
        if key in dictMap and str(key) not in matched_set:
            selected.append(key)
        if top_k > 0 and len(selected) >= top_k:
            break

    for key in selected:
        key_text = key if father is None else f'{father} -> {key}'
        suffix = ' (cached)' if cached else f" ({next(item['rank'] for item in matches if item['key'] == key)})"
        OlivOSAIChatAssassin.logger.log(f'PEAK UP - [{dictName}] {key_text}{suffix}')
        res[key] = dictMap[key]
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


def imgcode_format(data: Optional[dict] = None) -> str:
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


def set_think(bot_hash: str, group_id: str) -> None:
    OlivOSAIChatAssassin.data.gThinkTS.setdefault(bot_hash, {})
    OlivOSAIChatAssassin.data.gThinkTS[bot_hash][group_id] = time.perf_counter()


def get_think(bot_hash: str, group_id: str) -> bool:
    ts_now: float = time.perf_counter()
    ts_load: float = OlivOSAIChatAssassin.data.gThinkTS.get(bot_hash, {}).get(group_id, 0.0)
    ts_pass = ts_now - ts_load
    first_thinking_cooldown = float(
        OlivOSAIChatAssassin.data.gData.getConfig(bot_hash).get(
            'first_thinking_cooldown',
            OlivOSAIChatAssassin.data.configDefault['first_thinking_cooldown']
        )
    )
    res: bool = ts_pass > first_thinking_cooldown
    OlivOSAIChatAssassin.logger.log(f"FIRST THINK - {str(res).upper()} [{ts_pass:.1f}/{first_thinking_cooldown:.1f}]")
    return res
