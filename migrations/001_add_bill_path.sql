-- Adds bill storage to the cashbook.
-- Run once in Supabase: SQL Editor -> paste -> Run.
-- Bills are saved in the existing private cashbook-photos bucket under bills/.

alter table cashbook_entries
  add column if not exists bill_path text;

comment on column cashbook_entries.bill_path is
  'Path of the bill photo or PDF inside the cashbook-photos storage bucket (bills/...). Null if no bill.';
