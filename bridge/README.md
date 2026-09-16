# 宿主机 CLI worker

让服务复用你机器上**已登录的 AI CLI**（`claude`、`codex` 等），省掉 API Key。

## 它怎么工作

worker 主动来取活，不是被动等着被调用：

```
worker ──长轮询──▶ 后台 /bridge/jobs/next   （要活，没活就挂 30 秒）
worker ◀──任务──── 后台                      （body 就是提示词）
worker ──执行──▶ claude -p ...
worker ──回传──▶ 后台 /bridge/jobs/<id>/result
```

这么设计的原因：

- **零运行时依赖**。只用 `curl`（macOS/Linux 自带）或 PowerShell（Windows 自带），不需要 Python、Node、jq
- **不开端口**。worker 只发出站请求，没有监听、没有暴露面
- **不挑位置**。worker 不必是 Docker 宿主机，任何一台能访问后台地址、装了 CLI 的机器都行

## 安装

后台「系统设置 → 对话模型」选**本机 CLI**，生成一个桥接令牌并保存，
页面会按你的系统给出对应命令。也可以直接跑：

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/PMJ520/ai-baoxiao-reimburse-bot/main/bridge/setup.sh \
  | bash -s -- --url http://localhost:8080 --token 你的令牌
```

```powershell
# Windows
irm https://raw.githubusercontent.com/PMJ520/ai-baoxiao-reimburse-bot/main/bridge/setup.ps1 -OutFile setup.ps1
.\setup.ps1 -Url http://localhost:8080 -Token 你的令牌
```

`setup` 会检查依赖、认出你装的是哪个 CLI、预检后台连通性，再装成开机自启：

| 系统 | 自启方式 | 停止 |
|---|---|---|
| macOS | launchd `com.expensehub.worker` | `launchctl unload ~/Library/LaunchAgents/com.expensehub.worker.plist` |
| Linux | systemd `--user` 单元 | `systemctl --user disable --now expense-hub-worker` |
| Windows | 计划任务 `ExpenseHubWorker` | `Unregister-ScheduledTask -TaskName ExpenseHubWorker -Confirm:$false` |

加 `--no-autostart` / `-NoAutostart` 可只装不自启。

## 其它环境

`worker.sh` 是 POSIX sh，busybox 也能跑（群晖、OpenWrt、Alpine 等），
手动起即可：

```sh
sh worker.sh --url http://后台地址 --token 你的令牌 --cli claude
```

想用别的语言自己写一个？协议如下，**全程不涉及 JSON**。

## 协议

鉴权统一用 `Authorization: Bearer <桥接令牌>`，令牌就是后台里填的那个。

### `GET /bridge/jobs/next?wait=30&os=&arch=&cli=&ver=`

长轮询要活。`wait` 是最多挂多少秒（上限 60）。查询参数里的环境信息会显示在后台。

- `200` —— **body 就是提示词全文**（UTF-8 纯文本）。任务号在响应头 `X-Job-Id`，
  另有 `X-Job-Model`、`X-Job-Max-Tokens` 供参考
- `204` —— 这段时间没活，接着轮询
- `401` —— 令牌不对；`503` —— 后台还没配置桥接令牌

### `POST /bridge/jobs/<id>/result`

body 放 CLI 的原样输出（UTF-8 纯文本）。后台自己负责从中取出需要的部分。

### `POST /bridge/jobs/<id>/result?error=1`

body 放错误信息。后台会把它显示给使用者，而不是傻等到超时。

> 把结果 POST 回来时如果响应 `{"accepted": false}`，说明那条任务已被调用方
> 撤单（通常是等超时了），丢弃即可。

## 支持的 CLI

| CLI | 调用方式 |
|---|---|
| `claude` | `claude -p <提示词>` |
| `codex` | `codex exec <提示词>` |
| 其它 | `<命令> <提示词>`，或改 `worker.sh` 的 `run_cli()` |

## 限制

- **慢**：每次要起一个 CLI 进程，单次数秒，比直连 API 慢
- **串行**：一个 worker 一次只跑一条，批量任务会排队（要快就多起几个 worker，
  或者干脆用 API）
- **依赖登录态**：CLI 登录过期后调用会失败，后台的状态和报错里能看出来

## 安全

- 桥接令牌是唯一的闸门，别用 `test`、`123` 这种；后台的「生成一个」会给足够随机的值
- 令牌不写在命令行里长期存在：`setup` 把它存进 launchd/systemd 的环境变量或
  Windows 用户级环境变量，不进任务参数
- worker 会把后台发来的提示词原样交给 CLI 执行。**只把 worker 指向你自己的后台**
