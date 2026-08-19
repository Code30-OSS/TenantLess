# GOLDEN (valid marker accepted): a well-formed same-line SYNRES-ALLOW marker with a
# closed-set category and a non-empty reason sanctions its occurrence — zero violations.
def q(cur):
    cur.execute("SELECT id FROM synthetic.resources")  # SYNRES-ALLOW[baseline-replay]: golden valid marker, reads the immutable baseline
