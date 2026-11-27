use crate::{api::Room, config::Lef, day, store::Store};
use anyhow::{Context, Result, ensure};
use flate2::{Compression, write::GzEncoder};
use serde_json::{Value, json};
use std::{io::Write, path::PathBuf};

pub(crate) struct Markers {
    config: Lef,
    token: String,
}

impl Markers {
    pub(crate) fn new(config: &Lef) -> Result<Self> {
        let path = std::env::var_os("BILIBILI_LIVE_HELPER_LEF_TOKEN_FILE")
            .context("LEF token file is required")?;
        let credential = std::fs::read_to_string(PathBuf::from(path))?;
        let token = credential
            .trim()
            .strip_prefix("ADMIN_TOKEN=")
            .unwrap_or(credential.trim());
        ensure!(
            token.len() >= 12 && token.bytes().all(|byte| byte.is_ascii_graphic()),
            "invalid LEF token"
        );
        Ok(Self {
            config: config.clone(),
            token: token.to_owned(),
        })
    }

    pub(crate) async fn observe(
        &self,
        client: &wreq::Client,
        store: &mut Store,
        rooms: &[Room],
        non_live_rooms: &[Room],
    ) -> Result<()> {
        let Some(room) = rooms.iter().find(|room| {
            room.room_id == self.config.room_id
                && room.live_time > 0
                && room.live_time <= crate::now()
        }) else {
            if non_live_rooms
                .iter()
                .any(|room| room.room_id == self.config.room_id)
            {
                store.observe_lef_session(self.config.room_id, 0)?;
            }
            return Ok(());
        };
        if store
            .observe_lef_session(room.room_id, room.live_time)?
            .is_some()
            && let Err(error) = self.upload(client, room).await
        {
            tracing::warn!(room_id=room.room_id, started=room.live_time, %error, "LEF markers not confirmed");
        }
        Ok(())
    }

    async fn upload(&self, client: &wreq::Client, room: &Room) -> Result<()> {
        let response = client
            .post(self.config.server.join("upload")?.as_str())
            .bearer_auth(&self.token)
            .header("Content-Type", "application/lca")
            .body(archive(
                room,
                self.config.server.join("marker-avatar.png")?.as_str(),
            )?)
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("LEF upload transport failed"))?;
        ensure!(
            response.status().is_success(),
            "LEF upload rejected: {}",
            response.status()
        );
        let result: Value = response
            .json()
            .await
            .context("invalid LEF upload response")?;
        ensure!(result["success"] == true, "LEF upload not accepted");
        Ok(())
    }
}

fn archive(room: &Room, avatar: &str) -> Result<Vec<u8>> {
    let date = day(room.live_time)?.format("%-m月%-d日");
    let prefix = "『本条为弹幕机开播时自动标记，请无视』";
    let mut event = json!({
        "origin":room.room_id,"originIdx":0,"timestamp":room.live_time,
        "timestampNormalized":room.live_time*1000,"uid":0,"username":"","avatar":avatar,
        "price":100,"priceNormalized":100,"duration":0,"read":false,"guardType":0,
        "medal":{"uid":room.uid,"name":"GOGO队","room":0,"level":99,"guardType":0,"lightened":1}
    });
    let mut sc = event.clone();
    let fields = sc.as_object_mut().context("SC object")?;
    fields.extend(serde_json::from_value::<serde_json::Map<String, Value>>(
        json!({
            "id":format!("lef-sc-marker-{}-{}",room.room_id,room.live_time),"type":"superchat",
            "message":format!("{prefix}\n\n以下为今日直播前所发送 SC（{date}）"),
            "scId":-room.live_time,"scAmount":1,"scName":"醒目留言","rate":1000,
            "token":"","deleted":false,"avatarFrame":"","guardBackground":"",
            "isAudited":0,"transMark":0,"messageTrans":"","messageColor":"#A3F6FF"
        }),
    )?);
    let fields = event.as_object_mut().context("gift object")?;
    fields.extend(serde_json::from_value::<serde_json::Map<String, Value>>(json!({
        "id":format!("lef-gift-marker-{}-{}",room.room_id,room.live_time),"type":"gift",
        "message":format!("以下为今日直播前所发送礼物（{date}）"),"price":100000,
        "giftName":"场次分隔","giftId":0,"giftAmount":1,"giftIcon":"","giftRemain":0,
        "giftAction":"投喂","giftType":0,"coinType":"gold","bizSource":"Live",
        "receiver":{"uid":room.uid,"uname":room.name,"master":null},"blindGift":null,"wealthMedalLevel":0
    }))?);
    let mut gzip = GzEncoder::new(Vec::new(), Compression::default());
    gzip.write_all(&serde_json::to_vec(&json!([
        {"name":"superchats","data":[sc]}, {"name":"gifts","data":[event]}
    ]))?)?;
    Ok(gzip.finish()?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markers_use_the_approved_format_and_current_shanghai_date_once() -> Result<()> {
        let mut room: Room = serde_json::from_value(json!({
            "room_id": 25512443, "uname": "主播", "title": "", "live_status": 1,
            "live_time": 1795777200, "area_v2_id": 1, "area_v2_parent_id": 1
        }))?;
        room.uid = 1669777785;
        let previous = 1795622400;
        let state = tempfile::tempdir()?;
        let path = state.path().join("state.sqlite");
        let mut store = Store::open(&path, "2026-11-27")?;
        assert_eq!(store.observe_lef_session(room.room_id, previous)?, None);
        drop(store);
        let mut store = Store::open(&path, "2026-11-27")?;
        assert_eq!(
            store.observe_lef_session(room.room_id, room.live_time)?,
            Some(previous)
        );
        assert_eq!(
            store.observe_lef_session(room.room_id, room.live_time)?,
            None
        );
        assert_eq!(store.observe_lef_session(room.room_id, previous)?, None);

        let avatar = "https://lef.elfofera.com/marker-avatar.png";
        let compressed = archive(&room, avatar)?;
        let data: Value =
            serde_json::from_reader(flate2::read::GzDecoder::new(compressed.as_slice()))?;
        for (index, kind) in ["superchat", "gift"].iter().enumerate() {
            let event = &data[index]["data"][0];
            assert_eq!(event["type"], *kind);
            assert_eq!(event["timestamp"], room.live_time);
            assert_eq!(event["priceNormalized"], 100);
            assert_eq!(event["price"], if index == 0 { 100 } else { 100000 });
            assert_eq!(event["duration"], 0);
            assert_eq!(event["avatar"], avatar);
            assert_eq!(event["username"], "");
            assert_eq!(event["uid"], 0);
            assert_eq!(
                event["medal"],
                json!({"uid":1669777785,"name":"GOGO队","room":0,"level":99,"guardType":0,"lightened":1})
            );
            let expected = if index == 0 {
                "『本条为弹幕机开播时自动标记，请无视』\n\n以下为今日直播前所发送 SC（11月27日）"
            } else {
                "以下为今日直播前所发送礼物（11月27日）"
            };
            assert_eq!(event["message"], expected);
        }
        Ok(())
    }
}
