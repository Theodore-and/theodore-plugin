//! C1 frames: a 4-byte little-endian length N (1 to 16 MiB), then N bytes of UTF-8 JSON, one object with a field `t`.

use crate::json::{self, Json};
use std::io::{self, Read, Write};

pub const MAX_FRAME: usize = 16 * 1024 * 1024;

/// The frame's bytes, ready for one write (a frame is never split between two writers).
pub fn encode(value: &Json) -> io::Result<Vec<u8>> {
    let body = value.dump();
    if body.is_empty() || body.len() > MAX_FRAME {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("a frame of {} bytes", body.len())));
    }
    let mut out = Vec::with_capacity(4 + body.len());
    out.extend_from_slice(&(body.len() as u32).to_le_bytes());
    out.extend_from_slice(body.as_bytes());
    Ok(out)
}

pub fn write<W: Write>(w: &mut W, value: &Json) -> io::Result<()> {
    let bytes = encode(value)?;
    w.write_all(&bytes)?;
    w.flush()
}

/// The next frame, or None when the stream ends cleanly between frames.
pub fn read<R: Read>(r: &mut R) -> io::Result<Option<Json>> {
    let mut len = [0u8; 4];
    let mut got = 0;
    while got < 4 {
        match r.read(&mut len[got..]) {
            Ok(0) if got == 0 => return Ok(None),
            Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "the stream ended inside a frame")),
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    let n = u32::from_le_bytes(len) as usize;
    if n == 0 || n > MAX_FRAME {
        return Err(io::Error::new(io::ErrorKind::InvalidData, format!("a frame length of {n}")));
    }
    let mut body = vec![0u8; n];
    r.read_exact(&mut body)?;
    let text = String::from_utf8(body).map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "a frame that is not UTF-8"))?;
    let value = json::parse(&text).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, format!("a frame that is not JSON: {e}")))?;
    if value.get("t").and_then(Json::as_str).is_none() {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "a frame without its type"));
    }
    Ok(Some(value))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frames_round_trip_and_end_cleanly() {
        let a = Json::obj().with("t", "out".into()).with("id", "1".into()).with("data", "é\n".into());
        let b = Json::obj().with("t", "done".into()).with("id", "1".into()).with("exit", Json::int(0));
        let mut wire = Vec::new();
        write(&mut wire, &a).unwrap();
        write(&mut wire, &b).unwrap();
        assert_eq!(&wire[..4], &(a.dump().len() as u32).to_le_bytes());
        let mut cursor = io::Cursor::new(wire);
        assert_eq!(read(&mut cursor).unwrap(), Some(a));
        assert_eq!(read(&mut cursor).unwrap(), Some(b));
        assert_eq!(read(&mut cursor).unwrap(), None);
    }

    #[test]
    fn bad_frames_are_errors() {
        for wire in [vec![0, 0, 0, 0], vec![1, 0], vec![5, 0, 0, 0, b'{'], {
            let mut v = 2u32.to_le_bytes().to_vec();
            v.extend_from_slice(b"{}");
            v
        }] {
            assert!(read(&mut io::Cursor::new(wire.clone())).is_err(), "{wire:?}");
        }
        let mut huge = ((MAX_FRAME + 1) as u32).to_le_bytes().to_vec();
        huge.extend_from_slice(b"{}");
        assert!(read(&mut io::Cursor::new(huge)).is_err());
    }
}
