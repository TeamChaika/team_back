-- Keep the full inventory job for bootstrap, document history and in-flight requests.
SET LOCAL lock_timeout = '10s';
ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check CHECK(job IN
 ('references','inventory','employees','store_balances','dictionaries','counteragent_balances',
  'events','employee_roles','accounts','cash_shifts','products','assembly_charts'));
