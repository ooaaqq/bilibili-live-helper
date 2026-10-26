use anyhow::{Result, ensure};
use serde::Deserialize;
use std::{collections::HashSet, path::Path};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ntfy {
    pub server: url::Url,
    pub topic: String,
    pub token: Option<String>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub include_uids: Vec<i64>,
    pub watch_uids: Vec<i64>,
    pub poll_interval_seconds: i64,
    pub request_timeout_seconds: i64,
    pub api_interval_seconds: i64,
    pub like_clicks_per_request: i64,
    pub like_request_count: i64,
    pub like_interval_seconds: i64,
    pub watch_minutes: i64,
    pub heartbeat_interval_seconds: i64,
    pub danmaku_count: i64,
    pub danmaku_interval_seconds: i64,
    pub global_danmaku_interval_seconds: i64,
    pub ntfy: Option<Ntfy>,
}
impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let config: Self = serde_yaml_ng::from_str(&std::fs::read_to_string(path)?)
            .map_err(|_| anyhow::anyhow!("invalid helper configuration"))?;
        config.validate()?;
        Ok(config)
    }
    pub fn validate(&self) -> Result<()> {
        let included = self.include_uids.iter().copied().collect::<HashSet<_>>();
        ensure!(
            !included.is_empty()
                && included.len() == self.include_uids.len()
                && included.iter().all(|id| *id > 0),
            "invalid include_uids"
        );
        ensure!(
            self.watch_uids.iter().all(|id| included.contains(id))
                && self.watch_uids.iter().collect::<HashSet<_>>().len() == self.watch_uids.len(),
            "invalid watch_uids"
        );
        for (value, min, max) in [
            (self.poll_interval_seconds, 30, 3600),
            (self.request_timeout_seconds, 1, 120),
            (self.api_interval_seconds, 1, 60),
            (self.like_clicks_per_request, 1, 300),
            (self.like_request_count, 1, 100),
            (self.like_interval_seconds, 1, 3600),
            (self.watch_minutes, 0, 1440),
            (self.heartbeat_interval_seconds, 30, 300),
            (self.danmaku_count, 0, 100),
            (self.danmaku_interval_seconds, 1, 3600),
            (self.global_danmaku_interval_seconds, 1, 300),
        ] {
            ensure!(
                (min..=max).contains(&value),
                "configuration interval or count out of range"
            );
        }
        ensure!(
            self.watch_uids.is_empty() || self.watch_minutes > 0,
            "watch_minutes must be positive"
        );
        if let Some(n) = &self.ntfy {
            ensure!(
                matches!(n.server.scheme(), "http" | "https")
                    && n.server.host_str().is_some()
                    && n.server.username().is_empty()
                    && n.server.password().is_none()
                    && n.server.query().is_none()
                    && n.server.fragment().is_none(),
                "invalid ntfy server"
            );
            ensure!(
                !n.topic.is_empty()
                    && n.topic.len() <= 64
                    && n.topic
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b)),
                "invalid ntfy topic"
            );
            ensure!(
                n.token
                    .as_ref()
                    .is_none_or(|t| !t.is_empty() && t.bytes().all(|b| (32..127).contains(&b))),
                "invalid ntfy token"
            );
        }
        Ok(())
    }
}
