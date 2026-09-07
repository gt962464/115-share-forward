# 115 分享转发 · 前端管理面板

轻量化 SPA 前端，配合 card-admin 后端 API 使用。

## 功能

- 🔐 密码登录（cookie 认证）
- 🖥️ 服务状态总览 + 一键启动/停止/重启
- ⚙️ 卡片机器人 & Telegram 监听配置编辑
- 🤖 LLM 提示词管理（编辑/恢复默认）
- 📜 实时日志查看（支持切换服务）
- 🔁 重试队列查看 & 清空
- 📂 转存状态（cd2-mount-mover）可视化
- 📦 一键备份配置

## 部署

### 1. Docker 部署（推荐）

`ash
cd frontend
docker compose up -d --build
`

面板地址：http://NAS_IP:18811

### 2. 直接使用

只需将 index.html 放到任意 HTTP 服务器（Nginx / Caddy / Python http.server），
打开后在顶部输入后端地址（如 http://192.168.31.70:18810）即可使用。

## 与后端的关系

| 组件 | 端口 | 说明 |
|------|------|------|
| card-admin (后端) | 18810 | aiohttp API，管理容器和配置 |
| card-admin-web (本前端) | 18811 | 纯静态前端，调用后端 API |

前端不存储任何数据，所有状态来自后端 API。前端地址自动保存到浏览器 localStorage。

## 注意事项

- 仅建议局域网访问，不要做公网端口映射
- 前端页面无缓存，修改即可生效
- 后端 .env 中的敏感字段（Cookie、Token 等）不会在前端显示明文