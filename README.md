# 115 分享转存机器人 v2

纯 Telegram Bot 交互的 115 分享转存系统。

## 功能

- 📥 **115 分享转存** - 自动转存 115 分享链接到自己的账号
- ✏️ **智能重命名** - 基于文件名/TMDB 信息自动重命名
- 🔗 **永久分享** - 生成新的永久分享链接
- 👀 **自动监控** - 监听指定 Bot 的消息，自动捕获解锁后的 115 链接
- 🤖 **TG Bot 交互** - 所有操作通过 Telegram Bot 完成

## 架构

```
┌─────────────────────────────────────────────────────┐
│            Telegram Bot API (用户交互)                │
│                                                     │
│  /link <url>     手动提交115链接                     │
│  /status         查看运行状态                        │
│  /log            查看日志                           │
│  /config         查看配置                           │
│                                                     │
│  发送115链接 → 自动转存+重命名+生成永久链接            │
└────────────────────────┬────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────┐
│            Telethon (用户账号监听)                    │
│                                                     │
│  监听指定 Bot (如 Postedia_bot) 的私聊消息           │
│  捕获"解锁成功"后显示的 115 链接                      │
│  自动送入转存流程                                     │
└─────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 准备配置

```bash
cp .env.example .env
# 编辑 .env 填写配置
```

必填配置：
- `P115_COOKIE` - 115 网盘 Cookie
- `TG_BOT_TOKEN` - Telegram Bot Token (通过 @BotFather 获取)
- `TG_API_ID` + `TG_API_HASH` - 从 https://my.telegram.org 获取
- `TG_MONITOR_TARGETS` - 要监听的 Bot 用户名

### 2. 启动

```bash
docker-compose up -d
```

### 3. 登录 Telethon（首次）

```bash
docker logs -f 115-bot
# 输入手机号和验证码完成登录
```

登录成功后 session 会保存到 `data/user.session`，重启不需要再登录。

## 命令

| 命令 | 说明 |
|------|------|
| `/start` | 帮助信息 |
| `/link <url>` | 手动提交 115 链接 |
| `/status` | 运行状态 |
| `/log [N]` | 查看最近 N 条日志 |
| `/config` | 当前配置 |
| `/stats` | 统计信息 |

直接发送 115 链接也会自动处理。

## 监听模式

### private（默认）

监听与指定 Bot 的私聊消息。适用于：
- Postedia_bot 等解锁机器人
- 需要用户私聊触发的 Bot

要求：Telethon 用户账号也需要和目标 Bot 有私聊。

### channel

监听指定频道的消息。适用于：
- 资源分享频道
- Bot 在频道中发消息

要求：用户账号需要是频道成员。

## 部署到 VPS

```bash
# 1. 上传代码
scp -r v2-bot/ root@your-vps:/opt/115-bot/

# 2. SSH 到 VPS
ssh root@your-vps
cd /opt/115-bot

# 3. 配置
cp .env.example .env
vim .env

# 4. 启动
docker-compose up -d

# 5. 查看日志
docker logs -f 115-bot

# 6. 首次登录 Telethon
docker exec -it 115-bot python -c "
from telethon import TelegramClient
from config import TG_API_ID, TG_API_HASH, TG_PHONE, TG_SESSION
client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)
client.start(phone=TG_PHONE)
print('登录成功!')
"
```

## 注意事项

- 115 Cookie 会过期，需要定期更新
- Telethon session 保存在 `data/user.session`，不要删除
- 日志保存在 `data/bot.log`
- 频繁操作可能触发 115 风控，程序会自动退避重试
