import asyncio
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from queue import Queue
from urllib.parse import unquote, urlparse

import requests
from flask_cors import CORS
from conf import BASE_DIR, LOCAL_CHROME_PATH
from myUtils.auth import check_cookie
from flask import Flask, request, jsonify, Response, send_from_directory
from myUtils.login import run_unified_login, delete_account
from newFileUpload.multiFileUploader import (
    post_file,
    post_multiple_files_to_multiple_platforms,
)
from newFileUpload.platform_configs import (
    get_platform_key_by_type,
    get_type_by_platform_key,
    PLATFORM_CONFIGS,
)

active_queues = {}
app = Flask(__name__)
source_url_column_lock = threading.Lock()
source_url_column_ready = False

# 允许所有来源跨域访问
CORS(app)

# 限制上传文件大小为160MB
app.config["MAX_CONTENT_LENGTH"] = 160 * 1024 * 1024

# 获取当前目录（假设 index.html 和 assets 在这里）
current_dir = os.path.dirname(os.path.abspath(__file__))


# 处理所有静态资源请求（未来打包用）
@app.route("/assets/<filename>")
def custom_static(filename):
    return send_from_directory(os.path.join(current_dir, "assets"), filename)


# 处理 favicon.ico 静态资源（未来打包用）
@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(current_dir, "assets"), "vite.svg")


@app.route("/vite.svg")
def vite_svg():
    return send_from_directory(os.path.join(current_dir, "assets"), "vite.svg")


# （未来打包用）
@app.route("/")
def index():  # put application's code here
    return send_from_directory(current_dir, "index.html")


def is_http_file_url(file_value):
    if not isinstance(file_value, str):
        return False

    candidate = file_value.strip()
    return candidate.startswith("http://") or candidate.startswith("https://")


def get_file_value_from_item(file_item):
    if isinstance(file_item, str):
        return file_item

    if isinstance(file_item, dict):
        for key in ("fileName", "filename", "file_path", "filepath", "path", "url"):
            value = file_item.get(key)
            if value:
                return value

    raise ValueError("文件项格式错误，必须是字符串或包含 fileName/path/url 的对象")


def download_http_file_to_material(file_url):
    ensure_file_records_source_url_column()

    normalized_url = file_url.strip()
    if not normalized_url:
        raise ValueError("文件URL不能为空")

    with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT file_path FROM file_records WHERE source_url = ? ORDER BY id DESC LIMIT 1",
            (normalized_url,),
        )
        existing = cursor.fetchone()
        if existing:
            existing_file_path = existing["file_path"]
            if existing_file_path:
                existing_file = Path(BASE_DIR / "videoFile" / existing_file_path)
                if existing_file.exists():
                    return existing_file_path

    parsed = urlparse(normalized_url)
    source_filename = Path(unquote(parsed.path)).name
    if not source_filename:
        source_filename = f"remote_{uuid.uuid4().hex}.bin"

    source_filename = source_filename.replace("\x00", "")
    if not source_filename:
        source_filename = f"remote_{uuid.uuid4().hex}.bin"

    uuid_v1 = uuid.uuid1()
    final_filename = f"{uuid_v1}_{source_filename}"
    video_dir = Path(BASE_DIR / "videoFile")
    video_dir.mkdir(parents=True, exist_ok=True)
    file_path = video_dir / final_filename

    try:
        with requests.get(normalized_url, stream=True, timeout=120) as response:
            response.raise_for_status()
            with open(file_path, "wb") as file_stream:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file_stream.write(chunk)
    except Exception as e:
        if file_path.exists():
            file_path.unlink()
        raise ValueError(f"下载远程文件失败: {normalized_url}, 错误: {str(e)}")

    file_size_mb = round(float(os.path.getsize(file_path)) / (1024 * 1024), 2)
    with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
                INSERT INTO file_records (filename, filesize, file_path, source_url)
                VALUES (?, ?, ?, ?)
            """,
            (source_filename, file_size_mb, final_filename, normalized_url),
        )
        conn.commit()

    return final_filename


def prepare_publish_files(file_items):
    if not isinstance(file_items, list):
        raise ValueError("fileList/files 必须是数组")

    normalized_files = []
    url_download_cache = {}

    for item in file_items:
        file_value = get_file_value_from_item(item)

        if is_http_file_url(file_value):
            normalized_url = file_value.strip()
            if normalized_url not in url_download_cache:
                url_download_cache[normalized_url] = download_http_file_to_material(
                    normalized_url
                )
            normalized_files.append(url_download_cache[normalized_url])
        else:
            normalized_files.append(file_value)

    return normalized_files


def ensure_file_records_source_url_column():
    global source_url_column_ready

    if source_url_column_ready:
        return

    with source_url_column_lock:
        if source_url_column_ready:
            return

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(file_records)")
            columns = {row[1] for row in cursor.fetchall()}

            if "source_url" not in columns:
                try:
                    cursor.execute(
                        "ALTER TABLE file_records ADD COLUMN source_url TEXT"
                    )
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise

            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_records_source_url ON file_records(source_url)"
            )
            conn.commit()

        source_url_column_ready = True


###################################################文件管理（媒体素材和账号cookie）#############################################
@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify(
            {"code": 200, "data": None, "msg": "No file part in the request"}
        ), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"code": 200, "data": None, "msg": "No selected file"}), 400
    try:
        # 保存文件到指定位置
        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")
        filepath = Path(BASE_DIR / "videoFile" / f"{uuid_v1}_{file.filename}")
        file.save(filepath)
        return jsonify(
            {
                "code": 200,
                "msg": "File uploaded successfully",
                "data": f"{uuid_v1}_{file.filename}",
            }
        ), 200
    except Exception as e:
        return jsonify({"code": 200, "msg": str(e), "data": None}), 500


@app.route("/getFile", methods=["GET"])
def get_file():
    # 获取 filename 参数
    filename = request.args.get("filename")

    if not filename:
        return {"error": "filename is required"}, 400

    # 防止路径穿越攻击
    if ".." in filename or filename.startswith("/"):
        return {"error": "Invalid filename"}, 400

    # 拼接完整路径
    file_path = str(Path(BASE_DIR / "videoFile"))

    # 返回文件
    return send_from_directory(file_path, filename)


@app.route("/uploadSave", methods=["POST"])
def upload_save():
    if "file" not in request.files:
        return jsonify(
            {"code": 400, "data": None, "msg": "No file part in the request"}
        ), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"code": 400, "data": None, "msg": "No selected file"}), 400

    # 获取表单中的自定义文件名（可选）
    custom_filename = request.form.get("filename", None)
    if custom_filename:
        filename = custom_filename + "." + file.filename.split(".")[-1]
    else:
        filename = file.filename

    try:
        # 生成 UUID v1
        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")

        # 构造文件名和路径
        final_filename = f"{uuid_v1}_{filename}"
        filepath = Path(BASE_DIR / "videoFile" / f"{uuid_v1}_{filename}")

        # 保存文件
        file.save(filepath)

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                                INSERT INTO file_records (filename, filesize, file_path)
            VALUES (?, ?, ?)
                                """,
                (
                    filename,
                    round(float(os.path.getsize(filepath)) / (1024 * 1024), 2),
                    final_filename,
                ),
            )
            conn.commit()
            print("✅ 上传文件已记录")

        return jsonify(
            {
                "code": 200,
                "msg": "File uploaded and saved successfully",
                "data": {"filename": filename, "filepath": final_filename},
            }
        ), 200

    except Exception as e:
        print(f"Upload failed: {e}")
        return jsonify({"code": 500, "msg": f"upload failed: {e}", "data": None}), 500


@app.route("/getFiles", methods=["GET"])
def get_all_files():
    try:
        # 使用 with 自动管理数据库连接
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row  # 允许通过列名访问结果
            cursor = conn.cursor()

            # 查询所有记录
            cursor.execute("SELECT * FROM file_records")
            rows = cursor.fetchall()

            # 将结果转为字典列表，并提取UUID
            data = []
            for row in rows:
                row_dict = dict(row)
                # 从 file_path 中提取 UUID (文件名的第一部分，下划线前)
                if row_dict.get("file_path"):
                    file_path_parts = row_dict["file_path"].split(
                        "_", 1
                    )  # 只分割第一个下划线
                    if len(file_path_parts) > 0:
                        row_dict["uuid"] = file_path_parts[0]  # UUID 部分
                    else:
                        row_dict["uuid"] = ""
                else:
                    row_dict["uuid"] = ""
                data.append(row_dict)

            return jsonify({"code": 200, "msg": "success", "data": data}), 200
    except Exception as e:
        return jsonify({"code": 500, "msg": str("get file failed!"), "data": None}), 500


@app.route("/deleteFile", methods=["GET"])
def delete_file():
    file_id = request.args.get("id")

    if not file_id or not file_id.isdigit():
        return jsonify(
            {"code": 400, "msg": "Invalid or missing file ID", "data": None}
        ), 400

    try:
        # 获取数据库连接
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 查询要删除的记录
            cursor.execute("SELECT * FROM file_records WHERE id = ?", (file_id,))
            record = cursor.fetchone()

            if not record:
                return jsonify(
                    {"code": 404, "msg": "File not found", "data": None}
                ), 404

            record = dict(record)

            # 获取文件路径并删除实际文件
            file_path = Path(BASE_DIR / "videoFile" / record["file_path"])
            if file_path.exists():
                try:
                    file_path.unlink()  # 删除文件
                    print(f"✅ 实际文件已删除: {file_path}")
                except Exception as e:
                    print(f"⚠️ 删除实际文件失败: {e}")
                    # 即使删除文件失败，也要继续删除数据库记录，避免数据不一致
            else:
                print(f"⚠️ 实际文件不存在: {file_path}")

            # 删除数据库记录
            cursor.execute("DELETE FROM file_records WHERE id = ?", (file_id,))
            conn.commit()

        return jsonify(
            {
                "code": 200,
                "msg": "File deleted successfully",
                "data": {"id": record["id"], "filename": record["filename"]},
            }
        ), 200

    except Exception as e:
        return jsonify({"code": 500, "msg": str("delete failed!"), "data": None}), 500


# 统计数据API：获取文件统计
@app.route("/getFileStats", methods=["GET"])
def get_file_stats():
    try:
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取文件大小统计
            cursor.execute("""
                SELECT
                    COUNT(*) as total_files,
                    SUM(filesize) as total_size,
                    AVG(filesize) as avg_size,
                    MAX(filesize) as max_size
                FROM file_records
            """)
            size_stats = cursor.fetchone()

            # 获取最近上传的文件
            cursor.execute("""
                SELECT * FROM file_records
                ORDER BY id DESC
                LIMIT 10
            """)
            recent_files = [dict(row) for row in cursor.fetchall()]

            return jsonify(
                {
                    "code": 200,
                    "msg": "success",
                    "data": {
                        "size_stats": {
                            "total_files": size_stats["total_files"],
                            "total_size_mb": round(float(size_stats["total_size"]), 2),
                            "avg_size_mb": round(float(size_stats["avg_size"]), 2),
                            "max_size_mb": round(float(size_stats["max_size"]), 2),
                        },
                        "recent_files": recent_files,
                    },
                }
            ), 200
    except Exception as e:
        print(f"获取文件统计数据失败: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"获取文件统计数据失败: {str(e)}", "data": None}
        ), 500


# 统计数据API：获取平台账号统计
@app.route("/getPlatformStats", methods=["GET"])
def get_platform_stats():
    try:
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取各平台账号数量统计
            cursor.execute("""
                SELECT type, COUNT(*) as count, SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) as valid_count
                FROM user_info
                GROUP BY type
            """)
            platform_stats = []
            for row in cursor.fetchall():
                platform_stats.append(
                    {
                        "platform": row["type"],
                        "total": row["count"],
                        "valid": row["valid_count"],
                    }
                )

            # 获取总体统计
            cursor.execute("""
                SELECT COUNT(*) as total_accounts,
                       SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) as valid_accounts,
                       (SELECT COUNT(*) FROM file_records) as total_files
                FROM user_info
            """)
            overall_stats = cursor.fetchone()

            return jsonify(
                {
                    "code": 200,
                    "msg": "success",
                    "data": {
                        "platform_stats": platform_stats,
                        "overall": {
                            "total_accounts": overall_stats["total_accounts"],
                            "valid_accounts": overall_stats["valid_accounts"],
                            "total_files": overall_stats["total_files"],
                        },
                    },
                }
            ), 200
    except Exception as e:
        print(f"获取统计数据失败: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"获取统计数据失败: {str(e)}", "data": None}
        ), 500


###################################################账号管理#############################################
# 统一登录接口
@app.route("/login")
def login_unified():
    """
    统一登录接口，支持所有平台的登录
    参数：
        type: 平台类型编号
        id: 账号名
    返回：
        SSE 流，返回登录状态
    """
    type = request.args.get("type")
    # 账号名
    id = request.args.get("id")

    # 如果账号名已存在，查找原有账户的id，并删除原有记录
    with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM user_info WHERE userName = ? AND type = ?", (id, type)
        )
        row = cursor.fetchone()
        if row:
            account_id = row["id"]
            # 删除数据库中的原账号
            print(f"删除原账号ID: {account_id}")
            delete_account(account_id)

    # 模拟一个用于异步通信的队列
    status_queue = Queue()
    active_queues[id] = status_queue

    def on_close():
        print(f"清理队列: {id}")
        del active_queues[id]

    # 启动异步任务线程
    thread = threading.Thread(
        target=run_unified_login, args=(type, id, status_queue), daemon=True
    )
    thread.start()

    response = Response(
        sse_stream(
            status_queue,
        ),
        mimetype="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"  # 关键：禁用 Nginx 缓冲
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Connection"] = "keep-alive"
    return response


# SSE 流生成器函数
def sse_stream(status_queue):
    while True:
        if not status_queue.empty():
            msg = status_queue.get()
            yield f"data: {msg}\n\n"
        else:
            # 避免 CPU 占满
            time.sleep(0.1)


@app.route("/updateUserinfo", methods=["POST"])
def updateUserinfo():
    # 获取JSON数据
    data = request.get_json()

    # 从JSON数据中提取 type 和 userName
    user_id = data.get("id")
    type = data.get("type")
    userName = data.get("userName")
    try:
        # 获取数据库连接
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 更新数据库记录
            cursor.execute(
                """
                           UPDATE user_info
                           SET type     = ?,
                               userName = ?
                           WHERE id = ?;
                           """,
                (type, userName, user_id),
            )
            conn.commit()

        return jsonify(
            {"code": 200, "msg": "account update successfully", "data": None}
        ), 200

    except Exception as e:
        return jsonify({"code": 500, "msg": str("update failed!"), "data": None}), 500


# 获取所有账号信息
@app.route("/getAccounts", methods=["GET"])
def getAccounts():
    """快速获取所有账号信息，不进行cookie验证"""
    try:
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM user_info""")
            rows = cursor.fetchall()
            rows_list = [list(row) for row in rows]

            print("\n📋 当前数据表内容（快速获取）：")
            for row in rows_list:
                print(row)

            return jsonify({"code": 200, "msg": None, "data": rows_list}), 200
    except Exception as e:
        print(f"获取账号列表时出错: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"获取账号列表失败: {str(e)}", "data": None}
        ), 500


# 验证所有账号实时状态
@app.route("/getValidAccounts", methods=["GET"])
async def getValidAccounts():
    try:
        platform_type = request.args.get("type", type=int, default=0)

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            if platform_type == 0:
                cursor.execute("SELECT * FROM user_info")
            else:
                cursor.execute(
                    "SELECT * FROM user_info WHERE type = ?", (platform_type,)
                )
            rows = cursor.fetchall()
            rows_list = [list(row) for row in rows]
            print("\n📋 当前数据表内容：")
            for row in rows:
                print(row)
            # 定义并发限制数量
            CONCURRENCY_LIMIT = 10  # 可以根据系统资源调整

            # 使用并发方式验证cookie
            async def check_and_update_cookie(row):
                try:
                    flag = await check_cookie(row[1], row[2])
                    if flag:
                        row[4] = 1  # 验证成功，状态设为1
                        return row[0], 1
                    else:
                        row[4] = 0  # 验证失败，状态设为0
                        return row[0], 0
                except Exception as e:
                    print(f"❌ 验证账号 {row[3]} (ID: {row[0]}) 时出错: {str(e)}")
                    # 验证失败，标记为失效
                    row[4] = 0
                    return row[0], 0

            # 分批处理以控制并发数量
            def chunked_list(lst, chunk_size):
                for i in range(0, len(lst), chunk_size):
                    yield lst[i : i + chunk_size]

            print(f"\n🔄 开始并发验证账号状态（并发数: {CONCURRENCY_LIMIT}）...")

            # 记录需要更新的账号ID和状态
            accounts_to_update = []

            # 分批处理所有账号
            for batch in chunked_list(rows_list, CONCURRENCY_LIMIT):
                # 为当前批次中的每个账号创建验证任务
                tasks = [check_and_update_cookie(row) for row in batch]
                # 并发执行当前批次的所有任务，return_exceptions=True确保即使某个任务失败，其他任务仍能继续执行
                results = await asyncio.gather(*tasks, return_exceptions=True)
                # 收集需要更新的账号ID和状态，过滤掉异常结果
                for result in results:
                    if isinstance(result, Exception):
                        print(f"⚠️  批次处理中遇到异常: {str(result)}")
                    elif result is not None:
                        accounts_to_update.append(result)

            # 批量更新数据库，减少数据库操作次数
            if accounts_to_update:
                # 分离正常和失效账号，分别处理
                valid_accounts = [acc[0] for acc in accounts_to_update if acc[1] == 1]
                invalid_accounts = [acc[0] for acc in accounts_to_update if acc[1] == 0]

                update_queries = []
                update_params = []

                # 更新正常账号状态
                if valid_accounts:
                    placeholders_valid = ",".join(["?" for _ in valid_accounts])
                    update_queries.append(
                        f"UPDATE user_info SET status = 1 WHERE id IN ({placeholders_valid})"
                    )
                    update_params.extend(valid_accounts)

                # 更新失效账号状态
                if invalid_accounts:
                    placeholders_invalid = ",".join(["?" for _ in invalid_accounts])
                    update_queries.append(
                        f"UPDATE user_info SET status = 0 WHERE id IN ({placeholders_invalid})"
                    )
                    update_params.extend(invalid_accounts)

                # 执行所有更新语句
                for query in update_queries:
                    if "status = 1" in query:
                        cursor.execute(query, valid_accounts)
                    else:
                        cursor.execute(query, invalid_accounts)

                conn.commit()

                total_updated = len(valid_accounts) + len(invalid_accounts)
                print(
                    f"✅ 已批量更新 {total_updated} 个账号的状态，其中 {len(valid_accounts)} 个正常，{len(invalid_accounts)} 个失效"
                )
            else:
                print("✅ 所有账号状态均无需更新")
            # for row in rows:
            #    print(row)
            return jsonify({"code": 200, "msg": None, "data": rows_list}), 200
    except Exception as e:
        print(f"❌ 获取有效账号列表时发生异常: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"获取有效账号列表失败: {str(e)}", "data": None}
        ), 500


# Cookie文件上传API
@app.route("/uploadCookie", methods=["POST"])
def upload_cookie():
    try:
        if "file" not in request.files:
            return jsonify(
                {"code": 500, "msg": "没有找到Cookie文件", "data": None}
            ), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify(
                {"code": 500, "msg": "Cookie文件名不能为空", "data": None}
            ), 400

        if not file.filename.endswith(".json"):
            return jsonify(
                {"code": 500, "msg": "Cookie文件必须是JSON格式", "data": None}
            ), 400

        # 获取账号信息
        account_id = request.form.get("id")
        platform = request.form.get("platform")

        if not account_id or not platform:
            return jsonify(
                {"code": 500, "msg": "缺少账号ID或平台信息", "data": None}
            ), 400

        # 从数据库获取账号的文件路径
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT filePath FROM user_info WHERE id = ?", (account_id,))
            result = cursor.fetchone()

        if not result:
            return jsonify({"code": 500, "msg": "账号不存在", "data": None}), 404

        # 保存上传的Cookie文件到对应路径
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / result["filePath"])
        cookie_file_path.parent.mkdir(parents=True, exist_ok=True)

        file.save(str(cookie_file_path))

        # 更新数据库中的账号信息（可选，比如更新更新时间）
        # 这里可以根据需要添加额外的处理逻辑

        return jsonify({"code": 200, "msg": "Cookie文件上传成功", "data": None}), 200

    except Exception as e:
        print(f"上传Cookie文件时出错: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"上传Cookie文件失败: {str(e)}", "data": None}
        ), 500


# Cookie文件下载API
@app.route("/downloadCookie", methods=["GET"])
def download_cookie():
    try:
        file_path = request.args.get("filePath")
        if not file_path:
            return jsonify({"code": 500, "msg": "缺少文件路径参数", "data": None}), 400

        # 验证文件路径的安全性，防止路径遍历攻击
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / file_path).resolve()
        base_path = Path(BASE_DIR / "cookiesFile").resolve()

        if not cookie_file_path.is_relative_to(base_path):
            return jsonify({"code": 500, "msg": "非法文件路径", "data": None}), 400

        if not cookie_file_path.exists():
            return jsonify({"code": 500, "msg": "Cookie文件不存在", "data": None}), 404

        # 返回文件
        return send_from_directory(
            directory=str(cookie_file_path.parent),
            path=cookie_file_path.name,
            as_attachment=True,
        )

    except Exception as e:
        print(f"下载Cookie文件时出错: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"下载Cookie文件失败: {str(e)}", "data": None}
        ), 500


# 访问平台个人中心API
@app.route("/getPlatformHomepage", methods=["GET"])
async def get_platform_homepage():
    try:
        # 获取账号ID
        account_id = request.args.get("id")
        if not account_id:
            return jsonify({"code": 400, "msg": "缺少账号ID参数", "data": None}), 400

        # 从数据库获取账号信息
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT filePath, type FROM user_info WHERE id = ?", (account_id,)
            )
            result = cursor.fetchone()

        if not result:
            return jsonify({"code": 404, "msg": "账号不存在", "data": None}), 404

        file_path = result["filePath"]
        platform_type = result["type"]

        # 验证cookie文件是否存在
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / file_path)
        if not cookie_file_path.exists():
            return jsonify({"code": 400, "msg": "Cookie文件不存在", "data": None}), 400

        # 获取平台配置
        platform_key = get_platform_key_by_type(platform_type)
        if not platform_key or platform_key not in PLATFORM_CONFIGS:
            return jsonify({"code": 400, "msg": "平台配置不存在", "data": None}), 400

        platform_config = PLATFORM_CONFIGS[platform_key]
        personal_url = platform_config.get("personal_url")
        if not personal_url:
            return jsonify(
                {"code": 400, "msg": "平台个人中心URL未配置", "data": None}
            ), 400

        # 使用playwright携带cookie访问个人中心
        from playwright.async_api import async_playwright

        # 初始化playwright实例
        p = await async_playwright().start()

        # 启动浏览器
        if LOCAL_CHROME_PATH:
            browser = await p.chromium.launch(
                headless=False, executable_path=LOCAL_CHROME_PATH
            )
        else:
            browser = await p.chromium.launch(headless=False)

        # 创建上下文并加载cookie
        context = await browser.new_context(storage_state=str(cookie_file_path))

        # 创建新页面并访问个人中心
        page = await context.new_page()
        await page.goto(personal_url, wait_until="domcontentloaded", timeout=30000)

        # 获取页面标题
        page_title = await page.title()
        print(f"页面标题: {page_title}")

        # 不关闭浏览器，等待用户主动关闭
        # 注意：这里不会自动关闭浏览器和playwright实例，需要用户手动关闭浏览器窗口
        # 浏览器关闭后，playwright实例会自动清理

        return jsonify(
            {
                "code": 200,
                "msg": "访问成功",
                "data": {
                    "platform": platform_key,
                    "personal_url": personal_url,
                    "page_title": page_title,
                },
            }
        ), 200

    except Exception as e:
        print(f"访问平台个人中心失败: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"访问平台个人中心失败: {str(e)}", "data": None}
        ), 500


# 删除账号API
@app.route("/deleteAccount", methods=["GET"])
def delete_account_route():
    account_id = int(request.args.get("id"))

    # 调用myUtils.login模块中的delete_account函数
    result = delete_account(account_id)

    # 根据结果返回响应
    if result["code"] == 200:
        return jsonify(result), 200
    elif result["code"] == 404:
        return jsonify(result), 404
    else:
        return jsonify(result), 500


###################################################发布任务记录管理#############################################
# 获取发布任务记录
@app.route("/getPublishTaskRecords", methods=["GET"])
def get_publish_task_records():
    try:
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取查询参数
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 10))
            status = request.args.get("status")
            platform_name = request.args.get("platform_name")
            account_name = request.args.get("account_name")
            filename = request.args.get("filename")

            # 构建查询条件
            conditions = []
            params = []

            if status:
                conditions.append("status = ?")
                params.append(status)
            if platform_name:
                conditions.append("platform_name = ?")
                params.append(platform_name)
            if account_name:
                conditions.append("account_name LIKE ?")
                params.append(f"%{account_name}%")
            if filename:
                conditions.append("filename LIKE ?")
                params.append(f"%{filename}%")

            # 构建WHERE子句
            where_clause = ""
            if conditions:
                where_clause = f"WHERE {' AND '.join(conditions)}"

            # 获取总记录数
            cursor.execute(
                f"SELECT COUNT(*) as total FROM publish_task_records {where_clause}",
                params,
            )
            total = cursor.fetchone()["total"]

            # 计算偏移量
            offset = (page - 1) * page_size

            # 获取分页数据
            cursor.execute(
                f"""
                SELECT * FROM publish_task_records 
                {where_clause}
                ORDER BY create_time DESC 
                LIMIT ? OFFSET ?
            """,
                params + [page_size, offset],
            )

            records = [dict(row) for row in cursor.fetchall()]

            # 格式化数据
            formatted_records = []
            for record in records:
                formatted_records.append(
                    {
                        "id": record["id"],
                        "taskId": record["task_id"],
                        "fileName": record["filename"],
                        "fileId": record["file_id"],
                        "accountId": record["account_id"],
                        "accountName": record["account_name"],
                        "platformName": record["platform_name"],
                        "platformType": record["platform_type"],
                        "status": record["status"],
                        "createTime": record["create_time"],
                        "updateTime": record["update_time"],
                        "errorMsg": record["error_msg"],
                    }
                )

            # 构造返回数据
            result = {
                "records": formatted_records,
                "total": total,
                "page": page,
                "pageSize": page_size,
            }

            return jsonify(
                {"code": 200, "msg": "获取发布任务记录成功", "data": result}
            ), 200
    except Exception as e:
        print(f"获取发布任务记录失败: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"获取发布任务记录失败: {str(e)}", "data": None}
        ), 500


# 更新发布任务状态
@app.route("/updatePublishTaskStatus", methods=["POST"])
def update_publish_task_status():
    try:
        data = request.get_json()
        id = data.get("id")
        status = data.get("status")
        error_msg = data.get("errorMsg")

        if not id or not status:
            return jsonify({"code": 400, "msg": "缺少必要参数", "data": None}), 400

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()

            if error_msg:
                cursor.execute(
                    """
                    UPDATE publish_task_records 
                    SET status = ?, error_msg = ?, update_time = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """,
                    [status, error_msg, id],
                )
            else:
                cursor.execute(
                    """
                    UPDATE publish_task_records 
                    SET status = ?, update_time = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """,
                    [status, id],
                )

            conn.commit()

            if cursor.rowcount == 0:
                return jsonify(
                    {"code": 404, "msg": "发布任务记录不存在", "data": None}
                ), 404

            return jsonify(
                {"code": 200, "msg": "更新发布任务状态成功", "data": None}
            ), 200
    except Exception as e:
        print(f"更新发布任务状态失败: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"更新发布任务状态失败: {str(e)}", "data": None}
        ), 500


# 重试发布任务
@app.route("/retryPublishTask", methods=["POST"])
def retry_publish_task():
    try:
        data = request.get_json()
        id = data.get("id")

        if not id:
            return jsonify({"code": 400, "msg": "缺少必要参数", "data": None}), 400

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取任务记录
            cursor.execute(
                """
                SELECT * FROM publish_task_records WHERE id = ?
            """,
                [id],
            )
            record = cursor.fetchone()

            if not record:
                return jsonify(
                    {"code": 404, "msg": "发布任务记录不存在", "data": None}
                ), 404

            # 更新任务状态为"发布中"
            cursor.execute(
                """
                UPDATE publish_task_records 
                SET status = ?, error_msg = NULL, update_time = CURRENT_TIMESTAMP 
                WHERE id = ?
            """,
                ["发布中", id],
            )

            conn.commit()

            # 这里可以添加实际的重试逻辑，比如调用发布函数
            # 由于发布逻辑比较复杂，这里简化处理，只更新状态

            return jsonify({"code": 200, "msg": "发布任务重试成功", "data": None}), 200
    except Exception as e:
        print(f"重试发布任务失败: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"重试发布任务失败: {str(e)}", "data": None}
        ), 500


# 取消发布任务
@app.route("/cancelPublishTask", methods=["POST"])
def cancel_publish_task():
    try:
        data = request.get_json()
        id = data.get("id")

        if not id:
            return jsonify({"code": 400, "msg": "缺少必要参数", "data": None}), 400

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取任务记录
            cursor.execute(
                """
                SELECT * FROM publish_task_records WHERE id = ?
            """,
                [id],
            )
            record = cursor.fetchone()

            if not record:
                return jsonify(
                    {"code": 404, "msg": "发布任务记录不存在", "data": None}
                ), 404

            # 只有发布中或待发布的任务才能取消
            if record["status"] not in ["发布中", "待发布"]:
                return jsonify(
                    {
                        "code": 400,
                        "msg": f"只有发布中或待发布的任务才能取消，当前状态：{record['status']}",
                        "data": None,
                    }
                ), 400

            # 更新任务状态为"已取消"
            cursor.execute(
                """
                UPDATE publish_task_records 
                SET status = ?, update_time = CURRENT_TIMESTAMP 
                WHERE id = ?
            """,
                ["已取消", id],
            )

            conn.commit()

            # 这里可以添加实际的取消逻辑，比如停止发布进程
            # 由于发布逻辑比较复杂，这里简化处理，只更新状态

            return jsonify({"code": 200, "msg": "发布任务取消成功", "data": None}), 200
    except Exception as e:
        print(f"取消发布任务失败: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"取消发布任务失败: {str(e)}", "data": None}
        ), 500


# 删除发布任务记录
@app.route("/deletePublishTask", methods=["POST"])
def delete_publish_task():
    try:
        data = request.get_json()
        id = data.get("id")

        if not id:
            return jsonify({"code": 400, "msg": "缺少必要参数", "data": None}), 400

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 检查任务是否存在
            cursor.execute(
                """
                SELECT * FROM publish_task_records WHERE id = ?
            """,
                [id],
            )
            record = cursor.fetchone()

            if not record:
                return jsonify(
                    {"code": 404, "msg": "发布任务记录不存在", "data": None}
                ), 404

            # 删除任务记录
            cursor.execute(
                """
                DELETE FROM publish_task_records WHERE id = ?
            """,
                [id],
            )

            conn.commit()

            return jsonify(
                {"code": 200, "msg": "发布任务记录删除成功", "data": None}
            ), 200
    except Exception as e:
        print(f"删除发布任务记录失败: {str(e)}")
        return jsonify(
            {"code": 500, "msg": f"删除发布任务记录失败: {str(e)}", "data": None}
        ), 500


###################################################发布管理#############################################
# 将单个或多个视频发布到指定平台
@app.route("/postVideo", methods=["POST"])
def postVideo():
    """
    参数说明：
    type: 发布平台类型号，1-小红书 2-视频号 3-抖音 4-快手 5-tiktok 6-instagram 7-facebook
    platform: 发布平台类型，1-xiaohongshu 2- tencent 3-douyin 4-kuaishou 5-tiktok 6-instagram 7-facebook
    accountList: 账号列表，每个元素为一个字典，包含账号信息
    fileType: 文件类型，默认值为2：1-图文 2-视频
    title: 文件标题
    text: 文件正文描述
    tags: 文件标签，逗号分隔
    thumbnail: 视频缩略图封面路径
    location: 视频发布位置，1-国内 2-海外
    category: 文件分类，0-无分类 1-美食 2-日常 3-旅行 4-娱乐 5-教育 6-其他
    enableTimer: 是否启用定时发布，0-否 1-是
    videosPerDay: 每天发布文件数量
    dailyTimes: 每天发布时间，逗号分隔，格式为HH:MM
    startDays: 开始发布时间，距离当前时间的天数，负数表示之前的时间

    """
    task_id = None
    try:
        # 获取JSON数据的POST请求体
        data = request.get_json()
        type = data.get(
            "type"
        )  # 发布平台类型，1-小红书 2-视频号 3-抖音 4-快手 5-tiktok 6-instagram 7-facebook
        platform = data.get(
            "platform"
        )  # 发布平台类型，1-小红书 2-视频号 3-抖音 4-快手 5-tiktok 6-instagram 7-facebook
        account_list = data.get(
            "accountList", []
        )  # 账号列表，每个元素为一个字典，包含账号信息
        file_type = data.get("fileType")  # 文件类型，默认值为2：1-图文 2-视频
        file_list = data.get(
            "fileList", []
        )  # 文件列表，每个元素为一个字典，包含文件路径和文件名
        file_list = prepare_publish_files(file_list)
        title = data.get("title")  # 文件标题
        text = data.get("text", "demo")  # 文件正文描述，默认值为demo
        tags = data.get("tags")  # 文件标签，逗号分隔
        category = data.get(
            "category"
        )  # 文件分类，0-无分类 1-美食 2-日常 3-旅行 4-娱乐 5-教育 6-其他
        if category == 0:
            category = None
        thumbnail_path = data.get("thumbnail", "")  # 视频缩略图封面路径
        location = data.get("location", 1)  # 视频发布位置，1-国内 2-海外
        productLink = data.get("productLink", "")  # 商品链接
        productTitle = data.get("productTitle", "")  # 商品标题
        is_draft = data.get("isDraft", False)  # 是否保存为草稿
        enableTimer = data.get("enableTimer")  # 是否启用定时发布，0-否 1-是
        videos_per_day = data.get("videosPerDay")  # 每天发布文件数量
        daily_times = data.get("dailyTimes")  # 每天发布时间，逗号分隔，格式为HH:MM
        start_days = data.get(
            "startDays"
        )  # 开始发布时间，距离当前时间的天数，负数表示之前的时间
        # 打印获取到的数据（仅作为示例）
        print("File List:", file_list)
        print("Account List:", account_list)
        # 根据type获取platform
        platform = get_platform_key_by_type(type)
        if not platform:
            return jsonify({"code": 400, "msg": "Invalid type", "data": None}), 400

        # 生成唯一任务ID
        task_id = str(uuid.uuid4())

        # 创建发布任务记录
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 遍历每个账号
            for account in account_list:
                # 处理账号列表可能是字符串列表的情况
                if isinstance(account, str):
                    account_file = account
                    # 从数据库中查询账号名称
                    cursor.execute(
                        "SELECT userName FROM user_info WHERE filePath = ?",
                        [account_file],
                    )
                    result = cursor.fetchone()
                    account_name = (
                        result["userName"]
                        if result
                        else account_file.split(".")[0]
                        if "." in account_file
                        else account_file
                    )
                else:
                    account_file = account["filePath"]
                    account_name = account["userName"]

                # 遍历每个文件
                for file_info in file_list:
                    # 处理文件列表可能是字符串列表的情况
                    if isinstance(file_info, str):
                        filename = file_info
                    else:
                        filename = file_info["fileName"]

                    # 解析文件名，提取文件ID和真正的文件名
                    # 格式：file_id_filename.ext -> file_id: file_id, filename: filename.ext
                    if "_" in filename:
                        parts = filename.split("_")
                        file_id = parts[0]
                        real_filename = "_".join(parts[1:])
                    else:
                        file_id = None
                        real_filename = filename

                    # 插入发布任务记录
                    cursor.execute(
                        """
                        INSERT INTO publish_task_records (
                            task_id, filename, file_id, account_id, account_name, 
                            platform_name, platform_type, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        [
                            task_id,
                            real_filename,
                            file_id,
                            account_file,
                            account_name,
                            platform,
                            type,
                            "发布中",
                        ],
                    )

            conn.commit()

        # 调用post_file函数并获取返回值
        result = post_file(
            platform,
            account_list,
            file_type,
            file_list,
            title,
            text,
            tags,
            thumbnail_path,
            location,
            enableTimer,
            videos_per_day,
            daily_times,
            start_days,
        )

        # 更新发布任务记录状态
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()

            # 根据发布结果更新状态
            status = "发布成功" if result else "发布失败"
            cursor.execute(
                """
                UPDATE publish_task_records 
                SET status = ?, update_time = CURRENT_TIMESTAMP 
                WHERE task_id = ?
            """,
                [status, task_id],
            )

            conn.commit()

        # 根据返回值返回不同的响应
        if result:
            return jsonify({"code": 200, "msg": "发布成功", "data": None}), 200
        else:
            return jsonify({"code": 500, "msg": "发布失败", "data": None}), 500
    except Exception as e:
        # 捕获所有异常，更新发布任务记录状态
        print(f"发布视频时发生异常: {str(e)}")

        # 更新发布任务记录状态为发布失败，并添加错误信息
        if task_id:
            with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
                cursor = conn.cursor()

                cursor.execute(
                    """
                    UPDATE publish_task_records 
                    SET status = ?, error_msg = ?, update_time = CURRENT_TIMESTAMP 
                    WHERE task_id = ?
                """,
                    ["发布失败", str(e), task_id],
                )

                conn.commit()

        return jsonify({"code": 500, "msg": f"发布失败: {str(e)}", "data": None}), 500


# 批量发布多个文件到多个平台
@app.route("/postVideosToMultiplePlatforms", methods=["POST"])
def post_videos_to_multiple_platforms():
    """
    参数说明：
    platforms: 平台名称列表，如["xiaohongshu", "douyin", "kuaishou"]
    accountFiles: 账号文件字典，key为平台名称，value为该平台对应的账号文件列表
    fileType: 文件类型，1-图文 2-视频
    files: 文件列表，每个元素为文件名
    title: 文件标题
    text: 文件正文描述
    tags: 文件标签，多个标签用逗号隔开
    thumbnail: 视频缩略图封面路径
    location: 视频发布位置，1-国内 2-海外
    enableTimer: 是否启用定时发布，0-否 1-是
    videosPerDay: 每天发布文件数量
    dailyTimes: 每天发布时间，逗号分隔，格式为HH:MM
    startDays: 开始发布时间，距离当前时间的天数，负数表示之前的时间
    """
    task_id = None
    try:
        # 获取JSON数据的POST请求体
        data = request.get_json()

        # 解析请求参数
        platforms = data.get("platforms", [])  # 平台名称列表
        account_files = data.get("accountFiles", {})  # 账号文件字典
        file_type = data.get("fileType", 2)  # 文件类型，默认值为2：1-图文 2-视频
        files = data.get("files", [])  # 文件列表
        files = prepare_publish_files(files)
        title = data.get("title", "")  # 文件标题
        text = data.get("text", "")  # 文件正文描述
        tags = data.get("tags", "")  # 文件标签，逗号分隔
        thumbnail_path = data.get("thumbnail", "")  # 视频缩略图封面路径
        location = data.get("location", 1)  # 视频发布位置，1-国内 2-海外
        enable_timer = data.get("enableTimer", 0)  # 是否启用定时发布，0-否 1-是
        videos_per_day = data.get("videosPerDay", 1)  # 每天发布文件数量
        daily_times = data.get("dailyTimes", [])  # 每天发布时间，逗号分隔，格式为HH:MM
        start_days = data.get("startDays", 0)  # 开始发布时间，距离当前时间的天数

        # 修复Account Files：过滤每个平台的账号文件，只保留对应类型的文件
        # 1. 获取所有账号的实际类型映射
        file_type_map = {}
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT filePath, type FROM user_info")
            rows = cursor.fetchall()
            for row in rows:
                file_type_map[row[0]] = row[1]

        # 2. 过滤每个平台的账号文件
        filtered_account_files = {}
        for platform, account_files_list in account_files.items():
            # 获取当前平台对应的类型
            platform_type = get_type_by_platform_key(platform)
            if platform_type is None:
                filtered_account_files[platform] = []
                continue

            # 过滤出类型匹配的文件
            filtered_files = [
                file
                for file in account_files_list
                if file in file_type_map and file_type_map[file] == platform_type
            ]
            filtered_account_files[platform] = filtered_files

        # 使用过滤后的账号文件
        account_files = filtered_account_files

        # 打印获取到的数据
        print("Platforms:", platforms)
        print("Account Files:", account_files)
        print("File List:", files)

        # 生成唯一任务ID
        task_id = str(uuid.uuid4())

        # 创建发布任务记录
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 遍历每个平台
            for platform in platforms:
                platform_name = platform
                if platform_name in account_files:
                    account_files_list = account_files[platform_name]

                    # 获取平台类型
                    platform_type = get_type_by_platform_key(platform)
                    if platform_type is None:
                        continue

                    # 遍历每个账号文件
                    for account_file in account_files_list:
                        # 从数据库中查询账号名称
                        cursor.execute(
                            "SELECT userName FROM user_info WHERE filePath = ?",
                            [account_file],
                        )
                        result = cursor.fetchone()
                        account_name = (
                            result["userName"] if result else account_file.split(".")[0]
                        )

                        # 遍历每个文件
                        for filename in files:
                            # 解析文件名，提取文件ID和真正的文件名
                            # 格式：file_id_filename.ext -> file_id: file_id, filename: filename.ext
                            if "_" in filename:
                                parts = filename.split("_")
                                file_id = parts[0]
                                real_filename = "_".join(parts[1:])
                            else:
                                file_id = None
                                real_filename = filename

                            # 插入发布任务记录
                            cursor.execute(
                                """
                                INSERT INTO publish_task_records (
                                    task_id, filename, file_id, account_id, account_name, 
                                    platform_name, platform_type, status
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                                [
                                    task_id,
                                    real_filename,
                                    file_id,
                                    account_file,
                                    account_name,
                                    platform_name,
                                    platform_type,
                                    "待发布",
                                ],
                            )

            conn.commit()

        # 调用批量发布函数
        if enable_timer == 1:
            enable_timer = True
        else:
            enable_timer = False

        result = post_multiple_files_to_multiple_platforms(
            platforms=platforms,
            account_files=account_files,
            file_type=file_type,
            files=files,
            title=title,
            text=text,
            tags=tags,
            thumbnail_path=thumbnail_path,
            location=location,
            enableTimer=enable_timer,
            videos_per_day=videos_per_day,
            daily_times=daily_times,
            start_days=start_days,
        )

        # 更新发布任务记录状态
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()

            # 遍历每个平台的发布结果，更新对应的任务记录状态
            for platform, platform_result in result.items():
                if platform_result and "success" in platform_result:
                    # 根据发布结果更新状态
                    # 这里简化处理：如果该平台有成功发布的文件，则将该平台的所有任务记录标记为发布成功
                    # 否则标记为发布失败
                    status = (
                        "发布成功" if platform_result["success"] > 0 else "发布失败"
                    )
                    cursor.execute(
                        """
                        UPDATE publish_task_records 
                        SET status = ?, update_time = CURRENT_TIMESTAMP 
                        WHERE task_id = ? AND platform_name = ?
                    """,
                        [status, task_id, platform],
                    )

            conn.commit()

        # 返回响应给客户端
        return jsonify({"code": 200, "msg": "发布任务已完成", "data": result}), 200

    except Exception as e:
        print(f"发布视频到多个平台时出错: {str(e)}")

        # 更新发布任务记录状态为发布失败，并添加错误信息
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                UPDATE publish_task_records 
                SET status = ?, error_msg = ?, update_time = CURRENT_TIMESTAMP 
                WHERE task_id = ?
            """,
                ["发布失败", str(e), task_id],
            )

            conn.commit()

        return jsonify(
            {"code": 500, "msg": f"发布视频到多个平台失败: {str(e)}", "data": None}
        ), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5409)
