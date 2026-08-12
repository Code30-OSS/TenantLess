# GOLDEN (comment-immunity, Python): a synthetic.resources reference in a # comment
# or a triple-quoted docstring is stripped before matching and must NOT be flagged.
def f():
    """Docstring that mentions synthetic.resources — ignored."""
    x = 1  # historically read synthetic.resources, now via the resolver view
    return x
