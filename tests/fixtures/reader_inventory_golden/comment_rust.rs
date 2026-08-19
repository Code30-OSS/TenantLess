// GOLDEN (comment-immunity, Rust): a synthetic.resources reference that lives only
// in a // line comment is stripped before matching and must NOT be flagged.
fn f() {
    let _x = 1; // historically read synthetic.resources, now via the resolver view
    /* block comment mentioning synthetic.resources — also ignored */
}
