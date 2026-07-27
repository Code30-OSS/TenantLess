//! OData `$filter` parser + AST + parameterized-SQL translator (MOCK-06, D-01..D-04).
//!
//! This is the NEW SQL-injection boundary introduced by Phase 4 (threat T-04-03,
//! extending Phase 3's T-03-09/10). It is a self-contained, DB-free module — exactly
//! like `pagination.rs` isolates the keyset cursor codec — so the injection-safety
//! invariant can be exhaustively unit-tested without a database
//! (`cargo test -p tenantless-server --lib filter::`).
//!
//! The grammar (RESEARCH Pattern 3; CONTEXT D-01..D-04) is a tiny recursive-descent
//! subset of OData, hand-rolled over `&str` with std only (no parser-combinator crate —
//! CLAUDE.md prefers extending, and an auditable ~150-line parser is easier to verify
//! for the bound-`$N`-only contract than a combinator's threaded error type):
//!
//! ```text
//! filter      := or_expr
//! or_expr     := and_expr ( "or" and_expr )*
//! and_expr    := comparison ( "and" comparison )*    // `and` binds tighter than `or`
//! comparison  := field "eq" literal
//! field       := "resourceType" | "location" | "tagName" | "tagValue"
//! literal     := "'" <single-quoted; '' escapes one quote> "'"
//! ```
//!
//! The non-negotiable invariant: [`Filter::to_sql`] emits a fragment whose only
//! non-literal-text tokens are column names (`location`, `type`, `tags ->> $k`),
//! boolean keywords, parens, and `$N` placeholders. Every user literal flows ONLY
//! through the parallel bound-args `Vec`, never into the returned SQL string.
//! On ANY parse/validation failure the parser returns a fixed-string 400 — never
//! panics, never leaks internals (threat T-04-04, mirroring `"invalid $skiptoken"`).

#![allow(dead_code)] // GREEN task wires `parse`/`to_sql` into handlers (Plan 04).

use crate::error::ApiError;

/// A filterable field, mapped to its physical column via a closed `match` in
/// [`Filter::to_sql`] (never via `format!` over user text — that is the injection guard).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Field {
    /// `resourceType` ⇒ the `type` column (D-04).
    ResourceType,
    /// `location` ⇒ the `location` column (D-04).
    Location,
}

/// The parsed `$filter` AST. `Eq` carries a scalar field comparison; `TagPair` is the
/// folded `tagName eq 'K' and tagValue eq 'V'` idiom (D-01); `And`/`Or` compose.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Filter {
    /// `field eq value` — `value` goes to the bound-args Vec, NEVER into SQL text.
    Eq { field: Field, value: String },
    /// Folded paired tag lookup: `tags ->> key = value` (exactly one predicate, two binds).
    TagPair { key: String, value: String },
    /// Lone `tagName eq 'K'` — tag-key existence (`jsonb_exists(tags, key)`), one bind.
    /// Azure supports `tagName eq` on its own as a presence filter (MOCK-06); `key` goes
    /// to the bound-args Vec, never into SQL text.
    TagExists { key: String },
    /// `and` — binds tighter than `or`.
    And(Box<Filter>, Box<Filter>),
    /// `or` — the lowest-precedence binary operator.
    Or(Box<Filter>, Box<Filter>),
}

/// Parse a `$filter` string into a [`Filter`] AST.
///
/// Returns `ApiError::BadRequest { message: "invalid $filter" }` (fixed string) for
/// any malformed input, unknown field, unknown operator, unbalanced quotes, or a lone
/// `tagValue` with no `tagName`. A lone `tagName eq 'K'` is valid (tag-key existence).
/// Never panics.
pub fn parse(input: &str) -> Result<Filter, ApiError> {
    let tokens = tokenize(input).map_err(|_| bad_filter())?;
    let mut p = Parser {
        tokens: &tokens,
        pos: 0,
    };
    let filter = p.parse_or().map_err(|_| bad_filter())?;
    // Reject trailing junk (e.g. a stray token the grammar never consumed).
    if p.pos != p.tokens.len() {
        return Err(bad_filter());
    }
    Ok(filter)
}

/// The fixed-string 400 used for EVERY parse/validation failure (T-04-04 no-leak).
fn bad_filter() -> ApiError {
    ApiError::BadRequest {
        message: "invalid $filter".to_string(),
    }
}

impl Filter {
    /// Translate the AST into a SQL `WHERE` fragment of placeholders only, pushing each
    /// literal into `args` in bind order. `next_param` is the next free `$N` index
    /// (seeded past the handler's fixed binds, e.g. `$4`); it is advanced as args are pushed.
    ///
    /// INVARIANT: the returned string contains ONLY column names, boolean keywords,
    /// parens, and `$N` tokens — never a user `value`/`key`. The count of distinct `$N`
    /// placeholders equals the number of args pushed by this call.
    pub fn to_sql(&self, next_param: &mut i32, args: &mut Vec<String>) -> String {
        match self {
            // Field → column is a CLOSED match — user text never reaches the SQL string.
            Filter::Eq { field, value } => {
                let column = match field {
                    Field::ResourceType => "type",
                    Field::Location => "location",
                };
                let idx = *next_param;
                *next_param += 1;
                args.push(value.clone());
                format!("{column} = ${idx}")
            }
            Filter::TagPair { key, value } => {
                let k_idx = *next_param;
                let v_idx = *next_param + 1;
                *next_param += 2;
                args.push(key.clone());
                args.push(value.clone());
                format!("tags ->> ${k_idx} = ${v_idx}")
            }
            Filter::TagExists { key } => {
                let idx = *next_param;
                *next_param += 1;
                args.push(key.clone());
                // `jsonb_exists(tags, $N)` (function form, not the `?` operator) avoids any
                // placeholder/operator ambiguity while staying placeholders-only.
                format!("jsonb_exists(tags, ${idx})")
            }
            Filter::And(a, b) => {
                let left = a.to_sql(next_param, args);
                let right = b.to_sql(next_param, args);
                format!("({left} AND {right})")
            }
            Filter::Or(a, b) => {
                let left = a.to_sql(next_param, args);
                let right = b.to_sql(next_param, args);
                format!("({left} OR {right})")
            }
        }
    }
}

// ---- tokenizer -------------------------------------------------------------

/// A lexical token. Identifiers cover field names and the `eq`/`and`/`or` keywords
/// (disambiguated by the parser, not the lexer); literals are unquoted, `''`-unescaped.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Token {
    Ident(String),
    Literal(String),
}

/// Lex the input into identifiers and single-quoted literals.
///
/// Whitespace separates tokens; an identifier is a run of ASCII letters; a literal is
/// `'...'` where `''` is an escaped quote. Any other byte (digits, punctuation, an
/// unterminated quote) is a lex error → caller maps to the fixed-string 400.
fn tokenize(input: &str) -> Result<Vec<Token>, ()> {
    let bytes = input.as_bytes();
    let mut tokens = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        let c = bytes[i];
        if c.is_ascii_whitespace() {
            i += 1;
        } else if c == b'\'' {
            // Single-quoted literal; '' is one literal quote.
            i += 1;
            let mut value = String::new();
            loop {
                if i >= bytes.len() {
                    return Err(()); // unterminated literal (unbalanced quote)
                }
                if bytes[i] == b'\'' {
                    if i + 1 < bytes.len() && bytes[i + 1] == b'\'' {
                        value.push('\'');
                        i += 2;
                    } else {
                        i += 1; // closing quote
                        break;
                    }
                } else {
                    // Multi-byte UTF-8 inside the literal is preserved via char decode.
                    let ch = input[i..].chars().next().ok_or(())?;
                    value.push(ch);
                    i += ch.len_utf8();
                }
            }
            tokens.push(Token::Literal(value));
        } else if c.is_ascii_alphabetic() {
            let start = i;
            while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
                i += 1;
            }
            tokens.push(Token::Ident(input[start..i].to_string()));
        } else {
            // Digits, operators like `<`, stray punctuation — outside the grammar.
            return Err(());
        }
    }
    Ok(tokens)
}

// ---- recursive-descent parser ----------------------------------------------

/// One conjunct produced by the `and_expr` rule, before tag-pair folding.
enum Conjunct {
    Scalar(Filter),
    TagName(String),
    TagValue(String),
}

struct Parser<'a> {
    tokens: &'a [Token],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.pos)
    }

    /// Consume an identifier exactly equal to `kw` (the `eq`/`and`/`or` keywords).
    fn eat_keyword(&mut self, kw: &str) -> bool {
        if let Some(Token::Ident(s)) = self.peek()
            && s == kw
        {
            self.pos += 1;
            return true;
        }
        false
    }

    /// or_expr := and_expr ( "or" and_expr )*
    fn parse_or(&mut self) -> Result<Filter, ()> {
        let mut left = self.parse_and()?;
        while self.eat_keyword("or") {
            let right = self.parse_and()?;
            left = Filter::Or(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    /// and_expr := comparison ( "and" comparison )*  — folds a tagName+tagValue pair.
    fn parse_and(&mut self) -> Result<Filter, ()> {
        let mut conjuncts = vec![self.parse_comparison()?];
        while self.eat_keyword("and") {
            conjuncts.push(self.parse_comparison()?);
        }
        fold_conjuncts(conjuncts)
    }

    /// comparison := field "eq" literal
    fn parse_comparison(&mut self) -> Result<Conjunct, ()> {
        let field = match self.peek() {
            Some(Token::Ident(s)) if s != "and" && s != "or" && s != "eq" => s.clone(),
            _ => return Err(()),
        };
        self.pos += 1;
        if !self.eat_keyword("eq") {
            return Err(()); // missing or unknown operator (e.g. `ne`, `gt`)
        }
        let value = match self.peek() {
            Some(Token::Literal(v)) => v.clone(),
            _ => return Err(()), // missing literal
        };
        self.pos += 1;
        match field.as_str() {
            "resourceType" => Ok(Conjunct::Scalar(Filter::Eq {
                field: Field::ResourceType,
                value,
            })),
            "location" => Ok(Conjunct::Scalar(Filter::Eq {
                field: Field::Location,
                value,
            })),
            "tagName" => Ok(Conjunct::TagName(value)),
            "tagValue" => Ok(Conjunct::TagValue(value)),
            _ => Err(()), // unknown field
        }
    }
}

/// Combine the conjuncts of one `and_expr` into a single [`Filter`], folding a
/// `tagName`/`tagValue` pair (D-01) and rejecting a lone `tagName`/`tagValue`.
fn fold_conjuncts(conjuncts: Vec<Conjunct>) -> Result<Filter, ()> {
    let mut tag_name: Option<String> = None;
    let mut tag_value: Option<String> = None;
    let mut scalars: Vec<Filter> = Vec::new();

    for c in conjuncts {
        match c {
            Conjunct::Scalar(f) => scalars.push(f),
            Conjunct::TagName(k) => {
                if tag_name.is_some() {
                    return Err(()); // two tagName clauses in one and_expr — unsupported
                }
                tag_name = Some(k);
            }
            Conjunct::TagValue(v) => {
                if tag_value.is_some() {
                    return Err(());
                }
                tag_value = Some(v);
            }
        }
    }

    // Tag clause folding (D-01 + MOCK-06 tag-presence):
    //   tagName + tagValue → one TagPair (`tags ->> $k = $v`)
    //   lone tagName       → TagExists (`jsonb_exists(tags, $k)`) — Azure tag-presence filter
    //   lone tagValue      → reject (no standalone Azure semantics for a value without a key)
    match (tag_name, tag_value) {
        (Some(key), Some(value)) => scalars.push(Filter::TagPair { key, value }),
        (Some(key), None) => scalars.push(Filter::TagExists { key }),
        (None, None) => {}
        (None, Some(_)) => return Err(()), // lone tagValue
    }

    // Left-fold the (possibly tag-augmented) scalar conjuncts with AND.
    let mut iter = scalars.into_iter();
    let first = iter.next().ok_or(())?;
    Ok(iter.fold(first, |acc, f| Filter::And(Box::new(acc), Box::new(f))))
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- helpers -----------------------------------------------------------

    /// Parse and unwrap, failing the test with the input on a rejection.
    fn ok(input: &str) -> Filter {
        parse(input).unwrap_or_else(|_| panic!("expected `{input}` to parse, but it was rejected"))
    }

    /// Assert that `input` is rejected with the fixed-string 400 (no-leak contract).
    fn assert_rejected(input: &str) {
        match parse(input) {
            Err(ApiError::BadRequest { message }) => {
                assert_eq!(
                    message, "invalid $filter",
                    "wrong rejection message for `{input}`"
                );
            }
            Err(other) => panic!("expected BadRequest for `{input}`, got {other:?}"),
            Ok(f) => panic!("expected `{input}` to be rejected, but it parsed to {f:?}"),
        }
    }

    /// Render a filter to (fragment, args) starting at `$1`.
    fn render(f: &Filter) -> (String, Vec<String>) {
        let mut next = 1;
        let mut args = Vec::new();
        let sql = f.to_sql(&mut next, &mut args);
        (sql, args)
    }

    // ---- grammar / behavior ------------------------------------------------

    #[test]
    fn parses_resource_type_eq() {
        let f = ok("resourceType eq 'Microsoft.Storage/storageAccounts'");
        assert_eq!(
            f,
            Filter::Eq {
                field: Field::ResourceType,
                value: "Microsoft.Storage/storageAccounts".to_string(),
            }
        );
    }

    #[test]
    fn parses_location_eq() {
        let f = ok("location eq 'eastus'");
        assert_eq!(
            f,
            Filter::Eq {
                field: Field::Location,
                value: "eastus".to_string(),
            }
        );
    }

    #[test]
    fn folds_tag_pair_into_single_tagpair() {
        // D-01: tagName + tagValue fold into ONE TagPair, not two Eq clauses.
        let f = ok("tagName eq 'env' and tagValue eq 'prod'");
        assert_eq!(
            f,
            Filter::TagPair {
                key: "env".to_string(),
                value: "prod".to_string(),
            }
        );
    }

    #[test]
    fn folds_tag_pair_regardless_of_order() {
        // tagValue first, tagName second — still one TagPair.
        let f = ok("tagValue eq 'prod' and tagName eq 'env'");
        assert_eq!(
            f,
            Filter::TagPair {
                key: "env".to_string(),
                value: "prod".to_string(),
            }
        );
    }

    #[test]
    fn parses_and_of_two_scalar_fields() {
        let f = ok("location eq 'eastus' and resourceType eq 'X'");
        assert_eq!(
            f,
            Filter::And(
                Box::new(Filter::Eq {
                    field: Field::Location,
                    value: "eastus".to_string()
                }),
                Box::new(Filter::Eq {
                    field: Field::ResourceType,
                    value: "X".to_string()
                }),
            )
        );
    }

    #[test]
    fn parses_or_of_two_scalar_fields() {
        let f = ok("location eq 'eastus' or location eq 'westus'");
        assert_eq!(
            f,
            Filter::Or(
                Box::new(Filter::Eq {
                    field: Field::Location,
                    value: "eastus".to_string()
                }),
                Box::new(Filter::Eq {
                    field: Field::Location,
                    value: "westus".to_string()
                }),
            )
        );
    }

    #[test]
    fn and_binds_tighter_than_or() {
        // a eq '1' and b eq '2' or c eq '3'  ==>  Or(And(a, b), c)
        let f = ok("location eq '1' and resourceType eq '2' or location eq '3'");
        assert_eq!(
            f,
            Filter::Or(
                Box::new(Filter::And(
                    Box::new(Filter::Eq {
                        field: Field::Location,
                        value: "1".to_string()
                    }),
                    Box::new(Filter::Eq {
                        field: Field::ResourceType,
                        value: "2".to_string()
                    }),
                )),
                Box::new(Filter::Eq {
                    field: Field::Location,
                    value: "3".to_string()
                }),
            )
        );
    }

    #[test]
    fn parses_escaped_quote_in_literal() {
        // '' inside a single-quoted literal is one literal quote.
        let f = ok("location eq 'east''us'");
        assert_eq!(
            f,
            Filter::Eq {
                field: Field::Location,
                value: "east'us".to_string()
            }
        );
    }

    // ---- rejection family --------------------------------------------------

    #[test]
    fn rejects_comparison_missing_literal() {
        assert_rejected("resourceType eq");
    }

    #[test]
    fn rejects_empty_input() {
        assert_rejected("");
        assert_rejected("   ");
    }

    #[test]
    fn rejects_unknown_field() {
        assert_rejected("foo eq 'x'");
    }

    #[test]
    fn rejects_unknown_operator() {
        assert_rejected("resourceType ne 'x'");
        assert_rejected("resourceType gt 'x'");
    }

    #[test]
    fn rejects_unbalanced_quote() {
        assert_rejected("location eq 'eastus");
    }

    #[test]
    fn lone_tag_name_is_key_existence() {
        // MOCK-06: `tagName eq 'env'` on its own is a tag-key presence filter.
        let f = ok("tagName eq 'env'");
        assert_eq!(
            f,
            Filter::TagExists {
                key: "env".to_string()
            }
        );
    }

    #[test]
    fn lone_tag_name_composes_with_and_or() {
        // tag-presence must compose like any other predicate. fold_conjuncts appends the
        // tag clause after the scalar conjuncts (same as TagPair), so the order is
        // And(location, TagExists) — AND is commutative, so this is equivalent.
        let f = ok("tagName eq 'env' and location eq 'eastus'");
        assert_eq!(
            f,
            Filter::And(
                Box::new(Filter::Eq {
                    field: Field::Location,
                    value: "eastus".to_string()
                }),
                Box::new(Filter::TagExists {
                    key: "env".to_string()
                }),
            )
        );
    }

    #[test]
    fn rejects_lone_tag_value() {
        // A value with no key has no standalone Azure semantics.
        assert_rejected("tagValue eq 'prod'");
    }

    // ---- translator: column mapping + bind order ---------------------------

    #[test]
    fn to_sql_maps_location_to_column_with_placeholder() {
        let f = Filter::Eq {
            field: Field::Location,
            value: "eastus".to_string(),
        };
        let (sql, args) = render(&f);
        assert_eq!(sql, "location = $1");
        assert_eq!(args, vec!["eastus".to_string()]);
    }

    #[test]
    fn to_sql_maps_resource_type_to_type_column() {
        let f = Filter::Eq {
            field: Field::ResourceType,
            value: "Microsoft.Storage/storageAccounts".to_string(),
        };
        let (sql, args) = render(&f);
        assert_eq!(sql, "type = $1");
        assert_eq!(args, vec!["Microsoft.Storage/storageAccounts".to_string()]);
    }

    #[test]
    fn to_sql_tag_pair_is_single_predicate_two_binds() {
        // Pitfall 2: exactly ONE `tags ->> $k = $v` predicate, two bound args (key then value).
        let f = Filter::TagPair {
            key: "env".to_string(),
            value: "prod".to_string(),
        };
        let (sql, args) = render(&f);
        assert_eq!(sql, "tags ->> $1 = $2");
        assert_eq!(args, vec!["env".to_string(), "prod".to_string()]);
        // Only one `tags`-touching clause.
        assert_eq!(sql.matches("tags").count(), 1);
    }

    #[test]
    fn to_sql_tag_exists_is_jsonb_exists_one_bind() {
        // Lone tagName → `jsonb_exists(tags, $N)`, one bound arg (the key), no value.
        let f = Filter::TagExists {
            key: "env".to_string(),
        };
        let (sql, args) = render(&f);
        assert_eq!(sql, "jsonb_exists(tags, $1)");
        assert_eq!(args, vec!["env".to_string()]);
    }

    #[test]
    fn to_sql_and_advances_placeholder_indices() {
        let f = Filter::And(
            Box::new(Filter::Eq {
                field: Field::Location,
                value: "eastus".to_string(),
            }),
            Box::new(Filter::Eq {
                field: Field::ResourceType,
                value: "X".to_string(),
            }),
        );
        let (sql, args) = render(&f);
        assert_eq!(sql, "(location = $1 AND type = $2)");
        assert_eq!(args, vec!["eastus".to_string(), "X".to_string()]);
    }

    #[test]
    fn to_sql_or_uses_or_keyword() {
        let f = Filter::Or(
            Box::new(Filter::Eq {
                field: Field::Location,
                value: "eastus".to_string(),
            }),
            Box::new(Filter::Eq {
                field: Field::Location,
                value: "westus".to_string(),
            }),
        );
        let (sql, _args) = render(&f);
        assert_eq!(sql, "(location = $1 OR location = $2)");
    }

    #[test]
    fn to_sql_honors_seed_index() {
        // Placeholders start past the handler's fixed binds (e.g. $4).
        let f = Filter::Eq {
            field: Field::Location,
            value: "eastus".to_string(),
        };
        let mut next = 4;
        let mut args = Vec::new();
        let sql = f.to_sql(&mut next, &mut args);
        assert_eq!(sql, "location = $4");
        assert_eq!(next, 5, "next_param advanced past the consumed placeholder");
    }

    // ---- injection-safety invariant (the highest-risk unit) ----------------

    #[test]
    fn placeholders_only_no_user_literal_in_fragment() {
        // A SQL-injection-shaped literal must be carried ENTIRELY in args, never in the fragment.
        let attack = "x' OR '1'='1";
        let f = parse(&format!("location eq '{}'", attack.replace('\'', "''")))
            .expect("escaped-injection literal should parse as a plain string");
        let (sql, args) = render(&f);
        // The fragment is placeholders + column names only — no fragment of the attack literal.
        assert_eq!(sql, "location = $1");
        assert!(
            !sql.contains("OR '1'"),
            "fragment must not contain the attack literal"
        );
        assert!(
            !sql.contains(attack),
            "fragment must not contain the attack literal"
        );
        // The literal is carried verbatim as data.
        assert_eq!(args, vec![attack.to_string()]);
    }

    #[test]
    fn placeholders_count_equals_args_len_for_compound() {
        // For any parsed filter, #($N) in the fragment == args.len().
        let f = ok(
            "location eq 'a' and tagName eq 'env' and tagValue eq 'prod' or resourceType eq 'X'",
        );
        let (sql, args) = render(&f);
        let placeholder_count = sql.matches('$').count();
        assert_eq!(
            placeholder_count,
            args.len(),
            "every placeholder must have exactly one bound arg (fragment: {sql:?}, args: {args:?})"
        );
        // Sanity: 1 (location) + 2 (tag pair) + 1 (resourceType) = 4 binds.
        assert_eq!(args.len(), 4);
    }
}
