import time
import threading

import OlivOSAIChatAssassin


# 公平锁
class FairLock:
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._next_ticket = 0
        self._serving = 0
        self._held = False  # 是否被持有
        self._busy_gate = 2
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

    def isBusy(self):
        with self._lock:
            return self._busy


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
