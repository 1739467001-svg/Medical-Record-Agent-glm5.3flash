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

## 边界与注意

- 数据安全：容器内仅挂载 `demo/`（脱敏数据），**不含任何真实病历**；原始数据永不进入本服务器与 Git。
- 端口 3904 已登记 `/srv/apps/registry.json` reserved_ports，绑定 127.0.0.1；公网仅 80 端口经 nginx 子路径 `/mra/` 暴露，云防火墙未新增放行端口。
- 共存约束：不占用 3000（hackathon）、3001（kaitu）、3901/3902/3903，不动 `/projects/` 与 `/cloud-directory/`。
- HTTPS：暂未配置（服务器现状 HTTP）；如需 HTTPS/域名，属公网访问范围变更，需另行授权。
