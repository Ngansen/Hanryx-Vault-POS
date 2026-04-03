#!/bin/bash
# Runs automatically when the PostgreSQL container initialises for the first time.
# Creates the storefront database alongside the default vaultpos database.

set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE storefront OWNER $POSTGRES_USER;
EOSQL

echo "[init-db] 'storefront' database created."
