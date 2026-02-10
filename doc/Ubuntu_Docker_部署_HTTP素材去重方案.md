# MediaPublishPlatform Ubuntu Docker 部署文档（HTTP 素材下载 + 去重）

本文档基于当前项目代码实现，目标是：

- 在 Ubuntu 服务器通过 Docker 部署 `MediaPublishPlatform`
- 使用已有 Nginx 暴露的 `/home/image` HTTP 资源作为素材来源
- 发布接口传入 HTTP 地址时，后端自动下载到本地素材库
- 相同 URL 自动去重，不重复下载

---

## 1. 当前方案说明（与代码对齐）

后端新增了以下逻辑（文件：`sau_backend/sau_backend.py`）：

- 识别 `http://` / `https://` 文件地址
- 下载远程文件到 `BASE_DIR/videoFile`
- 写入 `file_records`，并记录 `source_url`
- 再次传入同一 URL 时：
  - 若数据库中已有 `source_url` 且本地文件仍存在，直接复用，不重复下载
  - 若记录存在但本地文件丢失，则重新下载并写入新记录

发布时实际上传文件路径仍是本地文件路径（Playwright `set_files`），因此流程稳定。

---

## 2. 部署架构

- `app` 容器：
  - Flask 后端（端口 `5409`）
  - 打包后的前端静态资源（由 Flask 提供）
  - Chromium（供 Playwright 上传流程使用）
- 宿主机 Nginx：
  - 对外提供 `/image/`（映射 `/home/image/`）
  - 反向代理应用到 `127.0.0.1:5409`

---

## 3. Ubuntu 环境准备

```bash
sudo apt update
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

docker --version
docker compose version
```

---

## 4. 拉取项目与目录初始化

```bash
sudo mkdir -p /opt/mpp
sudo chown -R $USER:$USER /opt/mpp
cd /opt/mpp

git clone <你的仓库地址> MediaPublishPlatform
cd MediaPublishPlatform

mkdir -p runtime/db runtime/videoFile runtime/cookiesFile docker
```

---

## 5. 修改后端配置

编辑 `sau_backend/conf.py`，确保 Chromium 路径可在容器中使用：

```python
LOCAL_CHROME_PATH = "/usr/bin/chromium"
LOCAL_CHROME_HEADLESS = True
```

说明：项目上传器读取 `LOCAL_CHROME_PATH` / `LOCAL_CHROME_HEADLESS`。

---

## 6. 新建 Dockerfile

创建 `docker/Dockerfile.app`：

```dockerfile
FROM node:22 AS web-builder
WORKDIR /web
COPY sau_frontend/package*.json ./
RUN npm ci
COPY sau_frontend/ .
RUN npm run build

FROM python:3.10-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    curl \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libxkbcommon0 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN iconv -f UTF-16 -t UTF-8 /tmp/requirements.txt > /tmp/requirements.utf8.txt \
    && pip install --no-cache-dir -r /tmp/requirements.utf8.txt

COPY . .

# 前端产物放到 sau_backend 下，Flask 会从该目录提供 index/assets
COPY --from=web-builder /web/dist/index.html /app/sau_backend/index.html
COPY --from=web-builder /web/dist/assets /app/sau_backend/assets
COPY --from=web-builder /web/dist/vite.svg /app/sau_backend/assets/vite.svg

RUN mkdir -p /app/db /app/videoFile /app/cookiesFile

EXPOSE 5409
CMD ["python3", "sau_backend/sau_backend.py"]
```

---

## 7. 新建 Docker Compose

创建 `docker-compose.yml`：

```yaml
services:
  app:
    build:
      context: .
      dockerfile: docker/Dockerfile.app
    container_name: mpp-app
    ports:
      - "5409:5409"
    volumes:
      - ./runtime/db:/app/db
      - ./runtime/videoFile:/app/videoFile
      - ./runtime/cookiesFile:/app/cookiesFile
    restart: unless-stopped
    shm_size: "1gb"
```

---

## 8. 宿主机 Nginx 配置示例

确保你已有 `/image/` 暴露（映射 `/home/image/`），并反代应用：

```nginx
server {
    listen 80;
    server_name _;

    location /image/ {
        alias /home/image/;
        autoindex off;
    }

    location / {
        proxy_pass http://127.0.0.1:5409;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # /login 使用 SSE，禁用缓冲
    location /login {
        proxy_pass http://127.0.0.1:5409/login;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
        proxy_set_header Connection "";
    }
}
```

---

## 9. 首次启动

```bash
docker compose build
docker compose run --rm app python3 db/createTable.py
docker compose up -d

docker compose ps
docker compose logs -f app
```

---

## 10. API 调用示例（HTTP 素材地址）

### 10.1 单平台发布 `/postVideo`

```bash
curl -X POST "http://<你的域名或IP>/postVideo" \
  -H "Content-Type: application/json" \
  -d '{
    "type": 1,
    "accountList": ["demo_account.json"],
    "fileType": 2,
    "fileList": [
      "http://<你的域名或IP>/image/26/sbc/aaa.jpg"
    ],
    "title": "demo",
    "text": "demo"
  }'
```

### 10.2 多平台发布 `/postVideosToMultiplePlatforms`

```bash
curl -X POST "http://<你的域名或IP>/postVideosToMultiplePlatforms" \
  -H "Content-Type: application/json" \
  -d '{
    "platforms": ["xiaohongshu", "douyin"],
    "accountFiles": {
      "xiaohongshu": ["xhs_001.json"],
      "douyin": ["dy_001.json"]
    },
    "fileType": 2,
    "files": [
      "http://<你的域名或IP>/image/26/sbc/aaa.jpg"
    ],
    "title": "demo",
    "text": "demo"
  }'
```

---

## 11. 去重规则（重点）

- 仅当值是完整 `http://` 或 `https://` URL 才会触发下载逻辑
- `http:123/image/...` 这种不算合法 URL，不会触发下载
- 去重键：`file_records.source_url`（URL 字符串精确匹配）
- 相同 URL 再次传入：
  - 文件存在：复用已有 `file_path`
  - 文件不存在：重新下载

### 11.1 你的例子

传入：`http://123/image/26/sbc/aaa.img`

- 素材库记录：
  - `filename = aaa.img`
  - `file_path = <uuid>_aaa.img`
  - `source_url = http://123/image/26/sbc/aaa.img`
- 平台上传时读取：`videoFile/<uuid>_aaa.img`

---

## 12. 数据库变更说明

当前方案为 `file_records` 增加列：

- `source_url TEXT`（用于 HTTP 素材去重）

并创建索引：

- `idx_file_records_source_url`

代码里带自动兼容逻辑：旧库启动时会自动补列和索引。

---

## 13. 常见问题

### Q1: 为什么传了 URL 没下载？

- 检查是否是 `http://` / `https://` 开头
- 检查容器是否能访问该 URL（网络可达、权限、证书）

### Q2: 为什么还是重复下载？

- 去重按 URL 字符串精确匹配
- `?v=1`、`?v=2` 会被视为不同 URL

### Q3: `.img` 能发吗？

- `.img` 不是常见图片扩展名，部分平台可能不接受
- 建议使用 `.jpg/.jpeg/.png/.webp` 或标准视频扩展名

---

## 14. 运行检查命令

```bash
# 查看服务状态
docker compose ps

# 查看后端日志
docker compose logs -f app

# 查看素材记录
curl "http://127.0.0.1:5409/getFiles"

# 查看本地素材目录
ls -lh runtime/videoFile
```
