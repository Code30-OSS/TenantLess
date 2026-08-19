-- GOLDEN (comment-immunity, SQL): a synthetic.resources reference in a -- line comment
-- or a /* block */ comment is stripped before matching and must NOT be flagged.
SELECT 1;  -- synthetic.resources mentioned only in a trailing comment
/* block comment mentioning synthetic.resources — also ignored */
