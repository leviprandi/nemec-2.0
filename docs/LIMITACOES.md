# Limitações conhecidas

Limitações **aceitas** do MVP, sem previsão de correção nesta versão. Nada abaixo é medido por benchmark: são comportamentos observados e decisões de escopo.

## Aceitas formalmente
- **Intenção documental por palavras-chave.** Decidir se uma pergunta é sobre a biblioteca (e, portanto, recebe trechos de abertura dos documentos) usa palavras-chave como "arquivos", "documentos", "apostila"; pode haver falsos positivos.
- **Memória automática lexical e conservadora.** A atribuição ("isso é sobre o próprio usuário?") é verificada por regras determinísticas, não por interpretação semântica; na dúvida não grava (falsos negativos). O que é gravado é sempre o trecho literal do usuário, visível e editável.
- **Biblioteca global.** Os documentos pertencem à aplicação, não a uma conversa: qualquer conversa pode consultá-los.
- **Importação pontual de pastas.** Sem sincronização contínua com o Obsidian; mudanças nos originais exigem nova importação (o `sha256` evita duplicatas do mesmo conteúdo).

## Do modelo local
- O modelo local nem sempre chama `create_task`; a fonte da verdade é o cartão de proposta, e o prazo sugerido pode estar errado (é editável antes de confirmar).
- Destaques de citação só existem quando o modelo copia o literal entre aspas antes de `[n]`; sem isso a interface mostra o trecho inteiro.
- Instruções dentro de documentos podem influenciar o texto da resposta; o que o sistema garante é que **não** produzem ações sem confirmação nem alteram memórias.
- Os limiares de relevância (0,55/0,65 de distância de cosseno) foram calibrados com `bge-m3` em poucos documentos; bibliotecas grandes ou ruidosas podem exigir reajuste (`MAX_DIST` e `FTS_MAX_DIST` em `documents.py`).

## De escopo e de infraestrutura
- Sem OCR (PDF sem texto vira "sem texto"); PDFs protegidos por senha falham. Limites: 100 MB e 2000 páginas por arquivo; pastas até 500 arquivos / 1 GiB.
- Aplicação de usuário único; sem autenticação, multiusuário ou permissões por pasta.
- Os dados **não são cifrados pelo aplicativo** (dependem de FileVault ou equivalente); quem tem o `.env` ou acesso à conta do macOS lê o banco. Exclusão não é apagamento forense (páginas e WAL do PostgreSQL podem reter dados até o vacuum).
- Modelo de embedding fixo (1024 dimensões); trocar exige migração e reindexação.
- Busca vetorial exata, sem índice aproximado (HNSW): adequado a bibliotecas pessoais, não a grandes volumes.
- Testado em macOS (Apple Silicon). Linux é esperado funcionar (as dependências são multiplataforma), mas **não foi verificado**. Capturas de tela foram geradas em modo offscreen.
- Não há CI configurado no repositório; os testes rodam localmente com `make test`.
