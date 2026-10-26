pub mod api;
pub mod config;
pub mod runner;
pub mod store;

pub fn day(timestamp: i64) -> anyhow::Result<chrono::NaiveDate> {
    use anyhow::Context;
    Ok(chrono::DateTime::from_timestamp(timestamp, 0)
        .context("timestamp out of range")?
        .with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).context("Shanghai offset")?)
        .date_naive())
}
pub fn now() -> i64 {
    chrono::Utc::now().timestamp()
}
