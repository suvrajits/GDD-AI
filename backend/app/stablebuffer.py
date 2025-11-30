# backend/app/stablebuffer.py
class StableBuffer:
    def __init__(self):
        self._partial = ""
        self._committed = ""

    def update_partial(self, text: str) -> str:
        # keep latest partial
        self._partial = text or ""
        return self._committed + (" " + self._partial if self._partial else "")

    def commit_final(self, text: str) -> str:
        t = (text or "").strip()
        if t:
            if self._committed:
                self._committed += " " + t
            else:
                self._committed = t
        self._partial = ""
        return self._committed
