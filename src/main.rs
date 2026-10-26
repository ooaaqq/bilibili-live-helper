use anyhow::{Context, Result};
use bilibili_live_helper::{api::Api, config::Config, day, now, runner::Runner, store::Store};
use clap::{Parser, Subcommand};
use std::{
    fs::OpenOptions,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::PathBuf,
};
#[derive(Parser)]
#[command(version)]
struct Args {
    #[arg(
        long,
        env = "BILIBILI_LIVE_HELPER_CONFIG",
        default_value = "config.yaml"
    )]
    config: PathBuf,
    #[arg(
        long,
        env = "BILIBILI_LIVE_HELPER_ACCESS_KEY_FILE",
        default_value = "access_key"
    )]
    access_key_file: PathBuf,
    #[arg(
        long,
        env = "BILIBILI_LIVE_HELPER_DATABASE",
        default_value = "data/state.sqlite"
    )]
    database: PathBuf,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Run,
    Check,
    ImportState { file: PathBuf },
}
#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt().json().init();
    let args = Args::parse();
    let config = Config::load(&args.config)?;
    if matches!(args.command, Command::Check) {
        println!("configuration ok");
        return Ok(());
    }
    let parent = args.database.parent().context("database parent missing")?;
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(parent)?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .mode(0o600)
        .open(args.database.with_extension("lock"))?;
    lock.try_lock().context("helper is already running")?;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .mode(0o600)
        .open(&args.database)?;
    drop(file);
    let mut store = Store::open(&args.database, &day(now())?.to_string())?;
    match args.command {
        Command::ImportState { file } => store.import(&file)?,
        Command::Run => {
            let key = std::fs::read_to_string(args.access_key_file)
                .context("cannot read access key file")?;
            Runner::new(Api::new(key.trim().into(), &config)?, store, config)?
                .run()
                .await?;
        }
        Command::Check => {}
    }
    Ok(())
}
