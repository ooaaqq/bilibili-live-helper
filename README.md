# Bilibili Live Helper

单账户直播任务应用，不监听网络端口。使用 infra 工作区的开发环境。

## 配置与运行

从 `config.example.yaml` 创建本机 `config.yaml`：`include_uids`
是主播清单，`watch_uids` 是按优先级排列的观看清单，必须是前者的子集。
参数含义和当前数值见配置文件。重复字段、重复 UID、未知参数和空主播清单会被拒绝。
access key 从单独文件读取，不写入配置。

```sh
cp config.example.yaml config.yaml
uv sync --locked
uv run python -m bilibili_live_helper
infra check bilibili-live-helper
```

本地状态默认为 `data/state.json`。可通过环境变量指定路径：

| 变量                                   | 用途            |
| -------------------------------------- | --------------- |
| `BILIBILI_LIVE_HELPER_CONFIG`          | 配置文件        |
| `BILIBILI_LIVE_HELPER_ACCESS_KEY_FILE` | access key 文件 |
| `BILIBILI_LIVE_HELPER_STATE`           | 状态文件        |

ntfy 使用 JSON 发布接口，在 `config.yaml` 的 `ntfy` 中分别填写 server 和
topic，可设置 token。

## 任务与状态

每次轮询通过 `Room/get_status_info_by_uids`
批量获取直播状态，随后独立执行点赞、弹幕和观看任务。 观看一次只运行一个任务，按
`watch_uids` 顺序分配空闲位置，下播后停止。
观看目标按秒计时；心跳间隔只影响请求次数。不同直播间的点赞序列独立推进。

账户请求串行执行，间隔由配置控制。GET 可重试；明确的 API 拒绝可在后续轮询重试。
可能已送达的 POST 超时会记为不确定结果，不重复发送。响应 `code: 0` 视为成功。

状态在副作用请求前和确认响应后原子保存。重启继续当天未完成任务，上海时间午夜停止旧任务并进入新一天。
无效状态移至 `state.json.corrupt-TIMESTAMP`。ntfy
消息先进入持久队列，失败后退避重试。
通知包含开播标题、点赞及弹幕完成进度和每日观看汇总，区分已确认与不确定结果。

直播发现使用公开批量接口；点赞和弹幕保留 app access key 签名，观看使用认证的
`mobileHeartBeat`。 公开弹幕接口参考 `bilibili-api-python`，它不是运行依赖。

## 生产与检查

fleet 的 flake 固定本仓库 revision，构建原生 NixOS 服务。生产配置位于 fleet
的主机目录，access key 由 SOPS 管理。 服务使用动态用户和持久
`StateDirectory`，通过上述三个环境变量接收配置、凭据和状态路径。
更新源码后，需在 fleet 更新固定 revision 才会部署。

包提供 `bilibili-live-helper`、`bilibili-live-helper-check-config` 和
`bilibili-live-helper-healthcheck`。 `scripts/check.sh` 执行原生 flake
检查，测试环境由同一 Python 锁构建；默认分支与 PR 执行同一检查，文档跳过。
Python 锁维护应用和测试依赖，Nix 提供工具；生产包由 fleet 构建。
