use crate::model::Cell;
use anyhow::{Context, Result, bail, ensure};
use chrono::{DateTime, NaiveDateTime};
use serde_json::Value;

pub fn decimal(s: &str) -> Result<String> {
    if matches!(s, "NaN" | "Infinity" | "-Infinity") {
        return Ok(s.into());
    }
    let s = s.trim();
    let (negative, rest) = if let Some(t) = s.strip_prefix('-') {
        (true, t)
    } else {
        (false, s.strip_prefix('+').unwrap_or(s))
    };
    let (mantissa, exp) = match rest.find(['e', 'E']) {
        Some(i) => (&rest[..i], rest[i + 1..].parse::<i32>()?),
        None => (rest, 0),
    };
    ensure!(
        exp.unsigned_abs() <= 10000,
        "numeric exponent outside supported range"
    );
    let mut parts = mantissa.split('.');
    let whole = parts.next().unwrap_or_default();
    let fraction = parts.next().unwrap_or_default();
    ensure!(parts.next().is_none(), "invalid decimal");
    let digits = format!("{whole}{fraction}");
    ensure!(
        !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()),
        "invalid decimal"
    );
    let mut significant = digits.trim_start_matches('0').to_owned();
    if significant.is_empty() {
        return Ok("0".into());
    }
    let mut scale = i32::try_from(fraction.len())? - exp;
    while significant.ends_with('0') {
        significant.pop();
        scale -= 1;
    }
    Ok(format!(
        "{}{significant}e{}",
        if negative { "-" } else { "" },
        -scale
    ))
}

fn json_value(v: Value) -> Result<String> {
    Ok(match v {
        Value::Number(n) => format!("n{}", decimal(&n.to_string())?),
        Value::String(s) => format!("s{}", serde_json::to_string(&s)?),
        Value::Bool(b) => format!("b{b}"),
        Value::Null => "z".into(),
        Value::Array(a) => format!(
            "a[{}]",
            a.into_iter()
                .map(json_value)
                .collect::<Result<Vec<_>>>()?
                .join(",")
        ),
        Value::Object(o) => format!(
            "o{{{}}}",
            o.into_iter()
                .map(|(k, v)| Ok(format!("{}:{}", serde_json::to_string(&k)?, json_value(v)?)))
                .collect::<Result<Vec<_>>>()?
                .join(",")
        ),
    })
}

pub fn value(oid: u32, cell: &Cell) -> Result<Option<String>> {
    let s = match cell {
        Cell::Null => return Ok(Some("null".into())),
        Cell::Text(s) => s,
        Cell::Unknown | Cell::Unchanged => return Ok(None),
    };
    let result = match oid {
        20 | 21 | 23 | 26 | 1700 => format!("number:{}", decimal(s)?),
        16 => match s.as_str() {
            "t" | "true" | "1" => "bool:true".into(),
            "f" | "false" | "0" => "bool:false".into(),
            _ => bail!("invalid boolean {s}"),
        },
        700 => {
            let f: f32 = s.parse()?;
            if f.is_nan() {
                "float32:nan".into()
            } else {
                format!("float32:{:08x}", if f == 0.0 { 0 } else { f.to_bits() })
            }
        }
        701 => {
            let f: f64 = s.parse()?;
            if f.is_nan() {
                "float64:nan".into()
            } else {
                format!("float64:{:016x}", if f == 0.0 { 0 } else { f.to_bits() })
            }
        }
        114 | 3802 => format!("json:{}", json_value(serde_json::from_str(s)?)?),
        17 => {
            let hex = s.strip_prefix("\\x").unwrap_or(s);
            ensure!(
                hex.len() % 2 == 0 && hex.bytes().all(|b| b.is_ascii_hexdigit()),
                "bytea requires hex representation"
            );
            format!("bytes:{}", hex.to_ascii_lowercase())
        }
        2950 => format!("uuid:{}", s.to_ascii_lowercase()),
        1184 => {
            if matches!(s.as_str(), "infinity" | "-infinity") {
                format!("time:{s}")
            } else {
                let mut text = s.replace(' ', "T");
                if text.ends_with("+00") {
                    text.push_str(":00");
                }
                let dt = DateTime::parse_from_rfc3339(&text)
                    .or_else(|_| DateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f%#z"))
                    .context("invalid timestamptz")?;
                format!("time:{}", dt.timestamp_micros())
            }
        }
        1114 => {
            if matches!(s.as_str(), "infinity" | "-infinity") {
                format!("localtime:{s}")
            } else {
                format!(
                    "localtime:{}",
                    NaiveDateTime::parse_from_str(&s.replace('T', " "), "%Y-%m-%d %H:%M:%S%.f")?
                        .and_utc()
                        .timestamp_micros()
                )
            }
        }
        1082 => format!("date:{s}"),
        18 | 19 | 25 | 1043 => format!("text:{s}"),
        // bpchar equality ignores trailing spaces, unlike text.
        1042 => format!("char:{}", s.trim_end_matches(' ')),
        _ => return Ok(None),
    };
    Ok(Some(result))
}
