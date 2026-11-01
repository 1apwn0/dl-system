"""DL 短链 —— FastAPI 应用层（阶段2：MySQL 存映射 + Redis cache-aside，302 跳转）

运维层注意：
  - /health 是给 compose healthcheck 用的浅体检：不查数据库。
    如果这里查库，mysql 没就绪时 web 永远 unhealthy → 门禁死锁。
    （深体检给监控，浅体检给编排器——分级哲学）
  - 连接参数全走环境变量，compose 里配服务名（DB_HOST=mysql）。
"""

import os
import random
import string

import time

import pymysql
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
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

# ---- 阶段2：Redis 缓存配置（cache-aside 模式）----
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    password=os.getenv("REDIS_PASSWORD", ""),   # 单机版密码第三件套：应用侧带钥匙
    decode_responses=True,   # 命令返回 str 而非 bytes，省得到处 .decode()
    # 超时必须有：redis 消失时池里的旧连接收不到 RST，没超时会卡到 TCP 重传（实测 13s）
    socket_connect_timeout=0.5,
    socket_timeout=0.5,
    # 失败要快：最多 2 次尝试、不退避。慢失败比失败更糟（实测默认重试+退避要 4.3s/次）
    retry=Retry(NoBackoff(), 1),
)

CACHE_TTL = 3600          # 基准有效期：1 小时后过期重新查库（防脏数据）
CACHE_TTL_JITTER = 300    # 抖动上限：同批 key 错开过期时间，防雪崩
NULL_TTL = 60             # 空值哨兵的短命 TTL：挡穿透，又不会压住后来创建的同码
COUNT_TTL = 30 * 86400    # 计数键 30 天没被访问就回收，防无限增长

# ---- 熔断器：redis 挂了就安静 BREAKER_SECONDS 秒，别让整个线程池排队等它 ----
_breaker_until = 0.0
BREAKER_SECONDS = 5


def redis_available():
    """熔断窗口内直接判定不可用，一次都不碰 redis。"""
    return time.monotonic() >= _breaker_until


def redis_unavailable():
    """记录失败时刻，拉开熔断窗口。"""
    global _breaker_until
    _breaker_until = time.monotonic() + BREAKER_SECONDS

# 短码字母表：大小写字母 + 数字，共 62 个字符，6 位 = 62^6 ≈ 568 亿种
ALPHABET = string.ascii_letters + string.digits
CODE_LEN = 6


def get_db(**overrides):
    """每次请求现连现断，MVP 够用；连接池是以后的事。

    overrides 让健康检查能单独收紧超时，不必动全局配置。
    """
    return pymysql.connect(**{**DB_CONFIG, **overrides})


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
            # 清掉可能存在的空值哨兵：否则这码被探测过后，新建的它会 404 满 NULL_TTL
            if redis_available():
                try:
                    redis_client.delete(f"link:{code}")
                except redis.exceptions.RedisError:
                    redis_unavailable()
            return {"code": code}
        except pymysql.err.IntegrityError:
            continue  # 撞码了，换一个重来
    raise HTTPException(status_code=500, detail="短码生成失败，请重试")


# 注意注册顺序：/health、/ready 都必须在 /{code} 之前——FastAPI 按注册顺序匹配，
# 否则体检流量会被 /{code} 当成短码吞掉去查库（反而制造 500）
@app.get("/health")
def health():
    # 浅体检：只证明"进程活着、能应答"，不碰数据库
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """深体检：证明"能对外服务"，会真的查一次库。

    分工：/health 给编排器（只证进程活着、不探下游，避免门禁死锁）；
          /ready 给人、给监控、将来给 K8s 的 readinessProbe。
    失败返回 503 而不是 500 —— 500 是"代码有 bug"，503 才是"依赖不可用"。
    """
    try:
        # connect_timeout 收紧到 3s：健康检查不该陪着 mysql 卡 10s
        with get_db(connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        print(f"[ready] 数据库不可用：{exc}")  # 细节进日志，别吐给公网
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "reason": "database"},
        )
    # redis 只报状态、不判生死：读路径已软降级，redis 挂了服务仍可用。
    # 若这里跟着 503，K8s readinessProbe 会把还能正常干活的实例全部摘流。
    cache_state = "ok"
    if not redis_available():
        cache_state = "degraded"
    else:
        try:
            redis_client.ping()
        except redis.exceptions.RedisError as exc:
            redis_unavailable()
            cache_state = "degraded"
            print(f"[ready] redis 不可用，服务降级运行：{exc}")
    return {"status": "ok", "cache": cache_state}


@app.get("/api/stats/{code}")
def stats(code: str):
    """短码访问统计：读 redis 的 INCR 计数器（前端历史列表显示"N 次"用）。
    count=None 表示 redis 不可用（前端显示 --），0 表示还没人点过。"""
    if not redis_available():
        return {"code": code, "count": None}
    try:
        n = redis_client.get(f"count:{code}")
    except redis.exceptions.RedisError:
        redis_unavailable()
        return {"code": code, "count": None}
    return {"code": code, "count": int(n or 0)}


def bump(code: str):
    """访问计数：每次跳转 +1。与 PGA 监控栈 loadgen 的 INCR 计数器同构。
    redis 挂了就丢这一次计数——计数是锦上添花，不能拖垮主流程。"""
    if not redis_available():
        return
    try:
        pipe = redis_client.pipeline()
        pipe.incr(f"count:{code}")
        pipe.expire(f"count:{code}", COUNT_TTL)   # 每次访问续命；30 天没人碰就回收
        pipe.execute()
    except redis.exceptions.RedisError:
        redis_unavailable()


@app.get("/{code}")
def redirect(code: str):
    # ---- 阶段2：cache-aside 读路径（便签 → 通讯录 → 回填）----
    # ① 便签优先：命中直接跳，mysql 全程无感（这就是 hit）
    cached = None
    if redis_available():
        try:
            cached = redis_client.get(f"link:{code}")
        except redis.exceptions.RedisError:
            # 软降级 + 熔断：缓存是加速器不是命根，且接下来几秒谁都别再等它
            redis_unavailable()
    if cached is not None:      # 空串是"确认不存在"的哨兵，绝不能用真值判断
        if cached == "":
            raise HTTPException(status_code=404, detail="短码不存在")
        bump(code)
        return RedirectResponse(url=cached, status_code=302)

    # ② miss：翻通讯录（mysql）
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT url FROM links WHERE code = %s", (code,))
        row = cur.fetchone()
    if row is None:
        # 空值缓存防穿透：把"不存在"也记一笔，坏码不再每次打库
        if redis_available():
            try:
                redis_client.setex(f"link:{code}", NULL_TTL, "")
            except redis.exceptions.RedisError:
                redis_unavailable()
        raise HTTPException(status_code=404, detail="短码不存在")

    # ③ 回填便签（TTL 带抖动）：下次同样的短码就是 hit 了
    if redis_available():
        try:
            redis_client.setex(
                f"link:{code}", CACHE_TTL + random.randint(0, CACHE_TTL_JITTER), row[0]
            )
        except redis.exceptions.RedisError:
            redis_unavailable()

    bump(code)
    return RedirectResponse(url=row[0], status_code=302)
