# sau_backend

后端服务使用 Flask 提供 API，使用 Playwright 完成各平台登录、Cookie 校验和自动发布。

## 目录说明

```text
sau_backend/
├── conf.py
├── sau_backend.py
├── myUtils/
├── newFileUpload/
└── utils/
```

## 运行要求

- Python `3.10`
- `uv`
- Chrome / Chromium，或执行 `uv run playwright install chromium`

Linux 常见依赖：

```bash
sudo apt-get update
sudo apt-get install -y libnss3 libnspr4 libasound2
```

## 初始化

在项目根目录执行：

```bash
uv sync
uv run python db/createTable.py
```

然后编辑 `conf.py`：

- `LOCAL_CHROME_PATH` 为空时，默认使用 Playwright 自带浏览器
- 如果你要使用本机 Chrome / Chromium，填入绝对路径

示例：

```python
LOCAL_CHROME_PATH = "/usr/bin/google-chrome"
```

## 启动

在项目根目录执行：

```bash
uv run python sau_backend/sau_backend.py
```

服务地址：`http://localhost:5409`

## 数据库

- 数据库路径固定为 `db/database.db`
- 后端启动时会自动补齐缺失表
- 重新初始化可执行：

```bash
rm -f db/database.db
uv run python db/createTable.py
```

## 接口概览

- `GET /getFiles`
- `POST /upload`
- `GET /getAccounts`
- `GET /getValidAccounts`
- `POST /postVideo`
- `POST /postVideosToMultiplePlatforms`
- `GET /getPublishTaskRecords`

更完整的运行方式和前后端联调方式见项目根目录 `README.md`。
