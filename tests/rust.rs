use anyhow::Result;
use bilibili_live_helper::{
    api::{Discovery, Failure, Room},
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
        live_time: 0,
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
    store.refresh(
        day,
        &Discovery {
            rooms: vec![room.clone()],
            ..Default::default()
        },
        true,
        1,
    )?;
    assert!(store.reserve(day, 1, Action::Like, 60, &config)?);
    drop(store);
    let mut store = Store::open(&file, day)?;
    assert_eq!(store.progress(day, 1)?.likes_attempted, 1);
    assert_eq!(store.progress(day, 1)?.likes, 0);
    assert!(store.reserve(day, 1, Action::Like, 120, &config)?);
    store.settle(
        day,
        &room,
        Action::Like,
        Err(Failure::Rejected(10030)),
        &config,
        120,
    )?;
    assert_eq!(store.progress(day, 1)?.likes_attempted, 2);
    assert!(store.reserve(day, 1, Action::Watch(60), 0, &config)?);
    store.settle(
        day,
        &room,
        Action::Watch(60),
        Err(Failure::Transport),
        &config,
        120,
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
    store.refresh(
        "2026-10-25",
        &Discovery {
            rooms: vec![room.clone()],
            ..Default::default()
        },
        true,
        1,
    )?;
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

#[test]
fn unknown_rooms_do_not_fabricate_stream_endings_or_repeat_open_notifications() -> Result<()> {
    let day = "2026-11-18";
    let mut store = Store::open(std::path::Path::new(":memory:"), day)?;
    let room = room();
    let live = || Discovery {
        rooms: vec![room.clone()],
        ..Default::default()
    };
    store.refresh(day, &live(), true, 100)?;
    let message = store
        .pending(100)?
        .ok_or_else(|| anyhow::anyhow!("missing open notification"))?;
    store.delivered(&message)?;
    store.refresh(
        day,
        &Discovery {
            unavailable: vec![1],
            ..Default::default()
        },
        true,
        200,
    )?;
    store.refresh(day, &live(), true, 300)?;
    assert!(store.pending(300)?.is_none());
    store.refresh(
        day,
        &Discovery {
            offline: vec![1],
            ..Default::default()
        },
        true,
        400,
    )?;
    store.refresh(day, &live(), true, 500)?;
    assert!(store.pending(500)?.is_some());
    Ok(())
}

#[test]
fn rejected_and_uncertain_requests_cannot_exceed_daily_attempt_budget() -> Result<()> {
    let day = "2026-11-18";
    let mut config = config()?;
    config.danmaku_count = 2;
    config.watch_minutes = 1;
    config.ntfy = None;
    let room = room();
    for first in [Failure::Rejected(10030), Failure::Transport] {
        let mut store = Store::open(std::path::Path::new(":memory:"), day)?;
        store.refresh(
            day,
            &Discovery {
                rooms: vec![room.clone()],
                ..Default::default()
            },
            false,
            1,
        )?;
        assert!(store.reserve(day, 1, Action::Danmaku, 100, &config)?);
        store.settle(day, &room, Action::Danmaku, Err(first), &config, 110)?;
        assert_eq!(store.progress(day, 1)?.danmaku_attempted, 1);
        assert!(!store.reserve(day, 1, Action::Danmaku, 110, &config)?);
        assert!(store.reserve(
            day,
            1,
            Action::Danmaku,
            110 + config.danmaku_interval_seconds,
            &config
        )?);
        store.settle(day, &room, Action::Danmaku, Ok(()), &config, 500)?;
        assert!(!store.reserve(day, 1, Action::Danmaku, 1000, &config)?);
        assert_eq!(store.progress(day, 1)?.danmaku, 1);
        assert_eq!(store.progress(day, 1)?.danmaku_attempted, 2);
        assert!(store.reserve(day, 1, Action::Watch(60), 1000, &config)?);
        store.settle(day, &room, Action::Watch(60), Err(first), &config, 1000)?;
        assert!(!store.reserve(day, 1, Action::Watch(1), 1100, &config)?);
    }
    Ok(())
}

#[test]
fn cooldowns_survive_latency_restart_and_midnight_without_resetting_quotas_early() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let file = dir.path().join("state.sqlite");
    let day = "2026-11-18";
    let mut config = config()?;
    config.danmaku_count = 1;
    config.ntfy = None;
    let a = room();
    let b = Room {
        uid: 2,
        room_id: 3,
        ..a.clone()
    };
    let mut store = Store::open(&file, day)?;
    store.refresh(
        day,
        &Discovery {
            rooms: vec![a.clone(), b.clone()],
            ..Default::default()
        },
        false,
        1,
    )?;
    assert!(store.reserve(day, 1, Action::Danmaku, 100, &config)?);
    store.settle(day, &a, Action::Danmaku, Ok(()), &config, 120)?;
    drop(store);
    let mut store = Store::open(&file, day)?;
    assert!(!store.reserve(
        day,
        2,
        Action::Danmaku,
        120 + config.global_danmaku_interval_seconds - 1,
        &config
    )?);
    assert!(!store.reserve(day, 1, Action::Danmaku, 1000, &config)?);
    store.rollover("2026-11-19", false)?;
    assert_eq!(store.progress("2026-11-19", 1)?.danmaku_attempted, 0);
    assert!(!store.reserve(
        "2026-11-19",
        2,
        Action::Danmaku,
        120 + config.global_danmaku_interval_seconds - 1,
        &config
    )?);
    assert!(!store.reserve(
        "2026-11-19",
        1,
        Action::Danmaku,
        120 + config.danmaku_interval_seconds - 1,
        &config
    )?);
    assert!(store.reserve(
        "2026-11-19",
        2,
        Action::Danmaku,
        120 + config.global_danmaku_interval_seconds,
        &config
    )?);
    assert!(store.reserve(
        "2026-11-19",
        1,
        Action::Danmaku,
        120 + config.danmaku_interval_seconds,
        &config
    )?);
    Ok(())
}

#[test]
fn schema_one_upgrade_preserves_progress_notifications_and_conservative_cooldown() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let file = dir.path().join("state.sqlite");
    let db = rusqlite::Connection::open(&file)?;
    db.execute_batch(include_str!("fixtures/state-v1.sql"))?;
    db.execute(
        "INSERT INTO meta(id,day,failures,alerted) VALUES(1,'2026-11-18',2,1)",
        [],
    )?;
    db.execute("INSERT INTO progress(day,uid,name,danmaku,danmaku_attempted,danmaku_due) VALUES('2026-11-18',1,'主播',1,2,300)",[])?;
    db.execute(
        "INSERT INTO outbox(id,title,body,tag) VALUES('health','故障','原通知','warning')",
        [],
    )?;
    drop(db);
    let store = Store::open(&file, "2026-11-18")?;
    assert_eq!(store.progress("2026-11-18", 1)?.danmaku_attempted, 2);
    assert_eq!(store.progress("2026-11-18", 1)?.danmaku, 1);
    assert_eq!(store.danmaku_due()?, 300);
    assert_eq!(store.pending(0)?.map(|m| m.body), Some("原通知".into()));
    drop(store);
    let db = rusqlite::Connection::open(&file)?;
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))?,
        2
    );
    assert_eq!(
        db.query_row("SELECT failures,alerted FROM meta", [], |r| Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?
        )))?,
        (2, 1)
    );
    Ok(())
}
