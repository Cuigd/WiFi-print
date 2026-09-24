# [Modification] Added admin dashboard, device management APIs, and MQTT job notifications.

from __future__ import annotations

import cgi
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from print_pipeline import ConversionError, convert_to_print_ready, load_profiles


BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
FILES_DIR = STORAGE_DIR / "files"
PRINT_READY_DIR = STORAGE_DIR / "print_ready"
DB_PATH = STORAGE_DIR / "cloud_print.sqlite3"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"}
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
PROFILE_PRIORITY = [
    ("application/pdf", "pdf_passthrough"),
    ("image/pwg-raster", "pwg_raster"),
    ("image/urf", "urf"),
    ("application/pclm", "pclm"),
]
ONLINE_WINDOW_SECONDS = int(os.environ.get("DEVICE_ONLINE_WINDOW_SECONDS", "30"))
MQTT_ENABLED = os.environ.get("MQTT_ENABLED", "false").lower() == "true"
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "cloud-print").strip("/")
PRINT_UPLOAD_TOKEN = os.environ.get("PRINT_UPLOAD_TOKEN", "").strip()


def init_storage() -> None:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                device_token TEXT NOT NULL,
                printer_profile TEXT NOT NULL DEFAULT 'raw_passthrough',
                printer_capabilities_json TEXT NOT NULL DEFAULT '{}',
                last_request_path TEXT NOT NULL DEFAULT '',
                last_request_ip TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'online',
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                print_ready_path TEXT NOT NULL DEFAULT '',
                print_format TEXT NOT NULL DEFAULT '',
                printer_profile TEXT NOT NULL DEFAULT 'raw_passthrough',
                file_sha256 TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                options_json TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                assigned_at INTEGER,
                finished_at INTEGER,
                FOREIGN KEY(device_id) REFERENCES devices(device_id)
            );
            """
        )
        ensure_column(db, "devices", "printer_profile", "TEXT NOT NULL DEFAULT 'raw_passthrough'")
        ensure_column(db, "devices", "printer_capabilities_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(db, "devices", "last_request_path", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "devices", "last_request_ip", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "jobs", "print_ready_path", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "jobs", "print_format", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "jobs", "printer_profile", "TEXT NOT NULL DEFAULT 'raw_passthrough'")


def connect_db() -> sqlite3.Connection:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now_ts() -> int:
    return int(time.time())


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    payload = handler.rfile.read(length)
    return json.loads(payload.decode("utf-8"))


def json_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def row_to_device(row: sqlite3.Row) -> dict:
    last_seen_at = int(row["last_seen_at"] or 0)
    if MQTT_ENABLED:
        online = row["status"] == "online"
    else:
        online = now_ts() - last_seen_at <= ONLINE_WINDOW_SECONDS
    return {
        "device_id": row["device_id"],
        "device_name": row["device_name"],
        "printer_profile": row["printer_profile"],
        "printer_capabilities": json.loads(row["printer_capabilities_json"] or "{}"),
        "status": "online" if online else "offline",
        "last_seen_at": last_seen_at,
        "last_request_path": row["last_request_path"],
        "last_request_ip": row["last_request_ip"],
    }


def row_to_job(row: sqlite3.Row) -> dict:
    return {
        "job_id": row["job_id"],
        "user_id": row["user_id"],
        "device_id": row["device_id"],
        "original_name": row["original_name"],
        "file_sha256": row["file_sha256"],
        "file_size": row["file_size"],
        "print_format": row["print_format"],
        "printer_profile": row["printer_profile"],
        "options": json.loads(row["options_json"]),
        "status": row["status"],
        "message": row["message"],
        "stored_path": row["stored_path"],
        "print_ready_path": row["print_ready_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "assigned_at": row["assigned_at"],
        "finished_at": row["finished_at"],
    }


class CloudPrintHandler(BaseHTTPRequestHandler):
    server_version = "CloudPrintBox/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path_parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)

        if parsed.path in {"/", "/admin", "/admin/"}:
            self.handle_admin_page()
            return

        if parsed.path == "/health":
            self.send_json({"ok": True})
            return

        if parsed.path == "/api/devices":
            self.handle_list_devices()
            return

        if len(path_parts) == 3 and path_parts[:2] == ["api", "devices"]:
            self.handle_get_device(path_parts[2])
            return

        if len(path_parts) == 4 and path_parts[:2] == ["api", "devices"] and path_parts[3] == "jobs":
            self.handle_list_device_jobs(path_parts[2])
            return

        if len(path_parts) == 4 and path_parts[:2] == ["api", "devices"] and path_parts[3] == "files":
            self.handle_list_device_files(path_parts[2])
            return

        if parsed.path == "/api/printer-profiles":
            self.handle_list_printer_profiles()
            return

        if len(path_parts) == 5 and path_parts[:2] == ["api", "devices"] and path_parts[3:] == ["tasks", "next"]:
            self.handle_next_task(path_parts[2], query.get("token", [""])[0])
            return

        if len(path_parts) == 3 and path_parts[:2] == ["api", "jobs"]:
            self.handle_get_job(path_parts[2])
            return

        if len(path_parts) == 4 and path_parts[:2] == ["api", "jobs"] and path_parts[3] == "file":
            self.handle_download_file(path_parts[2], query.get("token", [""])[0])
            return

        self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def handle_admin_page(self) -> None:
        self.send_html(read_admin_html())

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path_parts = [part for part in parsed.path.split("/") if part]

        if parsed.path == "/api/devices/register":
            self.handle_register_device()
            return

        if parsed.path == "/api/jobs":
            self.handle_create_job()
            return

        if len(path_parts) == 4 and path_parts[:2] == ["api", "jobs"] and path_parts[3] == "status":
            self.handle_update_status(path_parts[2])
            return

        self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def handle_register_device(self) -> None:
        try:
            data = read_json(self)
            device_id = str(data.get("device_id", "")).strip()
            device_name = str(data.get("device_name", "")).strip() or device_id
            printer_profile = str(data.get("printer_profile", "raw_passthrough")).strip() or "raw_passthrough"
            printer_capabilities = data.get("printer_capabilities") or {}
            if printer_profile == "auto":
                printer_profile = choose_profile_from_capabilities(printer_capabilities)
            if not device_id:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "device_id is required")
                return
            if printer_profile not in load_profiles():
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"Unknown printer_profile: {printer_profile}")
                return

            token = secrets.token_urlsafe(24)
            timestamp = now_ts()
            with connect_db() as db:
                existing = db.execute(
                    "SELECT device_token FROM devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if existing:
                    token = existing["device_token"]
                    db.execute(
                        """
                        UPDATE devices
                        SET device_name = ?, printer_profile = ?, printer_capabilities_json = ?,
                            status = 'online', last_seen_at = ?
                        WHERE device_id = ?
                        """,
                        (
                            device_name,
                            printer_profile,
                            json.dumps(printer_capabilities, ensure_ascii=False),
                            timestamp,
                            device_id,
                        ),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO devices (
                            device_id, device_name, device_token, printer_profile, printer_capabilities_json,
                            status, created_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, 'online', ?, ?)
                        """,
                        (
                            device_id,
                            device_name,
                            token,
                            printer_profile,
                            json.dumps(printer_capabilities, ensure_ascii=False),
                            timestamp,
                            timestamp,
                        ),
                    )

            self.send_json({"device_id": device_id, "device_token": token, "printer_profile": printer_profile})
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")

    def handle_list_devices(self) -> None:
        with connect_db() as db:
            rows = db.execute(
                "SELECT * FROM devices ORDER BY created_at DESC"
            ).fetchall()
        self.send_json({"devices": [row_to_device(row) for row in rows]})

    def handle_get_device(self, device_id: str) -> None:
        with connect_db() as db:
            row = db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if not row:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Device not found")
            return
        self.send_json({"device": row_to_device(row)})

    def handle_list_device_jobs(self, device_id: str) -> None:
        with connect_db() as db:
            rows = db.execute(
                """
                SELECT * FROM jobs
                WHERE device_id = ?
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (device_id,),
            ).fetchall()
        self.send_json({"jobs": [row_to_job(row) for row in rows]})

    def handle_list_device_files(self, device_id: str) -> None:
        device_part = safe_path_part(device_id)
        self.send_json(
            {
                "files": list_files_under(FILES_DIR / device_part),
                "print_ready_files": list_files_under(PRINT_READY_DIR / device_part),
            }
        )

    def handle_list_printer_profiles(self) -> None:
        profiles = [
            {
                "profile_id": profile.profile_id,
                "display_name": profile.display_name,
                "print_format": profile.print_format,
                "output_suffix": profile.output_suffix,
            }
            for profile in load_profiles().values()
        ]
        self.send_json({"profiles": profiles})

    def handle_create_job(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Empty upload")
            return
        if content_length > MAX_UPLOAD_BYTES:
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File is too large")
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
        )

        # 生产环境设置 PRINT_UPLOAD_TOKEN 后，上传入口必须带同一个 token。
        upload_token = str(form.getfirst("upload_token", "") or self.headers.get("X-Upload-Token", "")).strip()
        if PRINT_UPLOAD_TOKEN and not secrets.compare_digest(upload_token, PRINT_UPLOAD_TOKEN):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid upload token")
            return

        user_id = str(form.getfirst("user_id", "demo")).strip() or "demo"
        device_id = str(form.getfirst("device_id", "")).strip()
        upload = form["file"] if "file" in form else None
        if not device_id:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "device_id is required")
            return
        if upload is None or not getattr(upload, "filename", ""):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "file is required")
            return

        original_name = Path(upload.filename).name
        extension = Path(original_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Only PDF, DOC, DOCX, JPG, JPEG, and PNG files are allowed")
            return

        with connect_db() as db:
            device = db.execute(
                "SELECT device_id, printer_profile FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if not device:
                # 当前阶段先保证小程序上传闭环，未绑定设备时自动创建占位云盒。
                timestamp = now_ts()
                db.execute(
                    """
                    INSERT INTO devices (
                        device_id, device_name, device_token, printer_profile, printer_capabilities_json,
                        status, created_at, last_seen_at
                    ) VALUES (?, ?, ?, 'raw_passthrough', '{}', 'offline', ?, ?)
                    """,
                    (device_id, "默认云盒", secrets.token_urlsafe(24), timestamp, timestamp),
                )
                printer_profile = "raw_passthrough"
            else:
                printer_profile = device["printer_profile"]

        job_id = uuid.uuid4().hex
        day = time.strftime("%Y%m%d")
        stored_dir = FILES_DIR / safe_path_part(device_id) / day
        stored_dir.mkdir(parents=True, exist_ok=True)
        stored_path = stored_dir / f"{job_id}_{safe_filename(original_name)}"
        file_hash = hashlib.sha256()
        file_size = 0
        with stored_path.open("wb") as target:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > MAX_UPLOAD_BYTES:
                    target.close()
                    stored_path.unlink(missing_ok=True)
                    self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File is too large")
                    return
                file_hash.update(chunk)
                target.write(chunk)

        options = {
            "copies": int(form.getfirst("copies", "1") or "1"),
            "paper_size": str(form.getfirst("paper_size", "A4") or "A4"),
            "duplex": str(form.getfirst("duplex", "false")).lower() == "true",
            "color": str(form.getfirst("color", "true")).lower() == "true",
            "dpi": int(form.getfirst("dpi", "600") or "600"),
        }
        timestamp = now_ts()
        try:
            # 云端先产出盒子可直接转发的打印流，盒子不再理解 PDF/Word/图片。
            print_ready_path, print_format = convert_to_print_ready(
                source_path=stored_path,
                job_id=job_id,
                profile_id=printer_profile,
                options=options,
            )
            print_ready_path = move_print_ready_file(print_ready_path, device_id, day, job_id)
            job_status = "pending"
            message = ""
        except ConversionError as exc:
            print_ready_path = Path("")
            print_format = ""
            job_status = "failed"
            message = str(exc)

        with connect_db() as db:
            db.execute(
                """
                INSERT INTO jobs (
                    job_id, user_id, device_id, original_name, stored_path,
                    print_ready_path, print_format, printer_profile, file_sha256,
                    file_size, options_json, status, message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    user_id,
                    device_id,
                    original_name,
                    str(stored_path),
                    str(print_ready_path),
                    print_format,
                    printer_profile,
                    file_hash.hexdigest(),
                    file_size,
                    json.dumps(options, ensure_ascii=False),
                    job_status,
                    message,
                    timestamp,
                    timestamp,
                ),
            )

        if job_status == "pending":
            publish_job_notification(device_id, job_id)

        self.send_json(
            {
                "job_id": job_id,
                "status": job_status,
                "message": message,
                "printer_profile": printer_profile,
                "print_format": print_format,
            },
            HTTPStatus.CREATED,
        )

    def handle_next_task(self, device_id: str, token: str) -> None:
        if not self.authorize_device(device_id, token):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid device token")
            return

        timestamp = now_ts()
        with connect_db() as db:
            db.execute(
                "UPDATE devices SET status = 'online', last_seen_at = ? WHERE device_id = ?",
                (timestamp, device_id),
            )
            db.execute(
                """
                UPDATE devices
                SET last_request_path = ?, last_request_ip = ?
                WHERE device_id = ?
                """,
                (self.path, self.client_address[0], device_id),
            )
            row = db.execute(
                """
                SELECT * FROM jobs
                WHERE device_id = ? AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            if not row:
                self.send_json({"task": None})
                return
            db.execute(
                """
                UPDATE jobs
                SET status = 'assigned', assigned_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'pending'
                """,
                (timestamp, timestamp, row["job_id"]),
            )

        job = row_to_job(row)
        job["status"] = "assigned"
        job["download_url"] = f"/api/jobs/{job['job_id']}/file?token={token}"
        self.send_json({"task": job})

    def handle_get_job(self, job_id: str) -> None:
        with connect_db() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Job not found")
            return
        self.send_json({"job": row_to_job(row)})

    def handle_download_file(self, job_id: str, token: str) -> None:
        with connect_db() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Job not found")
                return
            if not self.authorize_device(row["device_id"], token):
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid device token")
                return

        file_path = Path(row["print_ready_path"] or row["stored_path"])
        if not file_path.exists():
            self.send_error_json(HTTPStatus.NOT_FOUND, "Stored file missing")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(file_path.stat().st_size))
        download_name = f'{row["job_id"]}{Path(file_path).suffix or ".print"}'
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with file_path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def handle_update_status(self, job_id: str) -> None:
        try:
            data = read_json(self)
            device_id = str(data.get("device_id", "")).strip()
            token = str(data.get("token", "")).strip()
            status = str(data.get("status", "")).strip()
            message = str(data.get("message", "")).strip()
            if status not in {"printing", "succeeded", "failed"}:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid status")
                return
            if not self.authorize_device(device_id, token):
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid device token")
                return

            timestamp = now_ts()
            finished_at = timestamp if status in {"succeeded", "failed"} else None
            with connect_db() as db:
                result = db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, message = ?, updated_at = ?, finished_at = COALESCE(?, finished_at)
                    WHERE job_id = ? AND device_id = ?
                    """,
                    (status, message, timestamp, finished_at, job_id, device_id),
                )
                if result.rowcount == 0:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Job not found")
                    return
                db.execute(
                    """
                    UPDATE devices
                    SET status = 'online', last_seen_at = ?, last_request_path = ?, last_request_ip = ?
                    WHERE device_id = ?
                    """,
                    (timestamp, self.path, self.client_address[0], device_id),
                )

            self.send_json({"job_id": job_id, "status": status})
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")

    def authorize_device(self, device_id: str, token: str) -> bool:
        if not device_id or not token:
            return False
        with connect_db() as db:
            row = db.execute(
                "SELECT device_token FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return bool(row and secrets.compare_digest(row["device_token"], token))

    def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[cloud] {self.address_string()} - {fmt % args}")


def read_admin_html() -> str:
    dashboard_path = BASE_DIR / "admin" / "dashboard.html"
    try:
        return dashboard_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f'<!doctype html><meta charset="utf-8"><title>管理台不可用</title><h1>管理台文件读取失败</h1><p>{exc}</p>'


def main() -> None:
    init_storage()
    start_mqtt_status_listener()
    server = ThreadingHTTPServer((HOST, PORT), CloudPrintHandler)
    print(f"Cloud print server listening on http://{HOST}:{PORT}")
    server.serve_forever()


def choose_profile_from_capabilities(capabilities: dict) -> str:
    formats = {
        str(item).lower()
        for item in capabilities.get("document_format_supported", [])
    }
    for mime_type, profile_id in PROFILE_PRIORITY:
        if mime_type in formats and profile_id in load_profiles():
            return profile_id
    return "raw_passthrough"


def safe_path_part(value: str) -> str:
    safe = []
    for char in str(value):
        if char.isalnum() or char in "._-":
            safe.append(char)
    return "".join(safe) or "unknown"


def safe_filename(value: str) -> str:
    name = Path(value or "upload.bin").name
    safe = []
    for char in name:
        if char.isalnum() or char in "._-()[] ":
            safe.append(char)
    return "".join(safe).strip(" .") or "upload.bin"


def move_print_ready_file(source: Path, device_id: str, day: str, job_id: str) -> Path:
    if not source or not source.exists():
        return source
    target_dir = PRINT_READY_DIR / safe_path_part(device_id) / day
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{job_id}{source.suffix or '.print'}"
    if source.resolve() == target.resolve():
        return target
    shutil.move(str(source), str(target))
    return target


def mqtt_topic_for_device(device_id: str) -> str:
    return f"{MQTT_TOPIC_PREFIX}/{safe_path_part(device_id)}/jobs"


def mqtt_status_topic_for_device(device_id: str) -> str:
    return f"{MQTT_TOPIC_PREFIX}/{safe_path_part(device_id)}/status"


def publish_job_notification(device_id: str, job_id: str) -> None:
    if not MQTT_ENABLED:
        return

    payload = json.dumps(
        {
            "type": "job_ready",
            "device_id": device_id,
            "job_id": job_id,
            "created_at": now_ts(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    topic = mqtt_topic_for_device(device_id)

    # 优先使用 paho-mqtt；服务器没装 Python 包时，退回 mosquitto_pub 命令。
    try:
        import paho.mqtt.publish as mqtt_publish  # type: ignore

        auth = None
        if MQTT_USERNAME:
            auth = {"username": MQTT_USERNAME, "password": MQTT_PASSWORD}
        mqtt_publish.single(
            topic,
            payload=payload,
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            auth=auth,
            qos=1,
        )
        print(f"[cloud] MQTT notified {device_id}: {job_id}")
        return
    except Exception as exc:
        print(f"[cloud] paho MQTT publish failed: {exc}")

    command = [
        "mosquitto_pub",
        "-h",
        MQTT_HOST,
        "-p",
        str(MQTT_PORT),
        "-q",
        "1",
        "-t",
        topic,
        "-m",
        payload,
    ]
    if MQTT_USERNAME:
        command.extend(["-u", MQTT_USERNAME, "-P", MQTT_PASSWORD])
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        print(f"[cloud] MQTT notified {device_id}: {job_id}")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[cloud] MQTT publish failed for {device_id}/{job_id}: {exc}")


def start_mqtt_status_listener() -> None:
    if not MQTT_ENABLED:
        return

    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError:
        print("[cloud] MQTT status listener disabled: install paho-mqtt")
        return

    topic = f"{MQTT_TOPIC_PREFIX}/+/status"

    try:
        client = mqtt.Client(client_id="cloud-print-server")
    except TypeError:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cloud-print-server")
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    def on_connect(client_obj, _userdata, _flags, reason_code, *_extra) -> None:
        if int(reason_code) == 0:
            client_obj.subscribe(topic, qos=1)
            print(f"[cloud] MQTT status listener subscribed {topic}")
            return
        print(f"[cloud] MQTT status listener connect failed: {reason_code}")

    def on_message(_client_obj, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        device_id = str(payload.get("device_id", "")).strip()
        status = str(payload.get("status", "")).strip()
        if not device_id or status not in {"online", "offline"}:
            return
        with connect_db() as db:
            db.execute(
                """
                UPDATE devices
                SET status = ?, last_seen_at = ?, last_request_path = ?, last_request_ip = ?
                WHERE device_id = ?
                """,
                (status, now_ts(), message.topic, "mqtt", device_id),
            )
        print(f"[cloud] MQTT device {device_id} is {status}")

    client.on_connect = on_connect
    client.on_message = on_message

    def run() -> None:
        while True:
            try:
                client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                client.loop_forever()
            except Exception as exc:
                print(f"[cloud] MQTT status listener failed: {exc}")
                time.sleep(5)

    threading.Thread(target=run, daemon=True).start()


def list_files_under(root: Path) -> list[dict]:
    if not root.exists():
        return []
    rows = []
    files = [path for path in root.rglob("*") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[:200]:
        stat = path.stat()
        rows.append(
            {
                "path": str(path),
                "relative_path": str(path.relative_to(root)),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    return rows


if __name__ == "__main__":
    main()
