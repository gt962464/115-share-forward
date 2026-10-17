import os
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# Database path
# 🔧 优先使用挂载盘：容器 WORKDIR 是 /cardbot，相对路径会落到镜像层
# （/cardbot/data/p115share.db），容器一重建队列就清空。改用挂载的 /data。
# 可用 P115_DB_PATH 显式覆盖；本地/无挂载场景回退相对路径。
_ENV_DB_PATH = os.environ.get("P115_DB_PATH")
if _ENV_DB_PATH:
    DB_PATH = _ENV_DB_PATH
elif os.path.isdir("/data") and os.access("/data", os.W_OK):
    DB_PATH = "/data/p115share.db"
else:
    DB_PATH = "data/p115share.db"

# Ensure data directory exists
_dbp_dir = os.path.dirname(DB_PATH)
if _dbp_dir:
    os.makedirs(_dbp_dir, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"timeout": 30},
)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    # WAL allows concurrent readers during writes and reduces lock contention.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

class Base(DeclarativeBase):
    pass

async def get_db():
    async with async_session() as session:
        yield session
