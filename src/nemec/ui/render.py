"""Renderização segura do texto das mensagens. Todo texto é escapado primeiro: nada do modelo vira HTML
arbitrário, link ou recurso remoto. Só um subconjunto mínimo de Markdown (código, negrito, listas) é convertido,
sobre o texto já escapado."""
from __future__ import annotations

import html
import re

_FENCE = re.compile(r"```[^\n`]*\n?(.*?)(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_P = "margin:0 0 7px 0; line-height:145%;"
_CODE = "font-family:Menlo,Monaco,monospace; background-color:rgba(128,128,128,0.22);"


_CITE_LINK = "<a href='cite:{n}' style='color:#6b82f5; text-decoration:none;'>[{n}]</a>"
_CITE_MARK = re.compile(r"\[(\d{1,3})\]")


def _inline(escaped: str, cited: frozenset[int] = frozenset()) -> str:
    escaped = _INLINE_CODE.sub(lambda m: f"<code style='{_CODE}'>{m.group(1)}</code>", escaped)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    if cited:  # só `[n]` com citação gravada vira link (esquema `cite:` gerado aqui, nunca vindo do modelo)
        escaped = _CITE_MARK.sub(lambda m: _CITE_LINK.format(n=m.group(1)) if int(m.group(1)) in cited else m.group(0), escaped)
    return escaped


def _paragraphs(escaped: str, cited: frozenset[int] = frozenset()) -> str:
    out: list[str] = []
    for block in re.split(r"\n{2,}", escaped.strip("\n")):
        lines = block.split("\n")
        if all(_BULLET.match(line) for line in lines if line.strip()):
            items = "<br>".join("&nbsp;•&nbsp; " + _inline(_BULLET.match(line).group(1), cited) for line in lines if line.strip())
            out.append(f"<p style='{_P}'>{items}</p>")
        elif block.strip():
            out.append(f"<p style='{_P}'>{_inline('<br>'.join(lines), cited)}</p>")
    return "".join(out)


def message_html(text: str, cited: frozenset[int] = frozenset()) -> str:
    escaped = html.escape(text, quote=True)
    parts: list[str] = []
    pos = 0
    for m in _FENCE.finditer(escaped):
        parts.append(_paragraphs(escaped[pos:m.start()], cited))
        code = m.group(1).rstrip("\n")
        parts.append(
            "<table width='100%' cellspacing='0' cellpadding='8' style='margin:0 0 7px 0;'><tr>"
            f"<td style='background-color:rgba(128,128,128,0.16);'><pre style='margin:0; font-family:Menlo,Monaco,monospace;'>{code}</pre></td></tr></table>")
        pos = m.end()
    parts.append(_paragraphs(escaped[pos:], cited))
    return "".join(parts)
