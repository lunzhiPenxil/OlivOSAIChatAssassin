from collections import deque

import OlivOSAIChatAssassin


class Event:
    def init(plugin_event, Proc):
        # 初始化流程
        pass

    def init_after(plugin_event, Proc):
        # 初始化后处理流程
        OlivOSAIChatAssassin.data.gProc = Proc
        OlivOSAIChatAssassin.load.load_config()
        OlivOSAIChatAssassin.load.load_staticKnowledge()
        OlivOSAIChatAssassin.load.load_memory()
        # 初始化消息历史
        OlivOSAIChatAssassin.data.gMessageHistory = {}
        # 如果配置中启用了群组，初始化对应的历史队列
        if OlivOSAIChatAssassin.data.gConfig and 'enabled_groups' in OlivOSAIChatAssassin.data.gConfig:
            for group_id in OlivOSAIChatAssassin.data.gConfig['enabled_groups']:
                OlivOSAIChatAssassin.data.gMessageHistory[group_id] = deque(
                    maxlen=OlivOSAIChatAssassin.data.gConfig.get(
                        'history_size', OlivOSAIChatAssassin.data.configDefault['history_size']
                    )
                )

    def private_message(plugin_event, Proc):
        # 私聊消息事件入口
        pass  # 本插件仅处理群聊

    def group_message(plugin_event, Proc):
        # 群消息事件入口
        group_id = str(plugin_event.data.group_id)
        OlivOSAIChatAssassin.data.gGroupLock.setdefault(group_id, OlivOSAIChatAssassin.tools.FairLock())
        missed = OlivOSAIChatAssassin.data.gGroupLock[group_id].locked()
        with OlivOSAIChatAssassin.data.gGroupLock[group_id]:
            if (
                OlivOSAIChatAssassin.data.gGroupLock[group_id].isBusy()
                and OlivOSAIChatAssassin.data.gGroupLock[group_id].isLast()
            ):
                missed = False
            OlivOSAIChatAssassin.msg.unity_group_message(plugin_event, Proc, missed)

    def poke(plugin_event, Proc):
        # 戳一戳事件入口
        pass

    def save(plugin_event, Proc):
        # 插件卸载时执行的保存流程
        pass

    def menu(plugin_event, Proc):
        # 插件菜单事件监听
        if plugin_event.data.namespace == 'OlivOSAIChatAssassin':
            if plugin_event.data.event == 'OlivOSAIChatAssassin_Menu_Config':
                OlivOSAIChatAssassin.logger.log('配置：请编辑插件数据目录下的config.json文件，并重启插件。')
            elif plugin_event.data.event == 'OlivOSAIChatAssassin_Menu_Status':
                status = OlivOSAIChatAssassin.msg.get_status()
                OlivOSAIChatAssassin.logger.log(status)
