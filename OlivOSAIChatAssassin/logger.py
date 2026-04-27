import OlivOSAIChatAssassin


def logRaw(level: int, msg: str):
    if OlivOSAIChatAssassin.data.gProc is not None:
        OlivOSAIChatAssassin.data.gProc.log(level, msg, [
            (OlivOSAIChatAssassin.data.gPluginName, 'default')
        ])


def log(msg: str):
    logRaw(2, msg)


def warn(msg: str):
    logRaw(3, msg)
