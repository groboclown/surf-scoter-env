//! `envoy-inject --socket PATH`: give the Envoy gateway its secrets.
//!
//! Reads one JSON payload, `{"values": {"NAME": "value", ...}}`, from
//! standard input, and sends it to the inject listener's Unix socket.  It
//! prints the gateway's answer, which names secrets but never holds a value.
//!
//! Exit codes, as `seekrit-proxy inject`'s: 0, taken; 1, refused (already
//! delivered, or a bad payload); 2, couldn't reach the socket, or no answer.

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::process::ExitCode;
use std::time::Duration;

const TAKEN: u8 = 0;
const REFUSED: u8 = 1;
const UNREACHABLE: u8 = 2;

/// The largest payload, as the filter's `MAX_PAYLOAD_BYTES`.
const MAX_PAYLOAD_BYTES: usize = 1024 * 1024;
/// The largest answer read back.
const MAX_ANSWER_BYTES: u64 = 64 * 1024;
const TIMEOUT: Duration = Duration::from_secs(10);

fn zero(data: &mut [u8]) {
    for b in data.iter_mut() {
        // SAFETY: a valid, aligned, exclusive reference to the byte.
        unsafe { std::ptr::write_volatile(b, 0) };
    }
    std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
}

fn socket_arg() -> Option<String> {
    let mut args = std::env::args().skip(1);
    match (args.next().as_deref(), args.next(), args.next()) {
        (Some("--socket"), Some(path), None) if path.starts_with('/') => Some(path),
        _ => None,
    }
}

fn send(path: &str, payload: &[u8]) -> Result<(u16, String), u8> {
    let mut stream = UnixStream::connect(path).map_err(|err| {
        eprintln!(
            "envoy-inject: can't reach the gateway's socket {path}: {}",
            err.kind()
        );
        UNREACHABLE
    })?;
    let _ = stream.set_read_timeout(Some(TIMEOUT));
    let _ = stream.set_write_timeout(Some(TIMEOUT));
    let head = format!(
        "POST / HTTP/1.1\r\nHost: inject\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        payload.len()
    );
    // One buffer, so the payload goes in one write and is zeroed in one place.
    let mut request = Vec::with_capacity(head.len() + payload.len());
    request.extend_from_slice(head.as_bytes());
    request.extend_from_slice(payload);
    let written = stream.write_all(&request).and_then(|()| stream.flush());
    zero(&mut request);
    written.map_err(|err| {
        eprintln!("envoy-inject: couldn't send to the gateway: {}", err.kind());
        UNREACHABLE
    })?;
    let mut answer = Vec::new();
    // A closed connection after a full answer is the normal end.
    let _ = (&mut stream)
        .take(MAX_ANSWER_BYTES)
        .read_to_end(&mut answer);
    let text = String::from_utf8_lossy(&answer);
    let (head, body) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
    let status = head
        .lines()
        .next()
        .and_then(|line| line.split(' ').nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| {
            eprintln!("envoy-inject: no answer from the gateway");
            UNREACHABLE
        })?;
    Ok((status, body.trim().to_string()))
}

fn main() -> ExitCode {
    let Some(path) = socket_arg() else {
        eprintln!("Usage: envoy-inject --socket /ABSOLUTE/PATH < payload.json");
        return ExitCode::from(REFUSED);
    };
    let mut payload = Vec::with_capacity(4096);
    let read = std::io::stdin()
        .lock()
        .take(MAX_PAYLOAD_BYTES as u64 + 1)
        .read_to_end(&mut payload);
    if let Err(err) = read {
        zero(&mut payload);
        eprintln!("envoy-inject: couldn't read the payload: {}", err.kind());
        return ExitCode::from(REFUSED);
    }
    if payload.len() > MAX_PAYLOAD_BYTES {
        zero(&mut payload);
        eprintln!("envoy-inject: the payload is larger than {MAX_PAYLOAD_BYTES} bytes");
        return ExitCode::from(REFUSED);
    }
    let result = send(&path, &payload);
    zero(&mut payload);
    match result {
        Ok((200, text)) => {
            println!("{text}");
            ExitCode::from(TAKEN)
        }
        Ok((status, text)) => {
            eprintln!("envoy-inject: the gateway refused the secrets ({status}): {text}");
            ExitCode::from(REFUSED)
        }
        Err(code) => ExitCode::from(code),
    }
}
