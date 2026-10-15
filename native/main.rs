mod canonical;
mod journal;
mod model;
mod pgoutput;
mod state;
#[cfg(test)]
mod tests;

use anyhow::{Context, Result, bail, ensure};
use model::*;
use serde_json::{Value, json};
use std::{
    io::{BufRead, Write},
    path::Path,
};

fn emit(v: Value) -> Result<()> {
    let mut out = std::io::stdout().lock();
    serde_json::to_writer(&mut out, &v)?;
    writeln!(&mut out)?;
    out.flush()?;
    Ok(())
}

fn ingest(path: &Path) -> Result<()> {
    let (mut journal, prior) = journal::Journal::open(path)?;
    let mut state = state::State::default();
    let mut decoder = pgoutput::Decoder::default();
    let recovered_bytes = prior.as_ref().map_or(0, |s| s.trailing_bytes);
    if prior.is_some() {
        journal::visit(path, |entry| state.apply(&entry, false))?;
    }
    for rel in state.relations.values() {
        decoder.relations.insert(rel.id, rel.clone());
        decoder.primary_keys.insert(
            rel.id,
            rel.columns
                .iter()
                .filter(|c| c.key)
                .map(|c| c.name.clone())
                .collect(),
        );
    }
    emit(
        json!({"ready":true,"summary":state.summary(),"recovered_trailing_bytes":recovered_bytes}),
    )?;
    for line in std::io::stdin().lock().lines() {
        let line = line?;
        ensure!(
            line.len() <= 256 * 1024 * 1024,
            "input command exceeds limit"
        );
        let command: Value = serde_json::from_str(&line)?;
        let mut ack = None;
        match command["op"].as_str().context("command requires op")? {
            "wal" => {
                ensure!(state.sealed, "WAL before bootstrap seal");
                let bytes = pgoutput::unhex(command["hex"].as_str().context("WAL requires hex")?)?;
                if let Some(tx) = decoder.feed(&bytes)? {
                    if lsn_number(&tx.end_lsn)? > lsn_number(&state.last_lsn)? {
                        let entry = Entry::Transaction { tx };
                        // Validate before persisting. ACK is emitted only after append's sync_data.
                        state.apply(&entry, false)?;
                        journal.append(&entry)?;
                    }
                    ack = Some(state.last_lsn.clone());
                }
            }
            "entry" => {
                let entry: Entry = serde_json::from_value(command["entry"].clone())?;
                ensure!(
                    !matches!(entry, Entry::Transaction { .. }),
                    "transactions must be decoded from pgoutput"
                );
                if let Entry::Schema { relation } = &entry {
                    decoder.relations.insert(relation.id, relation.clone());
                    decoder.primary_keys.insert(
                        relation.id,
                        relation
                            .columns
                            .iter()
                            .filter(|c| c.key)
                            .map(|c| c.name.clone())
                            .collect(),
                    );
                }
                state.apply(&entry, false)?;
                journal.append(&entry)?;
                if state.sealed {
                    ack = Some(state.last_lsn.clone());
                }
            }
            "status" => {}
            _ => bail!("unknown ingest command"),
        }
        emit(json!({"ok":true,"ack":ack,"summary":state.summary()}))?;
    }
    Ok(())
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    ensure!(
        args.len() >= 2,
        "usage: rowglass-core ingest|inspect|export|audit|history JOURNAL [TARGET_JSON|TABLE KEY] [--at LSN]"
    );
    let path = Path::new(&args[1]);
    if args[0] == "ingest" {
        return ingest(path);
    }
    if args[0] == "stream" {
        let mut relations = std::collections::HashMap::new();
        journal::visit(path, |entry| {
            let mut value = serde_json::to_value(&entry)?;
            match entry {
                Entry::Schema { relation } => {
                    relations.insert(relation.id, relation);
                }
                Entry::Snapshot { relation_id, rows } => {
                    let rel = relations
                        .get(&relation_id)
                        .context("snapshot without schema")?;
                    value["keys"] = json!(
                        rows.iter()
                            .map(|cells| state::key(rel, &state::row_from(rel, cells)?))
                            .collect::<Result<Vec<_>>>()?
                    );
                }
                Entry::Transaction { tx } => {
                    for (i, change) in tx.changes.iter().enumerate() {
                        let rel = &change.relation;
                        for (label, cells) in [("old_key", &change.old), ("new_key", &change.new)] {
                            value["tx"]["changes"][i][label] = json!(
                                cells
                                    .as_ref()
                                    .map(|cells| state::key(rel, &state::row_from(rel, cells)?))
                                    .transpose()?
                            );
                        }
                    }
                }
                _ => {}
            }
            emit(value)
        })?;
        return Ok(());
    }
    let mut state = state::State::default();
    let at = args
        .iter()
        .position(|a| a == "--at")
        .map(|i| args.get(i + 1).context("--at requires LSN"))
        .transpose()?
        .map(|s| lsn_number(s))
        .transpose()?;
    if args[0] == "history" {
        let table = args.get(2).context("history requires TABLE KEY")?;
        let key = args.get(3).context("history requires KEY")?;
        state.history_filter = Some([format!("{table}\0{key}")].into_iter().collect());
    }
    let scan = journal::visit(path, |entry| {
        if let Entry::Seal { lsn } = &entry {
            ensure!(
                at.is_none_or(|n| lsn_number(lsn).is_ok_and(|v| n >= v)),
                "requested cut predates the recorded snapshot"
            );
        }
        if let Entry::Transaction { tx } = &entry
            && at.is_some_and(|n| lsn_number(&tx.end_lsn).map_or(true, |v| v > n))
        {
            return Ok(());
        }
        state.apply(&entry, matches!(args[0].as_str(), "export" | "history"))
    })?;
    let metadata = json!({"sha256":scan.digest,"valid_bytes":scan.valid_bytes,"ignored_incomplete_tail_bytes":scan.trailing_bytes});
    let mut result = match args[0].as_str() {
        "inspect" => state.summary(),
        "export" => state.export(2000),
        "audit" => {
            let target: state::Target = serde_json::from_reader(std::io::BufReader::new(
                std::fs::File::open(args.get(2).context("audit requires TARGET_JSON")?)?,
            ))?;
            let mut report = state.audit(&target)?;
            drop(target);
            let keys: std::collections::BTreeSet<String> = report["mismatches"]
                .as_array()
                .unwrap()
                .iter()
                .map(|m| {
                    format!(
                        "{}\0{}",
                        m["table"].as_str().unwrap(),
                        m["key"].as_str().unwrap()
                    )
                })
                .collect();
            if !keys.is_empty() {
                // Common matched audits avoid retaining every historical row. Only discrepancies
                // need a second streaming pass, with histories restricted to the selected keys.
                let history_cut = lsn_number(&state.last_lsn)?;
                drop(state);
                let mut history = state::State {
                    history_filter: Some(keys),
                    ..Default::default()
                };
                journal::visit(path, |entry| {
                    if let Entry::Transaction { tx } = &entry
                        && lsn_number(&tx.end_lsn)? > history_cut
                    {
                        return Ok(());
                    }
                    history.apply(&entry, true)
                })?;
                for mismatch in report["mismatches"].as_array_mut().unwrap() {
                    let key = format!(
                        "{}\0{}",
                        mismatch["table"].as_str().unwrap(),
                        mismatch["key"].as_str().unwrap()
                    );
                    mismatch["history"] = json!(history.history.get(&key));
                }
            }
            report
        }
        "history" => {
            let table = args.get(2).context("history requires TABLE KEY")?;
            let key = args.get(3).context("history requires KEY")?;
            json!({"source":state.summary(),"events":state.history.get(&format!("{table}\0{key}"))})
        }
        _ => bail!("unknown command"),
    };
    result["journal"] = metadata;
    emit(result)
}

fn main() {
    if let Err(error) = run() {
        eprintln!("rowglass: {error:#}");
        std::process::exit(1);
    }
}
