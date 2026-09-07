# 115 分享转发

115 网盘分享链接自动转存、重命名、Telegram 卡片推送系统。

## 架构

`
┌─────────────────────────────────────────────────────┐
│                   Docker 容器群                       │
├──────────────┬───────────────┬───────────────────────┤
│  p115-card-  │  card-admin   │  card-admin-web       │
│  bot         │  (后端API)     │  (前端面板)            │
│  :8000 内部   │  :18810       │  :18811               │
└──────────────┴───────────────┴───────────────────────┘
`

| 服务 | 端口 | 说明 |
|------|------|------|
| p115-card-bot | 内部 | 核心卡片机器人，处理 115 分享链接的转存、重命名、TMDB 匹配、Telegram 卡片推送 |
| card-admin | 18810 | 管理后台 API（aiohttp），提供配置编辑、日志查看、容器控制 |
| card-admin-web | 18811 | 轻量化前端管理面板（SPA + nginx） |

## 快速部署

### 1. 克隆项目

`ash
git clone https://github.com/gt962464/115-share-forward.git
cd 115-share-forward
`

### 2. 配置环境变量

`ash
cp .env.example .env
vi .env   # 填写必填凭据
`

**必填凭据：**
- P115_COOKIE — 115 网盘登录 Cookie
- TG_BOT_TOKEN — Telegram 机器人 Token
- TG_CHANNEL_ID — 发布卡片的目标频道 ID（负数）

**可选（不填功能降级）：**
- TMDB_API_KEY — 不填则卡片无海报/评分
- LLM_API_KEY / LLM_MODEL — 不填则跳过 LLM 辅助识别
- TG_USER_ID — 管理员白名单

### 3. 启动服务

`ash
docker compose -f docker-compose.server.yml up -d --build
`

### 4. 访问管理面板

- **前端面板**：http://服务器IP:18811
- **后端 API**：http://服务器IP:18810

打开前端面板后，在顶部输入后端地址即可连接。

## 目录结构

`
├── card-bot/              # 核心服务
│   ├── cardbot.py         # 卡片机器人主程序
│   ├── admin/             # 管理后台 API（aiohttp）
│   └── tg-monitor/        # Telegram 监听转发（可选）
├── p115-src/              # P115 API 源码
├── frontend/              # 轻量化前端面板（SPA + nginx）
├── patches/               # 补丁文件
├── docker-compose.server.yml  # 服务器部署 Compose
├── .env.example           # 环境变量模板
└── README.md
`

## 功能特性

- **自动转存**：监控 115 分享链接，自动转存到指定目录
- **智能重命名**：通过 LLM + TMDB API 自动解析文件名，匹配影视信息
- **卡片推送**：生成精美 Telegram 卡片消息推送到频道
- **管理后台**：Web 界面管理配置、查看日志、控制容器
- **前端面板**：轻量化 SPA，支持服务状态、配置编辑、日志查看、LLM 提示词管理

## 服务说明（可选）

### Telegram 监听转发（tg-monitor）

需要真实 Telegram 用户账号登录，另需配置：
- TG_API_ID、TG_API_HASH、TG_PHONE

### NAS 专用服务（cd2-mount-mover）

CloudDrive2 挂载迁移工具，仅适用于群晖 NAS 环境，服务器部署已跳过。

## 安全说明

- 仅建议局域网访问，不要做公网端口映射
- .env 文件包含敏感凭据，已被 .gitignore 排除
- 管理后台不显示 P115 Cookie、Telegram Token 等敏感值
- 修改配置前建议先备份

## 技术栈

- **后端**：Python 3.13 + aiohttp
- **前端**：纯 HTML/CSS/JS（零依赖 SPA）+ nginx
- **容器化**：Docker + Docker Compose
- **外部依赖**：115 网盘 API、Telegram Bot API、TMDB API、LLM API