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
    store.refresh(&day, &rooms, false, now())?;
    let api = Api::new("test-key".into(), &config)?;
    let target = config.watch_minutes * 60;
    let mut runner = Runner::new(api, store, config)?;
    runner.rooms = rooms;
    runner.schedule_watch()?;
    assert_eq!(runner.watch.as_ref().map(|w| w.uid), Some(2));
    runner.store.reserve(&day, 2, Action::Watch(target), 0)?;
    runner.watch = None;
    runner.schedule_watch()?;
    assert_eq!(runner.watch.as_ref().map(|w| w.uid), Some(1));
    runner.rooms.clear();
    runner.schedule_watch()?;
    assert!(runner.watch.is_none());
    Ok(())
}
