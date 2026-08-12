-- GOLDEN (single-use C): one preceding (K=1) marker covers ONLY the immediately-
-- following occurrence; the second consecutive occurrence is the one violation.
-- SYNRES-ALLOW[reset]: covers ONLY the next line's occurrence
SELECT id FROM synthetic.resources a;
SELECT id FROM synthetic.resources b;
