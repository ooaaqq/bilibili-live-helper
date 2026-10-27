# Bilibili Live Helper

单账户直播任务应用，原生 Rust 程序，不监听网络端口。开发工具由 infra 根 `nix develop` 提供。

## 配置与命令

复制 `config.example.yaml` 为私有 `config.yaml`。`include_uids` 是主播清单，
`watch_uids` 是其中按优先级排列的观看清单；所有间隔和任务数量必须明确填写。
未知或重复字段、重复 UID、无效类型和超范围参数会阻止启动。access key 保存在单独文件。

```sh
cargo run -- check
cargo run -- run
./scripts/check.sh
```

程序提供 `run`、`check` 和 `import-state <JSON 文件>` 子命令。
`import-state` 仅接受版本 4 的任务导出，并拒绝覆盖已有进度；导入在单个事务中完成。

| 参数 | 环境变量 | 默认值 |
|---|---|---|
| `--config` | `BILIBILI_LIVE_HELPER_CONFIG` | `config.yaml` |
| `--access-key-file` | `BILIBILI_LIVE_HELPER_ACCESS_KEY_FILE` | `access_key` |
| `--database` | `BILIBILI_LIVE_HELPER_DATABASE` | `data/state.sqlite` |

ntfy 配置包含 JSON 发布接口的 `server`、`topic` 和可选 `token`。不打印凭据或原始接口响应。

## 调度与持久状态

一个调度循环拥有 SQLite 状态和账户请求队列，批量获取直播状态，按到期时间执行点赞、弹幕和观看。
不同直播间各自维护互动进度；弹幕同时遵守全局最小间隔。
观看仅占一个位置，按 `watch_uids` 顺序分配，完成或下播后交给下一个目标。
心跳使用稳定会话身份、接口提供的实际分区和已等待的时间，不把网络延迟算成观看时长。
上海时间午夜切换日期并生成观看汇总；轮询失败时暂停直播任务。

副作用请求前先持久记录预约，确认成功后增加已确认进度。明确 API 拒绝释放预约；
连接异常、超时和无法解析的响应保留为结果不确定，不重复该次请求。
崩溃或重启后，未确认预约仍然占用任务额度，防止重复点赞、发送弹幕或上报观看。
GET 只对明确的暂时性传输故障和 HTTP 500/502/503/504 最多尝试三次；429 和 API 拒绝不即时重试。

wreq 与 wreq-util 提供 Chrome131 TLS/HTTP2 配置和长连接池，应用不实现浏览器指纹协议。
点赞、弹幕和 mobileHeartBeat 保留 Android 应用协议签名。

通知与对应状态一起提交到 SQLite。只确认 HTTP 2xx，失败按 30 秒到 15 分钟退避。
认证失败立即提醒，其他轮询故障连续三轮后提醒。健康通知只保留当前状态，
仅已确认送达的故障才生成恢复通知。ntfy 发布不能保证严格只投递一次。

## 构建与生产

Cargo.lock 固定依赖。`scripts/check.sh` 在本仓库运行格式、严格 Clippy 和行为测试，独立于 fleet。
源码禁止 unsafe、unwrap、expect、panic 和未完成实现；wreq 同时负责账户与通知 HTTP 请求。

GitHub Actions 检查 main/master 和 PR；推送与 Cargo.toml 版本一致的 `vX.Y.Z` tag 后，
检查通过才构建并发布 `bilibili-live-helper-X.Y.Z-x86_64-linux.tar.gz` 和 SHA-256。
发布包面向 Ubuntu 24.04 glibc，使用 SQLite 动态库；NixOS 消费方负责适配动态加载器、SQLite 和 CA 证书。

```sh
./scripts/check.sh
# 更新 Cargo.toml/Cargo.lock 中的版本并提交后
git tag v3.0.0
git push origin v3.0.0
```

发布 tag 与资产不覆盖；修正发布时递增版本。生产安装版本、systemd 和凭据由 fleet 管理。
Nix 提供二进制与必需启动配置；数据库、凭据和通知队列由应用管理。
