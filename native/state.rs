use crate::{canonical, model::*};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};

pub type Row = BTreeMap<String, Cell>;

#[derive(Deserialize)]
pub struct Target {
    pub tables: Vec<TargetTable>,
    pub consistency: Value,
    #[serde(default)]
    pub metadata: Value,
}
#[derive(Deserialize)]
pub struct TargetTable {
    pub source_table: String,
    pub rows: Vec<Row>,
}

#[derive(Default)]
pub struct State {
    pub relations: HashMap<u32, Relation>,
    pub tables: BTreeMap<String, BTreeMap<String, Row>>,
    pub identity: Value,
    pub sealed: bool,
    pub last_lsn: String,
    pub transactions: usize,
    pub changes: usize,
    pub history: BTreeMap<String, Vec<History>>,
    pub history_filter: Option<std::collections::BTreeSet<String>>,
}

#[derive(Clone, Serialize)]
pub struct History {
    pub sequence: usize,
    pub xid: Option<u32>,
    pub lsn: String,
    pub table: String,
    pub key: String,
    pub op: String,
    pub row: Option<Row>,
}

pub fn row_from(rel: &Relation, cells: &[Cell]) -> Result<Row> {
    ensure!(
        rel.columns.len() == cells.len(),
        "row width differs from schema"
    );
    Ok(rel
        .columns
        .iter()
        .zip(cells)
        .map(|(c, v)| (c.name.clone(), v.clone()))
        .collect())
}

pub fn key(rel: &Relation, row: &Row) -> Result<String> {
    let values: Vec<String> = rel
        .columns
        .iter()
        .filter(|c| c.key)
        .map(|c| {
            let cell = row.get(&c.name).context("missing primary key column")?;
            ensure!(!matches!(cell, Cell::Null), "null primary key column");
            canonical::value(c.oid, cell)?.context("unsupported or unknown primary key")
        })
        .collect::<Result<_>>()?;
    ensure!(
        !values.is_empty(),
        "relation {} requires a primary key",
        rel.label()
    );
    Ok(serde_json::to_string(&values)?)
}

impl State {
    // A history event names its row, operation, transaction and source cut explicitly.
    #[allow(clippy::too_many_arguments)]
    fn remember(
        &mut self,
        table: &str,
        key: &str,
        op: &str,
        row: Option<Row>,
        xid: Option<u32>,
        lsn: &str,
        keep_history: bool,
    ) {
        if !keep_history {
            return;
        }
        let history_key = format!("{table}\0{key}");
        if self
            .history_filter
            .as_ref()
            .is_some_and(|keys| !keys.contains(&history_key))
        {
            return;
        }
        self.history.entry(history_key).or_default().push(History {
            sequence: self.changes,
            xid,
            lsn: lsn.into(),
            table: table.into(),
            key: key.into(),
            op: op.into(),
            row,
        });
    }

    pub fn apply(&mut self, entry: &Entry, keep_history: bool) -> Result<()> {
        match entry {
            Entry::Start { identity } => {
                ensure!(self.identity.is_null(), "duplicate journal start");
                self.identity = identity.clone();
            }
            Entry::Schema { relation } => {
                ensure!(!self.sealed, "bootstrap schema after seal");
                self.tables.entry(relation.label()).or_default();
                self.relations.insert(relation.id, relation.clone());
            }
            Entry::Snapshot { relation_id, rows } => {
                ensure!(!self.sealed, "snapshot after seal");
                let rel = self
                    .relations
                    .get(relation_id)
                    .context("snapshot references unknown relation")?
                    .clone();
                let table = rel.label();
                for cells in rows {
                    let row = row_from(&rel, cells)?;
                    let k = key(&rel, &row)?;
                    ensure!(
                        self.tables
                            .entry(table.clone())
                            .or_default()
                            .insert(k.clone(), row.clone())
                            .is_none(),
                        "duplicate snapshot primary key"
                    );
                    self.remember(
                        &table,
                        &k,
                        "snapshot",
                        Some(row),
                        None,
                        "snapshot",
                        keep_history,
                    );
                }
            }
            Entry::Seal { lsn } => {
                ensure!(!self.sealed, "duplicate bootstrap seal");
                lsn_number(lsn)?;
                self.last_lsn = lsn.clone();
                self.sealed = true;
            }
            Entry::Transaction { tx } => {
                ensure!(self.sealed, "transaction before snapshot seal");
                ensure!(
                    lsn_number(&tx.end_lsn)? > lsn_number(&self.last_lsn)?,
                    "nonmonotonic transaction LSN"
                );
                for change in &tx.changes {
                    let rel = &change.relation;
                    let table = rel.label();
                    ensure!(
                        self.tables.contains_key(&table),
                        "publication added an unbootstrapped table: {table}"
                    );
                    self.relations.insert(rel.id, rel.clone());
                    self.changes += 1;
                    if change.op == "truncate" {
                        let keys: Vec<_> = self.tables[&table].keys().cloned().collect();
                        self.tables.get_mut(&table).unwrap().clear();
                        for k in keys {
                            self.remember(
                                &table,
                                &k,
                                "truncate",
                                None,
                                Some(tx.xid),
                                &tx.end_lsn,
                                keep_history,
                            );
                        }
                        continue;
                    }
                    let old = change.old.as_ref().map(|r| row_from(rel, r)).transpose()?;
                    let new = change.new.as_ref().map(|r| row_from(rel, r)).transpose()?;
                    let old_key = old.as_ref().map(|r| key(rel, r)).transpose()?;
                    let new_key = new.as_ref().map(|r| key(rel, r)).transpose()?;
                    let prior_key = old_key
                        .as_ref()
                        .or(new_key.as_ref())
                        .context("change without key")?;
                    let prior = self.tables[&table].get(prior_key).cloned();
                    if let Some(old_key) = &old_key
                        && new_key.as_ref() != Some(old_key)
                    {
                        self.tables.get_mut(&table).unwrap().remove(old_key);
                        self.remember(
                            &table,
                            old_key,
                            "delete",
                            None,
                            Some(tx.xid),
                            &tx.end_lsn,
                            keep_history,
                        );
                    }
                    if let Some(mut row) = new {
                        for (name, cell) in &mut row {
                            if matches!(cell, Cell::Unchanged) {
                                *cell = prior
                                    .as_ref()
                                    .and_then(|p| p.get(name))
                                    .cloned()
                                    .unwrap_or(Cell::Unknown);
                            }
                        }
                        let k = new_key.context("new tuple without key")?;
                        self.tables
                            .get_mut(&table)
                            .unwrap()
                            .insert(k.clone(), row.clone());
                        self.remember(
                            &table,
                            &k,
                            &change.op,
                            Some(row),
                            Some(tx.xid),
                            &tx.end_lsn,
                            keep_history,
                        );
                    }
                }
                self.transactions += 1;
                self.last_lsn = tx.end_lsn.clone();
            }
        }
        Ok(())
    }

    pub fn summary(&self) -> Value {
        json!({"sealed":self.sealed, "source_cut":self.last_lsn, "transactions":self.transactions,
            "changes":self.changes, "rows":self.tables.values().map(|t|t.len()).sum::<usize>(),
            "tables":self.tables.iter().map(|(name, rows)|json!({"name":name,"rows":rows.len()})).collect::<Vec<_>>(), "identity":self.identity})
    }

    pub fn export(&self, limit: usize) -> Value {
        let tables: Vec<_> = self.tables.iter().map(|(name,rows)| {
            let rel = self.relations.values().find(|r|r.label()==*name);
            json!({"name":name,"relation":rel,"count":rows.len(),"rows":rows.iter().take(limit).map(|(k,v)|json!({"key":k,"values":v})).collect::<Vec<_>>()})
        }).collect();
        json!({"format":"rowglass-source-v1","summary":self.summary(),"tables":tables,
            "history":self.history.values().flatten().take(limit).collect::<Vec<_>>(), "row_limit_per_table":limit})
    }

    pub fn audit(&self, target: &Target) -> Result<Value> {
        ensure!(self.sealed, "cannot audit incomplete bootstrap");
        let mut mismatches = Vec::new();
        let mut totals = BTreeMap::<String, usize>::new();
        let tables = &target.tables;
        for (name, expected) in &self.tables {
            let rel = self
                .relations
                .values()
                .find(|r| r.label() == *name)
                .context("missing schema")?;
            let observed = tables
                .iter()
                .find(|t| t.source_table == *name)
                .context(format!("target lacks {name}"))?;
            let mut actual = BTreeMap::new();
            for row in &observed.rows {
                let k = key(rel, row)?;
                ensure!(
                    actual.insert(k, row).is_none(),
                    "target has duplicate latest-version keys; resolve version ties before audit"
                );
            }
            let keys: std::collections::BTreeSet<_> =
                expected.keys().chain(actual.keys()).cloned().collect();
            for k in keys {
                let left = expected.get(&k);
                let right = actual.get(&k);
                let mut fields = Vec::new();
                let mut unknown = Vec::new();
                let kind = match (left, right) {
                    (Some(_), None) => "missing",
                    (None, Some(_)) => "unexpected",
                    (Some(a), Some(b)) => {
                        for col in &rel.columns {
                            let av = a.get(&col.name).unwrap_or(&Cell::Unknown);
                            let bv = b.get(&col.name).unwrap_or(&Cell::Unknown);
                            match (
                                canonical::value(col.oid, av)?,
                                canonical::value(col.oid, bv)?,
                            ) {
                                (Some(x), Some(y)) if x != y => fields.push(col.name.clone()),
                                (None, _) | (_, None) => unknown.push(col.name.clone()),
                                _ => {}
                            }
                        }
                        if !fields.is_empty() {
                            "changed"
                        } else if !unknown.is_empty() {
                            "unverifiable"
                        } else {
                            "matched"
                        }
                    }
                    _ => unreachable!(),
                };
                *totals.entry(kind.into()).or_default() += 1;
                if kind != "matched" {
                    let history = self.history.get(&format!("{name}\0{k}"));
                    mismatches.push(json!({"table":name,"key":k,"kind":kind,"fields":fields,"unknown_fields":unknown,"source":left,"target":right,"history":history}));
                }
            }
        }
        let controlled = target.consistency["mode"] == "controlled_quiescent"
            && target.consistency["source_cut"] == self.last_lsn;
        let mismatch_count: usize = totals
            .iter()
            .filter(|(k, _)| matches!(k.as_str(), "missing" | "unexpected" | "changed"))
            .map(|(_, n)| n)
            .sum();
        let status = if !controlled {
            "live_observation"
        } else if mismatch_count > 0 {
            "diverged_at_cut"
        } else if totals.get("unverifiable").copied().unwrap_or(0) > 0 {
            "inconclusive"
        } else {
            "matched_at_cut"
        };
        Ok(
            json!({"format":"rowglass-audit-v1","status":status,"source":self.summary(),"totals":totals,
            "consistency":target.consistency,"mismatches":mismatches,"target_metadata":target.metadata,
            "contract":"A live mismatch can be lag. A controlled cut is a caller-established experimental boundary, not an inferred global snapshot. No audit alone proves permanent loss."}),
        )
    }
}
