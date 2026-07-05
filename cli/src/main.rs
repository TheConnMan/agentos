//! AgentOS CLI. Filled in by task I1 (clap + tokio + reqwest: init/start/send/eval,
//! local runner orchestration via Docker). R0 ships only a compiling skeleton so the
//! Rust half of the verification harness has something green to run.

fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

fn main() {
    println!("agentos {}", version());
}

#[cfg(test)]
mod tests {
    use super::version;

    #[test]
    fn reports_package_version() {
        assert_eq!(version(), "0.0.0");
    }
}
