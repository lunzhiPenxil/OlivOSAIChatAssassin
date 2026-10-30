import OlivOS
import OlivOSAIChatAssassin


class Event:
    def init(plugin_event, Proc):
        # 初始化流程
        pass

    def init_after(plugin_event, Proc: OlivOS.pluginAPI.shallow):
        # 初始化后处理流程
        OlivOSAIChatAssassin.activity.clear_all()
        OlivOSAIChatAssassin.interaction.clear_all()
        OlivOSAIChatAssassin.eventInbox.clear_all()
        OlivOSAIChatAssassin.msg.clear_poke_state()
        OlivOSAIChatAssassin.data.gHistoryWindowGuard.clear_all()
        bot_hash_list = list(Proc.Proc_data['bot_info_dict'].keys())
        OlivOSAIChatAssassin.load.load_init_after()
        OlivOSAIChatAssassin.load.load_logger(Proc)
        OlivOSAIChatAssassin.webUI.restore_webui_assets(Proc)
        OlivOSAIChatAssassin.load.load_bot(bot_hash_list)
        OlivOSAIChatAssassin.load.load_staticKnowledge()

    def private_message(plugin_event, Proc):
        # 私聊消息事件入口
        OlivOSAIChatAssassin.eventInbox.receive_private_message(plugin_event, Proc)

    def group_message(plugin_event, Proc):
        OlivOSAIChatAssassin.msg.unity_group_message(plugin_event, Proc)

    def poke(plugin_event, Proc):
        # 戳一戳事件入口
        if OlivOSAIChatAssassin.eventInbox.receive_poke(plugin_event, Proc):
            OlivOSAIChatAssassin.msg.unity_group_poke(plugin_event, Proc)

    def private_message_recall(plugin_event, Proc):
        OlivOSAIChatAssassin.eventInbox.receive_private_message_recall(plugin_event, Proc)

    def group_message_recall(plugin_event, Proc):
        OlivOSAIChatAssassin.eventInbox.receive_group_message_recall(plugin_event, Proc)

    def group_member_increase(plugin_event, Proc):
        OlivOSAIChatAssassin.eventInbox.receive_group_member_increase(plugin_event, Proc)

    def group_member_decrease(plugin_event, Proc):
        OlivOSAIChatAssassin.eventInbox.receive_group_member_decrease(plugin_event, Proc)

    def group_admin(plugin_event, Proc):
        OlivOSAIChatAssassin.eventInbox.receive_group_admin(plugin_event, Proc)

    def group_ban(plugin_event, Proc):
        OlivOSAIChatAssassin.eventInbox.receive_group_ban(plugin_event, Proc)

    def group_file_upload(plugin_event, Proc):
        OlivOSAIChatAssassin.eventInbox.receive_group_file_upload(plugin_event, Proc)

    def save(plugin_event, Proc):
        # 插件卸载时执行的保存流程
        OlivOSAIChatAssassin.activity.clear_all()
        OlivOSAIChatAssassin.interaction.clear_all()
        OlivOSAIChatAssassin.eventInbox.clear_all()
        OlivOSAIChatAssassin.msg.clear_poke_state()
        OlivOSAIChatAssassin.data.gHistoryWindowGuard.clear_all()

    def menu(plugin_event, Proc):
        # 插件菜单事件监听
        if OlivOSAIChatAssassin.webUI.handle_menu(plugin_event, Proc):
            return
        if getattr(plugin_event.data, 'namespace', None) == 'OlivOSAIChatAssassin':
            if plugin_event.data.event == 'OlivOSAIChatAssassin_Menu':
                pass
