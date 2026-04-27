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
        self._busy_gate = 3
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

    def isBusy(self, gate: int = 3):
        with self._lock:
            return self._busy


def sleep(sleep_time: float):
    OlivOSAIChatAssassin.logger.log(f"WAIT - {sleep_time:.2f} s")
    time.sleep(sleep_time)


def get_recommendRank(word1_in: str, word2_in: str, gate_rank: int = 1000, rate: float = 0.1):
    iRank = 1
    find_flag = 1
    word1 = word1_in.lower()
    word2 = word2_in.lower()
    if not word1 or not word2:
        return gate_rank + 1  # 返回一个高数值，表示完全不匹配，因为有些键值可能因为误设置从而为空字符串
    # word1 为短字符串，此场景不进行对调
    # if len(word1) > len(word2):
    #     [word1, word2] = [word2, word1]
    if len(word1) > len(word2):
        return gate_rank + 2
    word1_len = len(word1)
    word2_len = len(word2)

    if word2.find(word1) != -1:
        find_flag = 0

    # LCS
    dp1 = []
    dp1_first = [0]
    for word1_this in word1:
        dp1_first.append(0)
    dp1.append(dp1_first)
    for word2_this in word2:
        dp1.append([0] + [0] * word1_len)
    tmp_i_list = range(1, word1_len + 1)
    tmp_j_list = range(1, word2_len + 1)
    for i in tmp_i_list:
        for j in tmp_j_list:
            if word1[i - 1] == word2[j - 1]:
                dp1[j][i] = dp1[j - 1][i - 1] + 1
            else:
                dp1[j][i] = max(dp1[j - 1][i], dp1[j][i - 1])
    iRank_1 = dp1[word2_len][word1_len]

    # minDistance
    dp2 = []
    dp2_first = [0]
    tmp_counter = 1
    for word1_this in word1:
        dp2_first.append(tmp_counter)
        tmp_counter += 1
    dp2.append(dp2_first)
    tmp_counter = 1
    for word2_this in word2:
        dp2.append([tmp_counter] + [0] * word1_len)
        tmp_counter += 1
    tmp_i_list = range(1, word1_len + 1)
    tmp_j_list = range(1, word2_len + 1)
    for i in tmp_i_list:
        for j in tmp_j_list:
            if word1[i - 1] == word2[j - 1]:
                dp2[j][i] = dp2[j - 1][i - 1]
            else:
                dp2[j][i] = min(dp2[j - 1][i - 1], min(dp2[j - 1][i], dp2[j][i - 1])) + 1
    iRank_2 = dp2[word2_len][word1_len]

    iRank = (find_flag) * (word2_len * (word1_len - iRank_1) + iRank_2 + 1)
    iRank = int(int((iRank * iRank) / word1_len) / word2_len)

    if iRank >= int(word1_len * word2_len * rate):
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
