use axum::{Json, Router, http::StatusCode, routing::get};
use tokio::sync::watch;

pub struct Health {
    state: watch::Sender<(Option<i64>, i64)>,
    max_age: i64,
}

impl Health {
    pub fn new(max_age: i64) -> Self {
        Self {
            state: watch::channel((None, 0)).0,
            max_age,
        }
    }

    pub fn record(&self, failures: i64) {
        self.state.send_modify(|state| {
            state.1 = failures;
            if failures == 0 {
                state.0 = Some(crate::now());
            }
        });
    }

    pub async fn start(&self, port: u16) -> anyhow::Result<std::net::SocketAddr> {
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)).await?;
        let address = listener.local_addr()?;
        let state = self.state.clone();
        let max_age = self.max_age;
        let app = Router::new().route("/healthz", get(move || {
            let (last_success, failures) = *state.borrow();
            async move {
                let ok = failures < 3 && last_success.is_some_and(|stamp| crate::now() - stamp <= max_age);
                let code = if ok { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE };
                (code, Json(serde_json::json!({"ok":ok,"last_success":last_success,"failures":failures})))
            }
        }));
        tokio::spawn(async move {
            if let Err(error) = axum::serve(listener, app).await {
                tracing::error!(%error, "health server stopped");
            }
        });
        Ok(address)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn endpoint_tracks_warmup_failure_staleness_and_recovery() -> anyhow::Result<()> {
        let health = Health::new(60);
        let url = format!("http://{}/healthz", health.start(0).await?);
        let client = wreq::Client::new();
        assert_eq!(client.get(&url).send().await?.status().as_u16(), 503);
        health.record(0);
        assert_eq!(client.get(&url).send().await?.status().as_u16(), 200);
        health.record(3);
        assert_eq!(client.get(&url).send().await?.status().as_u16(), 503);
        health.state.send_replace((Some(0), 0));
        assert_eq!(client.get(&url).send().await?.status().as_u16(), 503);
        health.record(0);
        assert_eq!(client.get(&url).send().await?.status().as_u16(), 200);
        Ok(())
    }
}
