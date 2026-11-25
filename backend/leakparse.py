"""
When the model WRITES the display call as text instead of calling the tool, it uses one of several shapes. JSON ("[[show_ui:[{...}]]", a ```json fence) is read by rag_engine; this reads the
others, found by testing in French, Russian and Spanish answers:
    show_ui(blocks: [ location(title: "...", text: "..."), callout(tone: "tip", text: "...") ])
    show_ui(type="compare", title="...", items=[{"label": "...", "cells": ["..."]}])
It is a tolerant reader of calls with named arguments (name: value or name=value), strings, numbers, lists, objects and nested calls (a nested call becomes a block whose
"type" is its name). Whatever does not parse gives []. The result is then validated like any other cards.
"""
import re
from typing import Any, Dict, List, Optional

_ID = re.compile(r"[A-Za-z_][\w\-]*")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


class _Bad(Exception):
    pass


class _P:
    def __init__(self, s: str, i: int = 0):
        self.s, self.i = s, i

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n,":
            self.i += 1

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def string(self) -> str:
        q = self.s[self.i]; self.i += 1; out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < len(self.s):
                n = self.s[self.i + 1]; out.append({"n": "\n", "t": "\t"}.get(n, n)); self.i += 2; continue
            if c == q:
                self.i += 1
                return "".join(out)
            out.append(c); self.i += 1
        raise _Bad("unterminated string")

    def value(self) -> Any:
        c = self.peek()
        if c in ("\"", "'"):
            return self.string()
        if c == "[":
            self.i += 1; arr: List[Any] = []
            while True:
                if self.peek() == "]":
                    self.i += 1
                    return arr
                if not self.peek():
                    raise _Bad("unterminated list")
                arr.append(self.value())
        if c == "{":
            self.i += 1; obj: Dict[str, Any] = {}
            while True:
                if self.peek() == "}":
                    self.i += 1
                    return obj
                k = self.key(); self.sep(); obj[k] = self.value()
        m = _NUM.match(self.s, self.i)
        if m and (self.i + len(m.group(0)) >= len(self.s) or not self.s[self.i + len(m.group(0))].isalpha()):
            self.i = m.end(); t = m.group(0)
            return float(t) if "." in t else int(t)
        m = _ID.match(self.s, self.i)
        if m:
            name = m.group(0); self.i = m.end()
            if self.peek() == "(":
                self.i += 1; args = self.args(); args.setdefault("type", name)
                return args
            return {"true": True, "false": False, "null": None, "none": None}.get(name.lower(), name)
        raise _Bad("unexpected " + repr(c))

    def key(self) -> str:
        c = self.peek()
        if c in ("\"", "'"):
            return self.string()
        m = _ID.match(self.s, self.i)
        if not m:
            raise _Bad("key expected")
        self.i = m.end()
        return m.group(0)

    def sep(self) -> None:
        if self.peek() in (":", "="):
            self.i += 1
        else:
            raise _Bad("':' or '=' expected")

    def args(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        while True:
            c = self.peek()
            if c == ")":
                self.i += 1
                return d
            if not c:
                raise _Bad("unterminated call")
            k = self.key(); self.sep(); d[k] = self.value()


def parse_call(text: str) -> List[Dict[str, Any]]:
    """The blocks inside a `show_ui(...)` call written as text, or []."""
    try:
        m = re.search(r"show_ui\s*\(", text)
        if not m:
            return []
        p = _P(text, m.end()); args = p.args()
        blocks: Any = args.get("blocks")
        if blocks is None and args.get("type"):
            blocks = [args]
        if isinstance(blocks, dict):
            blocks = [blocks]
        return [b for b in (blocks or []) if isinstance(b, dict) and b.get("type")]
    except (_Bad, RecursionError, IndexError, ValueError):
        return []
