from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageDraw, ImageFont

VALID_EXTENSIONS = {".png", ".jpg", ".jpeg"}
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("agent_config.json")
DEFAULT_EXTERNAL_IP_SERVICES = [
    "https://api64.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]
WINDOWS_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
]
LINUX_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
SYSTEM_USERNAMES = {"SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE"}


def default_data_root() -> str:
    if os.name == "nt":
        return r"%ProgramData%\ScreenshotAudit"
    return "/var/tmp/ScreenshotAudit"


DEFAULT_CONFIG: dict[str, Any] = {
    "agent_version": "0.1.2-pilot",
    "sharex_profile_version": "pilot-4",
    "poll_interval_seconds": 2,
    "retry_backoff_seconds": 15,
    "retry_backoff_max_seconds": 300,
    "max_retry_attempts": 0,
    "delete_local_after_success": True,
    "log_level": "INFO",
    "paths": {
        "spool_dir": f"{default_data_root()}\\spool" if os.name == "nt" else f"{default_data_root()}/spool",
        "tmp_dir": f"{default_data_root()}\\tmp" if os.name == "nt" else f"{default_data_root()}/tmp",
        "db_path": f"{default_data_root()}\\data\\queue.db" if os.name == "nt" else f"{default_data_root()}/data/queue.db",
        "log_path": f"{default_data_root()}\\logs\\agent.log" if os.name == "nt" else f"{default_data_root()}/logs/agent.log",
    },
    "watermark": {
        "enabled": True,
        "logo_path": f"{default_data_root()}\\assets\\logo.png" if os.name == "nt" else f"{default_data_root()}/assets/logo.png",
    },
    "external_ip_services": DEFAULT_EXTERNAL_IP_SERVICES,
    "external_ip_timeout_seconds": 5,
    "external_ip_cache_ttl_seconds": 300,
    "routing": {
        "force_tenant": "",
        "default_tenant": "bases-e-lojas",
        "default_bucket": "sharex-data-bases-e-lojas",
        "tenant_buckets": {
            "clickip": "sharex-data-clickip",
            "fiber": "sharex-data-fiber",
            "intlink": "sharex-data-intlink",
            "bases-e-lojas": "sharex-data-bases-e-lojas",
        },
        "external_ip_map": {},
    },
    "minio": {
        "endpoint_url": "http://s3.homelab.local",
        "access_key": "",
        "secret_key": "",
        "region_name": "us-east-1",
        "verify_tls": False,
    },
    "api": {
        "enabled": False,
        "base_url": "",
        "heartbeat_path": "/api/v1/agents/heartbeat",
        "ingest_confirm_path": "/api/v1/ingest/confirm",
        "bearer_token": "",
        "timeout_seconds": 5,
        "heartbeat_interval_seconds": 15,
        "agent_id": "",
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def expand_path(raw_value: str) -> Path:
    value = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        raw_value,
    )
    value = os.path.expandvars(os.path.expanduser(value))
    return Path(value)


def sanitize_segment(value: str, *, lowercase: bool = False) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    if not cleaned:
        cleaned = "unknown"
    return cleaned.lower() if lowercase else cleaned


def iso_now() -> str:
    return datetime.now().astimezone().isoformat()


def load_config(config_path: Path) -> dict[str, Any]:
    user_config: dict[str, Any] = {}
    if config_path.exists():
        user_config = json.loads(config_path.read_text(encoding="utf-8"))

    config = deep_merge(DEFAULT_CONFIG, user_config)

    config["paths"]["spool_dir"] = expand_path(config["paths"]["spool_dir"])
    config["paths"]["tmp_dir"] = expand_path(config["paths"]["tmp_dir"])
    config["paths"]["db_path"] = expand_path(config["paths"]["db_path"])
    config["paths"]["log_path"] = expand_path(config["paths"]["log_path"])
    config["watermark"]["logo_path"] = expand_path(config["watermark"]["logo_path"])

    validate_config(config, config_path)
    return config


def validate_config(config: dict[str, Any], config_path: Path) -> None:
    minio = config["minio"]
    routing = config["routing"]

    required_minio_fields = ["endpoint_url", "access_key", "secret_key"]
    missing = [field for field in required_minio_fields if not str(minio.get(field, "")).strip()]
    if missing:
        raise ValueError(
            "Config invalida em "
            f"{config_path}: faltam campos de MinIO: {', '.join(missing)}. "
            "Copie agent_config.example.json para agent_config.json e preencha os valores."
        )

    default_tenant = routing["default_tenant"]
    tenant_buckets = routing["tenant_buckets"]
    if default_tenant not in tenant_buckets:
        raise ValueError(
            f"Config invalida em {config_path}: o tenant padrao '{default_tenant}' nao tem bucket definido."
        )


def setup_logging(log_path: Path, level_name: str) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("screenshot-audit-agent")
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def ensure_directories(config: dict[str, Any]) -> None:
    config["paths"]["spool_dir"].mkdir(parents=True, exist_ok=True)
    config["paths"]["tmp_dir"].mkdir(parents=True, exist_ok=True)
    config["paths"]["db_path"].parent.mkdir(parents=True, exist_ok=True)
    config["paths"]["log_path"].parent.mkdir(parents=True, exist_ok=True)


def get_internal_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "N/A"


def normalize_username(value: str) -> str:
    cleaned = value.strip()
    if "\\" in cleaned:
        cleaned = cleaned.rsplit("\\", 1)[-1]
    elif "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    return cleaned.strip()


def is_system_identity(username: str) -> bool:
    candidate = normalize_username(username)
    return not candidate or candidate.upper() in SYSTEM_USERNAMES or candidate.endswith("$")


def get_active_windows_user() -> str:
    commands = [
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$user = (Get-CimInstance Win32_ComputerSystem).UserName; if ($user) { $user }",
        ],
        ["query", "user"],
    ]

    for command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue

        if completed.returncode != 0 or not completed.stdout:
            continue

        if command[0].lower() == "query":
            for raw_line in completed.stdout.splitlines():
                line = raw_line.strip().lstrip(">")
                if not line or line.upper().startswith("USERNAME"):
                    continue
                candidate = normalize_username(line.split()[0])
                if candidate:
                    return candidate
            continue

        candidate = normalize_username(completed.stdout.strip())
        if candidate:
            return candidate

    return ""


def get_effective_username() -> str:
    if os.name == "nt":
        active_username = get_active_windows_user()
        if active_username:
            return active_username

    env_username = normalize_username(os.environ.get("USERNAME", ""))
    if not is_system_identity(env_username):
        return env_username

    fallback = normalize_username(getpass.getuser())
    return fallback if not is_system_identity(fallback) else "unknown"


class ExternalIPResolver:
    def __init__(self, services: list[str], timeout_seconds: int, cache_ttl_seconds: int) -> None:
        self.services = services
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cached_value = ""
        self._cached_until = 0.0

    def get_external_ip(self) -> str:
        now = time.time()
        if self._cached_value and now < self._cached_until:
            return self._cached_value

        for service in self.services:
            try:
                request = Request(service, headers={"User-Agent": "ScreenshotAuditAgent/0.1"})
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    value = response.read().decode("utf-8", errors="ignore").strip()
                    if value:
                        self._cached_value = value
                        self._cached_until = now + self.cache_ttl_seconds
                        return value
            except (OSError, URLError):
                continue

        return self._cached_value or "N/A"


class QueueStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS queue_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                source_path TEXT NOT NULL,
                source_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                tenant TEXT,
                bucket_name TEXT,
                object_key TEXT,
                sha256 TEXT,
                captured_at TEXT NOT NULL,
                hostname TEXT NOT NULL,
                username TEXT NOT NULL,
                local_ip TEXT,
                external_ip TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                processed_at TEXT,
                UNIQUE(source_path, source_size, source_mtime_ns)
            );
            """
        )
        self.connection.commit()

    def reset_inflight_items(self) -> int:
        now = iso_now()
        cursor = self.connection.execute(
            """
            UPDATE queue_items
               SET status = 'failed',
                   last_error = 'Agent reiniciado durante upload',
                   next_attempt_at = ?,
                   updated_at = ?
             WHERE status = 'uploading'
            """,
            (now, now),
        )
        self.connection.commit()
        return cursor.rowcount

    def enqueue_file(
        self,
        *,
        source_path: Path,
        source_size: int,
        source_mtime_ns: int,
        captured_at: str,
        hostname: str,
        username: str,
        local_ip: str,
        external_ip: str,
    ) -> bool:
        now = iso_now()
        try:
            self.connection.execute(
                """
                INSERT INTO queue_items (
                    event_id,
                    source_path,
                    source_size,
                    source_mtime_ns,
                    status,
                    attempts,
                    next_attempt_at,
                    captured_at,
                    hostname,
                    username,
                    local_ip,
                    external_ip,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"evt_{uuid.uuid4().hex}",
                    str(source_path),
                    source_size,
                    source_mtime_ns,
                    now,
                    captured_at,
                    hostname,
                    username,
                    local_ip,
                    external_ip,
                    now,
                    now,
                ),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def fetch_due_items(self, limit: int = 50) -> list[sqlite3.Row]:
        cursor = self.connection.execute(
            """
            SELECT *
              FROM queue_items
             WHERE status IN ('pending', 'failed')
               AND next_attempt_at <= ?
             ORDER BY created_at ASC
             LIMIT ?
            """,
            (iso_now(), limit),
        )
        return cursor.fetchall()

    def mark_uploading(self, event_id: str) -> None:
        self.connection.execute(
            """
            UPDATE queue_items
               SET status = 'uploading',
                   updated_at = ?,
                   last_error = NULL
             WHERE event_id = ?
            """,
            (iso_now(), event_id),
        )
        self.connection.commit()

    def update_routing(self, event_id: str, *, tenant: str, bucket_name: str, object_key: str, external_ip: str) -> None:
        self.connection.execute(
            """
            UPDATE queue_items
               SET tenant = ?,
                   bucket_name = ?,
                   object_key = ?,
                   external_ip = ?,
                   updated_at = ?
             WHERE event_id = ?
            """,
            (tenant, bucket_name, object_key, external_ip, iso_now(), event_id),
        )
        self.connection.commit()

    def mark_completed(self, event_id: str, sha256_hash: str) -> None:
        self.connection.execute(
            """
            UPDATE queue_items
               SET status = 'done',
                   sha256 = ?,
                   processed_at = ?,
                   updated_at = ?,
                   last_error = NULL
             WHERE event_id = ?
            """,
            (sha256_hash, iso_now(), iso_now(), event_id),
        )
        self.connection.commit()

    def mark_failed(
        self,
        event_id: str,
        *,
        last_error: str,
        attempts: int,
        retry_backoff_seconds: int,
        retry_backoff_max_seconds: int,
        max_retry_attempts: int,
    ) -> None:
        new_attempts = attempts + 1
        delay_seconds = min(retry_backoff_seconds * max(1, 2 ** max(0, new_attempts - 1)), retry_backoff_max_seconds)
        next_attempt = datetime.fromtimestamp(time.time() + delay_seconds).astimezone().isoformat()

        next_status = "failed"
        if max_retry_attempts > 0 and new_attempts >= max_retry_attempts:
            next_status = "dead"
            next_attempt = "9999-12-31T23:59:59+00:00"

        self.connection.execute(
            """
            UPDATE queue_items
               SET status = ?,
                   attempts = ?,
                   next_attempt_at = ?,
                   last_error = ?,
                   updated_at = ?
             WHERE event_id = ?
            """,
            (next_status, new_attempts, next_attempt, last_error[:2000], iso_now(), event_id),
        )
        self.connection.commit()

    def count_by_status(self) -> dict[str, int]:
        cursor = self.connection.execute(
            """
            SELECT status, COUNT(*) AS total
              FROM queue_items
             GROUP BY status
            """
        )
        return {row["status"]: row["total"] for row in cursor.fetchall()}

    def get_operational_snapshot(self) -> dict[str, Any]:
        counts = self.count_by_status()
        last_upload_row = self.connection.execute(
            """
            SELECT processed_at
              FROM queue_items
             WHERE processed_at IS NOT NULL
             ORDER BY processed_at DESC
             LIMIT 1
            """
        ).fetchone()
        last_event_row = self.connection.execute(
            """
            SELECT captured_at
              FROM queue_items
             ORDER BY created_at DESC
             LIMIT 1
            """
        ).fetchone()
        last_error_row = self.connection.execute(
            """
            SELECT last_error, updated_at
              FROM queue_items
             WHERE last_error IS NOT NULL AND TRIM(last_error) <> ''
             ORDER BY updated_at DESC
             LIMIT 1
            """
        ).fetchone()

        return {
            "status_counts": counts,
            "queue_pending": counts.get("pending", 0) + counts.get("failed", 0) + counts.get("uploading", 0),
            "queue_done": counts.get("done", 0),
            "queue_failed": counts.get("dead", 0),
            "last_upload_at": last_upload_row["processed_at"] if last_upload_row else None,
            "last_event_at": last_event_row["captured_at"] if last_event_row else None,
            "last_error": last_error_row["last_error"] if last_error_row else None,
        }

    def close(self) -> None:
        self.connection.close()


def get_font(size: int):
    for font_path in WINDOWS_FONT_CANDIDATES + LINUX_FONT_CANDIDATES:
        path = Path(font_path)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_watermark_text(*, username: str, hostname: str, internal_ip: str, external_ip: str, captured_at: str) -> str:
    return " | ".join(
        build_watermark_segments(
            username=username,
            hostname=hostname,
            internal_ip=internal_ip,
            external_ip=external_ip,
            captured_at=captured_at,
        )
    )


def build_watermark_segments(*, username: str, hostname: str, internal_ip: str, external_ip: str, captured_at: str) -> list[str]:
    captured_display = datetime.fromisoformat(captured_at).strftime("%d/%m/%Y %H:%M:%S")
    return [
        f"USUARIO: {username}",
        f"HOST: {hostname}",
        f"IP_INTERNO: {internal_ip}",
        f"IP_EXTERNO: {external_ip}",
        f"DATA: {captured_display}",
    ]


def measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def wrap_watermark_segments(
    draw: ImageDraw.ImageDraw,
    *,
    segments: list[str],
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    if max_width <= 0:
        return [" | ".join(segments)]

    lines: list[str] = []
    current_line = ""

    for segment in segments:
        candidate = segment if not current_line else f"{current_line} | {segment}"
        candidate_width, _ = measure_text(draw, candidate, font)
        if candidate_width <= max_width:
            current_line = candidate
            continue

        if current_line:
            lines.append(current_line)
            current_line = segment
        else:
            lines.extend(split_long_watermark_text(draw, segment, font, max_width))
            current_line = ""

    if current_line:
        lines.append(current_line)

    return lines or [" | ".join(segments)]


def split_long_watermark_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    if max_width <= 0:
        return [text]

    tokens = text.split(" ")
    lines: list[str] = []
    current_line = ""

    for token in tokens:
        candidate = token if not current_line else f"{current_line} {token}"
        candidate_width, _ = measure_text(draw, candidate, font)
        if candidate_width <= max_width:
            current_line = candidate
            continue

        if current_line:
            lines.append(current_line)
            current_line = token
        else:
            remaining = token
            while remaining:
                split_index = len(remaining)
                while split_index > 1:
                    prefix = remaining[:split_index]
                    prefix_width, _ = measure_text(draw, prefix, font)
                    if prefix_width <= max_width:
                        break
                    split_index -= 1
                lines.append(remaining[:split_index])
                remaining = remaining[split_index:]
            current_line = ""

    if current_line:
        lines.append(current_line)

    return lines or [text]


def fit_watermark_layout(
    draw: ImageDraw.ImageDraw,
    *,
    image_size: tuple[int, int],
    segments: list[str],
    logo_path: Path,
) -> dict[str, Any]:
    image_width, image_height = image_size
    outer_padding = max(8, min(24, image_width // 50, image_height // 25))
    inner_padding = max(8, min(14, image_width // 90))
    gap = max(8, min(14, image_width // 120))
    min_font_size = 10
    max_font_size = max(min_font_size, min(28, image_width // 40, image_height // 10))

    raw_logo = None
    if logo_path.exists():
        with Image.open(logo_path) as handle:
            raw_logo = handle.convert("RGBA")

    for layout in ("horizontal", "stacked"):
        for font_size in range(max_font_size, min_font_size - 1, -1):
            font = get_font(font_size)
            _, line_height = measure_text(draw, "Ag", font)
            line_gap = max(4, font_size // 3)

            logo = None
            logo_width = 0
            logo_height = 0
            if raw_logo is not None:
                target_logo_height = max(22, line_height + (6 if layout == "horizontal" else 10))
                scale = target_logo_height / max(1, raw_logo.height)
                logo_width = max(1, int(raw_logo.width * scale))
                logo_height = max(1, int(raw_logo.height * scale))
                logo = raw_logo.resize((logo_width, logo_height), Image.LANCZOS)

            max_box_width = max(120, image_width - (outer_padding * 2))
            max_content_width = max(60, max_box_width - (inner_padding * 2))
            reserved_logo_width = (logo_width + gap) if logo and layout == "horizontal" else 0
            max_text_width = max(60, max_content_width - reserved_logo_width)

            if layout == "horizontal" and logo and max_text_width < 120:
                continue

            lines = wrap_watermark_segments(
                draw,
                segments=segments,
                font=font,
                max_width=max_text_width,
            )
            line_widths = [measure_text(draw, line, font)[0] for line in lines]
            text_width = max(line_widths) if line_widths else 0
            text_height = (line_height * len(lines)) + (line_gap * max(0, len(lines) - 1))

            if layout == "horizontal":
                content_width = text_width + reserved_logo_width
                content_height = max(text_height, logo_height)
            else:
                content_width = max(text_width, logo_width)
                content_height = text_height + ((logo_height + gap) if logo else 0)

            box_width = content_width + (inner_padding * 2)
            box_height = content_height + (inner_padding * 2)

            if box_width <= image_width - (outer_padding * 2) and box_height <= image_height - (outer_padding * 2):
                return {
                    "layout": layout,
                    "font": font,
                    "lines": lines,
                    "line_height": line_height,
                    "line_gap": line_gap,
                    "outer_padding": outer_padding,
                    "inner_padding": inner_padding,
                    "gap": gap,
                    "box_width": box_width,
                    "box_height": box_height,
                    "content_width": content_width,
                    "content_height": content_height,
                    "logo": logo,
                    "logo_width": logo_width,
                    "logo_height": logo_height,
                    "text_width": text_width,
                    "text_height": text_height,
                }

    font = get_font(min_font_size)
    _, line_height = measure_text(draw, "Ag", font)
    return {
        "layout": "stacked",
        "font": font,
        "lines": wrap_watermark_segments(draw, segments=segments, font=font, max_width=max(80, image_width - 32)),
        "line_height": line_height,
        "line_gap": max(4, min_font_size // 3),
        "outer_padding": 8,
        "inner_padding": 8,
        "gap": 8,
        "box_width": max(120, image_width - 16),
        "box_height": max(50, min(image_height - 16, image_height // 3)),
        "content_width": max(80, image_width - 32),
        "content_height": max(40, image_height // 5),
        "logo": None,
        "logo_width": 0,
        "logo_height": 0,
        "text_width": max(80, image_width - 32),
        "text_height": line_height,
    }


def add_watermark(
    input_path: Path,
    output_path: Path,
    *,
    logo_path: Path,
    username: str,
    hostname: str,
    internal_ip: str,
    external_ip: str,
    captured_at: str,
) -> None:
    with Image.open(input_path) as input_image:
        image = input_image.convert("RGBA")
        overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        segments = build_watermark_segments(
            username=username,
            hostname=hostname,
            internal_ip=internal_ip,
            external_ip=external_ip,
            captured_at=captured_at,
        )
        layout = fit_watermark_layout(
            draw,
            image_size=image.size,
            segments=segments,
            logo_path=logo_path,
        )

        box_x2 = image.width - layout["outer_padding"]
        box_y2 = image.height - layout["outer_padding"]
        box_x1 = box_x2 - layout["box_width"]
        box_y1 = box_y2 - layout["box_height"]

        draw.rounded_rectangle(
            (box_x1, box_y1, box_x2, box_y2),
            radius=max(8, min(12, layout["box_height"] // 4)),
            fill=(0, 0, 0, 155),
        )

        current_x = box_x1 + layout["inner_padding"]
        current_y = box_y1 + layout["inner_padding"]
        text_x = current_x
        text_y = current_y

        if layout["logo"] is not None:
            if layout["layout"] == "horizontal":
                logo_y = box_y1 + ((layout["box_height"] - layout["logo_height"]) // 2)
                overlay.alpha_composite(layout["logo"], (current_x, logo_y))
                text_x = current_x + layout["logo_width"] + layout["gap"]
                text_y = box_y1 + ((layout["box_height"] - layout["text_height"]) // 2)
            else:
                logo_x = box_x1 + ((layout["box_width"] - layout["logo_width"]) // 2)
                overlay.alpha_composite(layout["logo"], (logo_x, current_y))
                text_y = current_y + layout["logo_height"] + layout["gap"]

        line_y = text_y
        for line in layout["lines"]:
            draw.text((text_x, line_y), line, font=layout["font"], fill=(255, 255, 255, 225))
            line_y += layout["line_height"] + layout["line_gap"]

        result = Image.alpha_composite(image, overlay).convert("RGB")
        result.save(output_path, format="JPEG", quality=95)


def is_file_ready(path: Path) -> bool:
    try:
        if not path.is_file():
            return False

        first_stat = path.stat()
        if first_stat.st_size <= 0:
            return False

        time.sleep(0.2)
        second_stat = path.stat()
        if first_stat.st_size != second_stat.st_size or first_stat.st_mtime_ns != second_stat.st_mtime_ns:
            return False

        with open(path, "rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def collect_spool_files(spool_dir: Path) -> list[Path]:
    if not spool_dir.exists():
        return []

    candidates = []
    for item in spool_dir.rglob("*"):
        if item.is_file() and item.suffix.lower() in VALID_EXTENSIONS and is_file_ready(item):
            candidates.append(item)
    return sorted(candidates)


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_object_key(*, tenant: str, captured_at: str, hostname: str, event_id: str) -> str:
    captured_dt = datetime.fromisoformat(captured_at)
    return "/".join(
        [
            sanitize_segment(tenant, lowercase=True),
            captured_dt.strftime("%Y"),
            captured_dt.strftime("%m"),
            captured_dt.strftime("%d"),
            sanitize_segment(hostname),
            f"{sanitize_segment(event_id)}.jpg",
        ]
    )


def resolve_tenant_and_bucket(config: dict[str, Any], external_ip: str) -> tuple[str, str]:
    routing = config["routing"]
    forced_tenant = str(routing.get("force_tenant", "")).strip()

    if forced_tenant:
        tenant = forced_tenant
    else:
        tenant = routing["external_ip_map"].get(external_ip, routing["default_tenant"])

    bucket_name = routing["tenant_buckets"].get(tenant, routing["default_bucket"])
    return tenant, bucket_name


class ApiClient:
    def __init__(self, config: dict[str, Any], logger: logging.Logger) -> None:
        api_config = config.get("api", {})
        self.enabled = bool(api_config.get("enabled")) and bool(str(api_config.get("base_url", "")).strip())
        self.base_url = str(api_config.get("base_url", "")).rstrip("/")
        self.heartbeat_path = str(api_config.get("heartbeat_path", "/api/v1/agents/heartbeat"))
        self.ingest_confirm_path = str(api_config.get("ingest_confirm_path", "/api/v1/ingest/confirm"))
        self.bearer_token = str(api_config.get("bearer_token", "")).strip()
        self.timeout_seconds = int(api_config.get("timeout_seconds", 5))
        self.agent_id = str(api_config.get("agent_id", "")).strip()
        self.logger = logger
        if self.enabled and not self.bearer_token:
            self.logger.warning(
                "API habilitada sem bearer_token configurado; heartbeat e confirmacao de ingestao vao falhar com 401."
            )

    def send_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self._post_json(self.heartbeat_path, payload)

    def send_ingest_confirm(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self._post_json(self.ingest_confirm_path, payload)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"ScreenshotAuditAgent/{payload.get('agent_version', '0.1')}",
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="ignore").strip()
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="ignore").strip()
            detail = raw_error or exc.reason
            raise RuntimeError(f"HTTP {exc.code} ao chamar {url}: {detail}") from exc


def create_s3_client(config: dict[str, Any]):
    minio = config["minio"]
    return boto3.client(
        "s3",
        endpoint_url=minio["endpoint_url"],
        aws_access_key_id=minio["access_key"],
        aws_secret_access_key=minio["secret_key"],
        region_name=minio["region_name"],
        verify=minio["verify_tls"],
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def upload_file(s3_client, *, local_path: Path, bucket_name: str, object_key: str) -> None:
    s3_client.upload_file(
        str(local_path),
        bucket_name,
        object_key,
        ExtraArgs={"ContentType": "image/jpeg"},
    )


def enqueue_new_files(
    *,
    config: dict[str, Any],
    queue_store: QueueStore,
    external_ip_resolver: ExternalIPResolver,
    logger: logging.Logger,
) -> int:
    hostname = socket.gethostname()
    username = get_effective_username()
    local_ip = get_internal_ip()
    external_ip = external_ip_resolver.get_external_ip()
    queued_count = 0

    for path in collect_spool_files(config["paths"]["spool_dir"]):
        stat = path.stat()
        was_inserted = queue_store.enqueue_file(
            source_path=path,
            source_size=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
            captured_at=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
            hostname=hostname,
            username=username,
            local_ip=local_ip,
            external_ip=external_ip,
        )
        if was_inserted:
            queued_count += 1
            logger.info("Fila local: novo screenshot enfileirado: %s", path.name)

    return queued_count


def process_due_items(
    *,
    config: dict[str, Any],
    queue_store: QueueStore,
    external_ip_resolver: ExternalIPResolver,
    s3_client,
    api_client: ApiClient,
    logger: logging.Logger,
) -> int:
    processed_count = 0
    items = queue_store.fetch_due_items()

    for item in items:
        source_path = Path(item["source_path"])
        event_id = item["event_id"]
        tmp_file = config["paths"]["tmp_dir"] / f"{event_id}.jpg"

        try:
            if not source_path.exists():
                raise FileNotFoundError(f"Arquivo do spool nao encontrado: {source_path}")

            queue_store.mark_uploading(event_id)

            external_ip = item["external_ip"] or external_ip_resolver.get_external_ip()
            tenant, bucket_name = resolve_tenant_and_bucket(config, external_ip)
            object_key = build_object_key(
                tenant=tenant,
                captured_at=item["captured_at"],
                hostname=item["hostname"],
                event_id=event_id,
            )
            queue_store.update_routing(
                event_id,
                tenant=tenant,
                bucket_name=bucket_name,
                object_key=object_key,
                external_ip=external_ip,
            )

            if config["watermark"]["enabled"]:
                add_watermark(
                    source_path,
                    tmp_file,
                    logo_path=config["watermark"]["logo_path"],
                    username=item["username"],
                    hostname=item["hostname"],
                    internal_ip=item["local_ip"] or "N/A",
                    external_ip=external_ip,
                    captured_at=item["captured_at"],
                )
            else:
                with Image.open(source_path) as raw_image:
                    raw_image.convert("RGB").save(tmp_file, format="JPEG", quality=95)

            sha256_hash = compute_sha256(source_path)
            upload_file(
                s3_client,
                local_path=tmp_file,
                bucket_name=bucket_name,
                object_key=object_key,
            )

            queue_store.mark_completed(event_id, sha256_hash)
            processed_count += 1

            if api_client.enabled:
                try:
                    api_response = api_client.send_ingest_confirm(
                        {
                            "event_id": event_id,
                            "agent_id": api_client.agent_id or item["hostname"],
                            "hostname": item["hostname"],
                            "username": item["username"],
                            "local_ip": item["local_ip"],
                            "external_ip": external_ip,
                            "object_bucket": bucket_name,
                            "object_key": object_key,
                            "sha256": sha256_hash,
                            "file_size": item["source_size"],
                            "content_type": "image/jpeg",
                            "captured_at": item["captured_at"],
                            "metadata": {
                                "tenant": tenant,
                                "bucket_name": bucket_name,
                                "object_key": object_key,
                            },
                        }
                    )
                    logger.info(
                        "API confirmada: event_id=%s tenant=%s site=%s",
                        event_id,
                        (api_response or {}).get("tenant", "N/A"),
                        (api_response or {}).get("site", "N/A"),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Falha ao confirmar ingestao na API para %s: %s", event_id, exc)

            if config["delete_local_after_success"]:
                try:
                    source_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Nao foi possivel remover o arquivo do spool %s: %s", source_path, exc)

            tmp_file.unlink(missing_ok=True)
            logger.info(
                "Upload concluido: event_id=%s bucket=%s object_key=%s",
                event_id,
                bucket_name,
                object_key,
            )
        except Exception as exc:  # noqa: BLE001
            tmp_file.unlink(missing_ok=True)
            logger.exception("Falha ao processar event_id=%s", event_id)
            queue_store.mark_failed(
                event_id,
                last_error=str(exc),
                attempts=item["attempts"],
                retry_backoff_seconds=config["retry_backoff_seconds"],
                retry_backoff_max_seconds=config["retry_backoff_max_seconds"],
                max_retry_attempts=config["max_retry_attempts"],
            )

    return processed_count


def maybe_send_heartbeat(
    *,
    config: dict[str, Any],
    queue_store: QueueStore,
    external_ip_resolver: ExternalIPResolver,
    api_client: ApiClient,
    logger: logging.Logger,
    last_sent_at: float,
    force: bool = False,
) -> float:
    if not api_client.enabled:
        return last_sent_at

    interval_seconds = int(config["api"].get("heartbeat_interval_seconds", 15))
    now = time.monotonic()
    if not force and (now - last_sent_at) < interval_seconds:
        return last_sent_at

    hostname = socket.gethostname()
    snapshot = queue_store.get_operational_snapshot()
    payload = {
        "agent_id": api_client.agent_id or hostname,
        "hostname": hostname,
        "username": get_effective_username(),
        "local_ip": get_internal_ip(),
        "external_ip": external_ip_resolver.get_external_ip(),
        "service_status": "running",
        "agent_version": config["agent_version"],
        "sharex_profile_version": config["sharex_profile_version"],
        "queue_pending": snapshot["queue_pending"],
        "queue_done": snapshot["queue_done"],
        "queue_failed": snapshot["queue_failed"],
        "last_error": snapshot["last_error"],
        "last_event_at": snapshot["last_event_at"],
        "last_upload_at": snapshot["last_upload_at"],
        "heartbeat_at": iso_now(),
        "metadata": {
            "status_counts": snapshot["status_counts"],
            "spool_dir": str(config["paths"]["spool_dir"]),
            "db_path": str(config["paths"]["db_path"]),
        },
    }

    try:
        response = api_client.send_heartbeat(payload)
        logger.info(
            "Heartbeat enviado: agent_id=%s tenant=%s site=%s resolved_by=%s",
            payload["agent_id"],
            (response or {}).get("tenant", "N/A"),
            (response or {}).get("site", "N/A"),
            (response or {}).get("resolved_by", "N/A"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Falha ao enviar heartbeat para a API: %s", exc)
        return last_sent_at

    return now


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Screenshot Audit Agent MVP")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Caminho do agent_config.json",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Executa uma passada unica no spool e sai.",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERRO] Nao foi possivel carregar a configuracao: {exc}", file=sys.stderr)
        return 1

    ensure_directories(config)
    logger = setup_logging(config["paths"]["log_path"], config["log_level"])
    logger.info("Iniciando Screenshot Audit Agent")
    logger.info(
        "Versao do agent: %s | ShareX profile: %s",
        config["agent_version"],
        config["sharex_profile_version"],
    )
    logger.info("Spool: %s", config["paths"]["spool_dir"])
    logger.info("Tmp: %s", config["paths"]["tmp_dir"])
    logger.info("Queue DB: %s", config["paths"]["db_path"])
    logger.info("API heartbeat habilitado: %s", bool(config.get("api", {}).get("enabled")))

    queue_store = QueueStore(config["paths"]["db_path"])
    recovered_items = queue_store.reset_inflight_items()
    if recovered_items:
        logger.warning("%s item(ns) de upload foram recolocados para retry apos reinicio.", recovered_items)

    external_ip_resolver = ExternalIPResolver(
        services=list(config["external_ip_services"]),
        timeout_seconds=int(config["external_ip_timeout_seconds"]),
        cache_ttl_seconds=int(config["external_ip_cache_ttl_seconds"]),
    )
    s3_client = create_s3_client(config)
    api_client = ApiClient(config, logger)
    last_heartbeat_sent = 0.0

    try:
        last_heartbeat_sent = maybe_send_heartbeat(
            config=config,
            queue_store=queue_store,
            external_ip_resolver=external_ip_resolver,
            api_client=api_client,
            logger=logger,
            last_sent_at=last_heartbeat_sent,
            force=True,
        )
        while True:
            queued_count = enqueue_new_files(
                config=config,
                queue_store=queue_store,
                external_ip_resolver=external_ip_resolver,
                logger=logger,
            )
            processed_count = process_due_items(
                config=config,
                queue_store=queue_store,
                external_ip_resolver=external_ip_resolver,
                s3_client=s3_client,
                api_client=api_client,
                logger=logger,
            )

            last_heartbeat_sent = maybe_send_heartbeat(
                config=config,
                queue_store=queue_store,
                external_ip_resolver=external_ip_resolver,
                api_client=api_client,
                logger=logger,
                last_sent_at=last_heartbeat_sent,
                force=bool(queued_count or processed_count),
            )

            if queued_count or processed_count:
                logger.info("Resumo do ciclo: enfileirados=%s processados=%s status=%s", queued_count, processed_count, queue_store.count_by_status())

            if args.once:
                break

            time.sleep(config["poll_interval_seconds"])
    except KeyboardInterrupt:
        logger.info("Encerrando agent por interrupcao manual.")
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Erro de cliente S3/MinIO nao tratado: %s", exc)
        return 1
    finally:
        queue_store.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
