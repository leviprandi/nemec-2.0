"""Registro de ferramentas do LLM (G1) e validação de chamadas (G2).

O registro é uma constante: o LLM só conhece estas duas ferramentas. `save_memory` é a memória
automática (sem confirmação individual); `create_task` só gera uma proposta pendente e exige confirmação
humana. Não há ferramenta de SQL, shell, rede ou arquivo.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, field_validator
from typing_extensions import Annotated

MEMORY_MAX_CHARS = 2000
TASK_TITLE_MAX = 200
TASK_NOTES_MAX = 2000


class SaveMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MEMORY_MAX_CHARS)]
    # Referência (ex.: "m2") a uma memória listada no prompt que esta substitui/atualiza.
    replaces: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^m\d{1,4}$")] | None = None


class CreateTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TASK_TITLE_MAX)]
    notes: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TASK_NOTES_MAX)] | None = None
    # ISO 8601 (ex.: 2026-11-20T15:00:00-03:00). Sem fuso, vale o fuso local da máquina.
    due_at: str | None = None

    @field_validator("due_at")
    @classmethod
    def _iso(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            datetime.fromisoformat(v.strip())
        except ValueError as exc:
            raise ValueError("due_at deve estar em ISO 8601") from exc
        return v.strip()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: str  # valor de action_proposals.kind
    description: str
    args_model: type[BaseModel]


_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="save_memory",
        kind="create_memory",
        description=(
            "Guarda (ou atualiza) uma memória de longo prazo sobre o usuário: fato ou preferência duradoura que "
            "ELE mesmo afirmou na mensagem atual. `content`: o trecho EXATO da mensagem atual do usuário com a afirmação, copiado "
            "literalmente (sem reescrever). `replaces`: referência "
            "(ex.: m2) de uma memória já listada que esta corrige ou atualiza; omita se for um fato novo."
        ),
        args_model=SaveMemoryArgs,
    ),
    ToolSpec(
        name="create_task",
        kind="create_task",
        description=(
            "Propõe uma tarefa para o usuário. Use SOMENTE quando o USUÁRIO pedir explicitamente para criar/anotar uma "
            "tarefa, lembrete ou pendência. O usuário ainda precisa confirmar. `title`: curto e claro. `notes`: opcional. "
            "`due_at`: prazo em ISO 8601, só se o usuário informou."
        ),
        args_model=CreateTaskArgs,
    ),
)

TOOL_REGISTRY: Mapping[str, ToolSpec] = MappingProxyType({t.name: t for t in _TOOLS})
KIND_TO_SPEC: Mapping[str, ToolSpec] = MappingProxyType({t.kind: t for t in _TOOLS})


class ToolCallRejected(ValueError):
    """Nome desconhecido ou argumentos inválidos: nenhuma proposta é criada."""


@dataclass(frozen=True)
class ValidatedCall:
    spec: ToolSpec
    arguments: dict[str, Any]


def ollama_tool_schemas(names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Esquema enviado ao modelo (derivado do registro); `names` restringe quais são oferecidas."""
    out = []
    for t in _TOOLS:
        if names is not None and t.name not in names:
            continue
        schema = t.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        out.append({"type": "function", "function": {"name": t.name, "description": t.description, "parameters": schema}})
    return out


def validate_arguments(spec: ToolSpec, arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolCallRejected(f"argumentos não são JSON: {exc}") from exc
    if not isinstance(arguments, Mapping):
        raise ToolCallRejected("argumentos devem ser um objeto")
    try:
        return spec.args_model.model_validate(dict(arguments)).model_dump()
    except ValidationError as exc:
        raise ToolCallRejected(f"argumentos inválidos para {spec.name}: {exc.error_count()} erro(s)") from exc


def validate_tool_call(name: str, arguments: Any) -> ValidatedCall:
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise ToolCallRejected(f"ferramenta desconhecida: {name!r}")
    return ValidatedCall(spec=spec, arguments=validate_arguments(spec, arguments))
