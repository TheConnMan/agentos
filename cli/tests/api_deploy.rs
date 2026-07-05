//! Integration: the platform API client's deploy flow against the OpenAPI
//! contract shapes (apps/api openapi.json), served by a wire-level test server.

mod support;

use agentos::api::ApiClient;
use agentos::bundle::pack_tar_gz;
use agentos::scaffold::scaffold;
use support::{serve, Response};

const AGENT_ID: &str = "11111111-1111-1111-1111-111111111111";
const VERSION_ID: &str = "22222222-2222-2222-2222-222222222222";
const DEPLOYMENT_ID: &str = "33333333-3333-3333-3333-333333333333";

fn route(method: &str, path: &str) -> Response {
    match (method, path) {
        ("GET", "/agents") => Response::json(200, "[]"),
        ("POST", "/agents") => Response::json(
            201,
            &format!(
                r##"{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"#local-dev","created_at":"2026-07-05T00:00:00Z"}}"##
            ),
        ),
        ("POST", p) if p == format!("/agents/{AGENT_ID}/versions") => Response::json(
            201,
            &format!(
                r#"{{"id":"{VERSION_ID}","agent_id":"{AGENT_ID}","version_label":"0.1.0-1","bundle_ref":null,"bundle_sha256":null,"created_by":"tester","created_at":"2026-07-05T00:00:00Z"}}"#
            ),
        ),
        ("PUT", p) if p == format!("/agents/{AGENT_ID}/versions/{VERSION_ID}/bundle") => {
            Response::json(
                201,
                &format!(
                    r#"{{"version_id":"{VERSION_ID}","bundle_ref":"bundles/x.tar.gz","bundle_sha256":"deadbeef","size_bytes":512}}"#
                ),
            )
        }
        ("POST", "/deployments") => Response::json(
            201,
            &format!(
                r#"{{"id":"{DEPLOYMENT_ID}","agent_id":"{AGENT_ID}","version_id":"{VERSION_ID}","environment":"dev","status":"active","deployed_at":"2026-07-05T00:00:00Z"}}"#
            ),
        ),
        other => panic!("unexpected request: {other:?}"),
    }
}

#[tokio::test]
async fn deploy_walks_the_full_contract_flow_with_auth() {
    let server = serve(|req| route(&req.method, &req.path));
    let client = ApiClient::new(&server.base_url, "test-key").unwrap();

    let dir = tempfile::tempdir().unwrap();
    scaffold(dir.path(), "deal-desk").unwrap();
    let archive = pack_tar_gz(dir.path()).unwrap();

    let outcome = client
        .deploy(
            "deal-desk",
            "#local-dev",
            "0.1.0-1",
            "tester",
            "dev",
            archive,
        )
        .await
        .unwrap();

    assert_eq!(outcome.agent.id, AGENT_ID);
    assert_eq!(outcome.version.id, VERSION_ID);
    assert_eq!(outcome.bundle.bundle_sha256, "deadbeef");
    assert_eq!(outcome.deployment.id, DEPLOYMENT_ID);
    assert_eq!(outcome.deployment.environment, "dev");

    let recorded = server.recorded();
    let flow: Vec<(String, String)> = recorded
        .iter()
        .map(|r| (r.method.clone(), r.path.clone()))
        .collect();
    assert_eq!(
        flow,
        vec![
            ("GET".to_string(), "/agents".to_string()),
            ("POST".to_string(), "/agents".to_string()),
            ("POST".to_string(), format!("/agents/{AGENT_ID}/versions")),
            (
                "PUT".to_string(),
                format!("/agents/{AGENT_ID}/versions/{VERSION_ID}/bundle")
            ),
            ("POST".to_string(), "/deployments".to_string()),
        ]
    );
    for request in &recorded {
        assert_eq!(request.header("x-api-key"), Some("test-key"));
    }

    // The bundle upload is multipart with the archive under the `file` field.
    let upload = &recorded[3];
    assert!(upload
        .header("content-type")
        .unwrap()
        .starts_with("multipart/form-data"));
    let body = String::from_utf8_lossy(&upload.body);
    assert!(body.contains("name=\"file\""));
    assert!(body.contains("filename=\"bundle.tar.gz\""));
}

#[tokio::test]
async fn reuses_an_existing_agent_instead_of_creating() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => Response::json(
            200,
            &format!(
                r##"[{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"#x","created_at":"2026-07-05T00:00:00Z"}}]"##
            ),
        ),
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let agent = client
        .find_or_create_agent("deal-desk", "#local-dev")
        .await
        .unwrap();
    assert_eq!(agent.id, AGENT_ID);
    assert_eq!(server.recorded().len(), 1);
}

#[tokio::test]
async fn surfaces_api_errors_with_status_and_body() {
    let server = serve(|_req| Response::json(401, r#"{"detail":"invalid API key"}"#));
    let client = ApiClient::new(&server.base_url, "wrong").unwrap();
    let err = client.list_agents().await.unwrap_err();
    let text = err.to_string();
    assert!(text.contains("401"), "unexpected error: {text}");
    assert!(text.contains("invalid API key"), "unexpected error: {text}");
}
