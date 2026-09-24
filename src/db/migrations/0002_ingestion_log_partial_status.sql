-- Migration 0002 - allow 'partial' in ingestion_log.status.
--
-- Replaces the ALTER TABLE ... DROP/ADD CONSTRAINT pair that previously ran
-- on EVERY job, including the read-only-sounding connection check. That left
-- a brief window on each run in which the constraint did not exist, and it
-- required the runtime role to hold DDL rights.
--
-- Guarded: it only acts if the existing constraint does not already accept
-- 'partial'. On any database created by 0001, and on production (which the
-- old runtime ALTER already upgraded), it changes nothing.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO current_def
      FROM pg_constraint c
     WHERE c.conrelid = 'ingestion_log'::regclass
       AND c.conname = 'ingestion_log_status';

    IF current_def IS NULL OR position('partial' IN current_def) = 0 THEN
        ALTER TABLE ingestion_log DROP CONSTRAINT IF EXISTS ingestion_log_status;
        ALTER TABLE ingestion_log ADD CONSTRAINT ingestion_log_status
            CHECK (status IN ('loaded', 'partial', 'no_data', 'failed'));
        RAISE NOTICE 'ingestion_log_status widened to include partial';
    ELSE
        RAISE NOTICE 'ingestion_log_status already includes partial; nothing to do';
    END IF;
END
$$;
