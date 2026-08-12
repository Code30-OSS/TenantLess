//! Structural reader-inventory gate — the Rust MIRROR of the canonical pytest gate
//! (`tests/test_reader_inventory_gate.py`).
//!
//! A Rust-only CI run (a developer who never runs pytest) must ALSO fail on a resolver bypass, so
//! this re-implements the SAME byte scan — same `synthetic.resources(?![_a-z])` match, the same
//! closed sanctioned-category set, the same single-use K=1 `SYNRES-ALLOW[<category>]: <reason>`
//! marker association, and the same source-aware Rust/Python/SQL comment + docstring stripping with
//! executable SQL string literals kept in scope — over the SAME shipped source set, and asserts the
//! IDENTICAL violation set over the shared golden fixtures (`tests/fixtures/reader_inventory_golden/`
//! + its committed `expected_violations.tsv` manifest). The enforcement boundary cannot diverge
//! between the two CI jobs.
//!
//! DB-free (pure source scan) and non-vacuous: the file-count floor + the golden-fixture positive
//! controls (planted reader flagged; only valid single-use markers accepted; comment-immunity per
//! language; runtime SQL string literal still flagged; DDL definitional references exempt) prove the
//! gate can actually fail — mirroring `explain_plan_gate.rs`'s DB-free helper-test discipline.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

const CATEGORIES: [&str; 5] = [
    "schema/provisioning",
    "reset",
    "baseline-replay",
    "drift-hashing",
    "generation/writer",
];
const MIN_REASON_CHARS: usize = 3;
const OCC: &str = "synthetic.resources";

fn repo_root() -> PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/mock-server; the shared fixtures + source live at the repo root.
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("mock-server has a parent (repo root)")
        .to_path_buf()
}

fn read_normalized(p: &Path) -> Option<String> {
    std::fs::read_to_string(p)
        .ok()
        .map(|s| s.replace("\r\n", "\n").replace('\r', "\n"))
}

fn lang_for(p: &Path) -> Option<&'static str> {
    match p.extension().and_then(|e| e.to_str()) {
        Some("rs") => Some("rust"),
        Some("py") => Some("python"),
        Some("sql") => Some("sql"),
        _ => None,
    }
}

fn is_category(s: &str) -> bool {
    CATEGORIES.contains(&s)
}

// --------------------------------------------------------------------------- //
// Source-aware stripping (mirror of the Python state machines)
// --------------------------------------------------------------------------- //
fn seq_at(chars: &[char], i: usize, pat: &[char]) -> bool {
    i + pat.len() <= chars.len() && chars[i..i + pat.len()] == *pat
}

fn blank_cfg_test(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let n = chars.len();
    let mut out = chars.clone();
    let pat: Vec<char> = "#[cfg(test)]".chars().collect();
    let mut search = 0usize;
    while search < n {
        // locate the next "#[cfg(test)]"
        let mut start = None;
        let mut k = search;
        while k + pat.len() <= n {
            if chars[k..k + pat.len()] == pat[..] {
                start = Some(k);
                break;
            }
            k += 1;
        }
        let start = match start {
            Some(s) => s,
            None => break,
        };
        // opening brace of the module
        let mut brace = None;
        let mut j = start;
        while j < n {
            if chars[j] == '{' {
                brace = Some(j);
                break;
            }
            j += 1;
        }
        let b = match brace {
            Some(x) => x,
            None => break,
        };
        let mut i = b;
        let mut depth: i32 = 0;
        let mut end = n;
        while i < n {
            if seq_at(&chars, i, &['/', '/']) {
                while i < n && chars[i] != '\n' {
                    i += 1;
                }
                continue;
            }
            if seq_at(&chars, i, &['/', '*']) {
                let mut m = i + 2;
                while m + 1 < n && !(chars[m] == '*' && chars[m + 1] == '/') {
                    m += 1;
                }
                i = if m + 1 < n { m + 2 } else { n };
                continue;
            }
            let c = chars[i];
            if c == '"' || c == '\'' {
                i += 1;
                while i < n {
                    if chars[i] == '\\' {
                        i += 2;
                        continue;
                    }
                    if chars[i] == c {
                        i += 1;
                        break;
                    }
                    i += 1;
                }
                continue;
            }
            if c == '{' {
                depth += 1;
            } else if c == '}' {
                depth -= 1;
                if depth == 0 {
                    end = i + 1;
                    break;
                }
            }
            i += 1;
        }
        for x in start..end.min(n) {
            if out[x] != '\n' {
                out[x] = ' ';
            }
        }
        search = end;
    }
    out.into_iter().collect()
}

fn blank_comments(text: &str, lang: &str) -> String {
    let line_tok: Vec<char> = match lang {
        "rust" => vec!['/', '/'],
        "sql" => vec!['-', '-'],
        _ => vec!['#'],
    };
    let has_block = lang == "rust" || lang == "sql";
    let has_triple = lang == "python";
    let chars: Vec<char> = text.chars().collect();
    let n = chars.len();
    let mut out: Vec<char> = Vec::with_capacity(n);
    let mut i = 0;
    while i < n {
        if seq_at(&chars, i, &line_tok) {
            while i < n && chars[i] != '\n' {
                out.push(' ');
                i += 1;
            }
            continue;
        }
        if has_block && seq_at(&chars, i, &['/', '*']) {
            let mut m = i + 2;
            while m + 1 < n && !(chars[m] == '*' && chars[m + 1] == '/') {
                m += 1;
            }
            let end = if m + 1 < n { m + 2 } else { n };
            for k in i..end {
                out.push(if chars[k] == '\n' { '\n' } else { ' ' });
            }
            i = end;
            continue;
        }
        let c = chars[i];
        if has_triple
            && (seq_at(&chars, i, &['"', '"', '"']) || seq_at(&chars, i, &['\'', '\'', '\'']))
        {
            let q = chars[i];
            let mut m = i + 3;
            while m + 2 < n && !(chars[m] == q && chars[m + 1] == q && chars[m + 2] == q) {
                m += 1;
            }
            let end = if m + 2 < n { m + 3 } else { n };
            for k in i..end {
                out.push(if chars[k] == '\n' { '\n' } else { ' ' });
            }
            i = end;
            continue;
        }
        if c == '"' || c == '\'' {
            out.push(c);
            i += 1;
            while i < n {
                let d = chars[i];
                if d == '\\' && (lang == "rust" || lang == "python") {
                    out.push(d);
                    if i + 1 < n {
                        out.push(chars[i + 1]);
                        i += 2;
                        continue;
                    }
                    i += 1;
                    continue;
                }
                if lang == "sql" && d == c && i + 1 < n && chars[i + 1] == c {
                    out.push(d);
                    out.push(chars[i + 1]);
                    i += 2;
                    continue;
                }
                out.push(d);
                i += 1;
                if d == c {
                    break;
                }
            }
            continue;
        }
        out.push(c);
        i += 1;
    }
    out.into_iter().collect()
}

fn strip(text: &str, lang: &str) -> String {
    let t = if lang == "rust" {
        blank_cfg_test(text)
    } else {
        text.to_string()
    };
    blank_comments(&t, lang)
}

// --------------------------------------------------------------------------- //
// Marker + DDL predicates
// --------------------------------------------------------------------------- //
fn line_has_valid_marker(line: &str) -> bool {
    let tag = "SYNRES-ALLOW[";
    let mut idx = 0;
    while let Some(rel) = line[idx..].find(tag) {
        let s = idx + rel + tag.len();
        if let Some(close_rel) = line[s..].find(']') {
            let cat = line[s..s + close_rel].trim();
            let after = &line[s + close_rel + 1..];
            if let Some(reason) = after.strip_prefix(':') {
                if is_category(cat) && reason.trim().chars().count() >= MIN_REASON_CHARS {
                    return true;
                }
            }
        }
        idx = s;
    }
    false
}

fn is_ddl_definition(line: &str, start: usize, end: usize) -> bool {
    let pre = line[..start].trim_end();
    let word: String = pre
        .chars()
        .rev()
        .take_while(|c| c.is_ascii_alphabetic() || *c == '_')
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    let wu = word.to_uppercase();
    if wu.is_empty() {
        return false;
    }
    if wu == "TABLE" || wu == "INDEX" || wu == "REFERENCES" {
        return true;
    }
    if wu == "ON" {
        return line[end..].trim_start().starts_with('(');
    }
    false
}

// --------------------------------------------------------------------------- //
// The scanner
// --------------------------------------------------------------------------- //
fn find_violations(text: &str, lang: &str) -> Vec<(usize, String, String)> {
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    let raw_lines: Vec<&str> = normalized.split('\n').collect();
    let stripped = strip(&normalized, lang);
    let stripped_lines: Vec<&str> = stripped.split('\n').collect();

    let mut marker_lines: HashSet<usize> = HashSet::new();
    for (i, rl) in raw_lines.iter().enumerate() {
        if line_has_valid_marker(rl) {
            marker_lines.insert(i);
        }
    }

    let mut occurrences: Vec<(usize, String)> = Vec::new();
    for (i, sl) in stripped_lines.iter().enumerate() {
        for (start, m) in sl.match_indices(OCC) {
            let end = start + m.len();
            // lookahead (?![_a-z])
            if let Some(ch) = sl[end..].chars().next() {
                if ch == '_' || ch.is_ascii_lowercase() {
                    continue;
                }
            }
            if is_ddl_definition(sl, start, end) {
                continue;
            }
            occurrences.push((i, sl.trim().to_string()));
        }
    }

    let occ_lines: HashSet<usize> = occurrences.iter().map(|(l, _)| *l).collect();
    let mut consumed: HashSet<usize> = HashSet::new();
    let mut violations: Vec<(usize, String, String)> = Vec::new();

    for (li, snip) in &occurrences {
        let li = *li;
        if marker_lines.contains(&li) && !consumed.contains(&li) {
            consumed.insert(li);
            continue;
        }
        if li >= 1 && marker_lines.contains(&(li - 1)) && !consumed.contains(&(li - 1)) {
            consumed.insert(li - 1);
            continue;
        }
        violations.push((li + 1, "unmarked".to_string(), snip.clone()));
    }

    let mut ml: Vec<usize> = marker_lines.into_iter().collect();
    ml.sort_unstable();
    for m in ml {
        if occ_lines.contains(&m) || occ_lines.contains(&(m + 1)) {
            continue;
        }
        violations.push((m + 1, "floating".to_string(), raw_lines[m].trim().to_string()));
    }

    violations
}

// --------------------------------------------------------------------------- //
// Candidate-file sweep (positive enumeration; same scope as the pytest gate)
// --------------------------------------------------------------------------- //
fn collect(dir: &Path, ext: &str, out: &mut Vec<PathBuf>) {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let p = entry.path();
        let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if p.is_dir() {
            if matches!(
                name,
                "target" | "__pycache__" | ".git" | ".venv" | "node_modules" | "dist" | "coverage"
            ) {
                continue;
            }
            collect(&p, ext, out);
        } else if p.extension().and_then(|e| e.to_str()) == Some(ext) {
            out.push(p);
        }
    }
}

fn candidate_files() -> Vec<PathBuf> {
    let root = repo_root();
    let mut files: Vec<PathBuf> = Vec::new();
    collect(&root.join("mock-server").join("src"), "rs", &mut files);
    collect(&root.join("sql"), "sql", &mut files);
    collect(&root.join("src").join("tenantless"), "py", &mut files);
    // The one runtime-read SQL under tests/common (explicitly in scope; the rest of tests/ is not).
    let common = root.join("mock-server").join("tests").join("common");
    if let Ok(entries) = std::fs::read_dir(&common) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) == Some("sql") {
                files.push(p);
            }
        }
    }
    files
}

fn golden_dir() -> PathBuf {
    repo_root()
        .join("tests")
        .join("fixtures")
        .join("reader_inventory_golden")
}

fn load_manifest() -> Vec<(String, String, usize)> {
    let text = read_normalized(&golden_dir().join("expected_violations.tsv"))
        .expect("shared expected-violations manifest is readable");
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let parts: Vec<&str> = line.split('\t').collect();
        assert_eq!(parts.len(), 3, "manifest row must be name<TAB>lang<TAB>count: {line:?}");
        out.push((
            parts[0].to_string(),
            parts[1].to_string(),
            parts[2].parse::<usize>().expect("count is an integer"),
        ));
    }
    out
}

// =========================================================================== //
// Positive controls
// =========================================================================== //
#[test]
fn positive_control_planted_raw_reader_is_flagged() {
    let content = read_normalized(&repo_root().join("tests").join("fixtures").join("planted_raw_reader.txt"))
        .expect("planted fixture readable");
    let v = find_violations(&content, "sql");
    assert_eq!(v.len(), 1, "planted reader must yield exactly one violation: {v:?}");
    assert_eq!(v[0].1, "unmarked");
}

#[test]
fn positive_control_marker_validation() {
    let case = |marker: &str| {
        let text = format!("cur.execute(\"SELECT id FROM synthetic.resources\")  # {marker}");
        find_violations(&text, "python").len()
    };
    assert_eq!(case("SYNRES-ALLOW[reset]: a valid non-empty reason"), 0, "valid marker accepted");
    assert_eq!(case("SYNRES-ALLOW[bogus]: some reason"), 1, "unknown category rejected");
    assert_eq!(case("SYNRES-ALLOW[reset]:"), 1, "missing reason rejected");
    assert_eq!(case("SYNRES-ALLOW[reset]"), 1, "bare (no colon) rejected");
}

#[test]
fn positive_control_out_of_window_k_gt_1_rejected() {
    let text = "# SYNRES-ALLOW[reset]: too far above\nx = 1\ncur.execute(\"SELECT id FROM synthetic.resources\")\n";
    let v = find_violations(text, "python");
    assert!(
        v.iter().any(|(ln, k, _)| *ln == 3 && k == "unmarked"),
        "K>1 marker must not sanction the occurrence: {v:?}"
    );
}

#[test]
fn positive_control_regex_safety() {
    for probe in [
        "SELECT * FROM synthetic.arm_resolved_resources",
        "SELECT * FROM synthetic.resource_groups",
        "ALTER TABLE x DROP CONSTRAINT resources_pkey",
    ] {
        assert!(find_violations(probe, "sql").is_empty(), "must not match: {probe}");
    }
    assert_eq!(find_violations("SELECT * FROM synthetic.resources", "sql").len(), 1);
}

/// Negative control: the gate genuinely PANICS when a planted bypass is asserted green — proving
/// the gate can fail (a gate that cannot fail is where the last hole lives).
#[test]
#[should_panic(expected = "planted bypass")]
fn negative_control_gate_can_fail() {
    let planted = "SELECT id FROM synthetic.resources WHERE 1=1";
    let v = find_violations(planted, "sql");
    assert!(v.is_empty(), "planted bypass must be caught (this assert is expected to fire)");
}

// =========================================================================== //
// Python <-> Rust parity over the shared golden fixtures + manifest
// =========================================================================== //
#[test]
fn parity_golden_fixtures_match_shared_manifest() {
    let manifest = load_manifest();
    assert!(!manifest.is_empty(), "shared manifest is empty — parity would be vacuous");
    for (name, lang, expected) in manifest {
        let path = golden_dir().join(&name);
        let text = read_normalized(&path).unwrap_or_else(|| panic!("missing golden fixture {name}"));
        let got = find_violations(&text, &lang).len();
        assert_eq!(got, expected, "parity mismatch on {name}: expected {expected}, got {got}");
    }
}

// =========================================================================== //
// Non-vacuity floor + the real thing
// =========================================================================== //
#[test]
fn scanner_covers_a_meaningful_number_of_files() {
    let n = candidate_files().len();
    assert!(n > 50, "only {n} files scanned -- the sweep is not finding the tree");
}

#[test]
fn real_tree_is_green() {
    let root = repo_root();
    let mut offenders: Vec<String> = Vec::new();
    for path in candidate_files() {
        let lang = match lang_for(&path) {
            Some(l) => l,
            None => continue,
        };
        let text = match read_normalized(&path) {
            Some(t) => t,
            None => continue,
        };
        for (line_no, kind, snippet) in find_violations(&text, lang) {
            let rel = path.strip_prefix(&root).unwrap_or(&path).display();
            let head: String = snippet.chars().take(100).collect();
            offenders.push(format!("{rel}:{line_no}: [{kind}] {head}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "Unsanctioned synthetic.resources references (each direct reader needs its OWN single-use \
         SYNRES-ALLOW[<category>]: <reason> marker):\n{}",
        offenders.join("\n")
    );
}
