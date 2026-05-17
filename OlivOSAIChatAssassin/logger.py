import OlivOSAIChatAssassin


# 日志级别常量
LOG_DEBUG = 0
LOG_INFO = 2
LOG_WARN = 3
LOG_ERROR = 4


def logRaw(level: int, msg: str):
    if OlivOSAIChatAssassin.data.gProc is not None:
        OlivOSAIChatAssassin.data.gProc.log(level, msg, [
            (OlivOSAIChatAssassin.data.gPluginName, 'default')
        ])


def log(msg: str):
    logRaw(LOG_INFO, msg)


def warn(msg: str):
    logRaw(LOG_WARN, msg)
