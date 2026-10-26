use anyhow::Result;
use bilibili_live_helper::{
    api::{Failure, Room},
    config::Config,
    day,
    store::{Action, Store},
};
fn config() -> Result<Config> {
    Ok(serde_yaml_ng::from_str(include_str!(
        "../config.example.yaml"
    ))?)
}
fn room() -> Room {
    Room {
        uid: 1,
        room_id: 2,
        name: "主播".into(),
        title: "标题".into(),
        live_status: 1,
        area: 1,
        parent: 1,
    }
}
#[test]
fn ambiguous_requests_and_crash_reservations_survive_restart() -> Result<()> {
    let directory = tempfile::tempdir()?;
    let file = directory.path().join("state.sqlite");
    let day = "2026-10-26";
    let room = room();
    let config = config()?;
    let mut store = Store::open(&file, day)?;
    store.refresh(day, std::slice::from_ref(&room), true, 1)?;
    store.reserve(day, 1, Action::Like, 60)?;
    drop(store);
    let mut store = Store::open(&file, day)?;
    assert_eq!(store.progress(day, 1)?.likes_attempted, 1);
    assert_eq!(store.progress(day, 1)?.likes, 0);
    store.reserve(day, 1, Action::Like, 60)?;
    store.settle(
        day,
        &room,
        Action::Like,
        Err(Failure::Rejected(10030)),
        &config,
    )?;
    assert_eq!(store.progress(day, 1)?.likes_attempted, 1);
    store.reserve(day, 1, Action::Watch(60), 0)?;
    store.settle(
        day,
        &room,
        Action::Watch(60),
        Err(Failure::Transport),
        &config,
    )?;
    assert_eq!(store.progress(day, 1)?.watch_attempted, 60);
    assert_eq!(store.progress(day, 1)?.watched, 0);
    Ok(())
}
#[test]
fn health_discards_unsent_failure_and_recovers_only_after_delivered_alarm() -> Result<()> {
    let mut store = Store::open(std::path::Path::new(":memory:"), "2026-10-26")?;
    store.health(Some(Failure::Rejected(-101)), true)?;
    assert_eq!(store.pending(0)?.map(|m| m.tag), Some("warning".into()));
    store.health(None, true)?;
    assert!(store.pending(0)?.is_none());
    store.health(Some(Failure::Rejected(-101)), true)?;
    let message = store
        .pending(0)?
        .ok_or_else(|| anyhow::anyhow!("missing health"))?;
    store.delivered(&message)?;
    store.health(None, true)?;
    assert_eq!(
        store.pending(0)?.map(|m| m.tag),
        Some("white_check_mark".into())
    );
    Ok(())
}
#[test]
fn notifications_and_day_rollover_preserve_durable_progress() -> Result<()> {
    let mut store = Store::open(std::path::Path::new(":memory:"), "2026-10-25")?;
    let room = room();
    store.refresh("2026-10-25", std::slice::from_ref(&room), true, 1)?;
    let message = store
        .pending(1)?
        .ok_or_else(|| anyhow::anyhow!("missing live"))?;
    store.failed(&message.id, 1)?;
    assert!(store.pending(2)?.is_none());
    store.rollover("2026-10-26", true)?;
    let summary = store
        .pending(2)?
        .ok_or_else(|| anyhow::anyhow!("missing summary"))?;
    assert!(summary.body.contains("2026-10-25"));
    assert!(store.rollover("2026-10-24", true).is_err());
    Ok(())
}
#[test]
fn configuration_rejects_duplicates_unknown_fields_and_invalid_types() -> Result<()> {
    let text = include_str!("../config.example.yaml");
    config()?.validate()?;
    for invalid in [
        format!("{text}\nunknown: true"),
        format!("{text}\npoll_interval_seconds: 60"),
        text.replace("poll_interval_seconds: 120", "poll_interval_seconds: true"),
    ] {
        assert!(serde_yaml_ng::from_str::<Config>(&invalid).is_err());
    }
    let mut config = config()?;
    config.include_uids = vec![-1];
    assert!(config.validate().is_err());
    assert_eq!(day(1700064000)?.to_string(), "2023-11-16");
    Ok(())
}

#[test]
fn explicit_state_import_preserves_uncertain_work_and_rejects_overwrite() -> Result<()> {
    let directory = tempfile::tempdir()?;
    let file = directory.path().join("export.json");
    std::fs::write(
        &file,
        serde_json::to_vec(&serde_json::json!({
            "version":4,"day":"2026-10-26",
            "rooms":{"1":{"anchor_id":1,"anchor_name":"主播","likes_sent":2,"like_attempts":3,"danmaku_sent":1,"danmaku_attempts":1,"notification_queued":false}},
            "watches":{"1":{"anchor_id":1,"anchor_name":"主播","watched_seconds":60,"watch_seconds_attempted":120}},
            "outbox":{"event":{"sequence_id":"event","title":"标题","message":"内容","tags":"warning","attempts":1,"next_attempt_at":30.0}},
            "poll_failures":2,"poll_alerted":false
        }))?,
    )?;
    let mut store = Store::open(std::path::Path::new(":memory:"), "2026-10-26")?;
    store.import(&file)?;
    let progress = store.progress("2026-10-26", 1)?;
    assert_eq!(
        (
            progress.likes,
            progress.likes_attempted,
            progress.watched,
            progress.watch_attempted
        ),
        (2, 3, 60, 120)
    );
    assert!(store.import(&file).is_err());
    assert!(store.pending(29)?.is_none());
    assert_eq!(store.pending(30)?.map(|m| m.id), Some("event".into()));
    Ok(())
}

#[test]
fn invalid_import_rolls_back_before_accepting_valid_state() -> Result<()> {
    let directory = tempfile::tempdir()?;
    let file = directory.path().join("export.json");
    let mut value = serde_json::json!({
        "version":4,"day":"2026-10-26",
        "rooms":{"1":{"anchor_id":1,"anchor_name":"主播","likes_sent":2,"like_attempts":3,"danmaku_sent":1,"danmaku_attempts":1,"notification_queued":false}},
        "watches":{}, "outbox":{}, "poll_failures":-1,"poll_alerted":false
    });
    let mut store = Store::open(std::path::Path::new(":memory:"), "2026-10-26")?;
    std::fs::write(&file, serde_json::to_vec(&value)?)?;
    assert!(store.import(&file).is_err());
    assert!(store.progress("2026-10-26", 1).is_err());
    value["poll_failures"] = serde_json::json!(0);
    value["day"] = serde_json::json!("2026-02-30");
    std::fs::write(&file, serde_json::to_vec(&value)?)?;
    assert!(store.import(&file).is_err());
    value["day"] = serde_json::json!("2026-10-26");
    std::fs::write(&file, serde_json::to_vec(&value)?)?;
    store.import(&file)?;
    assert_eq!(store.progress("2026-10-26", 1)?.likes, 2);
    Ok(())
}
