use crate::{config::Config, now};
use anyhow::{Context, Result, ensure};
use md5::{Digest, Md5};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{collections::BTreeMap, time::Duration};
use tokio::time::Instant;
use wreq::{Client, Method};
use wreq_util::Emulation;

const APP_KEY: &str = "4409e2ce8ffd12b8";
// Public Android app signing material; this is not an account credential.
const APP_SECRET: &str = "59b43e04ad6965f34319062b478f83dd";
const APP_AGENT: &str = "Mozilla/5.0 BiliDroid/6.73.1 (bbcallen@gmail.com) os/android model/Mi 10 Pro mobi_app/android build/6731100 channel/xiaomi innerVer/6731110 osVer/12 network/2";
#[derive(Clone, Debug, Deserialize)]
pub struct Room {
    #[serde(skip)]
    pub uid: i64,
    pub room_id: i64,
    #[serde(rename = "uname")]
    pub name: String,
    pub title: String,
    pub live_status: u32,
    #[serde(rename = "area_v2_id")]
    pub area: i64,
    #[serde(rename = "area_v2_parent_id")]
    pub parent: i64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Failure {
    Rejected(i64),
    Transport,
    Invalid,
    Interrupted,
}
impl Failure {
    pub fn ambiguous(self) -> bool {
        !matches!(self, Self::Rejected(_) | Self::Interrupted)
    }
}
impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for Failure {}
#[derive(Deserialize)]
struct Envelope {
    code: i64,
    data: Option<Value>,
}
pub struct Api {
    client: Client,
    key: String,
    mid: i64,
    buvid: String,
    next: Instant,
    interval: Duration,
}
impl Api {
    pub fn new(key: String, config: &Config) -> Result<Self> {
        ensure!(
            !key.is_empty() && !key.chars().any(char::is_control),
            "invalid access key"
        );
        Ok(Self {
            client: Client::builder()
                .emulation(Emulation::Chrome131)
                .redirect(wreq::redirect::Policy::none())
                .timeout(Duration::from_secs(
                    (config.request_timeout_seconds).unsigned_abs(),
                ))
                .build()?,
            key,
            mid: 0,
            buvid: String::new(),
            next: Instant::now(),
            interval: Duration::from_secs((config.api_interval_seconds).unsigned_abs()),
        })
    }
    async fn request(
        &mut self,
        method: Method,
        url: &str,
        params: Vec<(String, String)>,
        signed: bool,
        like: bool,
    ) -> std::result::Result<Value, Failure> {
        let write = method == Method::POST;
        let request_day = crate::day(now()).map_err(|_| Failure::Invalid)?;
        let mut fields = params;
        if signed {
            fields = sign(fields, &self.key, now());
        }
        for attempt in 0..if write { 1 } else { 3 } {
            tokio::time::sleep_until(self.next).await;
            if write && crate::day(now()).map_err(|_| Failure::Invalid)? != request_day {
                return Err(Failure::Interrupted);
            }
            let mut request = self.client.request(method.clone(), url);
            if write {
                request = request.form(&fields).header("User-Agent", APP_AGENT);
                if like {
                    request = request
                        .header("x-bili-mid", self.mid.to_string())
                        .header("Cookie", format!("buvid3={}", self.buvid));
                }
            } else {
                request = request.query(&fields);
            }
            let response = request.send().await;
            self.next = Instant::now() + self.interval;
            match response {
                Err(e) => {
                    if !write && attempt < 2 && (e.is_timeout() || e.is_connect() || e.is_body()) {
                        tokio::time::sleep(Duration::from_secs(1 << attempt)).await;
                        continue;
                    }
                    return Err(Failure::Transport);
                }
                Ok(response) => {
                    let status = response.status().as_u16();
                    if !write && attempt < 2 && [500, 502, 503, 504].contains(&status) {
                        let _ = response.bytes().await;
                        tokio::time::sleep(Duration::from_secs(1 << attempt)).await;
                        continue;
                    }
                    // A non-2xx POST may have reached the application. Never replay it.
                    if !(200..300).contains(&status) {
                        return Err(Failure::Transport);
                    }
                    let body: Envelope = response.json().await.map_err(|_| Failure::Invalid)?;
                    if body.code != 0 {
                        return Err(Failure::Rejected(body.code));
                    }
                    return Ok(body.data.unwrap_or(Value::Null));
                }
            }
        }
        Err(Failure::Transport)
    }
    pub async fn login(&mut self) -> std::result::Result<(), Failure> {
        let value = self
            .request(
                Method::GET,
                "https://app.bilibili.com/x/v2/account/mine",
                vec![],
                true,
                false,
            )
            .await?;
        self.mid = value["mid"]
            .as_i64()
            .filter(|v| *v > 0)
            .ok_or(Failure::Invalid)?;
        let value = self
            .request(
                Method::GET,
                "https://api.bilibili.com/x/frontend/finger/spi",
                vec![],
                false,
                false,
            )
            .await?;
        self.buvid = value["b_3"]
            .as_str()
            .filter(|v| !v.is_empty())
            .ok_or(Failure::Invalid)?
            .into();
        Ok(())
    }
    pub async fn discover(&mut self, ids: &[i64]) -> std::result::Result<Vec<Room>, Failure> {
        let value = self
            .request(
                Method::GET,
                "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
                ids.iter()
                    .map(|uid| ("uids[]".into(), uid.to_string()))
                    .collect(),
                false,
                false,
            )
            .await?;
        decode_rooms(value, ids)
    }
    pub async fn like(&mut self, room: &Room, clicks: i64) -> std::result::Result<(), Failure> {
        self.request(
            Method::POST,
            "https://api.live.bilibili.com/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3",
            fields(&[
                ("click_time", clicks),
                ("room_id", room.room_id),
                ("anchor_id", room.uid),
                ("uid", self.mid),
            ]),
            true,
            true,
        )
        .await?;
        Ok(())
    }
    pub async fn danmaku(&mut self, room: &Room) -> std::result::Result<(), Failure> {
        let mut data = fields(&[("cid", room.room_id), ("rnd", now())]);
        data.extend([
            (
                "msg".into(),
                if rand::random::<bool>() {
                    "[花]"
                } else {
                    "[比心]"
                }
                .into(),
            ),
            ("color".into(), "16777215".into()),
            ("fontsize".into(), "25".into()),
        ]);
        self.request(
            Method::POST,
            "https://api.live.bilibili.com/xlive/app-room/v1/dM/sendmsg",
            data,
            true,
            false,
        )
        .await?;
        Ok(())
    }
    pub async fn heartbeat(
        &mut self,
        room: &Room,
        session: &Heartbeat,
        seconds: i64,
    ) -> std::result::Result<(), Failure> {
        let payload = heartbeat_payload(room, session, seconds, now());
        let data = payload
            .as_object()
            .ok_or(Failure::Invalid)?
            .iter()
            .map(|(k, v)| {
                v.as_str()
                    .map(|s| (k.clone(), s.to_owned()))
                    .ok_or(Failure::Invalid)
            })
            .collect::<std::result::Result<Vec<_>, _>>()?;
        self.request(
            Method::POST,
            "https://live-trace.bilibili.com/xlive/data-interface/v1/heartbeat/mobileHeartBeat",
            data,
            true,
            false,
        )
        .await?;
        Ok(())
    }
}
fn fields(values: &[(&str, i64)]) -> Vec<(String, String)> {
    values
        .iter()
        .map(|(k, v)| ((*k).into(), v.to_string()))
        .collect()
}
pub fn sign(fields: Vec<(String, String)>, key: &str, timestamp: i64) -> Vec<(String, String)> {
    let mut data = fields.into_iter().collect::<BTreeMap<_, _>>();
    data.extend([
        ("access_key".into(), key.into()),
        ("actionKey".into(), "appkey".into()),
        ("appkey".into(), APP_KEY.into()),
        ("ts".into(), timestamp.to_string()),
    ]);
    let query = url::form_urlencoded::Serializer::new(String::new())
        .extend_pairs(&data)
        .finish()
        .replace('*', "%2A")
        .replace("%7E", "~");
    let signature = hex::encode(Md5::digest(format!("{query}{APP_SECRET}")));
    data.insert("sign".into(), signature);
    data.into_iter().collect()
}
pub fn decode_rooms(value: Value, ids: &[i64]) -> std::result::Result<Vec<Room>, Failure> {
    let object = value.as_object().ok_or(Failure::Invalid)?;
    let mut rooms = Vec::new();
    for uid in ids {
        let raw = object.get(&uid.to_string()).ok_or(Failure::Invalid)?;
        // Validate even offline entries so malformed data cannot fabricate stream endings.
        let status = raw["live_status"].as_i64().ok_or(Failure::Invalid)?;
        if status != 1 {
            continue;
        }
        let mut room: Room = serde_json::from_value(raw.clone()).map_err(|_| Failure::Invalid)?;
        if room.room_id <= 0 || room.name.is_empty() {
            return Err(Failure::Invalid);
        }
        room.uid = *uid;
        rooms.push(room);
    }
    Ok(rooms)
}
pub struct Heartbeat {
    uuid: String,
    click: String,
    up: String,
}
impl Heartbeat {
    pub fn new(room: &Room) -> Self {
        Self {
            uuid: uuid::Uuid::new_v4().to_string(),
            click: uuid::Uuid::new_v4().to_string(),
            up: format!("l:one:live:record:{}:{}", room.room_id, now() - 88888),
        }
    }
}
fn random_token(length: usize) -> String {
    use rand::distr::{Alphanumeric, SampleString};
    Alphanumeric.sample_string(&mut rand::rng(), length)
}
pub fn heartbeat_payload(room: &Room, session: &Heartbeat, seconds: i64, timestamp: i64) -> Value {
    let seconds = seconds.min((timestamp + 8 * 3600).rem_euclid(86400));
    let mut data = json!({"platform":"android","uuid":session.uuid,"buvid":random_token(37).to_uppercase(),"seq_id":"1","room_id":room.room_id.to_string(),"parent_id":room.parent.to_string(),"area_id":room.area.to_string(),"timestamp":(timestamp-seconds).to_string(),"secret_key":"axoaadsffcazxksectbbb","watch_time":seconds.to_string(),"up_id":room.uid.to_string(),"up_level":"40","jump_from":"30000","gu_id":random_token(43).to_lowercase(),"play_type":"0","play_url":"","s_time":"0","data_behavior_id":"","data_source_id":"","up_session":session.up,"visit_id":random_token(32).to_lowercase(),"watch_status":"%7B%22pk_id%22%3A0%2C%22screen_status%22%3A1%7D","click_id":session.click,"session_id":"","player_type":"0","client_ts":timestamp.to_string()});
    let signature = client_sign(&data.to_string());
    data["client_sign"] = json!(signature);
    data
}
pub fn client_sign(input: &str) -> String {
    let s = hex::encode(sha2::Sha512::digest(input));
    let s = hex::encode(sha3::Sha3_512::digest(s));
    let s = hex::encode(sha2::Sha384::digest(s));
    let s = hex::encode(sha3::Sha3_384::digest(s));
    hex::encode(blake2::Blake2b512::digest(s))
}
pub async fn publish(
    client: &wreq::Client,
    config: &crate::config::Ntfy,
    message: &crate::store::Message,
) -> Result<()> {
    let mut request=client.post(config.server.as_str()).json(&json!({"topic":config.topic,"title":message.title,"message":message.body,"tags":[message.tag],"sequence_id":message.id}));
    if let Some(token) = &config.token {
        request = request.bearer_auth(token);
    }
    let response = request
        .send()
        .await
        .map_err(|_| anyhow::anyhow!("ntfy transport failed"))?;
    ensure!(response.status().is_success(), "ntfy rejected publish");
    let _ = response.bytes().await.context("ntfy response incomplete")?;
    Ok(())
}

#[cfg(test)]
#[path = "api_tests.rs"]
mod tests;
