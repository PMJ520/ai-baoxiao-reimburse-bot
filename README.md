# 费用报销中枢

把零散发来的**发票和支付截图**，整理成财务可直接受理的台账。
通过飞书等 IM 收材料、对话确认、产出表格；另有数据后台可查看与编辑。

- **零散存储** — 随时发，收到即识别入库，回执当场告诉你认成了什么
- **按需整理** — 一句「整理 7-8 月报销」，自动组批、配对发票、算账
- **查漏补缺** — 缺发票、缺截图、缺必填信息，主动追问
- **专用模版** — 通用数据处理 + 专用模版转化，两层分离，可支持多份模版

## 一键安装

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/PMJ520/ai-baoxiao-reimburse-bot/main/install.sh | bash
```

**Windows（PowerShell）**

```powershell
irm https://raw.githubusercontent.com/PMJ520/ai-baoxiao-reimburse-bot/main/install.ps1 | iex
```

带参数安装：

```bash
# 内网 IP 访问（纯 HTTP，不签证书）
curl -fsSL ... | bash -s -- --host 192.168.1.100 --port 8000

# 公网域名（Caddy 自动签 HTTPS）
curl -fsSL ... | bash -s -- --host hub.example.com \
     --llm claude --llm-key sk-xxx \
     --feishu-app-id cli_xxx --feishu-app-secret xxx
```

安装脚本会：检测并按需安装 Docker → 判断访问方式 → 生成配置 →
拉取镜像 → 启动 → **输出后台地址与账号密码**。

可重复运行，已完成的步骤会跳过，**重跑不会冲掉已有密码**。

## 访问方式与证书

传入的地址是 IP 还是域名，脚本会自动判断：

| 传入 | 证书 | 说明 |
|---|---|---|
| `192.168.1.100`、`localhost` | 纯 HTTP | 内网自用，不折腾证书 |
| 公网域名且解析指向本机 | Let's Encrypt 自动签 | Caddy 全自动续期 |
| 域名在公网、服务在内网 | `--tls dns` | DNS-01 挑战，仍拿真证书 |

域名模式会**预先校验解析是否指向本机**——不做这步，证书签发失败时
只会看到一堆 ACME 报错，根本看不出是 DNS 没配。

## 日常管理

```bash
./expense-hub start|stop|restart|status|logs|info|update|backup|uninstall
```

`start` 与 `restart` 会在健康检查通过后打印后台地址与账密。
凭据只打印在终端、不写入容器日志，避免被日志收集系统采走。
不想每次打印可设 `PRINT_CREDENTIALS_ON_START=false`，改用 `./expense-hub info` 查看。

`backup` 打包数据目录——SQLite 是单文件，连同原件一起压缩即可完整还原。

## 文字识别

三级自动降级，无需配置：

| 优先级 | 引擎 | 速度 | 适用 |
|---|---|---|---|
| 1 | macOS Vision | 约 0.4 秒/张 | 仅 macOS（本地开发） |
| 2 | **RapidOCR** | 约 1.1 秒/张 | **容器内主力**，跨平台 |
| 3 | 交给 AI 读图 | 较慢 | 兜底，任何环境可用 |

容器内没有 macOS Vision，RapidOCR 是主力，**已实测 20/20 金额与时间全对**。

识别结果始终与发票金额交叉验证，读错会在核对环节暴露。

## 架构

```
IM 适配层     飞书长连接（出站连接，内网无需公网入口）
对话编排      LLM：意图识别、明细提议、逐项确认
核心管线      通用数据处理（OCR/解析/匹配/核对） + 专用模版转化
存储          SQLite（元数据） + 文件系统（原件）
数据后台      文件 / 条目 / 批次 / 模版 管理
```

**分界线**：模型只判断「这笔钱花在什么事上」；金额、时间、凭证关系
全部由确定性代码计算，不经模型的手。模型也不许编造人数、客户姓名等
它无从得知的信息，只能标成待补占位。

对话模型可配置：`claude` 或 `openai`（通义、DeepSeek、智谱等 OpenAI
兼容端点通用）。

## 汇总开票

一张发票常覆盖多笔支付（如按城市按周期开票）。处理方式：

- **有辅助单据时**（如行程单）用其明细精确分摊，自动完成
- **没有时不猜**，列出候选交用户指定

刻意不做子集和自动匹配——实测它会凑出金额恰好、构成却完全错误的组合，
对财务数据而言这种「看似正确的错误」比匹配不上危险得多。

## 开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=./data API_TOKEN=dev .venv/bin/uvicorn app.main:app --reload
```

本地构建镜像：`./install.sh --build --host localhost`

## 许可

MIT
