use super::*;
#[test]
fn watching_uses_one_slot_in_priority_order_and_never_repeats_reserved_time() -> Result<()> {
    let mut config: Config = serde_yaml_ng::from_str(include_str!("../config.example.yaml"))?;
    config.include_uids = vec![1, 2];
    config.watch_uids = vec![2, 1];
    let day = day(now())?.to_string();
    let room = |uid| Room {
        uid,
        room_id: uid,
        name: format!("主播{uid}"),
        title: "演出".into(),
        live_status: 1,
        area: 1,
        parent: 1,
    };
    let rooms = vec![room(1), room(2)];
    let mut store = Store::open(std::path::Path::new(":memory:"), &day)?;
    store.refresh(
        &day,
        &crate::api::Discovery {
            rooms: rooms.clone(),
            ..Default::default()
        },
        false,
        now(),
    )?;
    let api = Api::new("test-key".into(), &config)?;
    let target = config.watch_minutes * 60;
    let mut runner = Runner::new(api, store, config)?;
    runner.rooms = rooms;
    runner.schedule_watch()?;
    assert_eq!(runner.watch.as_ref().map(|w| w.uid), Some(2));
    assert!(
        runner
            .store
            .reserve(&day, 2, Action::Watch(target), 0, &runner.config)?
    );
    runner.watch = None;
    runner.schedule_watch()?;
    assert_eq!(runner.watch.as_ref().map(|w| w.uid), Some(1));
    runner.rooms.clear();
    runner.schedule_watch()?;
    assert!(runner.watch.is_none());
    Ok(())
}

#[tokio::test]
async fn partial_discovery_keeps_other_tasks_running_and_all_unknown_remains_unhealthy()
-> Result<()> {
    use crate::api::{Discovery, decode_rooms};
    use serde_json::json;
    let mut config: Config = serde_yaml_ng::from_str(include_str!("../config.example.yaml"))?;
    config.include_uids = vec![1, 2];
    config.watch_uids = vec![2, 1];
    config.ntfy = None;
    let day = day(now())?.to_string();
    let store = Store::open(std::path::Path::new(":memory:"), &day)?;
    let api = Api::new("test-key".into(), &config)?;
    let mut runner = Runner::new(api, store, config)?;
    let address = runner.health.start(0).await?;
    let client = wreq::Client::new();
    let url = format!("http://{address}/healthz");
    let room = json!({"room_id":10,"uname":"主播","title":"直播","live_status":1,"area_v2_id":1,"area_v2_parent_id":1});
    runner.refresh(decode_rooms(json!({"1":room,"2":room}), &[1, 2])?, now())?;
    runner.schedule_watch()?;
    assert_eq!(runner.watch.as_ref().map(|w| w.uid), Some(2));
    runner.refresh(decode_rooms(json!({"1":room}), &[1, 2])?, now())?;
    runner.schedule_watch()?;
    assert_eq!(runner.watch.as_ref().map(|w| w.uid), Some(1));
    assert_eq!(
        runner.rooms.iter().map(|r| r.uid).collect::<Vec<_>>(),
        vec![1]
    );
    assert_eq!(client.get(&url).send().await?.status().as_u16(), 200);
    for _ in 0..3 {
        runner.refresh(
            Discovery {
                unavailable: vec![1, 2],
                ..Default::default()
            },
            now(),
        )?;
    }
    runner.schedule_watch()?;
    assert!(runner.rooms.is_empty());
    assert!(runner.watch.is_none());
    assert_eq!(client.get(&url).send().await?.status().as_u16(), 503);
    runner.refresh(
        Discovery {
            offline: vec![1, 2],
            ..Default::default()
        },
        now(),
    )?;
    assert_eq!(client.get(&url).send().await?.status().as_u16(), 200);
    Ok(())
}

#[tokio::test]
async fn live_rooms_do_not_send_danmaku_and_offline_rooms_do_not_like() -> Result<()> {
    let mut config: Config = serde_yaml_ng::from_str(include_str!("../config.example.yaml"))?;
    config.include_uids = vec![1, 2];
    config.watch_uids = vec![2];
    let day = day(now())?.to_string();
    let store = Store::open(std::path::Path::new(":memory:"), &day)?;
    let api = Api::new("test-key".into(), &config)?;
    let mut runner = Runner::new(api, store, config)?;
    let room = |id, status| {
        serde_json::json!({
            "room_id": id, "uname": "主播", "title": "", "live_status": status,
            "area_v2_id": 1, "area_v2_parent_id": 1
        })
    };
    runner.refresh(
        crate::api::decode_rooms(serde_json::json!({"1":room(11,1),"2":room(22,0)}), &[1, 2])?,
        now(),
    )?;
    runner.execute(1, Action::Danmaku).await?;
    runner.execute(2, Action::Like).await?;
    runner.schedule_watch()?;
    assert!(runner.watch.is_none());
    assert_eq!(runner.store.progress(&day, 1)?.danmaku_attempted, 0);
    assert_eq!(runner.store.progress(&day, 2)?.likes_attempted, 0);
    Ok(())
}
