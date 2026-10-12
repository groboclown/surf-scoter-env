//! Communication between the 'client' application, invoked within the container
//! by the restricted user, out to the 'host' process.
//! Between these two rests the broker process, that doesn't care about these message formats.
//!
//! This starts by the client sending an Invocation object.
//! Then, the communication starts.  This works by sending a 1 byte "command" followed by
//! the command arguments
//!    -> client:
//!       stdout: 0 + low::read_byte_array
//!       stderr: 1 + low::read_byte_array
//!       exit: 2 + low::read_i32
//!    <- client:
//!       stdin: 3 + low::write_byte_array
//!       signal: 4 + low::write_u32
//!    -> host:
//!       stdin: 3 + low::read_byte_array
//!       signal: 4 + low::read_u32
//!    <- host:
//!       stdout: 0 + low::write_byte_array
//!       stderr: 1 + low::write_byte_array
//!       exit: 2 + low::write_i32
//!
//! A stdin message with an empty byte array means the client's stdin reached EOF.
//! Signal values are the `SIGNAL_*` constants (Linux numbering); the host maps them
//! to its own platform's signals.

use super::low;
use std::io::{Read, Result, Write};

pub const SIGNAL_HUP: u32 = 1;
pub const SIGNAL_INT: u32 = 2;
pub const SIGNAL_QUIT: u32 = 3;
pub const SIGNAL_USR1: u32 = 10;
pub const SIGNAL_USR2: u32 = 12;
pub const SIGNAL_TERM: u32 = 15;
pub const SIGNAL_WINCH: u32 = 28;

/// Uses 'String' rather than OsString because of the possibility of
/// a Windows host + Linux container.
#[derive(Debug, PartialEq, Eq)]
pub struct Invocation {
    pub cwd: String,
    pub args: Vec<String>,
    pub envs: Vec<(String, String)>,
}

impl Invocation {
    /// Construct the invocation from the current process.
    pub fn new_current() -> Result<Self> {
        Ok(Invocation {
            cwd: std::env::current_dir()?.into_string().map_err(|e| {
                std::io::Error::new(std::io::ErrorKind::InvalidFilename, e.to_string_lossy())
            })?,
            args: std::env::args().collect(),
            envs: std::env::vars().collect(),
        })
    }

    /// Construct the invocation from the stream.
    pub fn read<R: Read>(r: &mut R) -> Result<Self> {
        let mut r = r;
        let cwd = low::read_string(&mut r)?;

        let arg_count = low::read_u64(&mut r)?;
        let mut args = Vec::new();
        for _ in 0..arg_count {
            let arg = low::read_string(&mut r)?;
            args.push(arg);
        }

        let env_count = low::read_u64(&mut r)?;
        let mut envs = Vec::new();
        for _ in 0..env_count {
            let key = low::read_string(&mut r)?;
            let val = low::read_string(&mut r)?;
            envs.push((key, val));
        }
        Ok(Invocation { cwd, args, envs })
    }

    /// Write the invocation for the `read` to handle.
    pub fn write<W: Write>(&self, w: &mut W) -> Result<()> {
        let mut w = w;
        low::write_string(&mut w, &self.cwd)?;

        low::write_u64(&mut w, self.args.len() as u64)?;
        for arg in &self.args {
            low::write_string(&mut w, arg)?;
        }

        low::write_u64(&mut w, self.envs.len() as u64)?;
        for (key, value) in &self.envs {
            low::write_string(&mut w, key)?;
            low::write_string(&mut w, value)?;
        }

        Ok(())
    }
}

const CMD_STDOUT: u8 = 0;
const CMD_STDERR: u8 = 1;
const CMD_EXIT_CODE: u8 = 2;
const CMD_STDIN: u8 = 3;
const CMD_SIGNAL: u8 = 4;

#[derive(Debug, PartialEq, Eq)]
pub enum Command {
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
    Stdin(Vec<u8>),
    ExitCode(i32),
    Signal(u32),
}

impl Command {
    pub fn read<R: Read>(r: &mut R) -> Result<Command> {
        match low::read_u8(r)? {
            CMD_STDOUT => Ok(Command::Stdout(low::read_byte_array(r)?)),
            CMD_STDERR => Ok(Command::Stderr(low::read_byte_array(r)?)),
            CMD_EXIT_CODE => Ok(Command::ExitCode(low::read_i32(r)?)),
            CMD_STDIN => Ok(Command::Stdin(low::read_byte_array(r)?)),
            CMD_SIGNAL => Ok(Command::Signal(low::read_u32(r)?)),
            cmd => low::data_format_err(format!("bad command {cmd}")),
        }
    }
}

pub fn send_stdout<W: Write>(w: &mut W, data: &[u8]) -> Result<()> {
    low::write_u8(w, CMD_STDOUT)?;
    low::write_byte_array(w, data)
}

pub fn send_stderr<W: Write>(w: &mut W, data: &[u8]) -> Result<()> {
    low::write_u8(w, CMD_STDERR)?;
    low::write_byte_array(w, data)
}

pub fn send_stdin<W: Write>(w: &mut W, data: &[u8]) -> Result<()> {
    low::write_u8(w, CMD_STDIN)?;
    low::write_byte_array(w, data)
}

pub fn send_exit_code<W: Write>(w: &mut W, exit_code: i32) -> Result<()> {
    low::write_u8(w, CMD_EXIT_CODE)?;
    low::write_i32(w, exit_code)
}

pub fn send_signal<W: Write>(w: &mut W, signal: u32) -> Result<()> {
    low::write_u8(w, CMD_SIGNAL)?;
    low::write_u32(w, signal)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, ErrorKind};

    fn roundtrip_invocation(inv: &Invocation) -> Invocation {
        let mut buf = Vec::new();
        inv.write(&mut buf).unwrap();
        let mut cur = Cursor::new(buf);
        let out = Invocation::read(&mut cur).unwrap();
        assert_eq!(
            cur.position() as usize,
            cur.get_ref().len(),
            "trailing bytes left"
        );
        out
    }

    fn read_one(bytes: Vec<u8>) -> Result<Command> {
        Command::read(&mut Cursor::new(bytes))
    }

    #[test]
    fn invocation_roundtrip() {
        let inv = Invocation {
            cwd: "/workspace/project".to_string(),
            args: vec!["docker".to_string(), "ps".to_string(), "-a".to_string()],
            envs: vec![
                ("PATH".to_string(), "/usr/bin:/bin".to_string()),
                ("LANG".to_string(), "C.UTF-8".to_string()),
            ],
        };
        assert_eq!(roundtrip_invocation(&inv), inv);
    }

    #[test]
    fn invocation_roundtrip_empty() {
        let inv = Invocation {
            cwd: String::new(),
            args: vec![],
            envs: vec![],
        };
        assert_eq!(roundtrip_invocation(&inv), inv);
    }

    #[test]
    fn invocation_roundtrip_unicode_and_empty_strings() {
        let inv = Invocation {
            cwd: "/tmp/ünïcødé 🦆".to_string(),
            args: vec!["".to_string(), "日本語".to_string(), "a b\tc\n".to_string()],
            envs: vec![
                ("EMPTY".to_string(), "".to_string()),
                ("".to_string(), "x=y".to_string()),
            ],
        };
        assert_eq!(roundtrip_invocation(&inv), inv);
    }

    #[test]
    fn invocation_layout() {
        let inv = Invocation {
            cwd: "/".to_string(),
            args: vec!["a".to_string()],
            envs: vec![("K".to_string(), "V".to_string())],
        };
        let mut buf = Vec::new();
        inv.write(&mut buf).unwrap();
        assert_eq!(
            buf,
            vec![
                0, 0, 0, 1, b'/', 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, b'a', 0, 0, 0, 0, 0, 0, 0, 1,
                0, 0, 0, 1, b'K', 0, 0, 0, 1, b'V',
            ]
        );
    }

    #[test]
    fn invocation_truncated() {
        let inv = Invocation {
            cwd: "/workspace".to_string(),
            args: vec!["x".to_string()],
            envs: vec![("A".to_string(), "B".to_string())],
        };
        let mut buf = Vec::new();
        inv.write(&mut buf).unwrap();
        buf.pop();
        let err = Invocation::read(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::UnexpectedEof);
    }

    #[test]
    fn invocation_invalid_utf8() {
        let buf = vec![0, 0, 0, 1, 0xff];
        let err = Invocation::read(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
    }

    #[test]
    fn new_current_matches_process() {
        let inv = Invocation::new_current().unwrap();
        assert_eq!(inv.cwd, std::env::current_dir().unwrap().to_str().unwrap());
        assert_eq!(inv.args, std::env::args().collect::<Vec<_>>());
        assert_eq!(inv.envs, std::env::vars().collect::<Vec<_>>());
    }

    #[test]
    fn stdout_roundtrip() {
        let mut buf = Vec::new();
        send_stdout(&mut buf, b"hello").unwrap();
        assert_eq!(read_one(buf).unwrap(), Command::Stdout(b"hello".to_vec()));
    }

    #[test]
    fn stderr_roundtrip() {
        // Regression: send_stderr previously wrote the stdout command byte.
        let mut buf = Vec::new();
        send_stderr(&mut buf, b"oops").unwrap();
        assert_eq!(buf[0], CMD_STDERR);
        assert_eq!(read_one(buf).unwrap(), Command::Stderr(b"oops".to_vec()));
    }

    #[test]
    fn stdin_roundtrip() {
        let mut buf = Vec::new();
        send_stdin(&mut buf, b"input").unwrap();
        assert_eq!(read_one(buf).unwrap(), Command::Stdin(b"input".to_vec()));
    }

    #[test]
    fn stdin_eof_is_empty_frame() {
        let mut buf = Vec::new();
        send_stdin(&mut buf, b"").unwrap();
        assert_eq!(buf, vec![CMD_STDIN, 0, 0, 0, 0]);
        assert_eq!(read_one(buf).unwrap(), Command::Stdin(vec![]));
    }

    #[test]
    fn exit_code_roundtrip() {
        for code in [0, 1, 255, -1, i32::MIN, i32::MAX] {
            let mut buf = Vec::new();
            send_exit_code(&mut buf, code).unwrap();
            assert_eq!(read_one(buf).unwrap(), Command::ExitCode(code));
        }
    }

    #[test]
    fn signal_roundtrip() {
        for sig in [
            SIGNAL_HUP,
            SIGNAL_INT,
            SIGNAL_QUIT,
            SIGNAL_USR1,
            SIGNAL_USR2,
            SIGNAL_TERM,
            SIGNAL_WINCH,
            u32::MAX,
        ] {
            let mut buf = Vec::new();
            send_signal(&mut buf, sig).unwrap();
            assert_eq!(read_one(buf).unwrap(), Command::Signal(sig));
        }
    }

    #[test]
    fn stdout_layout() {
        let mut buf = Vec::new();
        send_stdout(&mut buf, b"hi").unwrap();
        assert_eq!(buf, vec![0, 0, 0, 0, 2, b'h', b'i']);
    }

    #[test]
    fn exit_code_and_signal_layout() {
        let mut buf = Vec::new();
        send_exit_code(&mut buf, -2).unwrap();
        send_signal(&mut buf, SIGNAL_TERM).unwrap();
        assert_eq!(buf, vec![2, 0xff, 0xff, 0xff, 0xfe, 4, 0, 0, 0, 15]);
    }

    #[test]
    fn sequence_of_commands() {
        let mut buf = Vec::new();
        send_stdout(&mut buf, b"a").unwrap();
        send_stderr(&mut buf, b"b").unwrap();
        send_exit_code(&mut buf, 3).unwrap();
        let mut cur = Cursor::new(buf);
        assert_eq!(
            Command::read(&mut cur).unwrap(),
            Command::Stdout(b"a".to_vec())
        );
        assert_eq!(
            Command::read(&mut cur).unwrap(),
            Command::Stderr(b"b".to_vec())
        );
        assert_eq!(Command::read(&mut cur).unwrap(), Command::ExitCode(3));
        assert_eq!(
            Command::read(&mut cur).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
    }

    #[test]
    fn unknown_command() {
        let err = read_one(vec![5]).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
        let err = read_one(vec![0xff, 0, 0, 0, 0]).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
    }

    #[test]
    fn truncated_command() {
        assert_eq!(
            read_one(vec![]).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
        assert_eq!(
            read_one(vec![CMD_STDOUT, 0, 0]).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
        assert_eq!(
            read_one(vec![CMD_STDOUT, 0, 0, 0, 3, b'a'])
                .unwrap_err()
                .kind(),
            ErrorKind::UnexpectedEof
        );
        assert_eq!(
            read_one(vec![CMD_EXIT_CODE, 0]).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
        assert_eq!(
            read_one(vec![CMD_SIGNAL, 0, 0, 0]).unwrap_err().kind(),
            ErrorKind::UnexpectedEof
        );
    }
}
