#!/bin/sh
set -eu

create_service() {
  name="$1"
  password="$2"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<EOSQL
CREATE ROLE ${name}_user LOGIN PASSWORD '${password}';
CREATE SCHEMA ${name} AUTHORIZATION ${name}_user;
ALTER ROLE ${name}_user SET search_path = ${name};
EOSQL
}

create_service agent   "${AGENT_DB_PASSWORD:?}"
create_service ledger  "${LEDGER_DB_PASSWORD:?}"
create_service vendors "${VENDORS_DB_PASSWORD:?}"
create_service policy  "${POLICY_DB_PASSWORD:?}"
create_service audit   "${AUDIT_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<EOSQL
CREATE ROLE vendors_ro LOGIN PASSWORD '${VENDORS_RO_DB_PASSWORD:?}';
GRANT USAGE ON SCHEMA vendors TO vendors_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE vendors_user IN SCHEMA vendors GRANT SELECT ON TABLES TO vendors_ro;
ALTER ROLE vendors_ro SET search_path = vendors;
EOSQL

# Runtime login for the ledger API: it can only INSERT and SELECT (grants live in ledger/schema.sql)
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<EOSQL
CREATE ROLE ledger_api LOGIN PASSWORD '${LEDGER_API_DB_PASSWORD:?}';
ALTER ROLE ledger_api SET search_path = ledger;
EOSQL
