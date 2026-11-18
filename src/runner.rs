use crate::{
    api::{Api, Failure, Heartbeat, Room, publish},
    config::Config,
    day, now,
    store::{Action, Store},
};
use anyhow::Result;
use std::time::Duration;
use tokio::time::Instant;

struct Watch {
    uid: i64,
    room: i64,
    session: Heartbeat,
    started: Instant,
    due: Instant,
    seconds: i64,
}
pub struct Runner {
    api: Api,
    store: Store,
    config: Config,
    health: crate::health::Health,
    rooms: Vec<Room>,
    watch: Option<Watch>,
    poll: Instant,
    day: String,
    notifier: wreq::Client,
}
impl Runner {
    pub fn new(api: Api, store: Store, config: Config) -> Result<Self> {
        Ok(Self {
            api,
            store,
            health: crate::health::Health::new(
                config.poll_interval_seconds * 3 + config.request_timeout_seconds * 2,
            ),
            config,
            rooms: Vec::new(),
            watch: None,
            poll: Instant::now(),
            day: day(now())?.to_string(),
            notifier: wreq::Client::builder()
                .redirect(wreq::redirect::Policy::none())
                .timeout(Duration::from_secs(10))
                .build()?,
        })
    }
    pub async fn run(mut self) -> Result<()> {
        if let Ok(port) = std::env::var("BILIBILI_LIVE_HELPER_HEALTH_PORT") {
            self.health.start(port.parse()?).await?;
        }
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        loop {
            // Finish a possibly delivered request before shutdown. Its reservation
            // is durable even if systemd ultimately kills the process.
            tokio::select! {biased;_=term.recv()=>return Ok(()),result=tokio::signal::ctrl_c()=>{result?;return Ok(())},_=tokio::time::sleep(Duration::from_millis(100))=>{}}
            let current = day(now())?.to_string();
            if current != self.day {
                self.watch = None;
                self.rooms.clear();
                self.poll = Instant::now();
                self.day = current;
            }
            self.store.rollover(&self.day, self.config.ntfy.is_some())?;
            if Instant::now() >= self.poll {
                let result = async {
                    self.api.login().await?;
                    self.api.discover(&self.config.include_uids).await
                }
                .await;
                // A network operation may cross midnight. Never apply its old
                // room snapshot to the new day's jobs.
                if day(now())?.to_string() != self.day {
                    continue;
                }
                match result {
                    Ok(discovery) => self.refresh(discovery, now())?,
                    Err(failure) => {
                        tracing::warn!(?failure, "poll failed; live jobs suspended");
                        self.rooms.clear();
                        self.watch = None;
                        self.health.record(
                            self.store
                                .health(Some(failure), self.config.ntfy.is_some())?,
                            &[],
                        );
                    }
                }
                self.poll = Instant::now()
                    + Duration::from_secs((self.config.poll_interval_seconds).unsigned_abs());
            }
            if let Some(n) = &self.config.ntfy
                && let Some(message) = self.store.pending(now())?
            {
                if publish(&self.notifier, n, &message).await.is_ok() {
                    self.store.delivered(&message)?;
                } else {
                    self.store.failed(&message.id, now())?;
                }
            }
            if day(now())?.to_string() != self.day {
                continue;
            }
            self.schedule_watch()?;
            let timestamp = now();
            // Earliest due first; each room/action advances its own clock.
            let mut candidates = Vec::new();
            let danmaku_due = self.store.danmaku_due()?;
            for room in &self.rooms {
                self.store.complete(&self.day, room, &self.config)?;
                let p = self.store.progress(&self.day, room.uid)?;
                if p.likes_attempted < self.config.like_request_count {
                    candidates.push((p.likes_due, room.uid, Action::Like));
                }
                if p.danmaku_attempted < self.config.danmaku_count {
                    candidates.push((p.danmaku_due.max(danmaku_due), room.uid, Action::Danmaku));
                }
            }
            candidates.sort_by_key(|(due, uid, _)| (*due, *uid));
            if let Some(watch) = &self.watch
                && Instant::now() >= watch.due
            {
                self.execute(watch.uid, Action::Watch(watch.seconds))
                    .await?;
                // A late heartbeat reports only the scheduled interval, never
                // network or event-loop delays as fabricated viewing time.
                if let Some(watch) = &mut self.watch {
                    let p = self.store.progress(&self.day, watch.uid)?;
                    let remaining = (self.config.watch_minutes * 60 - p.watch_attempted).max(0);
                    if remaining == 0 {
                        self.watch = None;
                    } else {
                        watch.seconds = remaining.min(self.config.heartbeat_interval_seconds);
                        watch.started = Instant::now();
                        watch.due =
                            watch.started + Duration::from_secs(watch.seconds.unsigned_abs());
                    }
                }
            } else if let Some((due, uid, action)) = candidates.into_iter().next()
                && due <= timestamp
            {
                self.execute(uid, action).await?;
            }
        }
    }
    fn refresh(&mut self, discovery: crate::api::Discovery, timestamp: i64) -> Result<()> {
        if !discovery.unavailable.is_empty() {
            tracing::warn!(uids=?discovery.unavailable, "live rooms missing or invalid; other rooms continue");
        }
        let failure = if discovery.rooms.is_empty() && discovery.offline.is_empty() {
            Some(Failure::Invalid)
        } else {
            None
        };
        self.store
            .refresh(&self.day, &discovery, self.config.ntfy.is_some(), timestamp)?;
        self.health.record(
            self.store.health(failure, self.config.ntfy.is_some())?,
            &discovery.unavailable,
        );
        self.rooms = discovery.rooms;
        Ok(())
    }
    fn schedule_watch(&mut self) -> Result<()> {
        if let Some(w) = &self.watch {
            if self
                .rooms
                .iter()
                .any(|r| r.uid == w.uid && r.room_id == w.room)
            {
                return Ok(());
            }
            self.watch = None;
        }
        for uid in &self.config.watch_uids {
            if let Some(room) = self.rooms.iter().find(|r| r.uid == *uid) {
                let p = self.store.progress(&self.day, *uid)?;
                let remaining = (self.config.watch_minutes * 60 - p.watch_attempted).max(0);
                if remaining == 0 {
                    continue;
                }
                let seconds = remaining.min(self.config.heartbeat_interval_seconds);
                let started = Instant::now();
                self.watch = Some(Watch {
                    uid: *uid,
                    room: room.room_id,
                    session: Heartbeat::new(room),
                    started,
                    due: started + Duration::from_secs((seconds).unsigned_abs()),
                    seconds,
                });
                break;
            }
        }
        Ok(())
    }
    async fn execute(&mut self, uid: i64, action: Action) -> Result<()> {
        let Some(room) = self.rooms.iter().find(|r| r.uid == uid).cloned() else {
            return Ok(());
        };
        let timestamp = now();
        // Do not report an interval extending beyond Shanghai midnight.
        if let Action::Watch(seconds) = action
            && seconds > (timestamp + 8 * 3600).rem_euclid(86400)
        {
            return Ok(());
        }
        if !self
            .store
            .reserve(&self.day, uid, action, timestamp, &self.config)?
        {
            return Ok(());
        }
        let result = match action {
            Action::Like => {
                self.api
                    .like(&room, self.config.like_clicks_per_request)
                    .await
            }
            Action::Danmaku => self.api.danmaku(&room).await,
            Action::Watch(seconds) => {
                if let Some(w) = &self.watch {
                    debug_assert!(
                        w.started.elapsed() >= Duration::from_secs((seconds).unsigned_abs())
                    );
                    self.api.heartbeat(&room, &w.session, seconds).await
                } else {
                    Err(Failure::Invalid)
                }
            }
        };
        self.store
            .settle(&self.day, &room, action, result, &self.config, now())?;
        if matches!(action, Action::Watch(_)) && result.is_err() {
            self.watch = None;
        }
        if matches!(result, Err(Failure::Rejected(-101 | -102))) {
            self.health.record(
                self.store
                    .health(result.err(), self.config.ntfy.is_some())?,
                &[],
            );
            self.rooms.clear();
            self.watch = None;
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "runner_tests.rs"]
mod tests;
