DROP TABLE IF EXISTS vendors.invoices, vendors.historical_payments,
  vendors.vendor_risk_flags, vendors.vendor_bank_accounts, vendors.vendors CASCADE;

CREATE TABLE vendors.vendors (
  vendor_id  integer PRIMARY KEY,
  name       text NOT NULL,
  country    char(2) NOT NULL,
  status     text NOT NULL CHECK (status IN ('active','blocked')),
  created_at date NOT NULL
);

CREATE TABLE vendors.vendor_bank_accounts (
  account_id integer PRIMARY KEY,
  vendor_id  integer NOT NULL REFERENCES vendors.vendors(vendor_id),
  iban       text NOT NULL CHECK (iban LIKE 'XX%'),
  valid_from date NOT NULL,
  valid_to   date,
  CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE UNIQUE INDEX one_current_account_per_vendor
  ON vendors.vendor_bank_accounts (vendor_id) WHERE valid_to IS NULL;

CREATE TABLE vendors.vendor_risk_flags (
  flag_id   integer PRIMARY KEY,
  vendor_id integer NOT NULL REFERENCES vendors.vendors(vendor_id),
  flag_type text NOT NULL CHECK (flag_type IN ('new_vendor','prior_dispute','high_risk_country')),
  raised_at date NOT NULL
);

CREATE TABLE vendors.historical_payments (
  payment_id     integer PRIMARY KEY,
  vendor_id      integer NOT NULL REFERENCES vendors.vendors(vendor_id),
  account_id     integer NOT NULL REFERENCES vendors.vendor_bank_accounts(account_id),
  invoice_number text NOT NULL,
  amount         numeric(12,2) NOT NULL CHECK (amount > 0),
  paid_at        date NOT NULL
);

CREATE TABLE vendors.invoices (
  invoice_id       integer PRIMARY KEY,
  vendor_id        integer NOT NULL REFERENCES vendors.vendors(vendor_id),
  invoice_number   text NOT NULL,
  amount           numeric(12,2) NOT NULL CHECK (amount > 0),
  issue_date       date NOT NULL,
  due_date         date NOT NULL,
  iban_on_invoice  text NOT NULL,
  object_key       text
);