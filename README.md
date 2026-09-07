# 115 分享转发

115 网盘分享链接自动转存、重命名、长期分享和 Telegram 卡片推送系统。

## 架构概览

`
┌──────────────────────────────────────────────────────────┐
│                    Docker 容器群                           │
├──────────────┬──────────────┬──────────────┬─────────────┤
│  p115-card-  │ tg-user-     │ cd2-mount-   │ card-admin  │
│  bot         │ monitor      │ mover        │ (后端API)    │
│  :8000       │ :18800       │              │ :18810      │
├──────────────┴──────────────┴──────────────┴─────────────┤
│                   card-admin-web                          │
│                   (前端面板) :18811                        │
└──────────────────────────────────────────────────────────┘
`

## 服务说明

| 服务 | 端口 | 说明 |
|------|------|------|
| p115-card-bot | 8000 | 核心卡片机器人，处理 115 分享链接的转存、重命名、TMDB 匹配、Telegram 卡片推送 |
| 	g-user-monitor | 18800 | Telegram 监听转发，自动监控频道消息并转发 |
| cd2-mount-mover | — | CloudDrive2 挂载迁移工具，自动转存文件到目标目录 |
| card-admin | 18810 | 管理后台 API（aiohttp），提供配置编辑、日志查看、容器控制等接口 |
| card-admin-web | 18811 | 轻量化前端管理面板（SPA），调用 card-admin API |

## 快速部署

### 1. 克隆项目

`ash
git clone https://github.com/gt962464/115-share-forward.git
cd 115-share-forward
`

### 2. 配置环境变量

`ash
# 卡片机器人配置
cp card-bot/.env.example card-bot/.env
vi card-bot/.env

# Telegram 监听配置
cp card-bot/tg-monitor/.env.example card-bot/tg-monitor/.env
vi card-bot/tg-monitor/.env

# 管理后台配置
cp card-bot/admin/.env.example card-bot/admin/.env
vi card-bot/admin/.env
`

### 3. 启动全部服务

`ash
docker compose up -d --build
`

### 4. 访问管理面板

- **前端面板**：http://NAS_IP:18811
- **后端 API**：http://NAS_IP:18810

打开前端面板后，在顶部输入后端地址即可连接。

## 目录结构

`
├── card-bot/                  # 核心服务
│   ├── cardbot.py             # 卡片机器人主程序
│   ├── admin/                 # 管理后台 API（aiohttp）
│   ├── tg-monitor/            # Telegram 监听转发
│   └── cd2-mount-mover/       # CloudDrive2 迁移工具
├── p115-src/                  # P115 API 源码
├── frontend/                  # 轻量化前端面板（SPA + nginx）
├── patches/                   # 补丁文件
├── docker-compose.yml         # 根目录编排（含前端服务）
└── .gitignore                 # 排除 .env、缓存、凭据
`

## 功能特性

- **自动转存**：监控 115 分享链接，自动转存到指定目录
- **智能重命名**：通过 LLM + TMDB API 自动解析文件名，匹配影视信息
- **卡片推送**：生成精美 Telegram 卡片消息推送到频道
- **监听转发**：自动监控 Telegram 频道消息并转发
- **文件迁移**：CloudDrive2 挂载文件自动迁移到目标存储
- **管理后台**：Web 界面管理配置、查看日志、控制容器
- **前端面板**：轻量化 SPA，支持服务状态、配置编辑、日志查看、LLM 提示词管理

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