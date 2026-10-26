use super::*;
use axum::{Json, Router, http::StatusCode, routing::any};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
#[test]
fn signatures_match_independent_python_vectors() {
    let fields = sign(
        vec![("msg".into(), "[花] a~*".into())],
        "test-key",
        1700000000,
    );
    assert_eq!(
        fields
            .iter()
            .find(|(k, _)| k == "sign")
            .map(|(_, v)| v.as_str()),
        Some("427aad403af8f1071251369bdcd917e9")
    );
    assert_eq!(
        client_sign(r#"{"platform":"android","uuid":"fixed"}"#),
        "c4f02598ba05993036f542aee89ef5c7a20b2b44050946fc84d2ed9e92f55bba64eac733700766206164357dfe5d30ff538c3741225f288d50917dca41ddd685"
    );
}
#[test]
fn discovery_rejects_partial_or_malformed_snapshots() {
    assert!(decode_rooms(json!({}), &[1]).is_err());
    assert!(decode_rooms(json!({"1":{"live_status":true}}), &[1]).is_err());
    assert!(decode_rooms(json!({"1":{"live_status":0}}), &[1]).is_ok_and(|r| r.is_empty()));
}
#[test]
fn heartbeat_uses_actual_area_stable_identity_and_midnight_boundary() {
    let room = Room {
        uid: 1,
        room_id: 2,
        name: "name".into(),
        title: "title".into(),
        live_status: 1,
        area: 371,
        parent: 9,
    };
    let session = Heartbeat::new(&room);
    let a = heartbeat_payload(&room, &session, 60, 1700000000);
    let b = heartbeat_payload(&room, &session, 60, 1700000060);
    assert_eq!(a["uuid"], b["uuid"]);
    assert_eq!(a["click_id"], b["click_id"]);
    assert_eq!(a["area_id"], "371");
    assert_eq!(a["parent_id"], "9");
    let midnight = 1700006400; // 2023-11-15 08:00 UTC; use exact Shanghai midnight below.
    let midnight = midnight - (midnight + 28800) % 86400;
    assert_eq!(
        heartbeat_payload(&room, &session, 60, midnight + 5)["watch_time"],
        "5"
    );
}
#[tokio::test]
async fn transport_retries_only_get_and_reports_post_uncertainty() -> Result<()> {
    for (method, status, expected) in [
        (Method::GET, 503, 2),
        (Method::GET, 429, 1),
        (Method::POST, 503, 1),
    ] {
        let calls = Arc::new(AtomicUsize::new(0));
        let count = calls.clone();
        let fixture = Router::new().route(
            "/",
            any(move || {
                let n = count.fetch_add(1, Ordering::SeqCst);
                async move {
                    (
                        if n == 0 {
                            StatusCode::from_u16(status)
                                .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
                        } else {
                            StatusCode::OK
                        },
                        Json(json!({"code":0,"data":{}})),
                    )
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let server = tokio::spawn(async move {
            let _ = axum::serve(listener, fixture).await;
        });
        let mut api = Api {
            client: Client::builder()
                .emulation(Emulation::Chrome131)
                .redirect(wreq::redirect::Policy::none())
                .timeout(Duration::from_secs(3))
                .build()?,
            key: "test".into(),
            mid: 1,
            buvid: "test".into(),
            next: Instant::now(),
            interval: Duration::ZERO,
        };
        let result = api
            .request(
                method.clone(),
                &format!("http://{address}/"),
                vec![],
                false,
                false,
            )
            .await;
        assert_eq!(calls.load(Ordering::SeqCst), expected);
        if method == Method::POST {
            assert!(result.is_err_and(Failure::ambiguous));
        }
        server.abort();
    }
    Ok(())
}
