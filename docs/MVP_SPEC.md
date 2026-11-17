# Nemec 2.0 — Especificação do MVP (contrato vigente)

**Status:** vigente desde 2026-11-16 (escopo MVP de portfólio). Governa a implementação da primeira versão. A arquitetura completa originalmente imaginada (37 tabelas, protocolos adicionais) foi substituída por este escopo menor.
**Mudanças:** alterações em escopo, tabelas, garantias de segurança ou protocolos desta especificação são **classe C** e exigem aprovação do usuário (aprovação do mantenedor).

## 1. Escopo

| Macro | Capacidades |
|---|---|
| M1 | Chat PySide6 + Ollama com streaming e cancelamento; histórico persistente; **memória automática** (criada e atualizada pela IA sem aprovação individual, com prevenção de duplicidade, indicação discreta, desativável) visível, editável e excluível pelo usuário; interface com tema, barra lateral e painéis recolhíveis |
| M2 | Importação explícita de PDFs textuais e arquivos Markdown (`.md`, inclusive notas do Obsidian, sem sincronização); biblioteca de documentos; RAG híbrido (FTS + vetor + RRF); citações clicáveis e verificáveis (arquivo, página ou seção, trecho); ferramenta `create_task` com confirmação; lista de tarefas; renomear conversas |
| M3 | Integração, testes, documentação, demonstração, preparação para publicação |

**Fora do MVP:** áudio, TTS, OCR (PDF sem texto vira `no_text`), wake word, webcam, imagens, multiusuário, concessões e permissões de pasta, revogação com gerações, revisões de documento, vários modelos de embedding, ferramentas compostas, lembretes, integrações externas, restauração em quarentena, HNSW.
**Não declarar conformidade** com P1–P6, PHY, V07 ou V08: o MVP implementa garantias próprias (§4), algumas inspiradas nesses protocolos.

## 2. Arquitetura

- **UI:** PySide6. O stream do LLM roda em `QThread` e o cancelamento interrompe o stream.
- **Núcleo Python:** serviços de chat, memória, ingestão, recuperação e propostas; camada de repositório com psycopg 3 e transações curtas; nenhuma transação aberta durante chamada ao Ollama.
- **PostgreSQL 17 + pgvector:** Docker Compose, porta publicada só em `127.0.0.1`, imagem com tag fixa.
- **Ollama:** nativo, `localhost:11434`. Modelos configuráveis (`NEMEC_CHAT_MODEL`, `NEMEC_EMBED_MODEL`); padrões e requisitos em [`INSTALACAO.md`](INSTALACAO.md).
- **Papéis do banco:** `nemec_owner` (NOLOGIN, dono do schema `nemec`, usado só pelas migrações via `SET ROLE`); `nemec_app` (LOGIN, só `SELECT/INSERT/UPDATE/DELETE` nas tabelas do MVP; sem DDL, TRUNCATE, superusuário, CREATEDB, CREATEROLE, REPLICATION ou BYPASSRLS). O superusuário do contêiner só cria esses papéis na inicialização.
- **Limite documentado:** a proteção contra ações indevidas está no Python (allowlist, confirmação, validação). O banco garante integridade (constraints, FKs, transações), **não** autorização por usuário. Quem tem as credenciais de `.env` ou acesso à conta do macOS lê todos os dados. Os dados não são cifrados pelo app (depende do FileVault). Exclusão não é apagamento forense: páginas e WAL do PostgreSQL podem reter dados até o vacuum.

## 3. Modelo de dados (8 tabelas, schema `nemec`)

| Tabela | Colunas | Integridade |
|---|---|---|
| `conversations` | `id UUID PK`, `title TEXT`, `is_pinned BOOLEAN NOT NULL DEFAULT FALSE` (migração 0003; fixadas aparecem no topo), `created_at`, `updated_at` | `btrim(title) <> ''`. O título inicial é gerado uma vez; o usuário pode renomear (só `title`; nada o sobrescreve depois) |
| `messages` | `id`, `conversation_id`, `seq BIGINT`, `role`, `content TEXT`, `status`, `model TEXT NULL`, `created_at` | FK → `conversations` `ON DELETE CASCADE`; `UNIQUE(conversation_id, seq)`; `role IN ('user','assistant')`; `status IN ('generating','complete','canceled','failed')`; `seq > 0` |
| `memories` | `id`, `content`, `origin_conversation_id NULL`, `proposal_id NULL`, `created_at`, `updated_at` | FKs `ON DELETE SET NULL`; `btrim(content) <> ''`. **Só contém memórias aprovadas** |
| `action_proposals` | `id`, `kind`, `arguments JSONB`, `status`, `conversation_id`, `message_id NULL`, `result_task_id NULL`, `result_memory_id NULL`, `error TEXT NULL`, `created_at`, `decided_at NULL` | `kind IN ('create_task','create_memory')`; propostas de memória automática são gravadas já `executed` (trilha de auditoria; `arguments.action` = `created`\|`updated`), as demais nascem `pending`; `status IN ('pending','executed','rejected','failed')`; FK conversa e mensagem `CASCADE`; FKs de resultado `SET NULL`; `result_task_id IS NULL OR (kind='create_task' AND status='executed')`; `result_memory_id IS NULL OR (kind='create_memory' AND status='executed')`; `(status='pending') = (decided_at IS NULL)` |
| `tasks` | `id`, `title`, `notes NULL`, `due_at NULL`, `status`, `completed_at NULL`, `created_at` | `status IN ('open','done')`; `(status='done') = (completed_at IS NOT NULL)`; `btrim(title) <> ''` |
| `documents` | `id`, `title`, `file_name`, `source_type TEXT` (`pdf`\|`md`, padrão `pdf`), `sha256 TEXT UNIQUE`, `page_count INT`, `status`, `embedding_model TEXT`, `error TEXT NULL`, `imported_at` | `status IN ('processing','ready','failed','no_text')`; `source_type IN ('pdf','md')`; `sha256 ~ '^[0-9a-f]{64}$'`; `page_count > 0` (1 para `.md`) |
| `document_chunks` | `id`, `document_id`, `ordinal`, `page INT NULL`, `section TEXT NULL`, `char_start INT NULL`, `content TEXT`, `search_vector TSVECTOR GENERATED (portuguese)`, `embedding vector(1024) NOT NULL` | FK `ON DELETE CASCADE`; `UNIQUE(document_id, ordinal)`; `page IS NULL OR page >= 1`; `page IS NOT NULL OR char_start IS NOT NULL` (PDF: página; `.md`: sem página, com posição — a regra "página nula só para `.md`" é garantida pelo código de importação); `char_start >= 0`; `char_length(content) BETWEEN 1 AND 4000`; GIN em `search_vector`; sem índice vetorial aproximado. `section` = trilha de títulos (Markdown); `char_start` = posição do trecho no texto da página (PDF) ou do arquivo (`.md`) |
| `message_citations` | `message_id`, `ordinal` (= o `[n]` do texto), `chunk_id NULL`, `page NULL`, `quote_start NULL`, `quote_end NULL` | PK `(message_id, ordinal)`; FK mensagem `CASCADE`; FK chunk `ON DELETE SET NULL (chunk_id)` + gatilho `BEFORE DELETE` em `document_chunks` que zera `page`, `quote_start` e `quote_end` (tombstone "Fonte removida", sem conteúdo — o PostgreSQL só anula colunas da própria FK); `quote_start IS NULL OR (chunk_id IS NOT NULL AND quote_end IS NOT NULL AND 0 <= quote_start AND quote_start < quote_end)`. Em `.md`, `page` é NULL e a posição vem do chunk (`section`, `char_start`); `quote_*` são offsets dentro do `content` do chunk |

`d` = 1024 (`bge-m3`, local via Ollama), fixada na migração 0002; trocar de modelo exige nova migração e reindexação. A extensão `vector` (exige superusuário) é criada por `migrations/env.py` antes do `SET ROLE nemec_owner`; `nemec_app` só a usa.
Na inicialização do app: `messages.status='generating'` → `failed`; `documents.status='processing'` → `failed` (reimportar).

## 4. Garantias e protocolos

**G1 — Nenhum acesso arbitrário pelo LLM.** O LLM recebe só texto e o esquema de **duas** ferramentas: `create_task(title, notes?, due_at?)` e `save_memory(content, replaces?)`. Não existe ferramenta de SQL, shell, rede ou arquivo. O registro de ferramentas é uma constante testada.

**G2 — Ação com efeito externo ou de risco: proposta, nunca execução direta.** Uma chamada de ferramenta é validada (pydantic, allowlist; nome desconhecido ou argumento inválido → recusa, sem linha). Para `create_task` (M2) ela é gravada como `action_proposals.status='pending'` e só a confirmação humana (G3) a executa. Texto gerado pela IA (inclusive "confirmo") **nunca** aprova nada. **Exceção aprovada (2026-11-16): `save_memory`** não exige confirmação individual (memória automática), sob as salvaguardas G2a.

**G2a — Memória automática (substitui a confirmação humana de memórias).**
- **Origem:** só vale para fatos expressos diretamente pelo usuário **na mensagem do turno**. `content` é só um SELETOR: o Python exige que ele apareça literalmente (palavra por palavra, sem diferenciar caixa nem pontuação das bordas) na mensagem ATUAL do usuário e **persiste o trecho exato dessa mensagem**, não o texto do modelo; paráfrases e reescritas são recusadas. **Atribuição (regra conservadora, deterministica e lexical):** recusa-se trecho dentro de aspas/citação, em frase com verbo de atribuição ou sujeito de terceiro antes do trecho (diz, segundo, ela, personagem, documento…) e trecho sem marca de 1ª pessoa (pronome eu/me/mim/comigo/nós, verbo na 1ª pessoa do singular, ou possessivo seguido de atributo pessoal claro como nome, idade, cidade, profissão, time, "favorito"). **Possessivo sozinho não prova autoria:** "meu/minha + outro substantivo" (vizinha, irmão…) no trecho ou antes dele na frase é recusado. **Limite explícito:** a atribuição semântica absoluta não é verificável deterministicamente; na dúvida não se grava (pode haver falsos negativos), e o que o usuário afirma sobre si e é gravado continua sendo dado visível e editável. Documentos, títulos, catálogo, mensagens anteriores do assistente e resultados de ferramentas nunca podem originar memória, inclusive em `replaces`.
- **Fonte não confiável:** documentos, passagens recuperadas e resultados de ferramentas **nunca** criam, alteram ou excluem memórias: a ferramenta não é oferecida nem executada em turnos com conteúdo recuperado (M2), chamadas na continuação pós-ferramenta são descartadas, e a IA não tem ferramenta de exclusão.
- **Sensibilidade:** conteúdo com senha, token, chave de API/privada, CPF ou número de cartão é recusado (filtro determinístico; o usuário é avisado) e o prompt proíbe informações sensíveis apenas inferidas.
- **Duplicidade e atualização:** idêntica (após normalização) a qualquer outra memória, inclusive com `replaces` → nada é gravado; muito parecida ou com `replaces` válido → a memória existente é **atualizada**; caso contrário é criada. Gravação serializada por `pg_advisory_xact_lock`. Edição manual que duplique outra memória é recusada.
- **Atomicidade:** a decisão e a gravação ocorrem na mesma transação que finaliza a resposta (`complete`); cancelamento, falha ou conversa excluída → nenhuma memória é gravada. Falha de banco ao gravar → rollback e resposta `failed`.
- **Limites:** no máximo 3 chamadas de memória por turno; texto até 2000 caracteres.
- **Controle do usuário:** painel com lista, edição, exclusão e chave "Memória automática" (desligada: a ferramenta não é oferecida nem executada; memórias existentes continuam no prompt). Cada criação/atualização é indicada discretamente na conversa e registrada em `action_proposals` (`executed`, com texto anterior em `arguments.previous`).
- **Conversa natural:** se o modelo só chamar a ferramenta, o resultado é devolvido a ele para uma continuação em texto (uma vez, sem ferramentas); se ainda vier vazia, usa-se um reconhecimento curto. Nunca resposta vazia.

**G3 — Confirmação humana pela UI, consumo atômico e idempotente (ações como `create_task`).** `create_task(title, notes?, due_at?)`: no máximo 3 propostas por turno, repetidas descartadas; "Confirmar" na UI (com edição opcional de título, notas e prazo ISO 8601) cria a linha em `tasks`; tarefas têm listagem, conclusão (idempotente) e exclusão pelo usuário, sem proposta.
- O botão "Confirmar" da UI chama `confirm_proposal(id, args_editados)`. Numa transação: `SELECT … FOR UPDATE`; se já está `executed`, devolve o resultado existente; se está `pending`, revalida os argumentos, executa a ação, grava `executed` + resultado + `decided_at` e faz commit.
- Falha → rollback e `failed` registrado em nova transação.
- "Rejeitar" → `rejected`.
- Propostas de memória `pending` anteriores à memória automática continuam confirmáveis/rejeitáveis pelo mesmo mecanismo.
- Edição e exclusão de memórias e tarefas pelo próprio usuário na UI não passam por proposta.

**G4 — Chat e cancelamento.**
- A mensagem do usuário é gravada `complete`.
- A resposta é criada como `generating` e, ao final, vira `complete`, `canceled` (com o texto parcial) ou `failed` (Ollama indisponível ou erro).
- `seq` é alocado com `SELECT max(seq)+1 … FOR UPDATE` na conversa, na mesma transação da inserção; `UNIQUE` como garantia.
- O contexto do prompt usa só mensagens `complete` e memórias aprovadas. As memórias são dados: instruções explícitas do usuário na conversa atual prevalecem sobre preferências gerais guardadas (ex.: pedido de texto longo × preferência por respostas curtas).

**G5 — Importação de PDF e Markdown.**
- Só por diálogo de arquivo escolhido pelo usuário (seleção múltipla permitida; cada arquivo é uma importação independente). Os bytes são lidos uma vez; o original não é copiado nem alterado; não há sincronização com o Obsidian. `sha256` duplicado → aviso, sem nova importação. Aceitos: `.pdf`, `.md`, `.markdown` (UTF-8; frontmatter YAML não é indexado).
- **Importação de pasta (pontual, iniciada pelo usuário):** diálogo nativo → varredura só de listagem (subpastas incluídas; ignora diretórios/arquivos ocultos, `.obsidian`, links simbólicos, não regulares e formatos não suportados) → prévia com contagem e confirmação → pipeline acima, arquivo a arquivo, em `QThread`, com cancelamento (pendentes viram `canceled`) e resumo (importados, duplicados, sem texto, erros, ignorados, cancelados). Os bytes lidos são debitados do orçamento ao ler, antes de indexar, e o débito permanece se a indexação falhar; a leitura é sem buffer e limitada ao saldo (arquivo maior que o saldo, ou que cresça durante a leitura, não é lido além dele). Limites: 500 arquivos, 1 GiB, 20 000 itens visitados (arquivos e diretórios), 100 MB por arquivo — aplicados na varredura E na leitura efetiva (arquivos que crescem após a prévia respeitam o orçamento; o excedente vira `skipped`/erro do item). Falha de um item (inclusive laço de links criado depois da prévia) é registrada no item e o lote continua, salvo cancelamento ou limite. Cada arquivo é revalidado (regular, não-link, dentro da pasta) antes da leitura. Sem sincronização nem monitoramento.
- **T1:** `documents` com status `processing`.
- **Fora de transação:** extração (`pypdfium2` para PDF; leitura UTF-8 para `.md`), chunking (PDF: por página; `.md`: por seção de título), embeddings (`bge-m3`). Cada trecho é uma fatia exata do texto extraído (~1000 caracteres).
- **T2 (publicação):** `SELECT … FOR UPDATE` do documento com `status='processing'`; se não existir (excluído), descarta; senão insere todos os chunks e marca `ready`. Sem texto → `no_text`.

**G6 — Exclusão de documento.** A UI identifica o documento, pede confirmação e mostra o nome removido (ou a falha), sem histórico persistente. Uma transação: `DELETE FROM documents` → chunks e embeddings em cascata; citações viram tombstone. Exclusão concorrente com a publicação é serializada pelo lock da linha (G5-T2); exclusão concorrente com uma resposta é tratada em G7.

**G7 — Recuperação e citações verificáveis.**
- **Catálogo:** a existência dos documentos vem do banco (`documents`), nunca da busca: o prompt traz sempre o catálogo real (nome, tipo, situação; nomes neutralizados) ou a informação de biblioteca vazia. Só com intenção de consultar a biblioteca (arquivos, documentos, materiais, apostila, anotações, PDFs, biblioteca) a pergunta recebe também o trecho de abertura de cada documento pronto; conversas alheias à biblioteca não recebem trechos e mantêm a memória automática, com citações verificáveis como as demais passagens.
- **Consulta:** uma consulta SQL, com filtros **antes do LIMIT** (`documents.status='ready'` e `embedding_model` = modelo atual), FTS `portuguese` (termos da pergunta por OU) + distância de cosseno exata + RRF (k=60). **Relevância mínima:** só entram trechos a ≤ 0,55 de distância de cosseno, ou que casaram no FTS e estão a ≤ 0,65 (calibrado com `bge-m3`); perguntas alheias aos documentos não recebem passagens.
- **Prompt:** as passagens entram numeradas `[1..k]`, delimitadas como **conteúdo não confiável**. Com passagens no prompt, `save_memory` não é oferecida nem executada (G2a); `create_task` continua oferecida, mas só gera proposta pendente.
- **Pós-processamento:** só `[n]` dentro de `1..k` **e efetivamente incluído no prompt após o corte por orçamento** vira citação (os demais marcadores são removidos do texto); o literal entre aspas COLADO ao marcador só recebe offsets se for substring exata do chunk. Na UI, `[n]` e o botão de fonte abrem arquivo, página ou seção/posição e o trecho original (destacando o literal citado).
- **Gravação:** a resposta e as citações são gravadas numa transação que faz `SELECT … FOR SHARE` dos chunks citados. Chunk ausente (documento excluído durante a resposta) → citação omitida e aviso na UI.

**G8 — Injeção via conteúdo.** O texto de PDFs e respostas pode conter instruções; o efeito máximo é uma proposta pendente visível (G2/G3); documentos e resultados de ferramentas nunca alteram memórias (G2a).

**G9 — Local e gratuito.** Nenhuma chamada de rede além de `localhost` (Ollama, PostgreSQL); sem telemetria.

## 5. Testes obrigatórios

Descritos em [`TESTES.md`](TESTES.md). Testes não executados nunca são relatados como aprovados.
