"""DL 短链 —— FastAPI 应用层（阶段1：MySQL 存映射，302 跳转）

运维层注意：
  - /health 是给 compose healthcheck 用的浅体检：不查数据库。
    如果这里查库，mysql 没就绪时 web 永远 unhealthy → 门禁死锁。
    （深体检给监控，浅体检给编排器——分级哲学）
  - 连接参数全走环境变量，compose 里配服务名（DB_HOST=mysql）。
"""

import os
import random
import string

import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

app = FastAPI(title="DL 短链")

# ---- 配置：环境变量优先，默认值给本机调试用 ----
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "123"),
    "database": os.getenv("DB_NAME", "dl"),
    "charset": "utf8mb4",
}

# 短码字母表：大小写字母 + 数字，共 62 个字符，6 位 = 62^6 ≈ 568 亿种
ALPHABET = string.ascii_letters + string.digits
CODE_LEN = 6


def get_db():
    """每次请求现连现断，MVP 够用；连接池是以后的事。"""
    return pymysql.connect(**DB_CONFIG)


def init_table():
    """启动时建表（幂等）。MVP 简化：生产环境这活应该归 init.sql 或迁移工具。"""
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                code     VARCHAR(8)  PRIMARY KEY,   -- 短码做主键，天然防重复
                url      VARCHAR(2048) NOT NULL,     -- 原始长链
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        )
        # DDL 虽隐式提交，但保持和 shorten 同款写法：commit 永远在 with 块内
        conn.commit()


@app.on_event("startup")
def startup():
    # mysql 未进场时（两容器中间验收）预期发生：不炸进程，表等四容器阶段自然补上
    try:
        init_table()
    except Exception as exc:
        print(f"[startup] mysql 未就绪，跳过建表：{exc}")


class ShortenIn(BaseModel):
    url: str


@app.post("/api/shorten", status_code=201)
def shorten(body: ShortenIn):
    url = body.url.strip()
    # 手动校验：让错误原因说人话（pydantic 默认 422 的 detail 是数组，前端不好读）
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="链接必须以 http:// 或 https:// 开头")
    # 与数据库 VARCHAR(2048) 对齐：超长在这里拦成 400，别让 pymysql 报 Data too long 的 500
    if len(url) > 2048:
        raise HTTPException(status_code=400, detail="链接太长了（上限 2048 字符）")

    # 生成短码：随机抽 6 位；万一撞主键就换一个，最多试 3 次
    for _ in range(3):
        code = "".join(random.choices(ALPHABET, k=CODE_LEN))
        try:
            # pymysql 的 with conn 退出即关闭连接（不自动提交）——
            # commit 必须写在 with 块内；写块外=对尸体操作，报 InterfaceError(0,'')
            with get_db() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO links (code, url) VALUES (%s, %s)", (code, url))
                conn.commit()
            return {"code": code}
        except pymysql.err.IntegrityError:
            continue  # 撞码了，换一个重来
    raise HTTPException(status_code=500, detail="短码生成失败，请重试")


# 注意注册顺序：/health 必须在 /{code} 之前——FastAPI 按注册顺序匹配，
# 否则体检流量会被 /{code} 当成短码 "health" 吞掉去查库（mysql 不在就 500）
@app.get("/health")
def health():
    # 浅体检：只证明"进程活着、能应答"，不碰数据库
    return {"status": "ok"}


@app.get("/{code}")
def redirect(code: str):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT url FROM links WHERE code = %s", (code,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="短码不存在")
    # 阶段2 这里会先查 Redis 缓存，miss 才落库；阶段3 加访问计数
    return RedirectResponse(url=row[0], status_code=302)
