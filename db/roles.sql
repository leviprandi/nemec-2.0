-- Papéis do cluster (MVP_SPEC §2). Executado uma vez pelo superusuário do contêiner,
-- na inicialização. Variável psql: app_pw.
CREATE ROLE nemec_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE nemec_app LOGIN PASSWORD :'app_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
