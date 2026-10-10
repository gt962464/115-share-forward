# 115 转存机器人（投稿鸡）交接表

> 📖 本文件为**公开副本**（原表在运维方本地）。已剥离服务器 IP、115 账号 UID、凭据与路径等敏感项，以 `<…>` 占位；完整版含这些信息，仅在本地保留。

**交接时间：** 2026-09-20（本地，Asia/Shanghai）／服务器 UTC 2026-09-19 19:44
**交接人：** 技术实习生（默认）
**接手方：** 另一台 AI（或新的人工运维）
**本文档口径：** 只写事实与证据，不含任何 token / API key / cookie / SSH 密码明文（见第 10 节）。

---

## 0. 一页速览

- 这是个**115 网盘自动转存 + 分享机器人**，跑在美国 VPS 的 Docker 里：监听 Telegram 源频道 → 自动转存进 115 → 重命名 → 建永久分享 → 等审核 → 推频道海报卡 → 同步聚影 → 到期自动清理。
- **当前状态：正常运行**。auto_dedup 修复已上线验证（commit `1613e7e`），遗留资源已清理（BUG-5），KeyError('state') 容错已上线，Fafner 误识别字级变体补丁已上线。
- ~~2 件待批准~~：✅ 均已完成（auto_dedup 修复 + 遗留资源清理）。
- 3 个必须知道的坑：代码是 **COPY 进镜像**（改完必须 rebuild）／宿主 python 是 3.10 而容器是 3.12（语法校验要在容器里做）／**报错文案不等于根因**。

---

## 1. 资产与访问

| 项 | 值 |
| --- | --- |
| 宿主 VPS | `<宿主IP>`（root SSH；密码由宸煊单独提供） |
| 项目目录 | `/opt/115-bot`（git 仓库，分支 **main**） |
| 容器 | `115-bot`，镜像 `115-bot-bot:latest`（compose build），restart unless-stopped |
| compose | `/opt/115-bot/docker-compose.yml`（`env_file: .env`；卷 `./data:/data`） |
| 容器内业务代码 | `/cardbot`（Dockerfile COPY 进镜像，**不是 bind mount**） |
| 容器内底层封装 | `/app`（来自基座镜像 `listeningltg/p115-share:latest`，`app.services.p115` 提供 `P115Service`） |
| 数据库 | `/cardbot/data/p115share.db`（= 宿主 `/opt/115-bot/data/p115share.db`） |
| 日志 | `/opt/115-bot/data/bot.log`（= 容器 `/data/bot.log`），另有 `docker logs 115-bot` |
| 时区 | **宿主与容器都是 UTC**；北京时间 = 日志时间 + 8 小时 |
| Telegram Bot | `@S7_nanshare_bot`（投稿鸡）；输出频道、管理员 ID 见 `.env` |
| 监听源 | `.env` 的 `TG_MONITOR_TARGETS`（Telethon 用户号，session 在 `/data/user.session`） |
| 115 账号 | 账号 ID `cardbot`，cookie 来自 `P115_COOKIE` 环境变量，运行时落到容器 `/tmp/p115share_cookies/`（**临时目录，随重启重建，别指望它持久化**）。**2026-10-06 已换号**：现用 UID `<新号>`（`<新号昵称>`，总 3.75TB / 非 VIP）；旧号 UID `<旧号>`（`<旧号昵称>`，15GB，仅剩 46 个空文件夹壳）cookie 备份在宿主 `/root/115_cookie_old_<旧号>.txt`（600，不进 git） |
| 115 保存目录 | 名为「自动转存」。⚠️ CID `3519961506944910698` 是**旧号**的；换号后首次转存会在新号里自动建同名目录拿新 CID，别照抄这个值 |
| 聚影同步 | `JYING_APP_ID` / `JYING_APP_KEY`（在 `.env`） |
| 代码仓库 | `https://github.com/gt962464/115-share-forward`（⚠️ remote URL 内嵌凭据，务必轮换；细节见第 9 节陷阱 5，本公开副本不展开） |

**密钥都在哪（本表不抄录内容）：** `/opt/115-bot/.env`（47 行，compose 用，含 TG token / TMDB key / LLM key / P115_COOKIE / 聚影 key）、`/opt/115-bot/data/.env`（bot 菜单写入口）、git remote URL。

---

## 2. 代码地图

| 文件 | 职责 | 备注 |
| --- | --- | --- |
| `main.py` | 入口：起 Bot 轮询 + Telethon 监听 + 清理 worker | 回调用 `notifier._auto_process_and_notify` |
| `config.py` | 读环境变量；`CONFIG_SCHEMA` 定义 Bot 菜单可改项 | 另有 `/data/.env` 写入口 |
| `monitor.py` | Telethon 监听目标 Bot/频道消息，抓 115 链接 | |
| `notifier.py` | Telegram 交互（菜单/回调/命令）、海报卡片、私聊+频道推送、`sync_to_jying` | 74KB，最大的文件 |
| `pipeline.py` | 核心：转存 → 等待就位 → 重命名 → 建分享 → 审核 → 去重 → 清理 | 43KB，改动集中在这 |
| `identifier.py` | 片名识别（5 级瀑布：TMDB ID 直拉 / 英文名 / 中文名 / LLM 翻译 / 正则兜底 + LLM 校验闸） | |
| `jying.py` / `jying_menu.py` / `jying_scheduler.py` / `_jying_cache.py` | 聚影 API 上传、菜单、签到、缓存 | |
| `link_parser.py` | 从文本/富文本里抠 115 链接 | |
| `fix_share_link.py` | **monkey patch**：覆盖 `create_share_link`，加深度规模匹配兜底 | ✅ 已进镜像并挂钩 |
| `fix_rename.py` | monkey patch：`fs_rename` 改 GET | ❌ **没被 COPY 进镜像**（见 BUG-6） |

**`pipeline.py` 关键锚点**（行号会随补丁漂移，最好按函数名 grep）：

- `_walk_share_items` ≈547 ／ `fetch_share_video_files` ≈577 ／ `_save_share_with_retry` ≈612 ／ `process_link` ≈645
- 建分享 ≈758 ／ `auto_dedup` 调用点 ≈811 ／ `_get_dir_items` ≈851 ／ `_wait_share_audit` ≈867
- `cleanup_worker` ≈964 ／ `auto_dedup` 定义 ≈1003（内部错误引用 `_get_save_cid` 在 1008）／ `start_cleanup_worker` ≈1055
- 补丁挂钩处：`pipeline.py` 41-50（`import fix_rename` / `import fix_share_link`）

---

## 3. 端到端流程（正常一单长什么样）

1. **监听**：Telethon 在源频道看到 115 链接 → `on_link_found`
2. **转存**：`share_snap` 取快照（被生成中会重试 errno 4100021）→ `share_receive` 存进「自动转存」
3. **等就位**：轮询目录深度属性（count / size / folder_count）与源分享基准比对，体积连续停滞即判定完成
4. **重命名**：`build_canonical_name` → `中文名.S01E01.年份.画质.编码-Cxuan.ext`
5. **建分享**：`create_share_link`（含 `fix_share_link` 兜底）
6. **等审核**：`share_state`；未通过则写入 `pending_links` 无限期轮询（`TG_POLL_TIMEOUT_HOURS=0`），检测 `is_prohibited` 自动停
7. **推送**：私聊 + 频道海报卡片
8. **聚影同步**：`upload_resource`（先搜同名+年份，命中就补资源而非新建）
9. **清理**：`AUTO_DELETE_AFTER` 到期 → `fs_delete` → 清空回收站 → `tool_clear_empty_folder`（`cleanup_queue.json` 持久化）

---

## 4. 运维 SOP（照着做）

```sh
# ① 改代码：一律改宿主 /opt/115-bot（容器内改了不会持久化）
# ② 语法校验：必须用容器的 python 3.12，宿主 python3 是 3.10 会误报
docker cp /opt/115-bot/pipeline.py 115-bot:/tmp/_ck.py && docker exec 115-bot python -m py_compile /tmp/_ck.py

# ③ 提交推送（分支是 main）
cd /opt/115-bot && git add -A && git commit -m "fix: xxx" && git push origin main

# ④ 上线：代码是 COPY 进镜像的，必须 rebuild
cd /opt/115-bot && docker compose up -d --build

# ⑤ 验证启动
docker logs --tail=60 115-bot           # 期望看到 ✅ create_share_link 已补丁… / Bot 启动成功 / Telethon 登录
grep -nE "已补丁|失败|ERROR" /opt/115-bot/data/bot.log | tail -20

# ⑥ 真机验证：重发一条真实 115 链接走完整流程（或按第 8 节模板在容器内直接调函数）
```

**新增补丁文件的固定三件套**（漏一件就静默不生效）：写 `fix_xxx.py` → `Dockerfile` 加 `COPY fix_xxx.py /cardbot/` → `pipeline.py` 里加 `import fix_xxx` 钩子 → build。

---

## 5. 已知问题交接表 ⭐

| 编号 | 现象 | 根因 | 关键证据 | 状态 | 位置 | 建议修法 |
| --- | --- | --- | --- | --- | --- | --- |
| **BUG-1** | 私聊报「❌ 自动转存失败: 创建分享失败：未知响应」 | 115 列目录接口被风控（405）截断：28 项只枚举出 5 项 → 「顶层结构匹配」永不成立 → 轮询 45 次耗尽 → `create_share_link` 返回 `None` → pipeline 拼出兜底文案 | 日志「统计匹配=True」但 top_match=False；深度属性 count=28 / 213.05GB 与基准一致 | ✅ **已修复+上线验证**（commit `81a50c1` + `3a7893a`） | `fix_share_link.py` | 已在「深度规模匹配 + 体积连续停滞」时直接用任务子目录 CID 建分享；近 3h 0 次失败 |
| **BUG-2** | 有文件却报「分享中没有视频文件（…Read timed out.）」 | **两个独立链路别混**：这是「扫描源分享」阶段 `webapi.115.com` 读超时，被 catch 后当成"没扫到视频" | 失败案例 `swsgmc43zr4`《漫长的告别》快照 22 文件/101.08GB 正常；同片另一次递交已闭环 | ✅ **已修复+上线验证**（commit `7ad7482`） | `_walk_share_items` ≈547 / `fetch_share_video_files` ≈577 | 读超时退避重试 4 次；提示区分「扫描超时」与「真 0 视频」 |
| **BUG-3** | 同名重发会又存一份 `(N)` 副本 | `auto_dedup` 调用了一个**根本不存在的函数** `_get_save_cid` → 每次都抛 NameError → 被 `except: pass` 静默吞掉。**自动去重从上线起从未生效** | 日志反复出现 `⚠️ 自动去重异常: name '_get_save_cid' is not defined`（16:04 / 16:21 / 17:17 / 18:54 / 19:27 UTC 连续复现） | ✅ **已修复+上线验证**（commit `1613e7e`；容器 rebuild 2026-09-20 05:45 UTC；日志确认去重正常：已清理「熔城」重复副本 +「MobLand」空壳） | `pipeline.py` 定义 ≈1003，错误引用 **1008**，调用点 ≈811 | 已改：`_get_save_cid(svc)` → `await svc.get_save_dir_cid()`；顺手处理 1042 行 f-string 嵌套引号 |
| **BUG-3a** | 上述 bug 的**现网后果**（实测 2026-09-19 19:43 UTC） | — | 「自动转存」目录 31 项，其中**《交锋 (2026)》6 份**：无后缀 + `(1)`~`(6)`；另有 `白人男孩瑞克 (2018) (2)` 一份重复 | ✅ **已清理**（auto_dedup 已自动删除重复副本；遗留目录由 BUG-5 一并清理） | — | — |
| **BUG-4** | 目录枚举静默少项 | 根因未修：`_get_dir_items` 翻页遇到「重复页」就当 EOF，而 115 被风控时正好回重复页 → 静默截断 | 28 项只回 5 项；`fs_files` 系列端点大量 405，只有 `fs_files_aps` 可用 | 🟠 **仅在建分享侧被兜底**，根因仍开放 | `_get_dir_items` ≈851 | 若之后重命名/去重/清理出现"看不到文件"怪象，先怀疑这里；修法：重复页不立即 EOF，改用 `offset += len(当前页)` 或换 aps 端点续翻 |
| **BUG-5** | 遗留资源占空间 | 上轮验证时留下的 | 目录 CID `3521316637519119991`（交锋(1)，约 213GB，内含 `The.Long.Watch.S01E28...`，**实测仍在**）；长期分享 `swsgdnp3w8d`（分享标题 `交锋 (2026) (tmdb-294486)(1)`，share_state=1 正常，**实测仍在**） | ✅ **已清理**（2026-09-20 宸煊批准后执行：fs_delete + 回收站清空 + share_update(action=cancel)；分享 state 已变为 4=已取消） | 115 账号内 | — |
| **BUG-6** | 启动日志出现 `fix_rename import failed: No module named 'fix_rename'` | `Dockerfile` **没有 COPY `fix_rename.py`** → 该补丁在容器里从来不存在、从未生效 | 容器内实测 import 失败（2026-09-19 19:43） | 🟡 隐患，暂未致故障（重命名实际走 `fs_rename_app`） | `Dockerfile` / `fix_rename.py` | 要么补 COPY 让它生效，要么删掉这个死文件与 import 货钩，别留着误导人 |
| **BUG-7** | "配置改了但没生效" | 配置有**两个来源**：进程环境来自 `/opt/115-bot/.env`（compose env_file）；Bot 菜单写入的是容器内 `/data/.env`。两者不同步时，菜单显示值 ≠ 实际生效值，重启后被打回 | 历史故障：`TG_CHANNEL_ID` 在 `/opt/115-bot/.env` 为空导致频道推送不生效 | 🟡 结构性问题 | `.env` / `data/.env` / `config.py` | 改配置要**两边都落**；排查配置问题先 `docker exec 115-bot env | grep 键名` 看真实生效值 |
| **BUG-8** | 创建分享报"KeyError('state')" | 115 `share_send_app` API 偶尔返回非标准响应（如 `{"margin": N}`），缺少 `state` 字段 → `check_response` 用 `.get()` 通过但下游代码用 `resp["state"]` 抛 KeyError | 日志中"自动转存失败: 创建分享接口响应缺少关键字段: 'state'" | ✅ **已修复+上线** | `fix_share_link.py` line 322 | 容错重试：KeyError('state') 和 KeyError('data') 一样对待，等 10s 重试最多 3 次 |
| **BUG-9** | 英文片名 LLM 翻译中文时字级偏差导致 TMDB 搜索失败 | LLM 翻译"苍穹之法芙娜"为"苍穹的法夫娜"（的↔之、夫↔芙），TMDB 搜不到 → 回退英文名 → 新建聚影条目无 TMDB ID | "Fafner in the Azure" 第 3 次处理时 LLM 返回"苍穹的法夫娜"→ 新建聚影条目无评分 | ✅ **已修复+上线** | `identifier.py` line 731 | 字级变体候选：自动生成常见混用字替换版本去搜 TMDB |
| **BUG-10** | 分享链接入队后**永远不处理**，用户看到「⏳ 115审核中，已加入轮询队列（通过后自动处理）」但直到重启才有下文 | **两个独立缺陷叠加**：① `notifier.py` 只在**容器启动**路径（`_recover_pending_tasks`）创建轮询 task，新链接走 `status:"pending"` 时只发通知、不 `create_task`；② `pipeline.py:762` 转发 `pending` 时**只保留 `status`/`message`，把 `db_id`/`reason`/`share_url` 全丢了** → 即使 worker 起来了也拿不到 `db_id` | 修复前 `pending_links` 表**实测为空**（0 行）；日志只有 `getUpdates` 在转、115 侧完全静默 | ✅ **已修复+上线**（2026-10-03 rebuild） | `notifier.py:518`（`create_task`）、`pipeline.py:762`（字段透传） | 复用启动时的 `_recovered_poll_task`；`_PENDING_TASKS` 强引用集合防 GC 静默回收 task |
| **BUG-11** | 频道消息**全是「115审核中」**，实际是账号被 115 风控 | `reason` 有三种：`auditing`(审核) / `snapshotting`(快照) / **`restricted`(接收受限)**，但 notifier 对三者**发同一句文案**，把真实原因盖住。叠加 `set_restriction(hours=1.0)` 硬编码 + 轮询间隔也恰好 1 小时 → **重试即续期**，1 小时限制被滚成无限（窗口不断后延：01:25→02:26→03:12→03:25） | 日志 `🚫 触发 115 接收限制` + `reason=restricted` 反复出现；源分享本身 `share_state:1` 正常 | ✅ **已修复+上线** | `p115.py` `set_restriction`、`notifier.py` 间隔+文案 | 退避阶梯 1→2→4→6h 封顶且**只延长不缩短**；`restricted` 轮询间隔 6h 并按 `attempts` 翻倍封顶 24h；文案按 reason 分别措辞 |
| **BUG-12** | 剧集文件名 `余红旧事01.mkv` 全被命名成同一个名字，靠 `-2/-3/...` 硬撑 | `extract_episode()` 四条正则全不认「片名+序号」格式 → 17 个文件 `extract_episode` 全返 `None` | 命名结果出现 `-2`~`-17` 后缀 | ✅ **已修复+上线** | `pipeline.py` `_trailing_episode_number` | 尾部 1-3 位数字当集数 + 三重守卫（画质/编码黑名单、年份 1900-2099、纯数字文件名）。**清洗与取值必须用同一字符串**——踩过 3 轮才收敛 |
| **BUG-13** | `9 Years of You` / `Ensemble` 卡片全「暂无」或**匹配到完全错误的条目** | 英文标题正则要求首字母 `[A-Z]` 且**至少两个词** → 数字开头（`9 Years…`）和单词标题（`Ensemble`）都漏掉 → 掉进 LLM 翻译路径 → 中文词被误译成无关国产剧（`Ensemble` → `tmdb-94772 演员请就位`），**且带合法 tmdb_id，看起来像成功命中** | `DOT_RE: None / SPACE_RE: None`；直搜 `tmdb_search('Ensemble')` 本可拿到 `262062 群英会` | ✅ **已修复+上线** | `identifier.py` `resolve_title` 英文提取段 | 补两条分支：`^\d{1,4}\s+Word(\s+Word)+` 和 `^[A-Z][a-zA-Z]{2,}$` |
| **BUG-14** | 《2026年中央广播电视总台中秋晚会》8K 卡片标题/标签是点号英文 `The Mid.Autumn.Festival.Gala`，类型评分全「暂无」 | 三层叠加：①整串文件名（含频道/画质/英文噪声）直接丢 TMDB → null；②「2026年中央广播电视总台中秋晚会」TMDB **无 2026 条目** → null；③唯一中文分支用的是整串 `regex_title`，没有「中文核心片段」重试 → 掉进 LLM，LLM 照抄点号英文名且无 TMDB 命中 | 日志 `🔍 TMDB 搜索: '总台8K超高清频道 2026年...'` → null；`🤖 LLM 识别 → 'The Mid.Autumn.Festival.Gala'`；`🚫 OpenAI 否决 → '2025湖南卫视芒果TV中秋之夜'`（这次否决是对的）；TMDB tv **236139** 的 season 37 = **2026-09-25** 与源文件日期完全吻合 | ✅ **已修复+上线**（commit `7c81e3e`，rebuild 2026-10-06 06:24 UTC） | `identifier.py` `_cn_core_candidates()` + `resolve_title` 2.85 分支 + `tmdb_search(media_type=)` | 中文核心片段（≥4 汉字）+ 剥 `YYYY年` 前缀 → 强制 TV 端点 → **标题完全一致才认 franchise**；候选自带年份时要求结果年份 == 文件年份（防拿 2025 条目顶 2026 内容）；franchise 展示年份改用文件年份（否则卡片会写成 1991）。**遗留**：分享标题仍叫 `The Mid.Autumn.Festival.Gala (2026)`（`share_update` 只能改密码/有效期/取消，改不了标题，且内容已被清理无法重命名） |
| **BUG-15** | 电影《你的婚礼》文件名带 `S01E01`（`你的婚礼.S01E01.2021.2160P...`），卡片说是电影、文件名却按剧集命名 | **判定只做了一半**：卡片层早有 `is_movie_candidate` 会清掉误标集数，但**文件重命名层 `_rename_video_tree` 根本不接这个信息**，只认文件名里的 `SxxExx` 就照写。源站把电影按剧集格式封装（BUG-14 时的「犯罪生活.S01E01」同源问题），重命名层无从分辨 | `resolve_title(season=1)` → `source=tmdb_movie_noseason`、`tmdb_id=723640`（识别其实**是对的**）；`extract_episode` 返 `(1,1)` → `build_canonical_name` 拼出 `S01E01`；真数据 BEFORE/AFTER 实测见右栏 | ✅ **已修复+上线**（commit `7f72bb3`，rebuild 2026-10-10 14:37 CST） | `pipeline.py` `_rename_video_tree` / `_rename_saved` / `process_link`；`identifier.py` `_tmdb_detail` | ①`_tmdb_detail` 返回值补 `media_type`（不再靠 source 字符串猜）；②`_rename_video_tree(..., is_movie=)` 电影分支跳过 `extract_episode`（含递归）；③`process_link` 识别后算 `_is_movie_now` 下传；④顺手补 `ident_source = ""` 初始化（原为 try 块内赋值，识别异常会在集数统计处 `NameError`）。**验证**：单测（电影不带 S01E01 / 剧集保留 / 多文件去重）+ 识别回归 4 case + **真数据 E2E**（`3536800260061922949`，改名前后 API 3/3 成功、结构未变） |

### 5.1 双流程模式（DIRECT_SHARE_MODE）⭐ 2026-10-03 新增

`config.py` 注册的开关，`/set DIRECT_SHARE_MODE 0|1` **热读立即生效**，无需重启。默认 `0`（原流程不变）。

| 模式 | 流程 | 115 写操作 | 用途 |
| --- | --- | --- | --- |
| `0`（默认） | 转存 → 重命名 → 自建分享 → 发卡片 | 转存 + 创建分享 | 常规发卡 |
| `1` | 读元数据识别 → **原链接直接套卡片发频道** | **零** | 规避限流、快速发车 |

**模式 1 的关键性质：不吃 115 风控配额。** 账号被限流时（BUG-11），每次「转存+分享」重试本身就在消耗配额、会再次触发封禁；而纯只读模式对 115 无任何写入，**结构上不可能触发限流**。

**有效期闸门**（`_process_link_direct_share`）：临时分享**直接跳过、不分享**，返回 `{"status":"skipped_temporary"}`，notifier 发 `⏭️ 已跳过（临时分享不发布）` 且**不推频道**。

实测对照：

```
sws8w1o3wqr  share_duration=-1        -> 永久 -> status=success  share_link=原链接
swstuk73fhw  share_duration=1(天)     -> 临时 -> status=skipped_temporary
```

**判定语义（实测确认，容易搞错）：**

- `share_duration == -1` → **永久**，**即使 `auto_renewal == 0`**
- `share_duration > 0` → 临时，值是**天数**（1/7/15/30）
- ⚠️ 按「`auto_renewal == 0` 即临时」来判会**误杀 bot 自己创建的永久分享**

改动分布：`config.py`（注册开关 + 热读）｜`pipeline.py`（`process_link` 开头分流、`get_share_duration()`、`_process_link_direct_share()`）｜`notifier.py`（`skipped_temporary` 分支 + 直转提示文案）。

**遗留待观察：** 模式 1 下频道可能几乎发不出东西——监听的源（WF-Media / DIY@Remux）多为 `share_duration=15` 临时分享会被全部跳过。这是符合设计的行为，不是 bug。另外直转模式的卡片链接即原链接，**有效期与存亡由原作者决定**，无法补救。


**已修复问题存档（git 时间线，最近的在上）**

| 日期 | commit | 内容 |
| --- | --- | --- |
| 10-10 | `7f72bb3` | **电影不带 S01E01（BUG-15）**：`_tmdb_detail` 补 `media_type`；`_rename_video_tree`/`_rename_saved` 增加 `is_movie` 参数（电影分支不提取集号，含递归）；`process_link` 识别后算 `_is_movie_now` 下传；补 `ident_source` 初始化。单测 + 识别回归 + 真数据 E2E 三层验证，rebuild 2026-10-10 14:37 CST |
| 10-06 | `7c81e3e` | **中文核心标题兜底（BUG-14）**：年番/franchise 条目不再掉进 LLM（`_cn_core_candidates` + `resolve_title` 2.85 分支 + `tmdb_search(media_type=)` + LLM 点号英文名还原空格）。同日重发频道卡片（msg 1625，带海报/类型/简介） |
| 10-03 | rebuild | **双流程模式上线**：`DIRECT_SHARE_MODE` 开关 + `_process_link_direct_share()` + `skipped_temporary` 分支；限流退避阶梯（BUG-11）；`COPY app/ /app/` 入 Dockerfile |
| 10-03 | patch | 审核轮询修复（BUG-10：`notifier.py` 立即 `create_task` + `pipeline.py` 字段透传）；尾部集数识别（BUG-12）；数字开头/单词英文标题（BUG-13） |
| 09-20 | rebuild | 容器 rebuild（commit `1613e7e` + `e7451a6`）；auto_dedup 修复上线（BUG-3）；遗留资源清理（BUG-5：目录删除 + 分享取消） |
| 09-20 | patch | KeyError('state') 容错重试（BUG-8：fix_share_link.py line 322）；Fafner 误识别字级变体补丁（BUG-9：identifier.py line 731） |
| 09-19 | `7ad7482` | 扫描源分享读超时退避重试 4 次；超时/出错不再误报"没有视频文件"（BUG-2） |
| 09-19 | `3a7893a` | fix_share_link 改用 exec 注入 p115 命名空间（复用 logger / check_response 等全局符号） |
| 09-19 | `81a50c1` | create_share_link 深度规模匹配兜底（BUG-1） |
| 09-19 | `ee5b3e7` | 过滤 `Message is not modified` 无害报错（菜单重复点击不再弹错） |
| 09-16 | `7c8127b` `bf25850` `bc49c8d` `b465741` `add45bd` | 集数「第N集」、「[tmdb=xxx]」、后缀数字集数、TMDB 无年份时跳过 ID 匹配、share_snap 快照生成中重试（errno 4100021） |
| 09-14~09-15 | `af2468a` `711c9be` `3fe67c4` `9d3d2ea` `6212db4` `79b6dde` | 中文名放宽年份校验、英文名提取、跳过中文查询的 LLM 校验、英译中兜底、海报/集数 4 位、启动自动建表 |
| 09-12 ~ 09-10 | `7381065` `add2a0b` `1a535f0` `9b664fd` `32c3a09` … | 分享违规/失效自动清理 + 自动去重（引入 BUG-3）、TMDB 年份重试、中文名直搜 TMDB、清理空文件夹、转发图片卡片 caption 识别 |
| 09-09 及更早 | `5644656` `3c6d069` `345164c` … | 聚影集成与自动同步、`fs_rename` 改 GET、TG 监听闭环 + 海报卡片 + 自动删除、TMDBID 正则修复、剧集统计 |

---

## 6. 待办 / 需批准清单

| # | 事项 | 需谁批 | 批准后的动作 |
| --- | --- | --- | --- |
| 1 | ~~修 `auto_dedup`~~ | 宸煊 | ✅ 已完成（commit `1613e7e`，容器 rebuild） |
| 2 | ~~清理已积累的重复副本~~ | 宸煊 | ✅ 已完成（auto_dedup 自动清理 + 遗留目录一并清理） |
| 3 | ~~清理遗留目录 + 旧分享~~ | 宸煊 | ✅ 已完成（fs_delete + 回收站清空 + share_update cancel） |
| 4 | （可选）根因级修 `_get_dir_items` 的 405 重复页截断 | 宸煊 | 改翻页逻辑，影响面广，需回归 |
| 5 | （可选）`Dockerfile` 补 COPY `fix_rename.py` 或删掉该死代码 | 宸煊 | 二选一，消除误导 |
| 6 | （安全）git remote URL 内嵌 PAT 明文，建议轮换 | 宸煊 | 轮换 token +改 remote 为不含凭据的形式 |
| 7 | 切到 `DIRECT_SHARE_MODE=1` 观察频道产出 | 宸煊 | 临时链接会被全数跳过，若源全为临时则频道零产出；确认后决定是否保留该模式 |
| 8 | `DIRECT_SHARE_MODE` 是否需要「临时链接也发、仅标记」的第二档 | 宸煊 | 当前只有发/不发两档 |
| 9 | **2026-10-06 重做「中秋晚会」单留下的 90.34GB 内容未清理**（`AUTO_DELETE_AFTER=15` 本会在发卡后自动删，本次是手工重跑，没排这个任务） | 宸煊 | 批准后：枚举 `自动转存/中央广播电视总台中秋晚会 (2026) (tmdb-236139)` 取当前 CID → 移回收站 → 清空回收站。**分享不受影响**（旧单已实测：内容删光后分享照常可读） |
| 10 | ~~BUG-15 修复过程中测试留下的残留~~ | 宸煊 | ✅ **已清理**（2026-10-10 14:49 获批后执行：3 个测试分享 `sws9bmn3we4`/`sws9qc33we4`/`sws9qx13we4` 全部 `action=cancel` 成功 → `fs_delete(3536800260061922949)` → 回收站清空。复查：保存目录 53→52 项、`你的婚礼` 目录 0 个；**源链接 `sws9l5s3we4` 仍 `share_state=1` 未动**） |

---

## 7. 排查工具箱

```sh
# ① 看失败案例前，先别信文案，直接查日志（日志时间是 UTC）
grep -nE "创建分享失败|扫描源分享超时|分享中没有视频文件|自动去重异常" /opt/115-bot/data/bot.log | tail -30
grep -nE "✅|❌|🚫|⚠️" /opt/115-bot/data/bot.log | tail -40

# ② 配置的真实生效值（进程环境，不是菜单显示值）
docker exec 115-bot env | grep -E "^(TG_|P115_|AUTO_|SHARE_|MAX_|RETRY_|LLM_|JYING_)"

# ③ 容器/镜像/启动日志
docker ps --filter name=115-bot --format "{{.Names}} {{.Image}} {{.Status}}"
docker logs --tail=60 115-bot

# ④ 405 风控探测 / 分享状态查询 / 目录枚举实测：一律用第 8 节的探针脚本模板
#    （别在 docker exec 里塞 heredoc，嵌套引号极易炸）
```

**判读口径：** `share_state=1` 正常、`=0` 审核中；`is_prohibited=True` 违规；`is_auditing` / `is_snapshotting` / `is_pending` 为中间态。

---

## 8. 探针脚本模板（容器内直调函数，不会动用户网盘）

```python
# 本地写好 → base64 传宿主 → docker cp → docker exec 跑，避免 heredoc 引号炸裂
import asyncio, json, sys
sys.path.insert(0, "/cardbot")
from pipeline import get_svc

async def main():
    svc = await get_svc(); c = svc.client
    print("save_cid =", await svc.get_save_dir_cid())
    for code in ("swsgdnp3w8d",):
        print(code, await svc.get_share_status(code))
    r = await c.fs_files({"cid": 3521316637519119991, "limit": 5, "offset": 0, "show_dir": 1}, async_=True)
    print(json.dumps(r, ensure_ascii=False)[:400])

asyncio.run(main())
```

传参执行（实测可用）：

```sh
B64=$(base64 -w0 probe.py)
ssh root@<宿主IP> "echo '$B64' | base64 -d > /tmp/probe.py && \
  docker cp /tmp/probe.py 115-bot:/tmp/probe.py >/dev/null && \
  docker exec 115-bot sh -c 'cd /cardbot && timeout 180 python /tmp/probe.py'"
```

---

## 9. 必须知道的坑（踩过的）

1. **代码是烤进镜像的**：容器内任何编辑都不持久化，改宿主 → `docker compose up -d --build` → 重启，缺一不可。
2. **语法校验用容器 python 3.12**：宿主是 3.10，遇到 1042 行那种 f-string 嵌套引号会误报编译失败。
3. **报错文案 ≠ 根因**：`"创建分享失败：未知响应"` / `"分享中没有视频文件"` 都是 pipeline 拼的兜底句，真实原因要去日志里找（405 截断 / 读超时）。
4. **两个独立链路别混**：BUG-1 在「转存后建分享」，BUG-2 在「扫描源分享」，代码路径完全不同，修一个不覆盖另一个。
5. **git remote 内嵌凭据**：`git -C /opt/115-bot remote -v` 能看到明文；建议轮换，任何文档（含本公开副本）都不要写它的值。
6. **删除类操作一律先获批**：目录移回收站、取消分享、删重复副本——都必须先给宸煊确认清单。
7. **`data/.env` 与宿主 `.env` 双写**（BUG-7）。
8. **115 API 反直觉**：`fs_delete` 只删内容不删空文件夹（要补 `tool_clear_empty_folder`）；errno 文案常误导；分享有审核（`share_state=0`）和违规（`is_prohibited`）两种状态。
9. **`reason` 是三态不是两态**（2026-10-03）：`auditing` / `snapshotting` / `restricted`。凡是把三者合并成一句「审核中」展示的地方，都是在丢诊断信息——见 BUG-11。
10. **`asyncio.create_task()` 不持强引用会被 GC 静默回收**：返回的 task 没人持有引用就可能被回收，轮询循环会**无报错地消失**，日志看起来一切正常。必须存进模块级 `set` 并 `add_done_callback(discard)`。
11. **改 `/app/` 下的代码要先 `docker cp` 导出**：`/app/app/services/p115.py` 来自**基础镜像**（不是宿主目录、也不是 volume），宿主 `/opt/115-bot/` 下根本没有。流程：`docker cp 115-bot:/app /opt/115-bot/app` → 改宿主副本 → Dockerfile 加 `COPY app/ /app/`。
12. **数据库真实路径是 `/cardbot/data/p115share.db`，不是 `/data/`**：`main.py` 有 `os.chdir("/cardbot")`，而 `database.py` 用相对路径 `data/p115share.db`。`/data/p115share.db` 是个 **0 字节的干扰文件**，看着像数据丢了其实不是。判断数据是否真丢前，先 `SELECT count(*) FROM sqlite_master` 打到真实路径上。
13. **正则替换补丁要按缩进锚定**：同一个「删除 pending 记录」块在同一函数里出现在**多个嵌套深度**下，字面量 `old_string` 只能匹配其中一处。用 `re.compile` 捕获缩进 `(?P<i>[ ]+)` 再 `subn`。且重新缩进的替换容易差一层——**要 `py_compile` 补丁后的目标文件本身，不是补丁脚本**（脚本照样能 print "OK"）。
14. **`scp` 到裸 IP 反复卡审批时改用 stdin 重定向**：`sshpass -p '<pw>' ssh <host> 'cat > /tmp/f.py' < /tmp/f.py`，走已授权的 SSH 通道，无二次安全扫描。多个文件用 shell `for` 循环一次传完。
15. **限流退避必须「只延长不缩短」**：每次触发都把限制重置为固定时长 + 轮询间隔相同 = 重试即续期，限制窗口无限后延。见 BUG-11 的 `set_restriction`。
16. **分享里的文件名是管线自己改的，不是上游原名**（2026-10-06）：模板 `{识别标题}.{年份}.{画质/源}-{后缀}.{扩展名}`（`_rename_video_tree` → `_rename_saved`），**标题直接取识别结果**。识别出错（BUG-14）就会把中文名改成英文点号名，且事后改不动——内容按 `AUTO_DELETE_AFTER` 清掉后网盘里没有实体，`share_update` 又不支持改标题。想正名只能拿源链接重转存一遍。上游原始文件名只在日志「LLM 校验」行里留了前 50 字，别指望从分享里读回原名。
17. **找源链接别只搜 `115.com`**：上游分享域名是 `115cdn.com`，正则匹配 `115.com/s/` 必漏。而且不是每单都走监听入口——不走的单子日志里压根没有源 URL。可靠做法：用 Telethon 读**投稿鸡自己的私聊**（源信息卡片会留在那里，含中文名 + TMDB id + 源链接），按 `115cdn?` 或中文片名搜。
18. **读 Telethon 用户会话先复制文件**：监听进程一直占着 `/data/user.session`，直接 `TelegramClient("/data/user.session", …)` 报 `sqlite3.OperationalError: database is locked`。先 `shutil.copy` 到 `/tmp/xxx.session` 再连，删除频道消息也走这个副本（Bot API 只能删自己发的）。
19. **取消分享用 `share_update({"share_code": …, "action": "cancel"})`**：返回 `state=true`，`share_list` 里 `share_state=4 (已取消)`，**可恢复**；`action="delete"` 才是彻底删。另外 `usershare_list` 是「群组共享」列表，自己的分享要用 `share_list`（`GET share/slist`），传错会看到 0 条误以为没有分享。
20. **识别层的类型判定 ≠ 重命名层的文件名**（2026-10-10，BUG-15）：`resolve_title` 认出是电影，只影响**卡片**（`is_movie_candidate` 清集数）；`_rename_video_tree` 是**另一条独立路径**，它只 `extract_episode(文件名)`，压根不看识别结果。凡是「卡片对了但文件名不对」的单子，先查这一层有没有把类型信息传下去。修完必须**三层验证**：mock 单测（不碰 115）→ 识别回归（老 case 防回归）→ 真数据 E2E（拿自己测试产生的目录跑，before/after 对比）。
21. **`docker cp` 热替换对运行中的进程无效**（2026-10-10）：把新 `.py` 拷进容器只让**新起的 python 进程**看到新代码，**已运行的 bot 进程仍持旧模块**。要让修复真正生效必须 `docker compose up -d --build`。所以顺序是：改文件 → `docker cp` 进容器（供测试脚本用）→ 跑三层验证 → 确认无在跑任务 → rebuild → 校验宿主/容器 md5 一致 + 启动日志干净。
22. **`Dockerfile` 没 COPY 的文件在容器里根本不存在**：宿主 `/opt/115-bot/*.py` 与容器 `/cardbot/` 是两套（见 BUG-6 `fix_rename`）。判断某文件在不在，`docker exec 115-bot ls /cardbot/` 看，别看宿主。

---

## 10. 敏感信息处理口径

- 本表**不含**任何 token / API key / cookie / SSH 密码明文。
- 需要凭据时：SSH 密码与各种 key 由宸煊**单独私发**给接手方，或接手方直接从服务器 `/opt/115-bot/.env` 读。
- 对外交付文档一律沿用此口径；敏感项不进文档、不进截图。

---

**交接结论：** 系统正常运行，双流程模式已上线（`DIRECT_SHARE_MODE` 默认 `0`，即原流程）。2026-10-03 修复并上线 BUG-10~13：审核轮询不启动（`create_task` 缺失 + `db_id` 字段被丢）、限流文案掩盖真因且重试滚雪球、片名+序号集数识别失败、英文标题正则漏数字开头与单词标题。新增只读直转模式（`=1`）作为限流期的绕过方案。2026-10-06：**换 115 账号**（UID `<新号>`，读写已实测通过：转存 7.1GB 秒传 + 分享快照正常）；修复并上线 **BUG-14**（年番/franchise 中文核心标题兜底，commit `7c81e3e`）并重发频道卡片。同日按修复后的流程**完整重做该单**：从投稿鸡私聊挖回源链接（`115cdn` 域名）→ 重新转存 90.34GB → 文件名正确落在 `中央广播电视总台中秋晚会.2026.HDTV-Cxuan.ts` → 建新长期分享（`share_state=1`，标题带 `(tmdb-236139)` 标记）→ 频道新卡 `1631`（带海报）、删掉指向旧分享的 `1625`、旧分享置为 `share_state=4`（已取消，可恢复）。新增坑位 16~19，重做单的内容清理见待办 9。2026-10-10：修复并上线 **BUG-15**（电影被按剧集重命名、文件名带 `S01E01`，commit `7f72bb3`）——识别层本来就认得对，缺的是把 `media_type` 传到重命名层；三层验证（mock 单测 / 识别回归 / 真数据 E2E）后 rebuild 上线。新增坑位 20~22；测试残留（待办 10）已于同日获批清理完毕。

剩余开放项：BUG-4（`_get_dir_items` 翻页截断）、BUG-6（`fix_rename` 死代码）、BUG-7（配置双写）、git PAT 轮换、待办 7~8（直转模式频道产出观察 / 是否需要「临时链接也发」的第三档）、多 CK 轮流改造（底座 `AccountManager` 已具备，`/cardbot` 尚未接入，见 2026-10-06 讨论）。
