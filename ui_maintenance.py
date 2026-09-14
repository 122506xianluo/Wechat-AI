"""Explicit stop-only UI work, serialized on one dedicated thread."""

from concurrent.futures import ThreadPoolExecutor


def read_group_roster(name):
    def work():
        import pythoncom

        pythoncom.CoInitialize()
        try:
            from pyweixin import Contacts

            names = Contacts.get_groupMembers_info(name, close_weixin=False)
            if not isinstance(names, list) or any(
                not isinstance(n, str) for n in names
            ):
                raise ValueError("当前上游群成员返回格式不兼容，未导入")
            return names
        finally:
            pythoncom.CoUninitialize()

    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="wechat-maintenance"
    ) as pool:
        return pool.submit(work).result()


def read_current_group():
    """Read only: never activate a conversation, type, or send from the panel."""
    def work():
        import pythoncom
        pythoncom.CoInitialize()
        try:
            from bot import Config, WeChatDesktop
            desktop = WeChatDesktop(Config())
            name, kind = desktop.current()
            if kind != 'group' or not name.strip() or desktop._edit(required=False) is None:
                raise ValueError("请先在微信打开目标群聊，再回控制台读取群名")
            # Do not accidentally import a half-loaded title or a switched chat.
            if desktop.current() != (name, kind):
                raise ValueError("微信正在切换会话，请稍后重新读取")
            return name
        finally:
            pythoncom.CoUninitialize()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='wechat-read-group') as pool:
        return pool.submit(work).result()
