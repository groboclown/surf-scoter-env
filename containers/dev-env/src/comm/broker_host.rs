//! Communication between the host executable and the broker.
//!
//! The broker knows nothing about how the client and the host talk.  This just
//! passes the streaming data between the two with a process marker.
//!
//! u8 - command
//!    0: process started.  Followed by process id (u64)
//!    1: process terminated.  Followed by process id (u64) and exit code (i32)
//!    2: process communication. Followed by:
//!         process marker (u64)
//!         message size (u64)
//!         message bytes
//! All data passed as big endian.
//! Message sizes are limited to `low::MAXIMUM_BYTE_ARRAY_LEN`.
//!
//! broker -> host (broker's stdout):
//!    process started: a client connected.
//!    process communication: bytes the client sent.
//!    process terminated: the client connection ended before the host closed it.
//!         Exit code 0 means the client closed cleanly, 1 means a socket I/O error.
//! host -> broker (broker's stdin):
//!    process communication: bytes to send to the client.
//!    process terminated: send any pending bytes, then close the client connection.
//!         The exit code is ignored.  The broker sends no terminated message back.
//! Either side may receive a message for a process it already considers closed
//! (the closes crossed in flight); those are ignored.

use std::io::{Read, Result, Write};
use std::sync::mpsc::Receiver;

use super::low;

const COMMAND_PROCESS_STARTED: u8 = 0;
const COMMAND_PROCESS_TERMINATED: u8 = 1;
const COMMAND_PROCESS_MESSAGE: u8 = 2;

pub type ProcessId = u64;
pub type ExitCode = i32;

#[derive(Debug, PartialEq, Eq)]
pub enum Command {
    Started(ProcessId),
    Terminated((ProcessId, i32)),
    Message((ProcessId, Vec<u8>)),
}

impl Command {
    pub fn send<W: Write>(&self, w: &mut W) -> Result<()> {
        match &self {
            Command::Started(p) => {
                low::write_u8(w, COMMAND_PROCESS_STARTED)?;
                low::write_u64(w, *p)
            }
            Command::Terminated((p, ec)) => {
                low::write_u8(w, COMMAND_PROCESS_TERMINATED)?;
                low::write_u64(w, *p)?;
                low::write_i32(w, *ec)
            }
            Command::Message((p, m)) => {
                if m.len() > low::MAXIMUM_BYTE_ARRAY_LEN as usize {
                    return low::data_format_err("message too long");
                }
                low::write_u8(w, COMMAND_PROCESS_MESSAGE)?;
                low::write_u64(w, *p)?;
                low::write_u64(w, m.len() as u64)?;
                w.write_all(m.as_slice())
            }
        }
    }

    pub fn recv<R: Read>(r: &mut R) -> Result<Self> {
        match low::read_u8(r)? {
            COMMAND_PROCESS_STARTED => {
                let proc = low::read_u64(r)?;
                Ok(Command::Started(proc))
            }
            COMMAND_PROCESS_TERMINATED => {
                let proc = low::read_u64(r)?;
                let exit_code = low::read_i32(r)?;
                Ok(Command::Terminated((proc, exit_code)))
            }
            COMMAND_PROCESS_MESSAGE => {
                let proc = low::read_u64(r)?;
                let size = low::read_u64(r)?;
                if size > low::MAXIMUM_BYTE_ARRAY_LEN as u64 {
                    return low::data_format_err("message too long");
                }
                let mut msg: Vec<u8> = vec![0; size as usize];
                r.read_exact(msg.as_mut_slice())?;
                Ok(Command::Message((proc, msg)))
            }
            cmd => low::data_format_err(format!("bad command {cmd}")),
        }
    }
}
/// Write every command received on `rx` to `w`, until every sender is gone.
/// Commands already waiting are batched into the same flush.
pub fn send_all<W: Write>(rx: &Receiver<Command>, w: &mut W) -> Result<()> {
    while let Ok(cmd) = rx.recv() {
        cmd.send(w)?;
        for cmd in rx.try_iter() {
            cmd.send(w)?;
        }
        w.flush()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, ErrorKind};
    use std::sync::mpsc;

    fn encode(cmd: &Command) -> Vec<u8> {
        let mut buf = Vec::new();
        cmd.send(&mut buf).unwrap();
        buf
    }

    fn roundtrip(cmd: Command) {
        let buf = encode(&cmd);
        let mut cur = Cursor::new(buf);
        assert_eq!(Command::recv(&mut cur).unwrap(), cmd);
        assert_eq!(
            cur.position() as usize,
            cur.get_ref().len(),
            "trailing bytes left"
        );
    }

    #[test]
    fn started_roundtrip() {
        for p in [0, 1, u64::MAX] {
            roundtrip(Command::Started(p));
        }
    }

    #[test]
    fn terminated_roundtrip() {
        for (p, ec) in [(0, 0), (7, 1), (u64::MAX, -1), (3, i32::MIN), (4, i32::MAX)] {
            roundtrip(Command::Terminated((p, ec)));
        }
    }

    #[test]
    fn message_roundtrip() {
        roundtrip(Command::Message((5, b"hello".to_vec())));
        roundtrip(Command::Message((5, vec![])));
        roundtrip(Command::Message((u64::MAX, (0..=255).collect())));
    }

    #[test]
    fn started_layout() {
        assert_eq!(
            encode(&Command::Started(0x0102)),
            vec![0, 0, 0, 0, 0, 0, 0, 1, 2]
        );
    }

    #[test]
    fn terminated_layout() {
        assert_eq!(
            encode(&Command::Terminated((1, -2))),
            vec![1, 0, 0, 0, 0, 0, 0, 0, 1, 0xff, 0xff, 0xff, 0xfe,]
        );
    }

    #[test]
    fn message_layout() {
        assert_eq!(
            encode(&Command::Message((2, b"ab".to_vec()))),
            vec![
                2, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 2, b'a', b'b',
            ]
        );
    }

    #[test]
    fn sequence() {
        let mut buf = Vec::new();
        Command::Started(1).send(&mut buf).unwrap();
        Command::Message((1, b"x".to_vec())).send(&mut buf).unwrap();
        Command::Terminated((1, 0)).send(&mut buf).unwrap();
        let mut cur = Cursor::new(buf);
        assert_eq!(Command::recv(&mut cur).unwrap(), Command::Started(1));
        assert_eq!(
            Command::recv(&mut cur).unwrap(),
            Command::Message((1, b"x".to_vec()))
        );
        assert_eq!(
            Command::recv(&mut cur).unwrap(),
            Command::Terminated((1, 0))
        );
        assert_eq!(
            Command::recv(&mut cur).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
    }

    #[test]
    fn unknown_command() {
        for cmd in [3u8, 0xff] {
            let err =
                Command::recv(&mut Cursor::new(vec![cmd, 0, 0, 0, 0, 0, 0, 0, 0])).unwrap_err();
            assert_eq!(err.kind(), ErrorKind::InvalidData);
        }
    }

    #[test]
    fn truncated() {
        let full = encode(&Command::Message((9, b"abc".to_vec())));
        for len in 0..full.len() {
            let err = Command::recv(&mut Cursor::new(full[..len].to_vec())).unwrap_err();
            assert_eq!(err.kind(), ErrorKind::UnexpectedEof, "length {len}");
        }
        let full = encode(&Command::Terminated((9, 3)));
        let err = Command::recv(&mut Cursor::new(full[..full.len() - 1].to_vec())).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::UnexpectedEof);
    }

    #[test]
    fn recv_rejects_oversized_message() {
        // Must fail on the declared size, without allocating it.
        for size in [low::MAXIMUM_BYTE_ARRAY_LEN as u64 + 1, u64::MAX] {
            let mut buf = vec![COMMAND_PROCESS_MESSAGE];
            buf.extend_from_slice(&1u64.to_be_bytes());
            buf.extend_from_slice(&size.to_be_bytes());
            let err = Command::recv(&mut Cursor::new(buf)).unwrap_err();
            assert_eq!(err.kind(), ErrorKind::InvalidData);
        }
    }

    #[test]
    fn recv_accepts_maximum_declared_size() {
        // Exactly the maximum is allowed; the missing body makes this an EOF.
        let mut buf = vec![COMMAND_PROCESS_MESSAGE];
        buf.extend_from_slice(&1u64.to_be_bytes());
        buf.extend_from_slice(&(low::MAXIMUM_BYTE_ARRAY_LEN as u64).to_be_bytes());
        let err = Command::recv(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::UnexpectedEof);
    }

    #[test]
    fn send_rejects_oversized_message() {
        let cmd = Command::Message((1, vec![0; low::MAXIMUM_BYTE_ARRAY_LEN as usize + 1]));
        let mut buf = Vec::new();
        assert_eq!(
            cmd.send(&mut buf).unwrap_err().kind(),
            ErrorKind::InvalidData
        );
        assert!(buf.is_empty(), "nothing should be written on error");
    }

    #[test]
    fn send_all_writes_in_order() {
        let (tx, rx) = mpsc::channel();
        let cmds = vec![
            Command::Started(0),
            Command::Message((0, b"hi".to_vec())),
            Command::Terminated((0, 0)),
        ];
        let mut expected = Vec::new();
        for cmd in &cmds {
            expected.extend(encode(cmd));
        }
        for cmd in cmds {
            tx.send(cmd).unwrap();
        }
        drop(tx);
        let mut out = Vec::new();
        send_all(&rx, &mut out).unwrap();
        assert_eq!(out, expected);
    }

    #[test]
    fn send_all_flushes_each_batch() {
        /// Records what had been written at each flush.
        struct Recorder {
            buf: Vec<u8>,
            flushed: Vec<usize>,
        }
        impl Write for Recorder {
            fn write(&mut self, data: &[u8]) -> Result<usize> {
                self.buf.extend_from_slice(data);
                Ok(data.len())
            }
            fn flush(&mut self) -> Result<()> {
                self.flushed.push(self.buf.len());
                Ok(())
            }
        }
        let (tx, rx) = mpsc::channel();
        tx.send(Command::Started(0)).unwrap();
        tx.send(Command::Started(1)).unwrap();
        drop(tx);
        let mut out = Recorder {
            buf: Vec::new(),
            flushed: Vec::new(),
        };
        send_all(&rx, &mut out).unwrap();
        // Both commands were already queued, so a single flush covers them.
        assert_eq!(out.flushed, vec![18]);
    }

    #[test]
    fn send_all_write_error() {
        struct FailingWriter;
        impl Write for FailingWriter {
            fn write(&mut self, _: &[u8]) -> Result<usize> {
                Err(std::io::Error::new(ErrorKind::BrokenPipe, "closed"))
            }
            fn flush(&mut self) -> Result<()> {
                Ok(())
            }
        }
        let (tx, rx) = mpsc::channel();
        tx.send(Command::Started(0)).unwrap();
        let err = send_all(&rx, &mut FailingWriter).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::BrokenPipe);
    }
}
