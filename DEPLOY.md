# DEPLOY.md · medical-record-agent 部署与运维手册

## 部署信息

| 项 | 值 |
|---|---|
| 项目名 | medical-record-agent |
| 来源仓库 | https://github.com/1739467001-svg/Medical-Record-Agent-glm5.3flash.git（main，r299d01b） |
| 服务器 | 60.205.204.162（阿里云 Ubuntu 24.04，2C/2G） |
| 目录 | `/srv/apps/medical-record-agent/`（`repo/` 代码 · `conf/` 容器 nginx 配置 · `docker-compose.yml`） |
| 宿主端口 | **127.0.0.1:3904** → 容器 8080（nginx:1.27-alpine，镜像来自 public.ecr.aws，已本地存在） |
| 公网访问 | **http://60.205.204.162/mra/**（经宿主 nginx 反代，端口不对公网开放） |
| 导航页 | 已登记 `directory_public=true`（display_name：AI 病历智能体 · 医生工作台 Demo） |
| 资源限制 | mem 96m（实测 ~9MiB）· cpus 0.5 · 日志 json-file 1m×3 · restart unless-stopped · 健康检查 30s |
| LLM API 服务 | mra-llm-api 容器（python:3.12-alpine，ECR 源），**127.0.0.1:3905**→8090，公网经 `/mra/api/` 反代；密钥存 `/srv/apps/medical-record-agent/.llm_env`（chmod 600，不入 Git）；限流 6 次/分；数据源为脱敏 patients.json |
| AI 功能 | 患者全景页「⚡ AI 病例总结」：DeepSeek-v4-flash 实时生成五节病例总结（入院主诉诊断/治疗措施/异常检验/住院经过/综合分析），AI 草稿须经医生核对 |
| 认证服务 | 同 mra-llm-api 容器内（`server/auth.py`，`/mra/api/auth/*`）：登录/注册/会话/新手引导标记；密码 PBKDF2-HMAC-SHA256（200k 轮）+ 随机盐；会话 HttpOnly Cookie 7 天；存储 MySQL（生产）/SQLite（本地研发自动降级 `server/local_dev.db`，不入 Git） |
| 数据库 | 生产 MySQL：连接参数存 `.llm_env`（MRA_MYSQL_HOST/PORT/USER/PASSWORD/DB，chmod 600）；表 users/sessions 启动自动建表；限流 20 次/分/IP |

## 日常操作（服务器上）

```bash
cd /srv/apps/medical-record-agent

# 日志查看
docker logs --tail 100 medical-record-agent
docker logs -f medical-record-agent

# 更新（拉新代码即生效，静态卷挂载无需重启；改了 compose/conf 才需要 restart）
cd repo && git pull && cd ..
docker compose restart medical-record-agent   # 可选
docker compose up -d                          # 配置变更时

# 健康检查
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3904/index.html   # 期望 200
docker inspect --format "{{.State.Health.Status}}" medical-record-agent   # 期望 healthy

# 回滚到指定版本
cd repo && git fetch --depth 20 && git checkout <commit> && cd .. && docker compose restart
# 恢复 nginx 路由（如改坏）
cp /etc/nginx/sites-available/hackathon.before-mra /etc/nginx/sites-available/hackathon && nginx -t && systemctl reload nginx

# 备份（静态站，本质=代码版本本身；配置备份如下）
tar czf /root/backup-mra-$(date +%F).tgz /srv/apps/medical-record-agent/{docker-compose.yml,conf} /etc/nginx/sites-available/hackathon
```

## 更新全流程（本地 → GitHub → 服务器）

```bash
# 本地：改动 → 提交推送
git add -A && git commit -m "..." && git push
# 服务器：
cd /srv/apps/medical-record-agent/repo && git pull
```

### ⚠️ 2026-09-28 同步状态备注（服务器到 GitHub 网络中断期间的处置）

服务器网络连 GitHub 不稳定，r4d9daa0 的两个文件已用 SFTP 手工同步（内容与 Git 完全一致，在 repo 中为 untracked）：
- `repo/server/llm_api.py`、`repo/demo/js/app.js`

**下次 `git pull` 恢复正常时**，如报 "untracked working tree file would be overwritten"，执行：
```bash
cd /srv/apps/medical-record-agent/repo
rm server/llm_api.py demo/js/app.js && git pull   # pull 回的内容与手工放置的完全一致
docker compose restart medical-record-agent llm-api
```

### MySQL（认证数据库 · 首次部署一次性操作）

```bash
# 服务器上安装并初始化（Ubuntu 24.04）
sudo apt install -y mysql-server && sudo systemctl enable --now mysql
sudo mysql -e "CREATE DATABASE IF NOT EXISTS medical_record_agent DEFAULT CHARSET utf8mb4;
  CREATE USER IF NOT EXISTS 'mra'@'%' IDENTIFIED WITH mysql_native_password BY '<强密码>';
  GRANT ALL PRIVILEGES ON medical_record_agent.* TO 'mra'@'%'; FLUSH PRIVILEGES;"
# users/sessions 表由 auth.py 启动时自动建表，无需手工 DDL

# 连接参数追加到 .llm_env（chmod 600，不入 Git），llm-api 容器读取
cat >> /srv/apps/medical-record-agent/.llm_env <<EOF
MRA_MYSQL_HOST=host.docker.internal
MRA_MYSQL_PORT=3306
MRA_MYSQL_USER=mra
MRA_MYSQL_PASSWORD=<强密码>
MRA_MYSQL_DB=medical_record_agent
EOF
chmod 600 /srv/apps/medical-record-agent/.llm_env
```

- 容器访问宿主 MySQL：docker-compose.yml 的 llm-api 服务需加 `extra_hosts: ["host.docker.internal:host-gateway"]`，改后 `docker compose up -d`。
- MySQL 8 默认认证插件 caching_sha2_password 需要额外 C 依赖，故用户以 `mysql_native_password` 创建（见上）。
- 未配置 `MRA_MYSQL_HOST` 时自动降级 SQLite（`server/local_dev.db`，仅研发用，不入 Git）。

## 边界与注意

- 数据安全：容器内仅挂载 `demo/`（脱敏数据），**不含任何真实病历**；原始数据永不进入本服务器与 Git。
- 端口 3904 已登记 `/srv/apps/registry.json` reserved_ports，绑定 127.0.0.1；公网仅 80 端口经 nginx 子路径 `/mra/` 暴露，云防火墙未新增放行端口。
- 共存约束：不占用 3000（hackathon）、3001（kaitu）、3901/3902/3903，不动 `/projects/` 与 `/cloud-directory/`。
- HTTPS：暂未配置（服务器现状 HTTP）；如需 HTTPS/域名，属公网访问范围变更，需另行授权。
