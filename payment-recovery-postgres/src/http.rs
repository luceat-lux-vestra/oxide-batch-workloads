use std::time::Duration;

use anyhow::{bail, Context, Result};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;

use crate::crash::{CrashControl, CrashPoint};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const IO_TIMEOUT: Duration = Duration::from_secs(15);

pub struct AfterSendCrash<'a> {
    pub control: &'a CrashControl,
    pub point: CrashPoint,
    pub candidate_id: &'a str,
}

#[derive(Debug)]
pub struct HttpResponse {
    pub status: u16,
    pub body: Vec<u8>,
}

struct ParsedUrl {
    host: String,
    authority: String,
    port: u16,
    path: String,
}

pub fn normalize_http_base_url(name: &str, value: &str) -> Result<String> {
    let trimmed = value.trim_end_matches('/');
    let parsed = ParsedUrl::parse(trimmed)
        .with_context(|| format!("{name} base URL must be a valid http URL"))?;
    if parsed.path != "/" {
        bail!("{name} base URL must not include a path");
    }
    Ok(trimmed.to_owned())
}

pub async fn request_json(
    method: &str,
    url: &str,
    body: Option<&[u8]>,
    after_send: Option<AfterSendCrash<'_>>,
) -> Result<HttpResponse> {
    let parsed = ParsedUrl::parse(url)?;
    let mut stream = timeout(
        CONNECT_TIMEOUT,
        TcpStream::connect((parsed.host.as_str(), parsed.port)),
    )
    .await
    .context("HTTP connect timed out")?
    .context("HTTP connect failed")?;

    let body = body.unwrap_or_default();
    let request = format!(
        "{method} {} HTTP/1.1\r\nHost: {}\r\nAccept: application/json\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        parsed.path,
        parsed.authority,
        body.len()
    );
    timeout(IO_TIMEOUT, stream.write_all(request.as_bytes()))
        .await
        .context("HTTP request header write timed out")?
        .context("HTTP request header write failed")?;
    if !body.is_empty() {
        timeout(IO_TIMEOUT, stream.write_all(body))
            .await
            .context("HTTP request body write timed out")?
            .context("HTTP request body write failed")?;
    }
    timeout(IO_TIMEOUT, stream.flush())
        .await
        .context("HTTP request flush timed out")?
        .context("HTTP request flush failed")?;

    if let Some(hook) = after_send {
        hook.control.pause_if(hook.point, hook.candidate_id).await;
    }

    let mut raw = Vec::new();
    timeout(IO_TIMEOUT, stream.read_to_end(&mut raw))
        .await
        .context("HTTP response read timed out")?
        .context("HTTP response read failed")?;
    parse_response(&raw)
}

impl ParsedUrl {
    fn parse(value: &str) -> Result<Self> {
        let rest = value
            .strip_prefix("http://")
            .context("only http:// URLs are supported by the deterministic crash harness")?;
        let (authority, path) = match rest.split_once('/') {
            Some((authority, tail)) => (authority, format!("/{tail}")),
            None => (rest, "/".to_owned()),
        };
        if authority.is_empty() || authority.contains('@') {
            bail!("HTTP URL must contain a host and must not embed credentials");
        }

        let (host, port) = match authority.rsplit_once(':') {
            Some((host, port)) if !host.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()) => {
                let port = port.parse::<u16>().context("HTTP URL port is invalid")?;
                (host.to_owned(), port)
            }
            _ => (authority.to_owned(), 80),
        };
        if host.is_empty() || host.contains(':') {
            bail!("IPv6 literals are not supported by the deterministic crash harness");
        }

        Ok(Self {
            host,
            authority: authority.to_owned(),
            port,
            path,
        })
    }
}

fn parse_response(raw: &[u8]) -> Result<HttpResponse> {
    let split = find_bytes(raw, b"\r\n\r\n").context("HTTP response headers are incomplete")?;
    let header_bytes = &raw[..split];
    let body_bytes = &raw[split + 4..];
    let header_text = std::str::from_utf8(header_bytes).context("HTTP response headers are not UTF-8")?;
    let mut lines = header_text.split("\r\n");
    let status_line = lines.next().context("HTTP response status line is missing")?;
    let mut status_parts = status_line.split_whitespace();
    let protocol = status_parts.next().context("HTTP response protocol is missing")?;
    if !protocol.starts_with("HTTP/1.") {
        bail!("unsupported HTTP response protocol");
    }
    let status = status_parts
        .next()
        .context("HTTP response status is missing")?
        .parse::<u16>()
        .context("HTTP response status is invalid")?;

    let mut content_length = None;
    let mut chunked = false;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            bail!("malformed HTTP response header");
        };
        let name = name.trim();
        let value = value.trim();
        if name.eq_ignore_ascii_case("content-length") {
            content_length = Some(
                value
                    .parse::<usize>()
                    .context("HTTP content-length is invalid")?,
            );
        }
        if name.eq_ignore_ascii_case("transfer-encoding")
            && value
                .split(',')
                .any(|token| token.trim().eq_ignore_ascii_case("chunked"))
        {
            chunked = true;
        }
    }

    let body = if chunked {
        decode_chunked(body_bytes)?
    } else if let Some(length) = content_length {
        if body_bytes.len() < length {
            bail!("HTTP response body is shorter than content-length");
        }
        body_bytes[..length].to_vec()
    } else {
        body_bytes.to_vec()
    };

    Ok(HttpResponse { status, body })
}

fn decode_chunked(input: &[u8]) -> Result<Vec<u8>> {
    let mut output = Vec::new();
    let mut cursor = 0usize;

    loop {
        let relative = find_bytes(&input[cursor..], b"\r\n")
            .context("chunked response is missing a size terminator")?;
        let line_end = cursor + relative;
        let line = std::str::from_utf8(&input[cursor..line_end])
            .context("chunk size line is not UTF-8")?;
        let size_text = line.split(';').next().unwrap_or(line).trim();
        let size = usize::from_str_radix(size_text, 16).context("chunk size is invalid")?;
        cursor = line_end + 2;

        if size == 0 {
            return Ok(output);
        }
        let end = cursor
            .checked_add(size)
            .context("chunk size overflow")?;
        if end + 2 > input.len() || &input[end..end + 2] != b"\r\n" {
            bail!("chunked response payload is incomplete");
        }
        output.extend_from_slice(&input[cursor..end]);
        cursor = end + 2;
    }
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() {
        return Some(0);
    }
    haystack.windows(needle.len()).position(|window| window == needle)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_content_length_response() {
        let response = parse_response(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
        )
        .unwrap();
        assert_eq!(response.status, 200);
        assert_eq!(response.body, b"{}");
    }

    #[test]
    fn parses_chunked_response() {
        let response = parse_response(
            b"HTTP/1.1 202 Accepted\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ntest\r\n0\r\n\r\n",
        )
        .unwrap();
        assert_eq!(response.status, 202);
        assert_eq!(response.body, b"test");
    }

    #[test]
    fn base_url_is_reference_http_only() {
        assert!(normalize_http_base_url("merchant", "http://127.0.0.1:18083").is_ok());
        assert!(normalize_http_base_url("merchant", "https://example.invalid").is_err());
        assert!(normalize_http_base_url("merchant", "http://host/path").is_err());
    }
}
