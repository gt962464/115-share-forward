FROM listeningltg/p115-share:latest

# 安装额外依赖（覆盖 p115-share 的基础镜像）
RUN pip install --no-cache-dir \
    python-telegram-bot[job-queue] \
    telethon \
    aiohttp \
    aiohttp-socks \
    python-dotenv

# 创建数据目录
RUN mkdir -p /data /cardbot

# 复制 bot 代码到 /cardbot
# p115-share 的代码在 /app，我们的代码在 /cardbot
COPY main.py /cardbot/main.py
COPY config.py /cardbot/config.py
COPY link_parser.py /cardbot/link_parser.py
COPY pipeline.py /cardbot/pipeline.py
COPY notifier.py /cardbot/notifier.py
COPY monitor.py /cardbot/monitor.py
COPY identifier.py /cardbot/identifier.py

WORKDIR /cardbot
ENTRYPOINT []
CMD ["python", "-u", "main.py"]

