-- GOLDEN (single-use A): one same-line marker sanctions its OWN occurrence only;
-- a second, unmarked occurrence nearby is the one violation.
SELECT id FROM synthetic.resources a;  -- SYNRES-ALLOW[reset]: sanctioned single-use reader
SELECT id FROM synthetic.resources b;
