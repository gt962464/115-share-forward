# 115 项目第二阶段管理后台

这是完整管理后台的首版，面向 NAS 内网使用。登录后可查看两个容器状态、修改可编辑配置、重启服务、查看日志、查看重试队列、备份配置和启动/停止 Compose 服务。

## 首次部署

1. 复制 `.env.example` 为 `.env`，设置一个新的后台密码：

```bash
cp .env.example .env
vi .env
```

2. 启动：

```bash
docker compose build
docker compose up -d
```

3. 浏览器打开：

```text
http://NAS_IP:18810
```

默认 NAS 地址：`http://192.168.31.70:18810`

## 安全规则

- 只建议在局域网访问，不要做公网端口映射。
- 后台不显示 P115 Cookie、Telegram API Hash、Bot Token、回收站密码等敏感值。
- 修改配置前建议先点击“备份配置”。
- “停止全部服务”和“清空队列”是高影响操作，请确认后再点。
- `.env` 和后台 session 不提交、不同步到桌面工作台。

## 当前可编辑项

- 卡片机器人：保存目录、频道 ID、管理员 ID、白名单、TMDB、代理、日志等级、测试模式。
- Telegram 监听：手机号、来源机器人、转发目标、代理、起始消息 ID、超时、网页端口、网页密钥。
- `P115_COOKIE`、`TG_API_HASH`、`TG_BOT_TOKEN`、`RECYCLE_PASSWORD` 只保留原值，不在网页显示或修改。
