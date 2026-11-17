# Testes e verificações

## Como executar

```bash
make db-up migrate   # os testes precisam do PostgreSQL e do Ollama locais
make test            # pytest (QT_QPA_PLATFORM=offscreen)
make doctor          # diagnóstico de instalação (não é um teste automatizado)
```

Os testes criam um banco temporário (`nemec_test_*`) migrado do zero **com os papéis reais** (`nemec_owner`, `nemec_app`) e o removem ao final; o banco principal não é tocado. Se o PostgreSQL estiver indisponível o teste **falha** com mensagem explícita (nunca é pulado).

## Suíte automatizada (305 testes)

| Arquivo | Testes | Foco |
|---|---:|---|
| `test_db.py` | 27 | Migrações do zero, constraints, papéis e privilégios (`nemec_app` sem DDL), alocação concorrente de `seq` |
| `test_chat.py` | 14 | Turno de chat, estados da mensagem, cancelamento, falha do Ollama |
| `test_memory.py` | 41 | Memória automática: criação, atualização, duplicidade, atomicidade, painel |
| `test_memory_policy.py` | 15 | Política lexical (literalidade, atribuição, sensibilidade) |
| `test_documents.py` | 41 | Importação PDF/Markdown, chunking, pastas, recuperação híbrida, citações, exclusão |
| `test_tasks.py` | 22 | Proposta, confirmação atômica e idempotente, rejeição, concorrência |
| `test_security.py` | 33 | Registro com exatamente duas ferramentas, chamadas inválidas sem efeito, injeção sem efeito além do permitido, destinos só-localhost, ausência de módulos de rede/telemetria |
| `test_audit_m2.py` | 54 | Regressões dos achados A01–A06 da auditoria da M2 (origem da memória, limites de pasta, citações e orçamento de prompt) |
| `test_m2_ajustes.py` | 24 | Catálogo da biblioteca, documento hostil na visão geral, fixar conversas, importar/cancelar pasta, feedback de exclusão |
| `test_ui.py` | 26 | Interface com `pytest-qt` (offscreen): painéis, cartão de proposta, fontes, erro do Ollama |
| `test_real_ollama.py` | 8 | **Ollama real** (`qwen3.5:9b`, `bge-m3`): chat, embeddings, RAG, texto longo × memória |

Os testes com `real_ollama` exigem os dois modelos instalados e dependem do comportamento do modelo local; são testes de integração, não de qualidade de modelo.

## Evidências registradas (M3)

| Verificação | Tipo | Resultado |
|---|---|---|
| Instalação limpa (`make setup`, `db-up`, `migrate`, `doctor`) em cópia isolada, contêiner e volume próprios | Instalação | Aprovada |
| `make test` na cópia limpa | Automatizado | 305 aprovados |
| Privilégios: `nemec_app` só DML; sem `CREATE` em `nemec`/`public` | Instalação | Aprovada |
| Inicialização offscreen com configuração válida | Instalação | Aprovada |
| Falhas legíveis: sem `.env`, Ollama remoto, banco parado, banco sem migrar, modelo ausente, Ollama fora | Instalação (`make doctor` e `app.main` com `QMessageBox` interceptado) | Aprovadas |
| Regressão final no repositório de trabalho | Automatizado | Mesma contagem (305) |

**Não verificado:** instalação em outra máquina ou em Linux; CI; qualidade das respostas do modelo (não há avaliação de qualidade); auditoria independente do snapshot final da M2 e desta versão (a M2 teve uma auditoria independente em rodadas anteriores, cujos achados A01–A06 viraram `test_audit_m2.py`; as correções posteriores e a M3 não foram reauditadas) (os 305 testes são execução do próprio desenvolvedor).
