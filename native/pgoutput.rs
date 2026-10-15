use crate::model::*;
use anyhow::{Context, Result, bail, ensure};
use std::collections::HashMap;

struct Reader<'a> {
    bytes: &'a [u8],
    pos: usize,
}
impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        let end = self.pos.checked_add(n).context("message length overflow")?;
        ensure!(
            end <= self.bytes.len(),
            "truncated pgoutput message at {}",
            self.pos
        );
        let result = &self.bytes[self.pos..end];
        self.pos = end;
        Ok(result)
    }
    fn u8(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }
    fn u16(&mut self) -> Result<u16> {
        Ok(u16::from_be_bytes(self.take(2)?.try_into()?))
    }
    fn u32(&mut self) -> Result<u32> {
        Ok(u32::from_be_bytes(self.take(4)?.try_into()?))
    }
    fn u64(&mut self) -> Result<u64> {
        Ok(u64::from_be_bytes(self.take(8)?.try_into()?))
    }
    fn i64(&mut self) -> Result<i64> {
        Ok(i64::from_be_bytes(self.take(8)?.try_into()?))
    }
    fn string(&mut self) -> Result<String> {
        let n = self.bytes[self.pos..]
            .iter()
            .position(|b| *b == 0)
            .context("unterminated pgoutput string")?;
        let value = std::str::from_utf8(self.take(n)?)?.to_owned();
        self.take(1)?;
        Ok(value)
    }
    fn tuple(&mut self, relation: &Relation) -> Result<Vec<Cell>> {
        let n = self.u16()? as usize;
        ensure!(
            n == relation.columns.len(),
            "tuple width {n} does not match relation width {}",
            relation.columns.len()
        );
        (0..n)
            .map(|_| match self.u8()? {
                b'n' => Ok(Cell::Null),
                b'u' => Ok(Cell::Unchanged),
                b't' => {
                    let size = self.u32()? as usize;
                    Ok(Cell::Text(
                        std::str::from_utf8(self.take(size)?)?.to_owned(),
                    ))
                }
                b'b' => bail!("binary tuples are unsupported; request pgoutput text mode"),
                kind => bail!("unknown tuple kind {kind}"),
            })
            .collect()
    }
}

#[derive(Default)]
pub struct Decoder {
    pub relations: HashMap<u32, Relation>,
    pub primary_keys: HashMap<u32, Vec<String>>,
    pending: Option<(u32, Vec<Change>)>,
    pending_bytes: usize,
}

impl Decoder {
    pub fn feed(&mut self, bytes: &[u8]) -> Result<Option<Transaction>> {
        let mut r = Reader { bytes, pos: 0 };
        let kind = r.u8()?;
        let mut complete = None;
        if self.pending.is_some() {
            self.pending_bytes = self
                .pending_bytes
                .checked_add(bytes.len())
                .context("transaction size overflow")?;
            ensure!(
                self.pending_bytes <= 64 * 1024 * 1024,
                "transaction exceeds 64 MiB capture limit; no commit acknowledged"
            );
        }
        match kind {
            b'B' => {
                ensure!(self.pending.is_none(), "nested BEGIN in protocol v1");
                let _final_lsn = r.u64()?;
                let _time = r.i64()?;
                self.pending = Some((r.u32()?, Vec::new()));
                self.pending_bytes = bytes.len();
            }
            b'C' => {
                ensure!(r.u8()? == 0, "unsupported commit flags");
                let commit_lsn = lsn_string(r.u64()?);
                let end_lsn = lsn_string(r.u64()?);
                let time_micros_pg = r.i64()?;
                let (xid, changes) = self.pending.take().context("COMMIT without BEGIN")?;
                complete = Some(Transaction {
                    xid,
                    commit_lsn,
                    end_lsn,
                    time_micros_pg,
                    changes,
                });
                self.pending_bytes = 0;
            }
            b'R' => {
                let id = r.u32()?;
                let namespace = r.string()?;
                let name = r.string()?;
                let _identity = r.u8()?;
                let n = r.u16()?;
                let mut columns = Vec::with_capacity(n as usize);
                for _ in 0..n {
                    let flags = r.u8()?;
                    let name = r.string()?;
                    let oid = r.u32()?;
                    let _modifier = r.u32()?;
                    let key = self
                        .primary_keys
                        .get(&id)
                        .map_or(flags & 1 != 0, |keys| keys.contains(&name));
                    columns.push(Column { name, oid, key });
                }
                self.relations.insert(
                    id,
                    Relation {
                        id,
                        namespace,
                        name,
                        columns,
                    },
                );
            }
            b'I' | b'U' | b'D' => {
                let id = r.u32()?;
                let relation = self
                    .relations
                    .get(&id)
                    .context("row change before relation definition")?
                    .clone();
                ensure!(
                    relation.columns.iter().any(|c| c.key),
                    "{} has no known primary key",
                    relation.label()
                );
                let first = r.u8()?;
                let (old, new) = match (kind, first) {
                    (b'I', b'N') | (b'U', b'N') => (None, Some(r.tuple(&relation)?)),
                    (b'U', b'K' | b'O') => {
                        let old = Some(r.tuple(&relation)?);
                        ensure!(r.u8()? == b'N', "UPDATE lacks new tuple");
                        (old, Some(r.tuple(&relation)?))
                    }
                    (b'D', b'K' | b'O') => (Some(r.tuple(&relation)?), None),
                    _ => bail!("invalid tuple marker for row operation"),
                };
                let op = match kind {
                    b'I' => "insert",
                    b'U' => "update",
                    _ => "delete",
                }
                .to_owned();
                self.pending
                    .as_mut()
                    .context("change outside transaction")?
                    .1
                    .push(Change {
                        op,
                        relation,
                        old,
                        new,
                    });
            }
            b'T' => {
                let count = r.u32()?;
                let _options = r.u8()?;
                for _ in 0..count {
                    let id = r.u32()?;
                    let relation = self
                        .relations
                        .get(&id)
                        .context("TRUNCATE lacks relation definition")?
                        .clone();
                    self.pending
                        .as_mut()
                        .context("TRUNCATE outside transaction")?
                        .1
                        .push(Change {
                            op: "truncate".into(),
                            relation,
                            old: None,
                            new: None,
                        });
                }
            }
            b'Y' => {
                r.u32()?;
                r.string()?;
                r.string()?;
            }
            b'O' => {
                r.u64()?;
                r.string()?;
            }
            // Non-row logical messages have no effect on the captured row state.
            b'M' => {
                r.u8()?;
                r.u64()?;
                r.string()?;
                let n = r.u32()? as usize;
                r.take(n)?;
            }
            other => bail!("unsupported pgoutput message type {}", char::from(other)),
        }
        ensure!(r.pos == bytes.len(), "trailing bytes in pgoutput message");
        Ok(complete)
    }
}

pub fn unhex(s: &str) -> Result<Vec<u8>> {
    ensure!(s.len().is_multiple_of(2), "odd hex message length");
    s.as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let a = char::from(pair[0]).to_digit(16).context("invalid hex")?;
            let b = char::from(pair[1]).to_digit(16).context("invalid hex")?;
            Ok(((a << 4) | b) as u8)
        })
        .collect()
}
