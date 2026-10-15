use crate::{canonical, journal, model::*, pgoutput::Decoder, state::State};
use serde_json::json;
use std::{
    fs,
    io::Write,
    sync::atomic::{AtomicU64, Ordering},
};

fn text(s: &str) -> Cell {
    Cell::Text(s.into())
}
fn relation() -> Relation {
    Relation {
        id: 42,
        namespace: "public".into(),
        name: "orders".into(),
        columns: vec![
            Column {
                name: "id".into(),
                oid: 23,
                key: true,
            },
            Column {
                name: "payload".into(),
                oid: 25,
                key: false,
            },
        ],
    }
}
fn baseline() -> State {
    let mut s = State::default();
    s.apply(
        &Entry::Start {
            identity: json!({"source":"test"}),
        },
        true,
    )
    .unwrap();
    s.apply(
        &Entry::Schema {
            relation: relation(),
        },
        true,
    )
    .unwrap();
    s.apply(
        &Entry::Snapshot {
            relation_id: 42,
            rows: vec![vec![text("1"), text("large toast")]],
        },
        true,
    )
    .unwrap();
    s.apply(&Entry::Seal { lsn: "0/10".into() }, true).unwrap();
    s
}
fn tx(changes: Vec<Change>, lsn: &str) -> Entry {
    Entry::Transaction {
        tx: Transaction {
            xid: 7,
            commit_lsn: lsn.into(),
            end_lsn: lsn.into(),
            time_micros_pg: 0,
            changes,
        },
    }
}
fn temp() -> std::path::PathBuf {
    static N: AtomicU64 = AtomicU64::new(0);
    std::env::temp_dir().join(format!(
        "rowglass-test-{}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::SeqCst)
    ))
}

#[test]
fn exact_numeric_and_typed_json() {
    for (a, b) in [
        ("001.000", "1e0"),
        ("-0.000", "0"),
        ("123.400e2", "12340"),
        ("1e-7", "0.0000001"),
    ] {
        assert_eq!(
            canonical::decimal(a).unwrap(),
            canonical::decimal(b).unwrap()
        );
    }
    assert_ne!(
        canonical::decimal("9007199254740992").unwrap(),
        canonical::decimal("9007199254740993").unwrap()
    );
    assert_eq!(
        canonical::value(3802, &text(r#"{"b":2,"a":1.0}"#)).unwrap(),
        canonical::value(3802, &text(r#"{"a":1,"b":2.00}"#)).unwrap()
    );
    assert_ne!(
        canonical::value(3802, &text("1")).unwrap(),
        canonical::value(3802, &text(r#"{"$number":"1e0"}"#)).unwrap()
    );
    assert_ne!(
        canonical::value(25, &Cell::Null).unwrap(),
        canonical::value(25, &text("null")).unwrap()
    );
    assert!(canonical::decimal("1e100000000").is_err());
    assert_eq!(canonical::value(99999, &text("same")).unwrap(), None);
}

#[test]
fn timestamps_float_and_bytes_are_explicit() {
    assert_eq!(
        canonical::value(1184, &text("2026-01-01 00:00:00+00")).unwrap(),
        canonical::value(1184, &text("2025-12-31T17:00:00-07:00")).unwrap()
    );
    assert_eq!(
        canonical::value(17, &text("\\x00AB")).unwrap(),
        canonical::value(17, &text("00ab")).unwrap()
    );
    assert_eq!(
        canonical::value(701, &text("-0")).unwrap(),
        canonical::value(701, &text("0.0")).unwrap()
    );
    assert_ne!(
        canonical::value(701, &text("0.10000000000000001")).unwrap(),
        canonical::value(701, &text("0.10000000000000002")).unwrap()
    );
}

#[test]
fn toast_key_changes_deletes_and_time_travel_state() {
    let mut s = baseline();
    s.apply(
        &tx(
            vec![Change {
                op: "update".into(),
                relation: relation(),
                old: Some(vec![text("1"), Cell::Null]),
                new: Some(vec![text("2"), Cell::Unchanged]),
            }],
            "0/20",
        ),
        true,
    )
    .unwrap();
    assert_eq!(s.tables["public.orders"].len(), 1);
    let row = s.tables["public.orders"].values().next().unwrap();
    assert_eq!(row["payload"], text("large toast"));
    assert_eq!(row["id"], text("2"));
    s.apply(
        &tx(
            vec![Change {
                op: "delete".into(),
                relation: relation(),
                old: Some(vec![text("2"), Cell::Null]),
                new: None,
            }],
            "0/30",
        ),
        true,
    )
    .unwrap();
    assert!(s.tables["public.orders"].is_empty());
    assert!(s.apply(&tx(vec![], "0/20"), true).is_err());
}

#[test]
fn audit_does_not_confuse_lag_with_proven_loss() {
    let s = baseline();
    let mut target = json!({"tables":[{"source_table":"public.orders","rows":[]}],"consistency":{"mode":"live"}});
    let live = s
        .audit(&serde_json::from_value(target.clone()).unwrap())
        .unwrap();
    assert_eq!(live["status"], "live_observation");
    assert_eq!(live["totals"]["missing"], 1);
    target["consistency"] = json!({"mode":"controlled_quiescent","source_cut":"0/10"});
    assert_eq!(
        s.audit(&serde_json::from_value(target.clone()).unwrap())
            .unwrap()["status"],
        "diverged_at_cut"
    );
    target["tables"][0]["rows"] = json!([{"id":text("1"),"payload":text("large toast")}]);
    assert_eq!(
        s.audit(&serde_json::from_value(target.clone()).unwrap())
            .unwrap()["status"],
        "matched_at_cut"
    );
    target["tables"][0]["rows"] = json!([{"id":text("1"),"payload":Cell::Unknown}]);
    assert_eq!(
        s.audit(&serde_json::from_value(target).unwrap()).unwrap()["status"],
        "inconclusive"
    );
}

#[test]
fn durable_journal_torn_tail_and_corruption() {
    let path = temp();
    {
        let (mut j, _) = journal::Journal::open(&path).unwrap();
        j.append(&Entry::Start {
            identity: json!({"id":"test"}),
        })
        .unwrap();
        assert!(journal::Journal::open(&path).is_err());
    }
    let original = fs::read(&path).unwrap();
    for suffix in [vec![1, 2], vec![20, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3]] {
        fs::write(&path, &original).unwrap();
        fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(&suffix)
            .unwrap();
        {
            let (_, prior) = journal::Journal::open(&path).unwrap();
            assert_eq!(prior.unwrap().trailing_bytes, suffix.len() as u64);
        }
        assert_eq!(fs::read(&path).unwrap(), original);
    }
    let mut corrupt = original.clone();
    *corrupt.last_mut().unwrap() ^= 1;
    fs::write(&path, corrupt).unwrap();
    assert!(journal::scan(&path).is_err());
    fs::remove_file(path).unwrap();
}

#[test]
fn protocol_boundaries_and_transaction_commit() {
    let mut d = Decoder::default();
    d.relations.insert(42, relation());
    let mut begin = vec![b'B'];
    begin.extend(16u64.to_be_bytes());
    begin.extend(0i64.to_be_bytes());
    begin.extend(7u32.to_be_bytes());
    for cut in 0..begin.len() {
        assert!(Decoder::default().feed(&begin[..cut]).is_err());
    }
    assert!(d.feed(&begin).unwrap().is_none());
    let mut insert = vec![b'I'];
    insert.extend(42u32.to_be_bytes());
    insert.push(b'N');
    insert.extend(2u16.to_be_bytes());
    for s in ["1", "hello"] {
        insert.push(b't');
        insert.extend((s.len() as u32).to_be_bytes());
        insert.extend(s.as_bytes());
    }
    assert!(d.feed(&insert).unwrap().is_none());
    let mut commit = vec![b'C', 0];
    commit.extend(16u64.to_be_bytes());
    commit.extend(24u64.to_be_bytes());
    commit.extend(0i64.to_be_bytes());
    let tx = d.feed(&commit).unwrap().unwrap();
    assert_eq!(tx.end_lsn, "0/18");
    assert_eq!(tx.changes.len(), 1);
    assert_eq!(tx.changes[0].new.as_ref().unwrap()[1], text("hello"));
    assert!(d.feed(&commit).is_err());
}
