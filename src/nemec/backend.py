"""Backend de LLM: interface, token de cancelamento e implementação Ollama (streaming)."""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from nemec.errors import BackendError, BackendUnavailable


class CancelToken:
    """Cancelamento cooperativo + gancho para abortar a conexão em andamento."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def is_set(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for cb in callbacks:
            try:
                cb()
            except Exception:  # abortar é melhor-esforço; o flag já interrompe o laço
                pass

    def on_cancel(self, cb: Callable[[], None]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(cb)
                return
        cb()


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    num_ctx: int = 8192
    think: bool = False


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Any


StreamEvent = Delta | ToolCall


class ChatBackend(Protocol):
    def stream(self, request: ChatRequest, cancel: CancelToken) -> Iterator[StreamEvent]: ...


def _abort_connections(client: Any) -> None:
    """Derruba a leitura bloqueada: `shutdown` do socket acorda o `recv` em outra thread (fechar o
    cliente HTTP não acorda). Usa internos do httpx/httpcore, em melhor-esforço; o laço de streaming
    também confere o cancelamento a cada pedaço recebido."""
    import socket

    try:
        for conn in list(client._client._transport._pool.connections):
            sock = conn._connection._network_stream.get_extra_info("socket")
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        client._client.close()
    except Exception:
        pass


class OllamaBackend:
    """Ollama local. Um `Client` por requisição: cancelar fecha só aquela conexão HTTP."""

    def __init__(self, host: str, timeout: float = 300.0) -> None:
        self._host = host
        self._timeout = timeout

    def stream(self, request: ChatRequest, cancel: CancelToken) -> Iterator[StreamEvent]:
        import httpx
        import ollama

        client = ollama.Client(host=self._host, timeout=self._timeout)
        finished = threading.Event()

        def abort_until_finished() -> None:
            # Uma conexão ainda em estabelecimento pode escapar do primeiro abort: repete até o stream terminar.
            while not finished.is_set():
                _abort_connections(client)
                finished.wait(0.05)

        cancel.on_cancel(lambda: threading.Thread(target=abort_until_finished, daemon=True).start())
        try:
            if cancel.is_set():
                return
            kwargs: dict[str, Any] = {
                "model": request.model,
                "messages": request.messages,
                "stream": True,
                "think": request.think,
                "options": {"num_ctx": request.num_ctx},
            }
            if request.tools:
                kwargs["tools"] = request.tools
            # A leitura de rede roda numa thread auxiliar e o consumidor consulta o cancelamento a cada 50 ms:
            # o shutdown do socket nem sempre acorda um `recv` bloqueado (observado no macOS, ~15% das vezes),
            # então o retorno ao chamador nunca depende dele.
            items: queue.Queue[tuple[str, Any]] = queue.Queue()

            def pump() -> None:
                try:
                    for part in client.chat(**kwargs):
                        items.put(("part", part))
                    items.put(("end", None))
                except BaseException as exc:  # noqa: BLE001 — repassado ao consumidor
                    items.put(("err", exc))

            threading.Thread(target=pump, daemon=True, name="ollama-stream").start()
            while True:
                try:
                    kind, value = items.get(timeout=0.05)
                except queue.Empty:
                    if cancel.is_set():
                        return
                    continue
                if cancel.is_set():
                    return
                if kind == "end":
                    break
                if kind == "err":
                    raise value
                msg = value.message
                if msg.content:
                    yield Delta(msg.content)
                for call in msg.tool_calls or ():
                    yield ToolCall(call.function.name, dict(call.function.arguments or {}))
        except (ConnectionError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if cancel.is_set():
                return
            raise BackendUnavailable(
                f"Não foi possível conectar ao Ollama em {self._host}. Verifique se ele está em execução."
            ) from exc
        except ollama.ResponseError as exc:
            if cancel.is_set():
                return
            if exc.status_code == 404:
                raise BackendUnavailable(
                    f"Modelo '{request.model}' não encontrado no Ollama (ollama pull {request.model})."
                ) from exc
            raise BackendError(f"Erro do Ollama: {exc.error}") from exc
        except Exception as exc:
            if cancel.is_set():  # conexão fechada pelo cancelamento
                return
            raise BackendError(f"Falha na geração: {exc}") from exc
        finally:
            finished.set()
            try:
                client._client.close()
            except Exception:
                pass


class EmbedBackend(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    """Embeddings locais (Ollama, `bge-m3`). Devolve um vetor por texto, na mesma ordem."""

    BATCH = 16

    def __init__(self, host: str, model: str, dim: int = 1024, timeout: float = 300.0) -> None:
        self._host, self._model, self._dim, self._timeout = host, model, dim, timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        import math

        import httpx
        import ollama

        out: list[list[float]] = []
        client = ollama.Client(host=self._host, timeout=self._timeout)
        try:
            for i in range(0, len(texts), self.BATCH):
                batch = texts[i : i + self.BATCH]
                resp = client.embed(model=self._model, input=batch)
                vectors = [list(v) for v in resp.embeddings]
                if len(vectors) != len(batch):
                    raise BackendError("O modelo de embeddings devolveu um número inesperado de vetores.")
                for v in vectors:
                    if len(v) != self._dim or not all(math.isfinite(x) for x in v):
                        raise BackendError(f"Embedding inválido (esperado {self._dim} dimensões finitas).")
                out.extend(vectors)
        except (ConnectionError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise BackendUnavailable(f"Não foi possível conectar ao Ollama em {self._host}.") from exc
        except ollama.ResponseError as exc:
            if exc.status_code == 404:
                raise BackendUnavailable(
                    f"Modelo de embeddings '{self._model}' não encontrado (ollama pull {self._model})."
                ) from exc
            raise BackendError(f"Erro do Ollama (embeddings): {exc.error}") from exc
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"Falha ao gerar embeddings: {exc}") from exc
        finally:
            try:
                client._client.close()
            except Exception:
                pass
        return out
