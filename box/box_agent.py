# [Modification] Production cleanup: require an explicit printer transport.

from __future__ import annotations

import argparse
import http.client
import json
import queue
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "box_downloads"
JOB_ID_PATTERN = re.compile(r"([A-Za-z0-9_.-]+-\d+)")
IPP_FORMATS = {
    "pwg_raster": "image/pwg-raster",
    "pclm": "application/PCLm",
    "urf": "image/urf",
    "pdf": "application/pdf",
    "raw": "application/octet-stream",
}
PROFILE_PRIORITY = [
    ("application/pdf", "pdf_passthrough", "application/pdf"),
    ("image/pwg-raster", "pwg_raster", "image/pwg-raster"),
    ("image/urf", "urf", "image/urf"),
    ("application/pclm", "pclm", "application/PCLm"),
]


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as source:
        config = json.load(source)
    config["_config_path"] = str(path)
    return config


def request_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def register_or_update_device(config: dict) -> dict:
    if not config.get("auto_detect_printer", False):
        return config
    if str(config.get("print_transport", "")).lower() != "ipp":
        return config

    printer_uri = str(config.get("ipp_printer_uri", "")).strip()
    if not printer_uri:
        return config

    cached = load_printer_cache(config, printer_uri)
    if cached and not config.get("force_detect_printer", False):
        capabilities = cached.get("printer_capabilities", {})
        selected = {
            "profile_id": cached.get("printer_profile", "raw_passthrough"),
            "document_format": cached.get("ipp_document_format", "application/octet-stream"),
        }
        print(
            "[box] Loaded cached printer profile "
            f"{selected['profile_id']} ({selected['document_format']})"
        )
    else:
        capabilities = query_ipp_printer_capabilities(printer_uri)
        selected = select_profile_from_capabilities(capabilities)
        save_printer_cache(config, printer_uri, capabilities, selected)

    payload = {
        "device_id": config["device_id"],
        "device_name": config.get("device_name", "IPP Print Box"),
        "printer_profile": selected["profile_id"],
        "printer_capabilities": capabilities,
    }
    response = request_json(f"{config['server_url'].rstrip('/')}/api/devices/register", method="POST", payload=payload)
    configured_token = str(config.get("device_token") or "")
    if configured_token.startswith("replace-with-token"):
        configured_token = ""
    device_token = configured_token or str(response.get("device_token") or "")
    merged = {
        **config,
        "device_token": device_token,
        "selected_printer_profile": response.get("printer_profile", selected["profile_id"]),
        "ipp_document_format": selected["document_format"],
    }
    print(
        "[box] Printer capability selected "
        f"{merged['selected_printer_profile']} ({merged['ipp_document_format']})"
    )
    if not config.get("device_token") and device_token:
        print(f"[box] Save this device_token to config: {device_token}")
    return merged


def cache_path_for_config(config: dict) -> Path:
    configured = str(config.get("printer_cache_path", "")).strip()
    if configured:
        return Path(configured)
    config_path = Path(str(config.get("_config_path", "box_config.json")))
    return config_path.with_name("box_printer_cache.json")


def load_printer_cache(config: dict, printer_uri: str) -> dict:
    cache_path = cache_path_for_config(config)
    if not cache_path.exists():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as source:
            cache = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    if cache.get("device_id") != config.get("device_id"):
        return {}
    if cache.get("ipp_printer_uri") != printer_uri:
        return {}
    if not cache.get("printer_profile"):
        return {}
    return cache


def save_printer_cache(config: dict, printer_uri: str, capabilities: dict, selected: dict) -> None:
    cache_path = cache_path_for_config(config)
    cache = {
        "device_id": config.get("device_id"),
        "ipp_printer_uri": printer_uri,
        "printer_profile": selected["profile_id"],
        "ipp_document_format": selected["document_format"],
        "printer_capabilities": capabilities,
        "updated_at": int(time.time()),
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as target:
        json.dump(cache, target, ensure_ascii=False, indent=2)
    print(f"[box] Saved printer profile cache to {cache_path}")


def query_ipp_printer_capabilities(printer_uri: str) -> dict:
    parsed = urllib.parse.urlparse(printer_uri)
    host = parsed.hostname
    if not host:
        raise ValueError("Invalid ipp_printer_uri")
    path = parsed.path or "/ipp/print"
    port = parsed.port or (443 if parsed.scheme == "ipps" else 631)
    connection_class = http.client.HTTPSConnection if parsed.scheme == "ipps" else http.client.HTTPConnection
    body = build_ipp_get_attributes_request(printer_uri)

    connection = connection_class(host, port, timeout=30)
    try:
        connection.request("POST", path, body=body, headers={"Content-Type": "application/ipp"})
        response = connection.getresponse()
        response_body = response.read()
    finally:
        connection.close()

    if response.status != 200:
        raise RuntimeError(f"IPP attributes HTTP {response.status}: {response.reason}")
    attrs = parse_ipp_attributes(response_body)
    formats = attrs.get("document-format-supported", [])
    return {
        "printer_uri": printer_uri,
        "printer_name": first_attr(attrs, "printer-name"),
        "printer_info": first_attr(attrs, "printer-info"),
        "printer_make_and_model": first_attr(attrs, "printer-make-and-model"),
        "document_format_supported": formats,
        "ipp_features_supported": attrs.get("ipp-features-supported", []),
        "urf_supported": attrs.get("urf-supported", []),
        "pwg_raster_document_type_supported": attrs.get("pwg-raster-document-type-supported", []),
    }


def select_profile_from_capabilities(capabilities: dict) -> dict:
    formats = {
        str(item).lower()
        for item in capabilities.get("document_format_supported", [])
    }
    for mime_type, profile_id, document_format in PROFILE_PRIORITY:
        if mime_type in formats:
            return {"profile_id": profile_id, "document_format": document_format}
    return {"profile_id": "raw_passthrough", "document_format": "application/octet-stream"}


def first_attr(attrs: dict[str, list[str]], name: str) -> str:
    values = attrs.get(name, [])
    return values[0] if values else ""


def build_ipp_get_attributes_request(printer_uri: str) -> bytes:
    requested = [
        "printer-name",
        "printer-info",
        "printer-make-and-model",
        "document-format-supported",
        "ipp-features-supported",
        "urf-supported",
        "pwg-raster-document-type-supported",
    ]
    parts = [
        b"\x02\x00\x00\x0b\x00\x00\x00\x01",
        b"\x01",
        ipp_text_attr(0x47, "attributes-charset", "utf-8"),
        ipp_text_attr(0x48, "attributes-natural-language", "en"),
        ipp_text_attr(0x45, "printer-uri", printer_uri),
    ]
    first = True
    for name in requested:
        parts.append(ipp_text_attr(0x44, "requested-attributes" if first else "", name))
        first = False
    parts.append(b"\x03")
    return b"".join(parts)


def parse_ipp_attributes(data: bytes) -> dict[str, list[str]]:
    attrs: dict[str, list[str]] = {}
    index = 8
    current_name = ""
    while index < len(data):
        tag = data[index]
        index += 1
        if tag == 0x03:
            break
        if tag in {0x01, 0x02, 0x04, 0x05}:
            continue
        if index + 4 > len(data):
            break
        name_length = int.from_bytes(data[index:index + 2], "big")
        index += 2
        name = data[index:index + name_length].decode("utf-8", errors="ignore")
        index += name_length
        value_length = int.from_bytes(data[index:index + 2], "big")
        index += 2
        value = data[index:index + value_length].decode("utf-8", errors="ignore")
        index += value_length
        current_name = name or current_name
        if current_name and value:
            attrs.setdefault(current_name, []).append(value)
    return attrs


def download_file(base_url: str, download_url: str, target: Path) -> None:
    full_url = urllib.parse.urljoin(base_url, download_url)
    with urllib.request.urlopen(full_url, timeout=120) as response:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)


def report_status(config: dict, job_id: str, status: str, message: str) -> None:
    url = f"{config['server_url'].rstrip('/')}/api/jobs/{job_id}/status"
    request_json(
        url,
        method="POST",
        payload={
            "device_id": config["device_id"],
            "token": config["device_token"],
            "status": status,
            "message": message,
        },
    )


def run_command(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def cancel_stale_jobs(config: dict) -> None:
    if not config.get("cancel_stale_jobs_before_print", False):
        return
    printer_name = str(config.get("printer_name", "wifi_printer")).strip() or "wifi_printer"
    run_command(["cancel", "-a", printer_name], timeout=20)
    run_command(["cupsenable", printer_name], timeout=20)
    run_command(["cupsaccept", printer_name], timeout=20)


def print_file(config: dict, file_path: Path) -> tuple[bool, str]:
    transport = str(config.get("print_transport", "cups")).strip().lower()
    if transport == "ipp":
        return ipp_print_file(config, file_path)

    cancel_stale_jobs(config)
    command_template = str(config.get("printer_command", "")).strip()
    if not command_template:
        return False, "printer_command is required for CUPS transport"

    command_text = command_template.format(file=str(file_path))
    command = shlex.split(command_text, posix=False)
    result = run_command(command, timeout=int(config.get("submit_timeout_seconds", 60)))
    if result.returncode != 0:
        return False, result.stderr.strip() or f"Printer command exited with {result.returncode}"

    submit_message = result.stdout.strip() or "Submitted to printer"
    if not config.get("wait_for_cups_completion", True):
        return True, submit_message

    job_id = extract_cups_job_id(submit_message)
    if not job_id:
        return True, submit_message

    ok, wait_message = wait_for_cups_job(config, job_id)
    if ok:
        return True, wait_message
    return False, wait_message


def extract_cups_job_id(output: str) -> str:
    match = JOB_ID_PATTERN.search(output)
    return match.group(1) if match else ""


def wait_for_cups_job(config: dict, job_id: str) -> tuple[bool, str]:
    timeout_seconds = int(config.get("print_timeout_seconds", 180))
    poll_seconds = float(config.get("cups_poll_seconds", 2))
    started_at = time.time()
    last_state = ""

    while time.time() - started_at < timeout_seconds:
        result = run_command(["lpstat", "-t"], timeout=20)
        state = result.stdout.strip() or result.stderr.strip()
        last_state = state or last_state
        if job_id not in state:
            return True, f"Printed successfully: {job_id}"
        if "不可用" in state or "不存在" in state or "disabled" in state.lower():
            break
        time.sleep(poll_seconds)

    run_command(["cancel", job_id], timeout=20)
    printer_name = str(config.get("printer_name", "wifi_printer")).strip() or "wifi_printer"
    run_command(["cupsenable", printer_name], timeout=20)
    run_command(["cupsaccept", printer_name], timeout=20)
    detail = summarize_cups_state(last_state)
    return False, f"CUPS job stuck and canceled: {job_id}; {detail}"


def summarize_cups_state(state: str) -> str:
    for line in state.splitlines():
        if "不可用" in line or "不存在" in line or "disabled" in line.lower():
            return line.strip()
    return "print timeout"


def ipp_print_file(config: dict, file_path: Path) -> tuple[bool, str]:
    printer_uri = str(config.get("ipp_printer_uri", "")).strip()
    if not printer_uri:
        return False, "ipp_printer_uri is required for IPP transport"

    document_format = str(config.get("ipp_document_format", "image/pwg-raster")).strip()
    job_name = file_path.name
    request_body = build_ipp_print_job_request(
        printer_uri=printer_uri,
        job_name=job_name,
        document_format=document_format,
        document=file_path.read_bytes(),
        copies=int(config.get("copies", 1)),
        media=str(config.get("media", "iso_a4_210x297mm")),
    )
    parsed = urllib.parse.urlparse(printer_uri)
    path = parsed.path or "/ipp/print"
    port = parsed.port or (443 if parsed.scheme == "ipps" else 631)
    timeout = int(config.get("ipp_timeout_seconds", 180))
    connection_class = http.client.HTTPSConnection if parsed.scheme == "ipps" else http.client.HTTPConnection

    connection = connection_class(parsed.hostname, port, timeout=timeout)
    try:
        connection.request("POST", path, body=request_body, headers={"Content-Type": "application/ipp"})
        response = connection.getresponse()
        response_body = response.read()
    finally:
        connection.close()

    if response.status != 200:
        return False, f"IPP HTTP {response.status}: {response.reason}"
    status_code = parse_ipp_status_code(response_body)
    if status_code >= 0x0400:
        return False, f"IPP failed with status 0x{status_code:04x}"
    return True, f"IPP job submitted: {job_name}"


def build_ipp_print_job_request(
    *,
    printer_uri: str,
    job_name: str,
    document_format: str,
    document: bytes,
    copies: int,
    media: str,
) -> bytes:
    parts = [
        b"\x02\x00\x00\x02\x00\x00\x00\x01",
        b"\x01",
        ipp_text_attr(0x47, "attributes-charset", "utf-8"),
        ipp_text_attr(0x48, "attributes-natural-language", "en"),
        ipp_text_attr(0x45, "printer-uri", printer_uri),
        ipp_text_attr(0x42, "requesting-user-name", "cloud-print-box"),
        ipp_text_attr(0x42, "job-name", job_name),
        ipp_text_attr(0x49, "document-format", document_format),
        b"\x02",
        ipp_int_attr("copies", max(1, min(copies, 99))),
        ipp_text_attr(0x44, "media", media),
        b"\x03",
        document,
    ]
    return b"".join(parts)


def ipp_text_attr(tag: int, name: str, value: str) -> bytes:
    name_bytes = name.encode("utf-8")
    value_bytes = value.encode("utf-8")
    return bytes([tag]) + len(name_bytes).to_bytes(2, "big") + name_bytes + len(value_bytes).to_bytes(2, "big") + value_bytes


def ipp_int_attr(name: str, value: int) -> bytes:
    name_bytes = name.encode("utf-8")
    return b"\x21" + len(name_bytes).to_bytes(2, "big") + name_bytes + (4).to_bytes(2, "big") + value.to_bytes(4, "big", signed=True)


def parse_ipp_status_code(response_body: bytes) -> int:
    if len(response_body) < 4:
        return 0x0500
    return int.from_bytes(response_body[2:4], "big")


def handle_next_task(config: dict) -> None:
    base_url = config["server_url"].rstrip("/")
    device_id = urllib.parse.quote(config["device_id"])
    token = urllib.parse.quote(config["device_token"])
    url = f"{base_url}/api/devices/{device_id}/tasks/next?token={token}"
    response = request_json(url)
    task = response.get("task")
    if not task:
        return

    job_id = task["job_id"]
    original_name = task["original_name"]
    format_suffixes = {
        "xqx": ".xqx",
        "pcl": ".pcl",
        "postscript": ".ps",
        "pdf": ".pdf",
        "pwg_raster": ".pwg",
        "pclm": ".pclm",
        "urf": ".urf",
        "raw": Path(original_name).suffix or ".bin",
    }
    suffix = format_suffixes.get(str(task.get("print_format", "")).lower(), ".print")
    local_file = DOWNLOAD_DIR / f"{job_id}{suffix}"

    print(f"[box] Received job {job_id}: {original_name}")
    report_status(config, job_id, "printing", "Downloading file")
    download_file(base_url, task["download_url"], local_file)

    report_status(config, job_id, "printing", "Printing file")
    task_format = str(task.get("print_format", "")).lower()
    if config.get("print_transport") == "ipp" and task_format in IPP_FORMATS:
        config = {**config, "ipp_document_format": IPP_FORMATS[task_format]}
    ok, message = print_file(config, local_file)
    report_status(config, job_id, "succeeded" if ok else "failed", message)
    print(f"[box] Job {job_id} finished: {message}")


def poll_once(config: dict) -> None:
    handle_next_task(config)


def run_loop(config: dict) -> None:
    if str(config.get("job_transport", "poll")).lower() == "mqtt":
        run_mqtt_loop(config)
        return

    interval = int(config.get("poll_interval_seconds", 5))
    print(f"[box] Polling {config['server_url']} as {config['device_id']}")
    while True:
        try:
            handle_next_task(config)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(f"[box] Poll failed: {exc}")
        time.sleep(interval)


def run_mqtt_loop(config: dict) -> None:
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError as exc:
        raise RuntimeError("MQTT mode requires python3-paho-mqtt or paho-mqtt") from exc

    mqtt_config = config.get("mqtt", {}) or {}
    host = str(mqtt_config.get("host") or config.get("mqtt_host") or "").strip()
    if not host:
        raise ValueError("MQTT mode requires mqtt.host")
    port = int(mqtt_config.get("port") or config.get("mqtt_port") or 1883)
    username = str(mqtt_config.get("username") or config.get("mqtt_username") or "")
    password = str(mqtt_config.get("password") or config.get("mqtt_password") or "")
    keepalive = int(mqtt_config.get("keepalive_seconds") or 60)
    topic_prefix = str(mqtt_config.get("topic_prefix") or config.get("mqtt_topic_prefix") or "cloud-print").strip("/")
    topic = f"{topic_prefix}/{safe_topic_part(config['device_id'])}/jobs"
    status_topic = f"{topic_prefix}/{safe_topic_part(config['device_id'])}/status"
    client_id = str(mqtt_config.get("client_id") or f"cloud-print-{config['device_id']}")
    tasks: queue.Queue[str] = queue.Queue()
    online_payload = json.dumps(
        {"device_id": config["device_id"], "status": "online", "ts": int(time.time())},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    offline_payload = json.dumps(
        {"device_id": config["device_id"], "status": "offline", "ts": int(time.time())},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    try:
        client = mqtt.Client(client_id=client_id)
    except TypeError:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if username:
        client.username_pw_set(username, password)
    client.will_set(status_topic, offline_payload, qos=1, retain=True)

    def on_connect(client_obj, _userdata, _flags, reason_code, *_extra) -> None:
        if int(reason_code) == 0:
            client_obj.subscribe(topic, qos=1)
            client_obj.publish(status_topic, online_payload, qos=1, retain=True)
            print(f"[box] MQTT connected; subscribed {topic}")
            return
        print(f"[box] MQTT connect failed: {reason_code}")

    def on_message(_client_obj, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        job_id = str(payload.get("job_id", "")).strip()
        print(f"[box] MQTT job notification received: {job_id or message.topic}")
        tasks.put(job_id)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=keepalive)
    client.loop_start()
    print(f"[box] Waiting for MQTT jobs from {host}:{port} as {config['device_id']}")

    while True:
        tasks.get()
        try:
            handle_next_task(config)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(f"[box] MQTT-triggered job failed: {exc}")


def safe_topic_part(value: str) -> str:
    safe = []
    for char in str(value):
        if char.isalnum() or char in "._-":
            safe.append(char)
    return "".join(safe) or "unknown"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="box_config.json")
    args = parser.parse_args()
    config = register_or_update_device(load_config(Path(args.config)))
    run_loop(config)


if __name__ == "__main__":
    main()
