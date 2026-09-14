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
