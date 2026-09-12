"""Exact-row media capture. Runs exclusively on the bot's UI thread.

No bulk history download, screenshots-as-images or cache-directory guessing.
If WeChat does not expose a reliable row/Copy/Convert-to-text action, fail closed.
"""

from pathlib import Path
import io
import time
from media import UnsupportedMedia, inside, safe_name, MIB


def descriptor(row, token, occurrence=0):
    text = row.window_text().strip()
    klass = row.class_name()
    kind = None
    if klass == "mmui::ChatTextItemView":
        return None
    if klass not in (
        "mmui::ChatBubbleReferItemView",
        "mmui::ChatBubbleItemView",
        "mmui::ChatItemView",
    ):
        return None
    if text.startswith(("[图片]", "[Image]", "[Photo]")) or text in (
        "图片",
        "Image",
        "Photo",
    ):
        kind = "image"
    elif text.startswith(("[语音]", "[Voice]", "[Audio]")) or text.startswith(
        ("语音 ", "Voice ")
    ):
        kind = "audio"
    elif text.startswith(("[文件]", "[File]")):
        kind = "document"
    if kind is None:
        return None
    rid = getattr(row.element_info, "runtime_id", None)
    if callable(rid):
        rid = rid()
    return {
        "content_type": kind,
        "token": token,
        "occurrence": occurrence,
        "runtime_id": list(rid or []),
        "name": safe_name(text[:180]),
    }


def exact_row(desktop, message, item):
    from bot import row_token, uia_row_direction, FocusLost

    desktop.require_foreground()
    matches = []
    for session in desktop.sessions():
        if session.name == message.chat and desktop.activate(session) == message.kind:
            matches.append(session)
    if len(matches) != 1:
        raise UnsupportedMedia("附件目标同名或不可见，需人工处理")
    desktop.activate(matches[0])
    if desktop.current() != (message.chat, message.kind):
        raise FocusLost("附件读取前聊天变化")
    rows = [
        r
        for r in desktop.rows()
        if row_token(r.class_name(), r.window_text()) == item["token"]
    ]
    candidates = []
    for row in rows:
        rid = getattr(row.element_info, "runtime_id", None)
        rid = rid() if callable(rid) else rid
        if item.get("runtime_id") and list(rid or []) == item["runtime_id"]:
            candidates.append(row)
    # Never fall back to 'last image' when the original UI row has disappeared.
    if len(candidates) != 1:
        raise UnsupportedMedia("附件消息行已变化，无法可靠关联发送者")
    row = candidates[0]
    if uia_row_direction(row) != "incoming":
        raise UnsupportedMedia("附件方向未核验")
    if message.kind == "group":
        from members import extract_sender
        from permissions import normalize_name

        sender, _, _ = extract_sender(row, [message.sender_name])
        if not sender or normalize_name(sender) != normalize_name(message.sender_name):
            raise UnsupportedMedia("附件群成员核验失败")
    return row


def menu_click(desktop, row, names):
    desktop.require_foreground()
    row.right_click_input()
    time.sleep(0.15)
    # Context menus may be native popup windows of the same process.
    pid = desktop.window.element_info.process_id
    items = []
    for menu in desktop.desktop.windows(control_type="Menu", process=pid):
        items.extend(menu.descendants(control_type="MenuItem"))
    items.extend(desktop.window.descendants(control_type="MenuItem"))
    candidates = {
        tuple(getattr(x.element_info, "runtime_id", []) or [id(x)]): x
        for x in items
        if x.window_text().strip() in names
    }
    if len(candidates) != 1:
        import pyautogui

        pyautogui.press("esc")
        raise UnsupportedMedia("微信没有暴露所需附件菜单")
    list(candidates.values())[0].click_input()
    time.sleep(0.15)


def native_transcript(desktop, row):
    before = {x.window_text().strip() for x in row.descendants(control_type="Text")}
    menu_click(desktop, row, ("转文字", "转换为文字", "Convert to Text", "Transcribe"))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        desktop.require_foreground()
        texts = [x.window_text().strip() for x in row.descendants(control_type="Text")]
        new = [
            t
            for t in texts
            if t
            and t not in before
            and t
            not in (
                "转文字",
                "转换中",
                "转换失败",
                "收起",
                "展开",
                "Transcribing",
                "Collapse",
            )
        ]
        if len(new) == 1 and not new[0].startswith(("[语音]", "[Voice]")):
            return {"transcript": new[0], "name": "wechat-voice.txt"}
        time.sleep(0.25)
    raise UnsupportedMedia("微信语音转文字未返回可核验的结果")


def capture(desktop, message, item):
    import win32clipboard
    import win32con

    row = exact_row(desktop, message, item)
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
        seq = win32clipboard.GetClipboardSequenceNumber()
        menu_click(desktop, row, ("复制", "Copy"))
        if win32clipboard.GetClipboardSequenceNumber() == seq:
            raise UnsupportedMedia("复制没有产生新附件数据")
        win32clipboard.OpenClipboard()
        try:
            paths = (
                list(win32clipboard.GetClipboardData(win32con.CF_HDROP))
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP)
                else []
            )
        finally:
            win32clipboard.CloseClipboard()
        if paths:
            if len(paths) != 1:
                raise UnsupportedMedia("附件数量不匹配")
            cache = desktop.Tools.where_chatfile_folder(open_folder=False)
            if not cache or not isinstance(cache, (str, Path)):
                raise UnsupportedMedia("无法核验微信缓存目录")
            if str(cache).startswith(("\\", "//")):
                raise UnsupportedMedia("不读取网络共享缓存")
            path = inside(Path(cache), Path(paths[0]))
            limit = (10 if item["content_type"] == "image" else 25) * MIB
            if not path.is_file() or path.stat().st_size > limit:
                raise UnsupportedMedia("附件超过大小限制或未下载")
            with path.open("rb") as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise UnsupportedMedia("附件超过大小限制")
            return {"raw": raw, "name": safe_name(path.name)}
        if item["content_type"] == "image":
            from PIL import ImageGrab, Image

            image = ImageGrab.grabclipboard()  # Clipboard only, NEVER screen capture.
            if (
                not isinstance(image, Image.Image)
                or image.width * image.height > 20_000_000
            ):
                raise UnsupportedMedia("无法读取图片或图片超过20MP")
            out = io.BytesIO()
            image.save(out, format="PNG")
            return {"raw": out.getvalue(), "name": "image.png"}
        raise UnsupportedMedia("微信未复制出可校验的原始文件")
    except Exception:
        if item["content_type"] == "audio":
            return native_transcript(desktop, exact_row(desktop, message, item))
        raise
