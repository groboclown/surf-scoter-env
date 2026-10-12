//! Low-level communication standard.

use std::io::{Read, Result, Write};

pub(crate) const MAXIMUM_BYTE_ARRAY_LEN: u32 = 32 * 1024 * 1024;

/// Read in a list of bytes.
/// Size is dictated by the upstream.
pub(super) fn read_byte_array<R: Read>(r: &mut R) -> Result<Vec<u8>> {
    let mut r = r;

    // Read in the length
    let len = read_u32(&mut r)?;
    // Read those bytes.
    if len > MAXIMUM_BYTE_ARRAY_LEN {
        return data_format_err("byte array too long");
    }
    if len == 0 {
        return Ok(Vec::new());
    }
    let mut buf = vec![0; len as usize];
    r.read_exact(&mut buf)?;
    Ok(buf)
}

/// Write a list of bytes.
pub(super) fn write_byte_array<W: Write>(w: &mut W, data: &[u8]) -> Result<()> {
    let mut w = w;
    let count = data.len();
    if count > MAXIMUM_BYTE_ARRAY_LEN as usize {
        return data_format_err("byte array too long");
    }
    // Write the length.
    write_u32(&mut w, count as u32)?;
    if count > 0 {
        // Write the bytes.
        w.write_all(data)?;
    }
    Ok(())
}

pub(super) fn read_string<R: Read>(r: &mut R) -> Result<String> {
    let bytes = read_byte_array(r)?;
    String::from_utf8(bytes).map_err(data_format_error)
}

pub(super) fn write_string<W: Write>(w: &mut W, s: &str) -> Result<()> {
    write_byte_array(w, s.as_bytes())
}

/// Read a u64 number.
pub(super) fn read_u64<R: Read>(r: &mut R) -> Result<u64> {
    let mut bytes: [u8; 8] = [0; 8];
    r.read_exact(&mut bytes)?;
    Ok(u64::from_be_bytes(bytes))
}

/// Write a u64 number.
pub(super) fn write_u64<W: Write>(w: &mut W, data: u64) -> Result<()> {
    let bytes = data.to_be_bytes();
    w.write_all(&bytes)
}

/// Read a u32 number.
pub(super) fn read_u32<R: Read>(r: &mut R) -> Result<u32> {
    let mut bytes: [u8; 4] = [0; 4];
    r.read_exact(&mut bytes)?;
    Ok(u32::from_be_bytes(bytes))
}

/// Write a u32 number.
pub(super) fn write_u32<W: Write>(w: &mut W, data: u32) -> Result<()> {
    let bytes = data.to_be_bytes();
    w.write_all(&bytes)
}

/// Read a i32 number.
pub(super) fn read_i32<R: Read>(r: &mut R) -> Result<i32> {
    let mut bytes: [u8; 4] = [0; 4];
    r.read_exact(&mut bytes)?;
    Ok(i32::from_be_bytes(bytes))
}

/// Write a i32 number.
pub(super) fn write_i32<W: Write>(w: &mut W, data: i32) -> Result<()> {
    let bytes = data.to_be_bytes();
    w.write_all(&bytes)
}

/// Read a u8 number.
pub(super) fn read_u8<R: Read>(r: &mut R) -> Result<u8> {
    let mut bytes: [u8; 1] = [0];
    r.read_exact(&mut bytes)?;
    Ok(bytes[0])
}

/// Write a u8 number.
pub(super) fn write_u8<W: Write>(w: &mut W, data: u8) -> Result<()> {
    let bytes = [data];
    w.write_all(&bytes)
}

pub(super) fn data_format_err<E: Into<Box<dyn core::error::Error + Send + Sync>>, T>(
    msg: E,
) -> Result<T> {
    Err(data_format_error(msg))
}

fn data_format_error<E: Into<Box<dyn core::error::Error + Send + Sync>>>(msg: E) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, msg)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, ErrorKind};

    #[test]
    fn u8_roundtrip() {
        for v in [0u8, 1, 0x7f, 0xff] {
            let mut buf = Vec::new();
            write_u8(&mut buf, v).unwrap();
            assert_eq!(buf, vec![v]);
            assert_eq!(read_u8(&mut Cursor::new(buf)).unwrap(), v);
        }
    }

    #[test]
    fn u32_roundtrip_big_endian() {
        let mut buf = Vec::new();
        write_u32(&mut buf, 0x0102_0304).unwrap();
        assert_eq!(buf, vec![1, 2, 3, 4]);
        for v in [0u32, 1, 0x0102_0304, u32::MAX] {
            let mut buf = Vec::new();
            write_u32(&mut buf, v).unwrap();
            assert_eq!(read_u32(&mut Cursor::new(buf)).unwrap(), v);
        }
    }

    #[test]
    fn u64_roundtrip_big_endian() {
        let mut buf = Vec::new();
        write_u64(&mut buf, 0x0102_0304_0506_0708).unwrap();
        assert_eq!(buf, vec![1, 2, 3, 4, 5, 6, 7, 8]);
        for v in [0u64, 1, 0x0102_0304_0506_0708, u64::MAX] {
            let mut buf = Vec::new();
            write_u64(&mut buf, v).unwrap();
            assert_eq!(read_u64(&mut Cursor::new(buf)).unwrap(), v);
        }
    }

    #[test]
    fn i32_roundtrip_big_endian() {
        let mut buf = Vec::new();
        write_i32(&mut buf, -1).unwrap();
        assert_eq!(buf, vec![0xff, 0xff, 0xff, 0xff]);
        for v in [0i32, 1, -1, 255, -255, i32::MIN, i32::MAX] {
            let mut buf = Vec::new();
            write_i32(&mut buf, v).unwrap();
            assert_eq!(read_i32(&mut Cursor::new(buf)).unwrap(), v);
        }
    }

    #[test]
    fn short_reads_are_eof() {
        assert_eq!(
            read_u8(&mut Cursor::new(vec![])).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
        assert_eq!(
            read_u32(&mut Cursor::new(vec![0; 3])).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
        assert_eq!(
            read_i32(&mut Cursor::new(vec![0; 3])).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
        assert_eq!(
            read_u64(&mut Cursor::new(vec![0; 7])).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
    }

    #[test]
    fn byte_array_roundtrip() {
        let mut buf = Vec::new();
        write_byte_array(&mut buf, b"abc").unwrap();
        assert_eq!(buf, vec![0, 0, 0, 3, b'a', b'b', b'c']);
        assert_eq!(
            read_byte_array(&mut Cursor::new(buf)).unwrap(),
            b"abc".to_vec()
        );
    }

    #[test]
    fn byte_array_empty() {
        let mut buf = Vec::new();
        write_byte_array(&mut buf, b"").unwrap();
        assert_eq!(buf, vec![0, 0, 0, 0]);
        let mut cur = Cursor::new(buf);
        assert_eq!(read_byte_array(&mut cur).unwrap(), Vec::<u8>::new());
        assert_eq!(cur.position(), 4);
    }

    #[test]
    fn byte_array_binary_data() {
        let data: Vec<u8> = (0..=255).collect();
        let mut buf = Vec::new();
        write_byte_array(&mut buf, &data).unwrap();
        assert_eq!(read_byte_array(&mut Cursor::new(buf)).unwrap(), data);
    }

    #[test]
    fn byte_array_at_maximum_len_header_accepted() {
        // Header claims exactly the maximum; data is missing so it's an EOF, not a format error.
        let buf = MAXIMUM_BYTE_ARRAY_LEN.to_be_bytes().to_vec();
        let err = read_byte_array(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::UnexpectedEof);
    }

    #[test]
    fn byte_array_read_too_long() {
        let buf = (MAXIMUM_BYTE_ARRAY_LEN + 1).to_be_bytes().to_vec();
        let err = read_byte_array(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
        let buf = u32::MAX.to_be_bytes().to_vec();
        let err = read_byte_array(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
    }

    #[test]
    fn byte_array_write_too_long() {
        let data = vec![0u8; MAXIMUM_BYTE_ARRAY_LEN as usize + 1];
        let mut buf = Vec::new();
        let err = write_byte_array(&mut buf, &data).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
        assert!(buf.is_empty(), "nothing should be written on error");
    }

    #[test]
    fn byte_array_truncated() {
        let buf = vec![0, 0, 0, 5, 1, 2];
        let err = read_byte_array(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::UnexpectedEof);
    }

    #[test]
    fn string_roundtrip() {
        for s in ["", "plain", "ünïcødé 🦆"] {
            let mut buf = Vec::new();
            write_string(&mut buf, s).unwrap();
            assert_eq!(read_string(&mut Cursor::new(buf)).unwrap(), s);
        }
    }

    #[test]
    fn string_invalid_utf8() {
        let buf = vec![0, 0, 0, 2, 0xc3, 0x28];
        let err = read_string(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
    }

    #[test]
    fn data_format_err_kind() {
        let err = data_format_err::<_, ()>("bad").unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
        assert_eq!(err.to_string(), "bad");
    }
}
