FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tg_user_monitor.py pan123_link_parser.py ./

CMD ["python", "-u", "tg_user_monitor.py"]
