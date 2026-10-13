"""Minimal, lossless S-expression reader/writer for KiCad files.

Atoms are kept as their raw source text (quoted strings keep their quotes and
escapes), so a parse -> dump round trip never changes token content.
"""
from __future__ import annotations

from typing import List, Union

Node = Union[str, List["Node"]]


def parse(text: str) -> Node:
    """Parse the first S-expression found in *text*."""
    stack: list[list] = []
    root: list | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            new: list = []
            if stack:
                stack[-1].append(new)
            stack.append(new)
            i += 1
        elif c == ")":
            if not stack:
                raise ValueError("Unbalanced ')' in S-expression")
            done = stack.pop()
            i += 1
            if not stack:
                root = done
                break
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            stack[-1].append(text[i : j + 1])
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            if stack:
                stack[-1].append(text[i:j])
            i = j
    if root is None:
        raise ValueError("No complete S-expression found")
    return root


def quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def unquote(atom: str) -> str:
    if len(atom) >= 2 and atom[0] == '"' and atom[-1] == '"':
        out, i, body = [], 0, atom[1:-1]
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                i += 2
            else:
                out.append(body[i])
                i += 1
        return "".join(out)
    return atom


def _is_short(node: list) -> bool:
    return all(isinstance(x, str) for x in node) or len(node) <= 2 and all(
        isinstance(x, str) or _is_short(x) for x in node
    )


def dump(node: Node, indent: int = 0) -> str:
    """Serialise with KiCad-like formatting (tab indents, short lists inline)."""
    if isinstance(node, str):
        return node
    if _is_short(node):
        return "(" + " ".join(dump(x) for x in node) + ")"
    pad = "\t" * (indent + 1)
    head: list[str] = []
    rest: list[Node] = []
    for x in node:
        if isinstance(x, str) and not rest:
            head.append(x)
        else:
            rest.append(x)
    parts = ["(" + " ".join(head)]
    for x in rest:
        parts.append("\n" + pad + dump(x, indent + 1))
    parts.append("\n" + "\t" * indent + ")")
    return "".join(parts)


def dump_inline(node: Node) -> str:
    if isinstance(node, str):
        return node
    return "(" + " ".join(dump_inline(x) for x in node) + ")"


def head(node: Node) -> str | None:
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else None


def children(node: list, name: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and head(c) == name]


def child(node: list, name: str) -> list | None:
    for c in node:
        if isinstance(c, list) and head(c) == name:
            return c
    return None
