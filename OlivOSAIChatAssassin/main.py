import OlivOS
import OlivOSAIChatAssassin


class Event:
    def init(plugin_event, Proc):
        # 初始化流程
        pass

    def init_after(plugin_event, Proc: OlivOS.pluginAPI.shallow):
        # 初始化后处理流程
        bot_hash_list = list(Proc.Proc_data['bot_info_dict'].keys())
        OlivOSAIChatAssassin.load.load_init_after()
        OlivOSAIChatAssassin.load.load_logger(Proc)
        OlivOSAIChatAssassin.webuiAPI.restore_webui_assets(Proc)
        OlivOSAIChatAssassin.load.load_bot(bot_hash_list)
        OlivOSAIChatAssassin.load.load_staticKnowledge()

    def private_message(plugin_event, Proc):
        # 私聊消息事件入口
        pass  # 本插件仅处理群聊

    def group_message(plugin_event, Proc):
        OlivOSAIChatAssassin.msg.unity_group_message(plugin_event, Proc)

    def poke(plugin_event, Proc):
        # 戳一戳事件入口
        pass

    def save(plugin_event, Proc):
        # 插件卸载时执行的保存流程
        pass

    def menu(plugin_event, Proc):
        # 插件菜单事件监听
        if OlivOSAIChatAssassin.webuiAPI.handle_menu(plugin_event, Proc):
            return
        if getattr(plugin_event.data, 'namespace', None) == 'OlivOSAIChatAssassin':
            if plugin_event.data.event == 'OlivOSAIChatAssassin_Menu':
                pass
