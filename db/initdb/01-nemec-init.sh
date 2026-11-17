#!/bin/sh
# Inicialização do contêiner: cria os papéis e prepara o banco `nemec`.
set -eu
psql -v ON_ERROR_STOP=1 -v app_pw="$NEMEC_APP_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -f /nemec-db/roles.sql -f /nemec-db/bootstrap.sql
