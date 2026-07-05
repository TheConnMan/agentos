//! A minimal blocking HTTP/1.1 server for integration tests.
//!
//! Mocks sit at the external seam only: the CLI's HTTP peers (runner, platform
//! API) are separate services, so tests exercise the real reqwest client, real
//! NDJSON streaming, and the real frozen-contract serde against canned wire
//! responses. Nothing internal to the CLI is mocked.

// Each integration test binary compiles this module independently and uses a
// different subset of it; unused-in-this-binary items are not dead code.
#![allow(dead_code)]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;

#[derive(Debug, Clone)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Request {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

pub struct Response {
    pub status: u16,
    pub content_type: String,
    pub body: Vec<u8>,
}

impl Response {
    pub fn json(status: u16, body: &str) -> Self {
        Self {
            status,
            content_type: "application/json".into(),
            body: body.as_bytes().to_vec(),
        }
    }

    pub fn ndjson(lines: &[String]) -> Self {
        Self {
            status: 200,
            content_type: "application/x-ndjson".into(),
            body: format!("{}\n", lines.join("\n")).into_bytes(),
        }
    }
}

type Handler = dyn Fn(&Request) -> Response + Send + Sync + 'static;

/// Spawns a server thread; returns its base URL and the recorded requests.
pub fn serve(handler: impl Fn(&Request) -> Response + Send + Sync + 'static) -> MockServer {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
    let base_url = format!("http://{}", listener.local_addr().unwrap());
    let requests: Arc<Mutex<Vec<Request>>> = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&requests);
    let handler: Arc<Handler> = Arc::new(handler);

    thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { break };
            let recorded = Arc::clone(&recorded);
            let handler = Arc::clone(&handler);
            thread::spawn(move || {
                let mut reader = BufReader::new(stream);
                while let Some(request) = read_request(&mut reader) {
                    let response = handler(&request);
                    recorded.lock().unwrap().push(request);
                    let head = format!(
                        "HTTP/1.1 {} X\r\nContent-Type: {}\r\nContent-Length: {}\r\n\r\n",
                        response.status,
                        response.content_type,
                        response.body.len()
                    );
                    let stream = reader.get_mut();
                    if stream.write_all(head.as_bytes()).is_err()
                        || stream.write_all(&response.body).is_err()
                    {
                        break;
                    }
                    let _ = stream.flush();
                }
            });
        }
    });

    MockServer { base_url, requests }
}

pub struct MockServer {
    pub base_url: String,
    pub requests: Arc<Mutex<Vec<Request>>>,
}

impl MockServer {
    pub fn recorded(&self) -> Vec<Request> {
        self.requests.lock().unwrap().clone()
    }
}

fn read_request(reader: &mut BufReader<std::net::TcpStream>) -> Option<Request> {
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).ok()? == 0 {
        return None;
    }
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?.to_string();
    let path = parts.next()?.to_string();

    let mut headers = Vec::new();
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).ok()?;
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.push((name.trim().to_string(), value.trim().to_string()));
        }
    }

    let length: usize = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, v)| v.parse().ok())
        .unwrap_or(0);
    let mut body = vec![0u8; length];
    if length > 0 {
        reader.read_exact(&mut body).ok()?;
    }

    Some(Request {
        method,
        path,
        headers,
        body,
    })
}
