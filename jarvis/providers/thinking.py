"""Split inline reasoning tags (<think>, <thinking>, <thought>) out of streamed content.

Local reasoning models (Qwen, DeepSeek-R1, Gemma, ...) and Gemini's
OpenAI-compatible endpoint put their reasoning in the content stream itself.
``ThinkSplitter`` turns the stream into ``("reasoning", text)`` and
``("text", text)`` pieces, even when a tag is cut in half between chunks.

A tag only opens reasoning at the very start of the message (after optional
whitespace), so an answer that merely mentions "<think>" is left alone.
"""

OPEN_TAGS = ("<think>", "<thinking>", "<thought>")


def _partial_suffix(text, target):
    """Length of the longest suffix of *text* that is a proper prefix of *target*."""
    for size in range(min(len(text), len(target) - 1), 0, -1):
        if target.startswith(text[-size:]):
            return size
    return 0


class ThinkSplitter:
    def __init__(self):
        self.mode = "start"  # start -> think -> after -> text
        self.buf = ""
        self.close = ""

    def feed(self, chunk):
        """Consume a content fragment; returns a list of (kind, text) pieces."""
        self.buf += chunk
        out = []
        while True:
            if self.mode == "start":
                stripped = self.buf.lstrip()
                if not stripped:
                    return out
                lowered = stripped.lower()
                tag = next((t for t in OPEN_TAGS if lowered.startswith(t)), None)
                if tag:
                    self.mode = "think"
                    self.close = "</" + tag[1:]
                    self.buf = stripped[len(tag):]
                elif any(t.startswith(lowered) for t in OPEN_TAGS):
                    return out  # could still become a tag: wait for more
                else:
                    self.mode = "text"
            elif self.mode == "think":
                lowered = self.buf.lower()
                at = lowered.find(self.close)
                if at >= 0:
                    if at:
                        out.append(("reasoning", self.buf[:at]))
                    self.buf = self.buf[at + len(self.close):]
                    self.mode = "after"
                    continue
                keep = _partial_suffix(lowered, self.close)
                emit = self.buf[: len(self.buf) - keep]
                if emit:
                    out.append(("reasoning", emit))
                self.buf = self.buf[len(self.buf) - keep:]
                return out
            elif self.mode == "after":
                self.buf = self.buf.lstrip()  # drop the blank lines after </think>
                if not self.buf:
                    return out
                self.mode = "text"
            else:
                if self.buf:
                    out.append(("text", self.buf))
                    self.buf = ""
                return out

    def finish(self):
        """Flush whatever is held back at the end of the stream."""
        out = []
        if self.buf:
            out.append(("reasoning" if self.mode == "think" else "text", self.buf))
        self.buf = ""
        return out


def split_all(content):
    """One-shot split of a complete message: ``(reasoning, text)``."""
    splitter = ThinkSplitter()
    pieces = splitter.feed(content or "") + splitter.finish()
    reasoning = "".join(t for k, t in pieces if k == "reasoning")
    text = "".join(t for k, t in pieces if k == "text")
    return reasoning, text
