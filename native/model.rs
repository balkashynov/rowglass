use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum Cell {
    Null,
    Text(String),
    Unchanged,
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Column {
    pub name: String,
    pub oid: u32,
    pub key: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Relation {
    pub id: u32,
    pub namespace: String,
    pub name: String,
    pub columns: Vec<Column>,
}

impl Relation {
    pub fn label(&self) -> String {
        format!("{}.{}", self.namespace, self.name)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Change {
    pub op: String,
    pub relation: Relation,
    pub old: Option<Vec<Cell>>,
    pub new: Option<Vec<Cell>>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Transaction {
    pub xid: u32,
    pub commit_lsn: String,
    pub end_lsn: String,
    pub time_micros_pg: i64,
    pub changes: Vec<Change>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Entry {
    Start {
        identity: serde_json::Value,
    },
    Schema {
        relation: Relation,
    },
    Snapshot {
        relation_id: u32,
        rows: Vec<Vec<Cell>>,
    },
    Seal {
        lsn: String,
    },
    Transaction {
        tx: Transaction,
    },
}

pub fn lsn_string(n: u64) -> String {
    format!("{:X}/{:X}", n >> 32, n & 0xffff_ffff)
}

pub fn lsn_number(s: &str) -> anyhow::Result<u64> {
    let (a, b) = s
        .split_once('/')
        .ok_or_else(|| anyhow::anyhow!("invalid LSN"))?;
    let hi = u32::from_str_radix(a, 16)?;
    let lo = u32::from_str_radix(b, 16)?;
    Ok((u64::from(hi) << 32) | u64::from(lo))
}
