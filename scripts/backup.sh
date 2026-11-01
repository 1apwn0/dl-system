#!/usr/bin/env bash
# dl-system 数据库备份三步曲：dump → gzip → 清理过期
# 用法：./scripts/backup.sh （上服务器后由 cron 每天调用，本地可手动跑）
# 恢复方法：gunzip < backups/dl-XXXX.sql.gz | docker compose exec -T mysql-master mysql -uroot -p"密码"
set -euo pipefail
cd "$(dirname "$0")/.."

# 密码从 .env 读，不落脚本
source .env
: "${MYSQL_ROOT_PASSWORD:?请在 .env 里配置 MYSQL_ROOT_PASSWORD}"

STAMP=$(date +%F-%H%M)
mkdir -p backups

# -T 关掉 tty 分配才能接管道；--databases 带 CREATE DATABASE 语句，恢复时自动建库
docker compose exec -T mysql-master \
  mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --databases dl \
  | gzip > "backups/dl-$STAMP.sql.gz"

# 只留最近 7 天，磁盘不养坟头
find backups -name 'dl-*.sql.gz' -mtime +7 -delete

echo "[backup] backups/dl-$STAMP.sql.gz 完成（$(du -h "backups/dl-$STAMP.sql.gz" | cut -f1)）"
