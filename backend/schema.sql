-- Backend schema (1.3.1): runs and steps. Dev only: drops and recreates, like the other schema files.
-- A run is one invoice going through the pipeline; a step is one stage of it, with its input and output as JSON.
-- The database enforces the run state machine, so no code path can skip a state or reopen a finished run.
DROP TABLE IF EXISTS agent.steps, agent.runs CASCADE;
DROP FUNCTION IF EXISTS agent.check_run_transition() CASCADE;

CREATE TABLE agent.runs (
  run_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  status             text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','completed','failed','needs_review')),
  invoice_object_key text NOT NULL CHECK (length(invoice_object_key) > 0),
  error              text,
  -- what the pipeline concluded (1.4.4). review_required is a flag, not a status: the state machine stays small
  -- and the console can filter on it later. flags are soft reader notes (for example one reader's IBAN failed the
  -- checksum); they are recorded and never route.
  outcome            text CHECK (outcome IN ('payment_proposed','denied','needs_approval','unreadable')),
  review_required    boolean NOT NULL DEFAULT false,
  flags              jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- the Langfuse trace of this run, set only when tracing is on (backend/tracing.py): follow a run to its trace
  trace_id           text CHECK (trace_id ~ '^[0-9a-f]{32}$'),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent.steps (
  step_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id      uuid NOT NULL REFERENCES agent.runs(run_id),
  seq         integer NOT NULL CHECK (seq > 0),
  name        text NOT NULL CHECK (length(name) > 0),
  status      text NOT NULL CHECK (status IN ('running','completed','failed')),
  input       jsonb NOT NULL DEFAULT '{}'::jsonb,
  output      jsonb,
  error       text,
  decision_id uuid,  -- the authorize() decision made in this step, if any (docs/decisions.md)
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (run_id, seq),
  CHECK ((status = 'running') = (finished_at IS NULL))  -- a step has a finish time exactly when it is not running
);

CREATE FUNCTION agent.check_run_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;
  IF (OLD.status, NEW.status) IN (
       ('queued','running'), ('queued','failed'),
       ('running','completed'), ('running','failed'), ('running','needs_review'),
       ('needs_review','completed'), ('needs_review','failed')) THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'run % cannot go from % to %', OLD.run_id, OLD.status, NEW.status
    USING ERRCODE = 'check_violation';
END;
$$;

CREATE TRIGGER runs_state_machine BEFORE UPDATE OF status ON agent.runs
  FOR EACH ROW EXECUTE FUNCTION agent.check_run_transition();
