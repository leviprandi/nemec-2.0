# Segurança

O Nemec é um aplicativo **local de usuário único**. Este resumo descreve o que o MVP garante e o que não garante; o detalhe está em [`docs/MVP_SPEC.md` §4](docs/MVP_SPEC.md) e [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md).

## O que o MVP implementa
- **Sem acesso arbitrário pelo LLM**: só texto e duas ferramentas (`save_memory`, `create_task`), validadas por esquema; nome desconhecido ou argumento inválido é recusado.
- **Ações com efeito exigem confirmação na interface**; texto gerado pela IA nunca aprova nada.
- **Fontes não confiáveis**: documentos e trechos recuperados nunca criam, alteram ou excluem memórias; entram no prompt delimitados como dados.
- **Tudo em localhost**: a configuração recusa hosts remotos; sem telemetria. O PostgreSQL publica a porta só em `127.0.0.1`.
- **Banco com privilégio mínimo**: a aplicação usa `nemec_app` (DML apenas). Segredos ficam em `.env` (permissão 600, fora do Git); só `.env.example` é público.
- **Memória**: recusa segredos (senha, token, chave, CPF, cartão) por filtro determinístico.

## O que não garante
- Que o modelo ignore instruções maliciosas em documentos: o sistema limita o **efeito**, não o texto gerado.
- Confidencialidade em repouso: os dados não são cifrados pelo app; quem acessa a conta do macOS ou o `.env` lê o banco. Use FileVault.
- Apagamento forense: exclusão remove registros, mas o PostgreSQL pode reter páginas/WAL até o vacuum.
- Proteção contra um usuário local malicioso com acesso ao Docker ou ao `.env`.

## Reportar vulnerabilidades
- Para reportar uma vulnerabilidade de segurança, utilize [Report a vulnerability](https://github.com/leviprandi/nemec-2.0/security/advisories/new).
- Os relatos são privados e permitem discutir possíveis vulnerabilidades antes de sua divulgação pública.
- Não inclua dados pessoais, credenciais ou arquivos `.env`.
