# MPP 后端 API 文档

本文档基于 `sau_backend/sau_backend.py` 当前代码整理，作为后端接口的单一事实来源。

## 基础信息

- Base URL: `http://localhost:5409`
- 默认返回格式: `application/json`
- 文件下载/预览接口返回二进制流
- 登录接口 `GET /login` 返回 `text/event-stream` (SSE)

## 通用响应结构

大多数 JSON 接口返回：

```json
{
  "code": 200,
  "msg": "success",
  "data": {}
}
```

说明：

- `code`: 业务状态码（部分接口与 HTTP 状态码不完全一致）
- `msg`: 提示信息
- `data`: 业务数据

## 平台 type 对照

| type | platform_key | 平台 |
|---|---|---|
| 1 | xiaohongshu | 小红书 |
| 2 | tencent | 视频号 |
| 3 | douyin | 抖音 |
| 4 | kuaishou | 快手 |
| 5 | tiktok | TikTok |
| 6 | instagram | Instagram |
| 7 | facebook | Facebook |
| 8 | bilibili | B站 |
| 9 | baijiahao | 百家号 |

---

## 1) 静态资源与页面

### GET `/`

- 用途: 返回前端 `index.html`（打包部署场景）

### GET `/assets/<filename>`

- 用途: 返回 `sau_backend/assets` 下静态资源

### GET `/favicon.ico`

- 用途: 返回 `assets/vite.svg`

### GET `/vite.svg`

- 用途: 返回 `assets/vite.svg`

---

## 2) 文件管理

### POST `/upload`

- Content-Type: `multipart/form-data`
- 表单字段:
  - `file` (必填): 上传文件
- 成功响应:

```json
{
  "code": 200,
  "msg": "File uploaded successfully",
  "data": "uuid_filename.ext"
}
```

- 备注: 参数缺失时 HTTP 400，但 JSON 内 `code` 仍可能为 `200`（历史行为）

### GET `/getFile`

- Query:
  - `filename` (必填): 服务器保存文件名（如 `uuid_filename.ext`）
- 响应: 文件流（预览/下载）

### POST `/uploadSave`

- Content-Type: `multipart/form-data`
- 表单字段:
  - `file` (必填): 上传文件
  - `filename` (可选): 自定义文件名（后端会保留原扩展名）
- 行为: 文件落盘后写入 `file_records`
- 成功响应:

```json
{
  "code": 200,
  "msg": "File uploaded and saved successfully",
  "data": {
    "filename": "xxx.mp4",
    "filepath": "uuid_xxx.mp4"
  }
}
```

### GET `/getFiles`

- 用途: 查询素材列表（`file_records`）
- 成功响应 `data` 为数组，每项包含:
  - `id`
  - `filename`
  - `filesize` (MB)
  - `upload_time`
  - `file_path`
  - `uuid` (后端根据 `file_path` 拆分得到)

### GET `/deleteFile`

- Query:
  - `id` (必填): 文件记录 ID
- 行为:
  - 删除物理文件（若存在）
  - 删除 `file_records` 记录

### GET `/getFileStats`

- 用途: 获取素材统计信息
- 成功响应:

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "size_stats": {
      "total_files": 10,
      "total_size_mb": 123.45,
      "avg_size_mb": 12.35,
      "max_size_mb": 30.12
    },
    "recent_files": []
  }
}
```

---

## 3) 账号管理

### GET `/login` (SSE)

- Query:
  - `type` (必填): 平台 type
  - `id` (必填): 账号名
- 响应类型: `text/event-stream`
- 数据格式:

```text
data: xxx

data: yyy
```

- 说明: 用于触发统一登录流程并实时推送状态

### POST `/updateUserinfo`

- Body(JSON):

```json
{
  "id": 1,
  "type": 3,
  "userName": "account_name"
}
```

- 用途: 更新账号基础信息

### GET `/getAccounts`

- 用途: 快速获取账号列表（不做 cookie 实时校验）
- 成功响应 `data` 为二维数组（与数据库字段顺序一致）:

```json
[
  [1, 3, "cookie_file.json", "userA", 1]
]
```

### GET `/getValidAccounts`

- Query:
  - `type` (可选): 平台 type，默认 `0`（全部）
- 用途:
  - 并发校验 cookie 有效性
  - 同步更新 `user_info.status`
  - 返回账号二维数组

### POST `/uploadCookie`

- Content-Type: `multipart/form-data`
- 表单字段:
  - `file` (必填): `.json` cookie 文件
  - `id` (必填): 账号 ID
  - `platform` (必填): 平台标识（该字段仅校验存在性）
- 用途: 覆盖对应账号 cookie 文件

### GET `/downloadCookie`

- Query:
  - `filePath` (必填): cookie 相对路径/文件名
- 响应: cookie 文件下载流
- 安全: 后端做了路径越界校验

### GET `/getPlatformHomepage`

- Query:
  - `id` (必填): 账号 ID
- 用途:
  - 读取账号 cookie
  - 启动本地 Chrome
  - 访问平台个人中心页面
- 成功响应 `data`:

```json
{
  "platform": "douyin",
  "personal_url": "https://creator.douyin.com/creator-micro/home",
  "page_title": "页面标题"
}
```

### GET `/deleteAccount`

- Query:
  - `id` (必填): 账号 ID
- 用途: 删除账号及关联资源（调用 `myUtils.login.delete_account`）

### GET `/getPlatformStats`

- 用途: 获取平台账号统计 + 总体统计
- 成功响应 `data`:
  - `platform_stats`: 按平台聚合数量
  - `overall.total_accounts`
  - `overall.valid_accounts`
  - `overall.total_files`

---

## 4) 发布任务记录管理

### GET `/getPublishTaskRecords`

- Query:
  - `page` (可选, 默认 1)
  - `page_size` (可选, 默认 10)
  - `status` (可选)
  - `platform_name` (可选)
  - `account_name` (可选, 模糊匹配)
  - `filename` (可选, 模糊匹配)
- 成功响应:

```json
{
  "code": 200,
  "msg": "获取发布任务记录成功",
  "data": {
    "records": [
      {
        "id": 1,
        "taskId": "uuid",
        "fileName": "demo.mp4",
        "fileId": "123",
        "accountId": "cookie_file.json",
        "accountName": "userA",
        "platformName": "douyin",
        "platformType": 3,
        "status": "发布成功",
        "createTime": "2026-01-01 12:00:00",
        "updateTime": "2026-01-01 12:01:00",
        "errorMsg": null
      }
    ],
    "total": 100,
    "page": 1,
    "pageSize": 10
  }
}
```

### POST `/updatePublishTaskStatus`

- Body(JSON):

```json
{
  "id": 1,
  "status": "发布失败",
  "errorMsg": "可选"
}
```

- 用途: 手动更新任务状态

### POST `/retryPublishTask`

- Body(JSON):

```json
{
  "id": 1
}
```

- 用途: 将指定任务状态重置为 `发布中`

### POST `/cancelPublishTask`

- Body(JSON):

```json
{
  "id": 1
}
```

- 用途: 取消任务（仅允许当前状态 `发布中` 或 `待发布`）

### POST `/deletePublishTask`

- Body(JSON):

```json
{
  "id": 1
}
```

- 用途: 删除任务记录

---

## 5) 发布执行

### POST `/postVideo`

- Content-Type: `application/json`
- 主要字段:
  - `type` (必填): 平台 type
  - `accountList` (必填): 账号数组（支持对象或字符串）
  - `fileType` (可选): `1` 图文, `2` 视频
  - `fileList` (必填): 文件数组（支持对象或字符串）
  - `title` `text` `tags` `category` `thumbnail` `location`
  - `enableTimer` `videosPerDay` `dailyTimes` `startDays`
- 行为: 创建任务记录 -> 执行发布 -> 按结果回写状态
  - 当 `fileType=1` 且平台为 `douyin`/`kuaishou`/`xiaohongshu` 且 `fileList` 有多张图片时：按单次请求多图合并为一条作品发布

### POST `/postVideosToMultiplePlatforms`

- Content-Type: `application/json`
- 主要字段:
  - `platforms` (必填): 平台 key 数组
  - `accountFiles` (必填): `{platformKey: [accountFile...]}`
  - `fileType` `files` `title` `text` `tags`
  - `thumbnail` `location`
  - `enableTimer` `videosPerDay` `dailyTimes` `startDays`
- 行为:
  - 自动过滤账号与平台类型不匹配的数据
  - 批量创建任务记录
  - 调用多平台批量发布并回写状态
  - 当 `fileType=1` 且某平台为 `douyin`/`kuaishou`/`xiaohongshu` 且 `files` 有多张图片时：该平台按单次请求多图合并为一条作品发布

---

## 6) 常见错误码

- HTTP `200`: 请求成功
- HTTP `400`: 参数错误/状态不允许
- HTTP `404`: 资源不存在
- HTTP `500`: 服务端异常

注意：部分旧接口在 HTTP `4xx/5xx` 时，响应体中的 `code` 字段仍可能是 `200` 或 `500`，调用方应优先判断 HTTP 状态码。

---

## 7) 当前未实现接口（避免误用）

以下接口在当前 `sau_backend.py` 中未定义路由：

- `/account`
- `/taskStatus`
- `/platformConfig`
- `/reLogin`

如需这些接口，请先补充后端实现再在前端接入。
