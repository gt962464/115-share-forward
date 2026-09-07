# 115 分享转发

115 网盘分享链接自动转存、重命名、Telegram 卡片推送系统。

## 架构

```
┌─────────────────────────────────────────────────────┐
│                   Docker 容器群                       │
├──────────────┬───────────────┬───────────────────────┤
│  p115-card-  │  card-admin   │  card-admin-web       │
│  bot         │  (后端API)     │  (前端面板)            │
│  :8000 内部   │  :18810       │  :18811               │
└──────────────┴───────────────┴───────────────────────┘
```

| 服务 | 端口 | 说明 |
|------|------|------|
| p115-card-bot | 内部 | 核心卡片机器人，处理 115 分享链接的转存、重命名、TMDB 匹配、Telegram 卡片推送 |
| card-admin | 18810 | 管理后台 API（aiohttp），提供配置编辑、日志查看、容器控制 |
| card-admin-web | 18811 | 轻量化前端管理面板（SPA + nginx） |

## 快速部署

### 1. 克隆项目

```bash
git clone https://github.com/gt962464/115-share-forward.git
cd 115-share-forward
```

### 2. 配置环境变量（只需复制模板，具体配置在网页里填）

```bash
cp .env.example .env   # 只需复制模板，其余全部在管理面板里填
```

**不需要手动编辑 .env。** 启动后打开管理面板登录，在「卡片机器人」页填 `P115_COOKIE`、`TG_BOT_TOKEN`、`TG_CHANNEL_ID` 等并保存，卡片机器人会自动重启生效；每个连接项旁边都有「测试」按钮可验证连通性。

唯一需要在首次启动前决定的只有管理后台密码 `ADMIN_PASSWORD`（模板默认 `change-this-password`，用默认值即可登录，登录后再改）。

**必填凭据：**
- P115_COOKIE — 115 网盘登录 Cookie
- TG_BOT_TOKEN — Telegram 机器人 Token
- TG_CHANNEL_ID — 发布卡片的目标频道 ID（负数）

**可选（不填功能降级）：**
- TMDB_API_KEY — 不填则卡片无海报/评分
- LLM_API_KEY / LLM_MODEL — 不填则跳过 LLM 辅助识别（接口地址与模型名有代码内置默认值）
- TG_USER_ID / TG_SUBMITTER_IDS / TG_ALLOW_CHATS — 管理员与投稿白名单

### 3. 启动服务

```bash
docker compose -f docker-compose.server.yml up -d --build
```

### 4. 访问管理面板

面板端口默认只绑定到 `127.0.0.1`（本机），不直接暴露公网。两种访问方式：

**A. SSH 隧道（本地/临时）**

```bash
ssh -L 18810:127.0.0.1:18810 -L 18811:127.0.0.1:18811 user@服务器IP
```

浏览器打开 `http://127.0.0.1:18811`，后端地址会自动填成 `http://127.0.0.1:18810`，直接登录即可。

**B. Cloudflare 隧道（域名访问）**

把域名指到前端容器即可；前端已内置后端反向代理（同源），无需再单独暴露 18810：

```text
panel.example.com  →  http://127.0.0.1:18811
```

浏览器打开 `https://panel.example.com`，直接登录即可。

## 目录结构

```
├── card-bot/              # 核心服务
│   ├── cardbot.py         # 卡片机器人主程序
│   ├── admin/             # 管理后台 API（aiohttp）
│   ├── tg-monitor/        # Telegram 监听转发（可选）
│   └── cd2-mount-mover/   # NAS 挂载迁移工具（可选，默认关闭）
├── p115-src/              # P115 API 源码
├── frontend/              # 轻量化前端面板（SPA + nginx）
├── patches/               # 补丁文件
├── docker-compose.server.yml  # 服务器部署 Compose
├── docker-compose.yml     # NAS/本地部署 Compose
├── .env.example           # 环境变量模板
└── README.md
```

## 功能特性

- **自动转存**：监控 115 分享链接，自动转存到指定目录
- **智能重命名**：通过 LLM + TMDB API 自动解析文件名，匹配影视信息
- **卡片推送**：生成精美 Telegram 卡片消息推送到频道
- **管理后台**：Web 界面管理配置、查看日志、控制容器
- **前端面板**：轻量化 SPA，支持服务状态、配置编辑、日志查看、LLM 提示词管理

## 服务说明（可选）

### Telegram 监听转发（tg-monitor）

用你的真实 Telegram 用户账号，自动监听一个「发 115 链接的来源机器人/频道」，看到链接就转发给卡片机器人处理（省得手动转发）。服务器部署已包含该服务，但需要额外配置：

1. 到 https://my.telegram.org 创建应用，拿到 `api_id` / `api_hash`。
2. 在管理面板「Telegram 监听」页（或 `.env`）填写：
   - `TG_API_ID` / `TG_API_HASH` — 上面申请的
   - `TG_PHONE` — 你的 Telegram 手机号（`+区号` 格式）
   - `TG_SOURCE_CHAT` — 来源机器人/频道（`@用户名` 或数字 ID）
   - `TG_FORWARD_TO` — 卡片机器人接收聊天 ID（给卡片机器人发 `/id` 查看，通常就是你的用户 ID）
   - `TG_BOT_TOKEN` — 复用卡片机器人自己的 Bot Token
3. 首次登录：SSH 隧道到 18800（`ssh -L 18800:127.0.0.1:18800 user@服务器IP`），浏览器打开 `http://127.0.0.1:18800`，按网页提示完成「手机号 → 验证码 → 两步验证」，成功后自动生成 session，以后无需再登录。

> 未填全必填项前，tg-monitor 容器会启动报错循环重启，属正常现象，把配置填好即可。

### NAS 专用服务（cd2-mount-mover）

CloudDrive2 挂载迁移工具，仅适用于群晖 NAS 环境。默认关闭，服务器部署无需启用。

启用时在 `card-admin` 环境变量中设置：
- `MOVER_ENABLED=1`
- `MOVER_SOURCE_DIR` / `MOVER_TARGET_DIR`（挂载源/目标路径，默认 `/vol2/...`）

## 安全说明

- 面板端口（18810 / 18811 / 8000 / 18800）默认只绑定 `127.0.0.1`，仅本机可访问；请通过 SSH 隧道或内网访问，不要做公网端口映射
- `.env` 文件包含敏感凭据，已被 `.gitignore` 排除
- 管理后台可直接编辑 P115 Cookie、Bot Token、回收站密码、TG API 凭据等敏感项；敏感值只显示掩码，不提供明文查看
- 海外服务器通常无需代理，`TG_PROXY` / `APP_HTTP_PROXY` 留空即可；仅在国内或 NAS 环境访问 Telegram/TMDB 时才需配置
- 修改配置前建议先备份

## 技术栈

- **后端**：Python 3.13 + aiohttp
- **前端**：纯 HTML/CSS/JS（零依赖 SPA）+ nginx
- **容器化**：Docker + Docker Compose
- **外部依赖**：115 网盘 API、Telegram Bot API、TMDB API、LLM API
