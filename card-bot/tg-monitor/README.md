# Telegram 私聊 115 链接监听器

这个小服务用**你的 Telegram 用户账号**读取指定私聊消息，提取 115 分享链接，再用现有卡片机器人的 Bot Token 发给 `TG_FORWARD_TO`。来源机器人不需要提供授权码或 Token。

## 1. 前置条件

1. 用自己的 Telegram 账号打开 https://my.telegram.org，创建应用并取得 `api_id`、`api_hash`。
2. 确认你的账号能看到来源机器人私聊。
3. 确认现有 `cardbot.py` 能收到 `TG_FORWARD_TO` 发来的消息；如果启用了 `TG_ALLOW_CHATS`，把这个 chat ID 加进去。
4. 不要把 `TG_SESSION` 文件、`TG_API_HASH` 或 Bot Token 发给别人。

## 2. 网页登录（推荐）

先启动服务：

```bash
cd /vol2/1000/docker/115/card-bot/tg-monitor
docker compose build
docker compose up -d
docker logs -f tg-user-monitor
```

然后在同一局域网电脑/手机浏览器打开：

```text
http://192.168.31.70:18800
```

网页中按顺序点击：

1. “发送验证码”
2. 输入 Telegram 收到的验证码
3. 如果提示二步验证，再输入二步验证密码
4. 点击“启动监听”

成功后会在 `state/` 生成 `user.session`，以后重启服务会自动复用登录状态。

如果网页打不开，检查 NAS 防火墙和容器端口；也可以查看：

```bash
docker logs -f tg-user-monitor
```

## 3. 后台运行

```bash
docker compose up -d

docker logs -f tg-user-monitor
```

## 4. 配置说明

- `TG_SOURCE_CHAT`：来源私聊的 `@用户名` 或数字 ID。
- `TG_FORWARD_TO`：现有 cardbot 的接收聊天 ID。通常填你的个人 Telegram ID；可给 cardbot 发送 `/id` 查看。
- `TG_BOT_TOKEN`：**现有 cardbot 自己的 Token**，不是来源机器人 Token。
- `TG_START_FROM`：默认 0，只监听服务启动后的新消息；如需补读历史消息，填来源消息 ID（谨慎使用）。
- `TG_PROXY`：按当前 NAS 环境示例使用 `socks5://192.168.31.18:7893`，不需要代理则留空。

## 5. 重要限制

- 这是合规的“账号已授权可见范围内读取”，不能绕过私聊权限。
- 服务只转发链接文本，不转发来源消息的其他内容。
- `user.session` 相当于登录凭证。若泄露，应立即在 Telegram 设置的活动会话中注销该设备。
