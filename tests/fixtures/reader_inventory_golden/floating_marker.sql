-- GOLDEN (single-use B): a marker with NO adjacent occurrence is itself a violation
-- (no silent pass) — the next line has no synthetic.resources read.
-- SYNRES-ALLOW[reset]: floating marker, nothing adjacent to sanction
SELECT 1 AS ok;
