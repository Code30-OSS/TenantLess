# GOLDEN (runtime SQL string literal): an occurrence inside an EXECUTABLE (non-comment,
# non-docstring) SQL string literal is STILL scanned and, unmarked, is one violation.
def q(cur):
    cur.execute("SELECT id FROM synthetic.resources WHERE 1=1")
