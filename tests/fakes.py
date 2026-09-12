from __future__ import annotations

from types import SimpleNamespace


class FakeLLM:
    def __init__(self, answer="Synthetic reply", effect=None):
        self.answer, self.effect, self.calls = answer, effect, []
        self.closed = False

    def reply(self, question, history, **kwargs):
        self.calls.append((question, list(history)))
        if self.effect:
            self.effect()
        return self.answer

    def close(self):
        self.closed = True


class FakeDesktop:
    def __init__(self):
        self.foreground, self.sent, self.warmed = True, [], 0
        self.error = None
        self.pending = []
        self.on_poll = None

    def is_foreground(self):
        return self.foreground

    def send(self, message, answer, *, before_fill=None):
        from bot import SendUncertain
        if self.error and not isinstance(self.error,SendUncertain):
            raise self.error
        if before_fill:
            before_fill()
        if self.error:
            raise self.error
        self.sent.append((message, answer))

    def warmup(self):
        self.warmed += 1

    def poll(self):
        if self.on_poll:
            self.on_poll()
        messages, self.pending = self.pending, []
        return messages


class FakeControl:
    """Synthetic UIA control; no screenshots or real chat content."""
    def __init__(self, name="", kind="Pane", rect=(0, 0, 400, 100), children=(),
                 class_name="mmui::ChatTextItemView", automation_id=""):
        self.name, self.kind, self.box = name, kind, rect
        self.kids, self.cls, self.key = list(children), class_name, automation_id
        self.element_info = SimpleNamespace(control_type=kind)

    def children(self, control_type=None):
        return [c for c in self.kids if control_type is None or c.kind == control_type]

    def window_text(self):
        return self.name

    def rectangle(self):
        return SimpleNamespace(left=self.box[0], top=self.box[1], right=self.box[2], bottom=self.box[3])

    def class_name(self):
        return self.cls

    def automation_id(self):
        return self.key

    def capture_as_image(self):
        raise RuntimeError("No screenshot in synthetic UI tests")
