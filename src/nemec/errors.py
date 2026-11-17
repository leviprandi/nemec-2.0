"""Exceções do domínio."""


class NemecError(Exception):
    """Base."""


class ConfigError(NemecError):
    """Configuração inválida (inclui destino de rede que não é localhost — G9)."""


class ValidationFailed(NemecError):
    """Entrada do usuário inválida."""


class NotFound(NemecError):
    """Registro inexistente."""


class ProposalStateError(NemecError):
    """Operação incompatível com o estado atual da proposta."""


class ProposalExecutionError(NemecError):
    """A execução da proposta falhou; a proposta ficou `failed` e nada parcial foi gravado."""


class BackendUnavailable(NemecError):
    """Ollama indisponível (não está em execução, recusou conexão ou modelo ausente)."""


class BackendError(NemecError):
    """Falha durante a geração."""
