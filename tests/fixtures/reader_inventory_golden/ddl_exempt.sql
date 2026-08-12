-- GOLDEN (DDL exemption): naming the base table in its OWN schema definition
-- (CREATE/ALTER TABLE, CREATE INDEX ... ON, FK REFERENCES) is not a resolver-bypassing
-- read and is exempt. A FROM read, by contrast, still needs a marker — the one violation.
CREATE TABLE synthetic.resources (id text);
CREATE INDEX idx ON synthetic.resources (id);
ALTER TABLE synthetic.resources ADD COLUMN c text;
SELECT id FROM synthetic.resources;
