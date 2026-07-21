//! Integration (#767): drive the REAL `agentos::ops::down` through fake
//! `helm`/`kubectl` on PATH to prove the fail-forward WIRING that the ops-module
//! unit tests cannot exercise: the namespace sweep runs UNCONDITIONALLY after a
//! failed helm uninstall (the core anti-regression, since a revert to `bail!`
//! skips it), the human-visible error message carries the resume command (P1),
//! and the exit class tracks the failure kind (connectivity -> Transient exit 3,
//! permanent RBAC -> Failure exit 1, P2).
//!
//! EVERYTHING lives in ONE test fn. `down()` resolves `helm`/`kubectl` off the
//! process PATH, so the test mutates process-global PATH (and a SWEEP_MARKER env
//! var the fake kubectl records into); a second parallel test in this file would
//! race on that shared state. Each `tests/*.rs` file is its own test binary, so a
//! single test here is race-free, and it saves and restores the original PATH.
//!
//! These assertions are RED until the implementer hardens `down()`: today the
//! resume command lives only in the error `fix` (never composed into the Display
//! message, so scenario 1's message assertion fails), and any nonzero helm exit
//! returns Transient (so scenario 2's permanent case is mislabeled retryable).

use std::ffi::OsString;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use agentos::exit::{classify, ExitClass};
use agentos::ops::{down, CommonOpts, DownOpts};

/// Write `body` to `dir/name` and mark it executable (0o755).
fn write_exec(dir: &Path, name: &str, body: &str) {
    let path = dir.join(name);
    fs::write(&path, body).expect("write fake executable");
    let mut perms = fs::metadata(&path).expect("stat fake").permissions();
    perms.set_mode(0o755);
    fs::set_permissions(&path, perms).expect("chmod fake executable");
}

/// Prepend `dir` to the current process PATH so its fake binaries win resolution.
fn prepend_path(dir: &Path) {
    let existing = std::env::var_os("PATH").unwrap_or_default();
    let mut paths = vec![dir.to_path_buf()];
    paths.extend(std::env::split_paths(&existing));
    let joined = std::env::join_paths(paths).expect("join PATH");
    std::env::set_var("PATH", joined);
}

/// Restore PATH to the exact value captured at the start of the test.
fn restore_path(original: &Option<OsString>) {
    match original {
        Some(p) => std::env::set_var("PATH", p),
        None => std::env::remove_var("PATH"),
    }
}

/// The `cluster down` opts for a release whose name differs from its namespace,
/// so an assertion on the label VALUE unambiguously locks it to the release.
fn down_opts() -> DownOpts {
    DownOpts {
        common: CommonOpts {
            namespace: "agent-ns".into(),
            release: "prod-release".into(),
            dry_run: false,
        },
        yes: true,
    }
}

#[tokio::test]
async fn cluster_down_fails_forward_through_real_down() {
    let original_path = std::env::var_os("PATH");

    // ----- Scenario 1: unreachable API server (connectivity) -----
    // Fake helm fails with a TLS-handshake-timeout stderr. Fake kubectl records
    // that the sweep RAN (touch marker) and also fails, so both steps are
    // outstanding and the label-scoped resume command surfaces in the message.
    let dir1 = tempfile::tempdir().expect("tempdir");
    let marker1 = dir1.path().join("sweep-ran-1");
    std::env::set_var("SWEEP_MARKER", &marker1);
    let unreachable =
        "Error: Kubernetes cluster unreachable: Get \"https://h:6443/version\": net/http: TLS handshake timeout";
    write_exec(
        dir1.path(),
        "helm",
        &format!("#!/bin/sh\necho '{unreachable}' >&2\nexit 1\n"),
    );
    write_exec(
        dir1.path(),
        "kubectl",
        &format!("#!/bin/sh\ntouch \"$SWEEP_MARKER\"\necho '{unreachable}' >&2\nexit 1\n"),
    );
    prepend_path(dir1.path());

    let err = down(down_opts())
        .await
        .expect_err("an unreachable API server is an incomplete teardown");

    // Core anti-regression: the sweep ran even though helm failed first. A revert
    // to the old `bail!` on helm failure would leave this marker absent.
    assert!(
        marker1.exists(),
        "the namespace sweep must run unconditionally after a failed helm uninstall"
    );
    let (class, _fix) = classify(&err);
    assert_eq!(
        class,
        ExitClass::Transient,
        "a connectivity failure is retryable (exit 3)"
    );
    assert_eq!(class.code(), 3);
    // P1: a no-json human operator sees the resume command in the message itself.
    let shown = err.to_string();
    assert!(
        shown.contains("agentos.dev/created-by=prod-release"),
        "the human message must carry the label-scoped resume command: {shown}"
    );

    // ----- Scenario 2: permanent RBAC failure -----
    // Fake helm fails forbidden. Fake kubectl sweeps clean (exit 0), so only the
    // helm record remains, and a permanent failure must be a plain Failure (exit
    // 1), not a retryable transient. The sweep still ran (marker present).
    let dir2 = tempfile::tempdir().expect("tempdir");
    let marker2 = dir2.path().join("sweep-ran-2");
    std::env::set_var("SWEEP_MARKER", &marker2);
    let forbidden =
        "Error: uninstall: namespaces is forbidden: User \"x\" cannot delete resource \"namespaces\"";
    write_exec(
        dir2.path(),
        "helm",
        &format!("#!/bin/sh\necho '{forbidden}' >&2\nexit 1\n"),
    );
    write_exec(
        dir2.path(),
        "kubectl",
        "#!/bin/sh\ntouch \"$SWEEP_MARKER\"\nexit 0\n",
    );
    // Drop scenario 1's fakes: reset to the original PATH, then prepend dir2.
    restore_path(&original_path);
    prepend_path(dir2.path());

    let err = down(down_opts())
        .await
        .expect_err("a forbidden teardown is still an incomplete teardown");

    assert!(
        marker2.exists(),
        "the sweep still runs unconditionally on the permanent-failure path"
    );
    let (class, _fix) = classify(&err);
    assert_eq!(
        class,
        ExitClass::Failure,
        "a permanent RBAC failure must be a plain Failure (exit 1), not Transient"
    );
    assert_eq!(class.code(), 1);
    assert_ne!(class, ExitClass::Transient);

    // Restore PATH so nothing else in this process observes the fakes.
    restore_path(&original_path);
}
