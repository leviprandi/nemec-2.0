"""Backend de LLM falso, controlável pelos testes."""
from __future__ import annotations

import threading
from collections.abc import Iterator

from nemec.backend import CancelToken, ChatRequest, Delta, StreamEvent, ToolCall
from nemec.errors import BackendError, BackendUnavailable


class ScriptedBackend:
    def __init__(self, events: list[StreamEvent | Exception]) -> None:
        self.events = events
        self.requests: list[ChatRequest] = []

    def stream(self, request: ChatRequest, cancel: CancelToken) -> Iterator[StreamEvent]:
        self.requests.append(request)
        for ev in self.events:
            if isinstance(ev, Exception):
                raise ev
            yield ev


class SequenceBackend:
    """Uma lista de eventos por chamada a `stream` (1ª geração, depois a continuação após a ferramenta)."""

    def __init__(self, *rounds: list[StreamEvent | Exception]) -> None:
        self.rounds = list(rounds)
        self.requests: list[ChatRequest] = []

    def stream(self, request: ChatRequest, cancel: CancelToken) -> Iterator[StreamEvent]:
        self.requests.append(request)
        events = self.rounds.pop(0) if self.rounds else []
        for ev in events:
            if isinstance(ev, Exception):
                raise ev
            yield ev


class GatedBackend:
    """Emite `first`, sinaliza `started` e espera o cancelamento (ou `release`)."""

    def __init__(self, first: str = "texto parcial") -> None:
        self.first = first
        self.started = threading.Event()
        self.release = threading.Event()

    def stream(self, request: ChatRequest, cancel: CancelToken) -> Iterator[StreamEvent]:
        yield Delta(self.first)
        self.started.set()
        while not cancel.is_set() and not self.release.wait(0.01):
            pass
        yield Delta(" que nunca deve ser gravado")


class HashEmbedder:
    """Embedding falso e determinístico: saco de palavras em 1024 dimensões (textos com palavras em comum ficam
    próximos). `calls` registra os lotes; `gate` (opcional) bloqueia até ser liberado, para testes de concorrência."""

    DIM = 1024

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.fail: Exception | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib
        import math
        import re

        self.calls.append(list(texts))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail is not None:
            raise self.fail
        out = []
        for t in texts:
            v = [0.0] * self.DIM
            for w in re.findall(r"\w{3,}", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


def deltas(*texts: str) -> list[StreamEvent]:
    return [Delta(t) for t in texts]


__all__ = ["HashEmbedder", "ScriptedBackend", "SequenceBackend", "GatedBackend", "deltas", "Delta", "ToolCall", "BackendError", "BackendUnavailable"]
