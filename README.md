# DL 短链（dl-system）

自建短链服务：长链接 → 六位短码 → 302 跳转。Docker Compose 四容器编排，从零构建的云原生学习项目（SRE 方向），按生产形态组织、分阶段演进。

## 架构

```
                          ┌────────────────────────────────────┐
   浏览器                  │            Docker Compose          │
     │                    │                                     │
     │ http://host:8088   │  ┌─────────┐    ┌─────────────┐    │
     └───────────────────►│  │  nginx   │───►│  web        │    │
                          │  │  (80)    │    │  (FastAPI)  │    │
                          │  │          │    │  uvicorn    │    │
                          │  │ 静态前端  │    └──────┬──────┘    │
                          │  │ API 转发  │           │           │
                          │  │ 短码兜底  │           ▼           │
                          │  └─────────┘    ┌─────────────┐    │
                          │                 │ mysql-master │    │
                          │   ┌────────┐    │  (links 表)  │    │
                          │   │ redis  │    └─────────────┘    │
                          │   │(阶段2) │                       │
                          │   └────────┘                       │
                          └────────────────────────────────────┘
```

- **nginx**：唯一对外入口。`/api/` 转发 web；`/` 静态前端 + `try_files` 兜底（未知路径当短码甩给 web）；`=/health` 纯自探，`=/ready` 转发给 web 做深体检
- **web**：FastAPI + uvicorn。`POST /api/shorten` 生成 base62 六位码（撞码重试），`GET /{code}` 查库 302；`/health` 浅体检（不碰库），`/ready` 深体检（`SELECT 1`，失败返回 503）
- **mysql**：官方镜像 8.0，命名卷持久化，`MYSQL_DATABASE` 首启建库，应用层幂等建表
- **redis**：阶段 2 缓存进场（miss→hit + INCR 访问计数）

## 快速起步

```bash
# 1. 准备环境变量（密码自己定，本文件不入库）
cp .env.example .env   # 或手写：MYSQL_ROOT_PASSWORD=你的密码

# 2. 一键起栈（含构建 web 镜像）
docker compose up -d --build

# 3. 验收
curl -X POST http://localhost:8088/api/shorten \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/a/very/long/path"}'
# → {"code":"xxxxxx"}
curl -I http://localhost:8088/xxxxxx
# → 302 Found + Location: 原始长链
curl -s http://localhost:8088/ready
# → {"status":"ok"}（深体检：查库；库挂了返回 503）
```

浏览器打开 `http://localhost:8088/` 有现成前端页面（生成 / 复制 / 本地历史）。

## 目录结构

```
dl-system/
├── compose.yml          # 四容器编排（体检分级 + 依赖门禁）
├── .env.example         # 环境变量模板（真实 .env 已被 .gitignore 挡住）
├── frontend/index.html  # 单文件前端，零依赖，bind mount 进 nginx
├── nginx/default.conf   # 反代 + 静态 + 短码兜底
└── web/
    ├── main.py          # FastAPI 应用
    ├── requirements.txt
    ├── Dockerfile       # slim 基础镜像 + 分层缓存友好的依赖顺序
    └── .dockerignore
```

## 设计要点

- **体检分级**：所有 compose healthcheck 都是"浅体检"（只证自己活着，不探下游）——门禁体检若走全链路会死锁（下游未起 → 上游永远 unhealthy）。
  对外另开 `/ready` 深体检：会真查一次库，失败返回 **503**（503=依赖不可用，500=代码有 bug）。
  compose 里刻意**不**把 healthcheck 指向 `/ready`——那会把门禁重新变成死锁；它留给人和监控，将来直接当 K8s 的 readinessProbe
- **依赖门禁**：`nginx depends_on web(healthy)`、`web depends_on mysql(healthy)`——依赖图决定启动时序
- **密码单一真相源**：`.env` 一处定义，mysql 与 web 经 `${}` 同源引用；`compose config` 可验证插值
- **层缓存友好**：`requirements.txt` 先拷先装、代码后拷——改代码只重建 COPY 层，依赖层缓存命中
- **bind mount 用法**：nginx conf 挂单文件 `:ro`（不自改写的服务才敢挂单文件）；前端挂目录实时生效，改页面无需重建
- **单文件前端**：XSS 防线=用户数据只走 `textContent`；非安全上下文的剪贴板降级（http://IP 访问时 execCommand 兜底）

## 路线图

- [x] 阶段 1：四容器 MVP（生成短码 / 302 跳转 / 落库 / 浏览器前端）
- [x] 阶段 2：Redis 缓存（cache-aside、TTL 防雪崩、空值防穿透、熔断降级、INCR 计数）
- [x] 阶段 3：上云（阿里云 ECS 2C2G、安全组分层、资源限制、密码 .env 化、备份 cron、reboot 自愈演练通过）
- [x] 阶段 4：Prometheus + Grafana 监控（八容器、/metrics 埋点、SSH 隧道看板）
- [ ] 二期：域名 + HTTPS（备案中）
- [ ] 远期：K8s 版（大二下）→ Go 重写版（CKA 后）

---

学习项目，按"项目不是课程的作业，课程是项目的工具箱"推进。欢迎 issue。
