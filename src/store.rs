use crate::{
    api::{Failure, Room},
    config::Config,
};
use anyhow::{Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::Value;
use std::path::Path;

const SCHEMA:&str="
CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1), day TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE progress(day TEXT NOT NULL,uid INTEGER NOT NULL,name TEXT NOT NULL,likes INTEGER NOT NULL DEFAULT 0,likes_attempted INTEGER NOT NULL DEFAULT 0,danmaku INTEGER NOT NULL DEFAULT 0,danmaku_attempted INTEGER NOT NULL DEFAULT 0,watched INTEGER NOT NULL DEFAULT 0,watch_attempted INTEGER NOT NULL DEFAULT 0,likes_due INTEGER NOT NULL DEFAULT 0,danmaku_due INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(day,uid),CHECK(likes<=likes_attempted AND danmaku<=danmaku_attempted AND watched<=watch_attempted));
CREATE TABLE live(uid INTEGER PRIMARY KEY,room INTEGER NOT NULL,active INTEGER NOT NULL);
CREATE TABLE outbox(id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,tag TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due INTEGER NOT NULL DEFAULT 0);
PRAGMA user_version=1;";
pub struct Store {
    db: Connection,
}
pub struct Progress {
    pub likes: i64,
    pub likes_attempted: i64,
    pub danmaku: i64,
    pub danmaku_attempted: i64,
    pub watched: i64,
    pub watch_attempted: i64,
    pub likes_due: i64,
    pub danmaku_due: i64,
    pub completed: bool,
}
#[derive(Clone)]
pub struct Message {
    pub id: String,
    pub title: String,
    pub body: String,
    pub tag: String,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Action {
    Like,
    Danmaku,
    Watch(i64),
}
impl Action {
    fn columns(self) -> (&'static str, &'static str, i64) {
        match self {
            Self::Like => ("likes", "likes_attempted", 1),
            Self::Danmaku => ("danmaku", "danmaku_attempted", 1),
            Self::Watch(n) => ("watched", "watch_attempted", n),
        }
    }
}
impl Store {
    pub fn open(path: &Path, day: &str) -> Result<Self> {
        let mut db = Connection::open(path)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.execute_batch("PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;")?;
        let version: i64 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        if version == 0 {
            let tx = db.transaction()?;
            tx.execute_batch(SCHEMA)?;
            tx.execute("INSERT INTO meta(id,day) VALUES(1,?)", [day])?;
            tx.commit()?;
        }
        ensure!(version <= 1, "unsupported state schema");
        Ok(Self { db })
    }
    pub fn rollover(&mut self, day: &str, notify: bool) -> Result<()> {
        let old: String = self
            .db
            .query_row("SELECT day FROM meta", [], |r| r.get(0))?;
        if old == day {
            return Ok(());
        }
        ensure!(day > old.as_str(), "system clock moved to an earlier day");
        let tx = self.db.transaction()?;
        if notify {
            let (confirmed,attempted):(i64,i64)=tx.query_row("SELECT COALESCE(SUM(watched),0),COALESCE(SUM(watch_attempted),0) FROM progress WHERE day=?",[&old],|r|Ok((r.get(0)?,r.get(1)?)))?;
            tx.execute(
                "INSERT OR IGNORE INTO outbox(id,title,body,tag) VALUES(?,'每日观看汇总',?,'eyes')",
                params![
                    format!("watch-{old}"),
                    format!(
                        "{old}\n已确认 {confirmed} 秒；结果不确定 {} 秒",
                        attempted - confirmed
                    )
                ],
            )?;
        }
        tx.execute("DELETE FROM progress", [])?;
        tx.execute("UPDATE meta SET day=?", [day])?;
        tx.commit()?;
        Ok(())
    }
    pub fn refresh(&mut self, day: &str, rooms: &[Room], notify: bool, now: i64) -> Result<()> {
        let tx = self.db.transaction()?;
        for room in rooms {
            let active: Option<(bool, i64)> = tx
                .query_row(
                    "SELECT active,room FROM live WHERE uid=?",
                    [room.uid],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()?;
            if notify && active != Some((true, room.room_id)) {
                tx.execute(
                    "INSERT OR IGNORE INTO outbox(id,title,body,tag) VALUES(?,?,?,'red_circle')",
                    params![
                        format!("live-{}-{now}", room.uid),
                        format!("「{}」开播了", room.name),
                        format!("{}\nhttps://live.bilibili.com/{}", room.title, room.room_id)
                    ],
                )?;
            }
            tx.execute("INSERT INTO progress(day,uid,name) VALUES(?,?,?) ON CONFLICT(day,uid) DO UPDATE SET name=excluded.name",params![day,room.uid,room.name])?;
        }
        tx.execute("UPDATE live SET active=0", [])?;
        for room in rooms {
            tx.execute("INSERT INTO live VALUES(?,?,1) ON CONFLICT(uid) DO UPDATE SET active=1,room=excluded.room",params![room.uid,room.room_id])?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn progress(&self, day: &str, uid: i64) -> Result<Progress> {
        Ok(self.db.query_row("SELECT likes,likes_attempted,danmaku,danmaku_attempted,watched,watch_attempted,likes_due,danmaku_due,completed FROM progress WHERE day=? AND uid=?",params![day,uid],|r|Ok(Progress{likes:r.get(0)?,likes_attempted:r.get(1)?,danmaku:r.get(2)?,danmaku_attempted:r.get(3)?,watched:r.get(4)?,watch_attempted:r.get(5)?,likes_due:r.get(6)?,danmaku_due:r.get(7)?,completed:r.get(8)?}))?)
    }
    pub fn reserve(&self, day: &str, uid: i64, action: Action, due: i64) -> Result<()> {
        let (_, attempted, amount) = action.columns();
        let due_column = match action {
            Action::Like => "likes_due",
            Action::Danmaku => "danmaku_due",
            Action::Watch(_) => "likes_due",
        };
        let sql = if matches!(action, Action::Watch(_)) {
            format!("UPDATE progress SET {attempted}={attempted}+? WHERE day=? AND uid=?")
        } else {
            format!(
                "UPDATE progress SET {attempted}={attempted}+?,{due_column}={due} WHERE day=? AND uid=?"
            )
        };
        ensure!(
            self.db.execute(&sql, params![amount, day, uid])? == 1,
            "missing progress"
        );
        Ok(())
    }
    pub fn settle(
        &mut self,
        day: &str,
        room: &Room,
        action: Action,
        result: std::result::Result<(), Failure>,
        config: &Config,
    ) -> Result<()> {
        let (confirmed, attempted, amount) = action.columns();
        let tx = self.db.transaction()?;
        match result {
            Ok(()) => {
                tx.execute(
                    &format!("UPDATE progress SET {confirmed}={confirmed}+? WHERE day=? AND uid=?"),
                    params![amount, day, room.uid],
                )?;
            }
            Err(failure) => {
                if !failure.ambiguous() {
                    tx.execute(
                        &format!(
                            "UPDATE progress SET {attempted}={attempted}-? WHERE day=? AND uid=?"
                        ),
                        params![amount, day, room.uid],
                    )?;
                }
                if config.ntfy.is_some() {
                    tx.execute("INSERT INTO outbox(id,title,body,tag) VALUES(?,?,?,'warning') ON CONFLICT(id) DO UPDATE SET body=excluded.body,due=0,attempts=0",params![format!("task-{day}-{}-{confirmed}",room.uid),format!("「{}」任务异常",room.name),format!("{confirmed}: {failure}；结果{}",if failure.ambiguous(){"不确定，不重发该次请求"}else{"明确拒绝，稍后重新调度"})])?;
                }
            }
        }
        tx.commit()?;
        self.complete(day, room, config)
    }
    pub fn complete(&mut self, day: &str, room: &Room, config: &Config) -> Result<()> {
        let p = self.progress(day, room.uid)?;
        if !p.completed
            && p.likes_attempted >= config.like_request_count
            && p.danmaku_attempted >= config.danmaku_count
        {
            let tx = self.db.transaction()?;
            if config.ntfy.is_some() {
                tx.execute("INSERT OR IGNORE INTO outbox(id,title,body,tag) VALUES(?,?,?,'white_check_mark')",params![format!("tasks-{day}-{}",room.uid),format!("「{}」每日互动任务结束",room.name),format!("点赞确认 {}/{} 批；弹幕确认 {}/{} 次。其余为结果不确定。",p.likes,p.likes_attempted,p.danmaku,p.danmaku_attempted)])?;
            }
            tx.execute(
                "UPDATE progress SET completed=1 WHERE day=? AND uid=?",
                params![day, room.uid],
            )?;
            tx.commit()?;
        }
        Ok(())
    }
    pub fn health(&mut self, failure: Option<Failure>, notify: bool) -> Result<()> {
        let tx = self.db.transaction()?;
        let (failures, alerted): (i64, bool) =
            tx.query_row("SELECT failures,alerted FROM meta", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?;
        let failures = if matches!(failure, Some(Failure::Rejected(-101 | -102))) {
            3
        } else if failure.is_some() {
            (failures + 1).min(3)
        } else {
            0
        };
        tx.execute("UPDATE meta SET failures=?", [failures])?;
        let desired = failures >= 3;
        // Replaceable current health, not an event history. A pending failure
        // disappears on recovery unless the failure was actually delivered.
        tx.execute(
            "DELETE FROM outbox WHERE id='health' AND tag<>?",
            [if failure.is_some() {
                "warning"
            } else {
                "white_check_mark"
            }],
        )?;
        if notify && ((desired && !alerted) || (failure.is_none() && alerted)) {
            tx.execute(
                "INSERT OR IGNORE INTO outbox(id,title,body,tag) VALUES('health',?,?,?)",
                params![
                    if desired {
                        "直播助手轮询失败"
                    } else {
                        "直播助手已恢复"
                    },
                    if desired {
                        "账号认证或直播状态获取失败，请检查凭据和网络。"
                    } else {
                        "账号认证和直播轮询已恢复。"
                    },
                    if desired {
                        "warning"
                    } else {
                        "white_check_mark"
                    }
                ],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn pending(&self, now: i64) -> Result<Option<Message>> {
        Ok(self
            .db
            .query_row(
                "SELECT id,title,body,tag FROM outbox WHERE due<=? ORDER BY due,id LIMIT 1",
                [now],
                |r| {
                    Ok(Message {
                        id: r.get(0)?,
                        title: r.get(1)?,
                        body: r.get(2)?,
                        tag: r.get(3)?,
                    })
                },
            )
            .optional()?)
    }
    pub fn delivered(&mut self, message: &Message) -> Result<()> {
        let tx = self.db.transaction()?;
        if message.id == "health" {
            tx.execute("UPDATE meta SET alerted=?", [message.tag == "warning"])?;
        }
        tx.execute("DELETE FROM outbox WHERE id=?", [&message.id])?;
        tx.commit()?;
        Ok(())
    }
    pub fn failed(&self, id: &str, now: i64) -> Result<()> {
        self.db.execute("UPDATE outbox SET due=?+MIN(900,30*(1<<MIN(attempts,5))),attempts=attempts+1 WHERE id=?",params![now,id])?;
        Ok(())
    }
    pub fn import(&mut self, path: &Path) -> Result<()> {
        let value: Value = serde_json::from_slice(&std::fs::read(path)?)?;
        ensure!(value["version"] == 4, "unsupported JSON export");
        let day = value["day"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("missing state day"))?;
        let parsed = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")?;
        ensure!(parsed.to_string() == day, "invalid state day");
        let tx = self.db.transaction()?;
        let count:i64=tx.query_row("SELECT (SELECT COUNT(*) FROM progress)+(SELECT COUNT(*) FROM outbox)+(SELECT COUNT(*) FROM live)",[],|r|r.get(0))?;
        ensure!(count == 0, "refusing to import over existing progress");
        for room in value["rooms"]
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("missing rooms"))?
            .values()
        {
            let uid = number(room, "anchor_id")?;
            ensure!(uid > 0, "invalid anchor ID");
            let name = room["anchor_name"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("missing name"))?;
            let likes = number(room, "likes_sent")?;
            let la = number(room, "like_attempts")?;
            let dan = number(room, "danmaku_sent")?;
            let da = number(room, "danmaku_attempts")?;
            tx.execute("INSERT INTO progress(day,uid,name,likes,likes_attempted,danmaku,danmaku_attempted,completed) VALUES(?,?,?,?,?,?,?,?)",params![day,uid,name,likes,la,dan,da,room["notification_queued"].as_bool().ok_or_else(||anyhow::anyhow!("missing completion"))?])?;
        }
        for watch in value["watches"]
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("missing watches"))?
            .values()
        {
            ensure!(number(watch, "anchor_id")? > 0, "invalid watch anchor ID");
            tx.execute("INSERT INTO progress(day,uid,name,watched,watch_attempted) VALUES(?,?,?,?,?) ON CONFLICT(day,uid) DO UPDATE SET watched=excluded.watched,watch_attempted=excluded.watch_attempted",params![day,number(watch,"anchor_id")?,watch["anchor_name"].as_str().ok_or_else(||anyhow::anyhow!("missing watch name"))?,number(watch,"watched_seconds")?,number(watch,"watch_seconds_attempted")?])?;
        }
        for m in value["outbox"]
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("missing outbox"))?
            .values()
        {
            let text = |k: &str| {
                m[k].as_str()
                    .ok_or_else(|| anyhow::anyhow!("invalid outbox text"))
            };
            tx.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?,?)",
                params![
                    text("sequence_id")?,
                    text("title")?,
                    text("message")?,
                    text("tags")?,
                    number(m, "attempts")?,
                    notification_due(m)?
                ],
            )?;
        }
        tx.execute(
            "UPDATE outbox SET id='health' WHERE id='bilibili-poll-health'",
            [],
        )?;
        tx.execute(
            "UPDATE meta SET day=?,failures=?,alerted=?",
            params![
                day,
                number(&value, "poll_failures")?,
                value["poll_alerted"]
                    .as_bool()
                    .ok_or_else(|| anyhow::anyhow!("missing health"))?
            ],
        )?;
        tx.commit()?;
        Ok(())
    }
}
fn number(value: &Value, key: &str) -> Result<i64> {
    let number = value[key]
        .as_i64()
        .ok_or_else(|| anyhow::anyhow!("invalid state number {key}"))?;
    ensure!(number >= 0, "negative state number {key}");
    Ok(number)
}

fn notification_due(value: &Value) -> Result<i64> {
    let due = value["next_attempt_at"]
        .as_f64()
        .ok_or_else(|| anyhow::anyhow!("invalid notification due"))?;
    ensure!(
        due.is_finite() && due >= 0.0 && due < i64::MAX as f64,
        "invalid notification due"
    );
    Ok(due as i64)
}
