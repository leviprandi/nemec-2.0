-- Preparação de um banco (idempotente): schema `nemec` pertencente a nemec_owner e
-- privilégios mínimos. Usado na inicialização do contêiner e pelos testes (bancos temporários).
DO $bootstrap$
BEGIN
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO nemec_app, nemec_owner', current_database());
  REVOKE CREATE ON SCHEMA public FROM PUBLIC;
  CREATE SCHEMA IF NOT EXISTS nemec AUTHORIZATION nemec_owner;
  GRANT USAGE ON SCHEMA nemec TO nemec_app;
  REVOKE ALL ON SCHEMA nemec FROM PUBLIC;
END
$bootstrap$;
