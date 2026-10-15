use crate::model::Entry;
use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use sha2::{Digest, Sha256};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

const MAGIC: &[u8; 8] = b"ROWGLS01";
const MAX_FRAME: usize = 128 * 1024 * 1024;

pub struct Scan {
    pub valid_bytes: u64,
    pub trailing_bytes: u64,
    pub digest: String,
}

pub fn visit(path: &Path, mut accept: impl FnMut(Entry) -> Result<()>) -> Result<Scan> {
    let mut file = File::open(path)?;
    let size = file.metadata()?.len();
    let mut magic = [0u8; 8];
    file.read_exact(&mut magic)
        .context("incomplete journal header")?;
    ensure!(&magic == MAGIC, "not a Rowglass v1 journal");
    let mut valid_bytes = 8;
    let mut hash = Sha256::new();
    hash.update(MAGIC);
    while valid_bytes < size {
        let remaining = size - valid_bytes;
        if remaining < 8 {
            break;
        }
        let mut head = [0u8; 8];
        file.read_exact(&mut head)?;
        let len = u32::from_le_bytes(head[..4].try_into()?) as usize;
        ensure!(
            len <= MAX_FRAME,
            "journal frame length exceeds safety limit at {valid_bytes}"
        );
        if remaining - 8 < len as u64 {
            break;
        }
        let mut payload = vec![0u8; len];
        file.read_exact(&mut payload)?;
        let crc = u32::from_le_bytes(head[4..].try_into()?);
        if crc32fast::hash(&payload) != crc {
            bail!("journal checksum mismatch at byte {valid_bytes}");
        }
        accept(serde_json::from_slice(&payload).context("invalid journal entry")?)?;
        hash.update(head);
        hash.update(payload);
        valid_bytes += len as u64 + 8;
    }
    Ok(Scan {
        valid_bytes,
        trailing_bytes: size - valid_bytes,
        digest: format!("{:x}", hash.finalize()),
    })
}

#[cfg(test)]
pub fn scan(path: &Path) -> Result<Scan> {
    visit(path, |_| Ok(()))
}

pub struct Journal {
    file: File,
}
impl Journal {
    pub fn open(path: &Path) -> Result<(Self, Option<Scan>)> {
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        file.try_lock_exclusive()
            .context("another writer has this journal open")?;
        let previous = if file.metadata()?.len() == 0 {
            file.write_all(MAGIC)?;
            file.sync_all()?;
            // Persist the directory entry as well as the file before issuing a durable ACK.
            File::open(
                path.parent()
                    .filter(|p| !p.as_os_str().is_empty())
                    .unwrap_or(Path::new(".")),
            )?
            .sync_all()?;
            None
        } else {
            let result = visit(path, |_| Ok(()))?;
            if result.trailing_bytes > 0 {
                file.set_len(result.valid_bytes)?;
                file.sync_all()?;
            }
            Some(result)
        };
        file.seek(SeekFrom::End(0))?;
        Ok((Self { file }, previous))
    }
    pub fn append(&mut self, entry: &Entry) -> Result<()> {
        let bytes = serde_json::to_vec(entry)?;
        ensure!(
            bytes.len() <= MAX_FRAME,
            "entry exceeds journal frame limit"
        );
        self.file.write_all(&(bytes.len() as u32).to_le_bytes())?;
        self.file
            .write_all(&crc32fast::hash(&bytes).to_le_bytes())?;
        self.file.write_all(&bytes)?;
        self.file.sync_data()?;
        Ok(())
    }
}
