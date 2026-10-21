-- Ledger schema (1.1.1). Dev only: drops and recreates, like loader/schema.sql.
-- Sign convention: positive amount = debit, negative amount = credit.
-- Rules enforced by the database:
--   1. The postings of one journal entry sum to exactly zero, and an entry has at least two postings.
--      A CHECK cannot see other rows, so a deferred constraint trigger does it at COMMIT.
--   2. Append-only: posted entries are never updated or deleted (a mistake is fixed by a reversing entry).
--      The table owner could still drop these triggers; the API will connect as a role that cannot (1.1.3).
DROP TABLE IF EXISTS ledger.postings, ledger.journal_entries, ledger.accounts CASCADE;
DROP FUNCTION IF EXISTS ledger.assert_entry_balanced() CASCADE;
DROP FUNCTION IF EXISTS ledger.refuse_change() CASCADE;

CREATE TABLE ledger.accounts (
  account_id integer PRIMARY KEY,  -- the account code, e.g. 2000
  name       text NOT NULL UNIQUE,
  type       text NOT NULL CHECK (type IN ('asset','liability','equity','revenue','expense'))
);

CREATE TABLE ledger.journal_entries (
  entry_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idempotency_key text NOT NULL UNIQUE,  -- posting the same payment twice must not create two entries
  description     text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ledger.postings (
  posting_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id   bigint  NOT NULL REFERENCES ledger.journal_entries(entry_id),
  account_id integer NOT NULL REFERENCES ledger.accounts(account_id),
  amount     numeric(12,2) NOT NULL CHECK (amount <> 0)
);
CREATE INDEX postings_entry_idx ON ledger.postings (entry_id);

CREATE FUNCTION ledger.assert_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  eid   bigint;
  total numeric;
  n     integer;
BEGIN
  eid := NEW.entry_id;
  SELECT COALESCE(sum(amount), 0), count(*) INTO total, n FROM ledger.postings WHERE entry_id = eid;
  IF n < 2 OR total <> 0 THEN
    RAISE EXCEPTION 'journal entry % is not balanced (% postings, sum %)', eid, n, total
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;

-- Fires on every new posting, and on a new entry (so an entry with no postings is refused too).
CREATE CONSTRAINT TRIGGER postings_balanced
  AFTER INSERT ON ledger.postings
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_entry_balanced();
CREATE CONSTRAINT TRIGGER entries_have_postings
  AFTER INSERT ON ledger.journal_entries
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_entry_balanced();

CREATE FUNCTION ledger.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'ledger is append-only: % on % is not allowed', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER postings_append_only BEFORE UPDATE OR DELETE ON ledger.postings
  FOR EACH ROW EXECUTE FUNCTION ledger.refuse_change();
CREATE TRIGGER postings_no_truncate BEFORE TRUNCATE ON ledger.postings
  FOR EACH STATEMENT EXECUTE FUNCTION ledger.refuse_change();
CREATE TRIGGER entries_append_only BEFORE UPDATE OR DELETE ON ledger.journal_entries
  FOR EACH ROW EXECUTE FUNCTION ledger.refuse_change();
CREATE TRIGGER entries_no_truncate BEFORE TRUNCATE ON ledger.journal_entries
  FOR EACH STATEMENT EXECUTE FUNCTION ledger.refuse_change();

-- What the API login (ledger_api) may do: read everything, insert entries and postings, nothing else.
-- It does not own the tables, so it cannot drop the append-only triggers.
GRANT USAGE ON SCHEMA ledger TO ledger_api;
GRANT SELECT ON ALL TABLES IN SCHEMA ledger TO ledger_api;
GRANT INSERT ON ledger.journal_entries, ledger.postings TO ledger_api;

-- Small chart of accounts (one currency, USD)
INSERT INTO ledger.accounts VALUES
  (1000, 'Cash',                'asset'),
  (2000, 'Accounts Payable',    'liability'),
  (5000, 'Operating Expenses',  'expense');
