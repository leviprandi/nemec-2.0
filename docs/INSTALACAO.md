# Instalação e execução

Verificada em macOS (Apple Silicon) a partir de uma cópia limpa do repositório, com banco e `.venv` novos (ver [`TESTES.md`](TESTES.md)). Linux não foi verificado.

## Requisitos

| Item | Versão / observação |
|---|---|
| Python | **3.12** (`pyproject.toml` fixa `>=3.12,<3.13`); `brew install python@3.12` |
| Docker | Compose v2; fornece o PostgreSQL 17 + pgvector (`pgvector/pgvector:0.8.1-pg17`) |
| Ollama | Em execução em `127.0.0.1:11434` |
| Modelos | `ollama pull qwen3.5:9b` (~7 GB em memória) e `ollama pull bge-m3` (~1,2 GB). **O Nemec não baixa modelos.** |
| Memória | ~24 GB de RAM é a configuração em que foi desenvolvido; o modelo de chat é configurável (`NEMEC_CHAT_MODEL`) |

## Passo a passo

```bash
make setup     # cria .venv (Python 3.12), instala dependências, gera .env com senhas aleatórias (chmod 600)
make db-up     # sobe o PostgreSQL só em 127.0.0.1:5433 e espera ficar saudável
make migrate   # migrações Alembic (como nemec_owner)
make doctor    # diagnóstico somente leitura: Python, .env, banco, pgvector, migrações, Ollama e modelos
make run       # abre a interface
```

`make doctor` aponta o que falta e o comando para corrigir. Se `make migrate` já foi executado antes, ele é idempotente.

## Configuração (`.env`)

O `.env` é criado a partir de `.env.example`; **nunca é versionado**. Variáveis:

| Variável | Padrão | Uso |
|---|---|---|
| `NEMEC_DB_PORT` | `5433` | Porta no host (sempre ligada a 127.0.0.1) |
| `NEMEC_POSTGRES_PASSWORD` | gerada | Superusuário do contêiner (só para criar papéis e migrar) |
| `NEMEC_APP_PASSWORD` | gerada | Senha do papel `nemec_app` |
| `NEMEC_DATABASE_URL` | gerada | Conexão da aplicação (`nemec_app`) |
| `NEMEC_ADMIN_URL` | gerada | Conexão administrativa, usada **apenas** por `make migrate` e pelos testes |
| `NEMEC_OLLAMA_HOST` | `http://127.0.0.1:11434` | Só `localhost` é aceito |
| `NEMEC_CHAT_MODEL` | `qwen3.5:9b` | Modelo de chat |
| `NEMEC_EMBED_MODEL` | `bge-m3` | Embeddings (1024 dimensões, fixas na migração) |

Variáveis opcionais para instalar **ao lado** de outra instância (por exemplo, para testar uma instalação limpa sem tocar no banco principal):

```bash
NEMEC_DB_PORT=55433 NEMEC_CONTAINER_NAME=nemec-teste COMPOSE_PROJECT_NAME=nemec-teste make setup
NEMEC_DB_PORT=55433 NEMEC_CONTAINER_NAME=nemec-teste COMPOSE_PROJECT_NAME=nemec-teste make db-up migrate doctor
```

## Papéis e privilégios do banco

- `nemec_owner`: `NOLOGIN`, dono do schema `nemec`; só executa migrações (`SET ROLE`).
- `nemec_app`: `LOGIN`, apenas `SELECT/INSERT/UPDATE/DELETE` nas tabelas; sem DDL, `TRUNCATE`, superusuário, `CREATEDB`, `CREATEROLE`, `REPLICATION` ou `BYPASSRLS`.
- Os papéis são criados na primeira inicialização do volume (`db/initdb/`). Para recriá-los é preciso um volume novo — **isso apaga os dados**; faça-o só numa instalação descartável (`docker compose down -v`).

## Falhas comuns

| Sintoma | Causa provável | Ação |
|---|---|---|
| "Configuração inválida: NEMEC_DATABASE_URL não definido" | Falta o `.env` | `make setup` |
| "Não foi possível acessar o banco (make db-up / make migrate?)" | Docker parado, contêiner fora do ar ou banco sem migrar | `make db-up && make migrate`; `make doctor` |
| "NEMEC_OLLAMA_HOST: só é permitido localhost" | Host remoto configurado | Use um endereço de loopback (política G9) |
| "Não foi possível conectar ao Ollama…" na conversa | Ollama não está em execução | Inicie o Ollama; a resposta fica `failed`, nada se perde |
| "Modelo '…' não encontrado no Ollama (ollama pull …)" | Modelo ausente | `ollama pull <modelo>` |
| Porta 5433 ocupada | Outra instância | Defina `NEMEC_DB_PORT` antes de `make setup` |
| `make setup`: "Python 3.12 não encontrado" | Python errado | `brew install python@3.12` (ou `make setup PYTHON=/caminho/python3.12`) |

## Parar e remover

```bash
make db-down                    # para o contêiner (os dados permanecem no volume)
```

Dados do banco ficam no volume Docker `nemec_pgdata`. Removê-lo (`docker compose down -v`) é destrutivo e irreversível; o Nemec nunca faz isso por conta própria.
