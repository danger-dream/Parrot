"""Fallback splitter for models that wrap thinking in XML tags."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FilterResult:
    content: str = ""
    reasoning: str = ""


class ThinkingTagFilter:
    def __init__(self) -> None:
        self._buffer = ""
        self._in_thinking = False

    def process(self, text: str) -> FilterResult:
        self._buffer += text
        content: list[str] = []
        reasoning: list[str] = []
        while True:
            if self._in_thinking:
                end = self._buffer.find("</thinking>")
                if end < 0:
                    reasoning.append(self._buffer)
                    self._buffer = ""
                    break
                reasoning.append(self._buffer[:end])
                self._buffer = self._buffer[end + len("</thinking>") :]
                self._in_thinking = False
                continue
            start = self._buffer.find("<thinking>")
            if start < 0:
                content.append(self._buffer)
                self._buffer = ""
                break
            content.append(self._buffer[:start])
            self._buffer = self._buffer[start + len("<thinking>") :]
            self._in_thinking = True
        return FilterResult(content="".join(content), reasoning="".join(reasoning))

    def flush(self) -> FilterResult:
        leftover = self._buffer
        self._buffer = ""
        if self._in_thinking:
            self._in_thinking = False
            return FilterResult(reasoning=leftover)
        return FilterResult(content=leftover)
