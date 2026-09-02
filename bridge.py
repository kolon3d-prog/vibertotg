#!/usr/bin/env python3
"""Viber Desktop local DB (SEE) -> Telegram Bot API forum topic."""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import viber_db

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.env"
STATE_FILE = ROOT / "state.json"
LOG_FILE = ROOT / "bridge.log"
TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_TG = 4000
MAX_CAPTION = 1024
PHOTO_MAX = 9_500_000
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".3gp", ".m4v"}
AUDIO_EXT = {".ogg", ".opus", ".m4a", ".mp3", ".wav", ".aac"}


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if CONFIG_FILE.exists():
        for raw in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    for key in (
        "TG_BOT_TOKEN",
        "TG_CHAT_ID",
        "TG_THREAD_ID",
        "TG_TOPIC",
        "VIBER_GROUP",
        "VIBER_HEXKEY",
        "POLL_SEC",
    ):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def upsert_config(key: str, value: str) -> None:
    lines: list[str] = []
    if CONFIG_FILE.exists():
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    found = False
    out: list[str] = []
    for line in lines:
        raw = line.strip()
        if raw.startswith("#") or "=" not in raw:
            out.append(line)
            continue
        name = raw.split("=", 1)[0].strip()
        if name == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    CONFIG_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def tg_call(token: str, method: str, data: bytes, headers: dict, timeout: int = 60) -> dict:
    url = TG_API.format(token=token, method=method)
    req = urllib.request.Request(url, data=data, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("ok"):
                return payload
            die(f"telegram: {payload}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                err = json.loads(raw)
            except json.JSONDecodeError:
                err = {}
            if exc.code == 429:
                wait = int((err.get("parameters") or {}).get("retry_after") or 2) + 1
                time.sleep(wait)
                req = urllib.request.Request(url, data=data, headers=headers)
                continue
            die(f"telegram HTTP {exc.code}: {raw[:400]}")
        except urllib.error.URLError:
            time.sleep(1 + attempt)
            req = urllib.request.Request(url, data=data, headers=headers)
    die("telegram: send failed after retries")
    raise AssertionError


def topic_meta(msg: dict) -> tuple[str, int | None]:
    thread = msg.get("message_thread_id")
    name = ""
    for blob in (msg, msg.get("reply_to_message") or {}):
        created = blob.get("forum_topic_created") or {}
        edited = blob.get("forum_topic_edited") or {}
        if created.get("name"):
            name = created["name"]
        if edited.get("name"):
            name = edited["name"]
    if not name and msg.get("is_topic_message") and thread == 1:
        name = "General"
    return name, int(thread) if thread is not None else None


def topic_match(name: str, needle: str) -> bool:
    if not needle:
        return False
    return needle.casefold() in (name or "").casefold()


def send_text(token: str, chat_id: str, text: str, thread_id: str = "") -> None:
    payload = {
        "chat_id": chat_id,
        "text": text[:MAX_TG],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if thread_id:
        payload["message_thread_id"] = str(thread_id)
    body = urllib.parse.urlencode(payload).encode()
    tg_call(
        token,
        "sendMessage",
        body,
        {"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )


def multipart(fields: dict[str, str], files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    boundary = "----vibertotg" + os.urandom(8).hex()
    chunks: list[bytes] = []
    for key, val in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{val}\r\n"
            ).encode()
        )
    for field, path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        chunks.append(head)
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def send_file(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    thread_id: str,
    kind: str,
) -> None:
    fields = {"chat_id": chat_id}
    if thread_id:
        fields["message_thread_id"] = str(thread_id)
    if caption:
        fields["caption"] = caption[:MAX_CAPTION]
        fields["parse_mode"] = "HTML"
    ext = path.suffix.lower()
    method, field = "sendDocument", "document"
    if kind == "photo" or (kind != "video" and kind != "voice" and ext in IMAGE_EXT and path.stat().st_size <= PHOTO_MAX):
        method, field = "sendPhoto", "photo"
    if kind == "video" or ext in VIDEO_EXT:
        method, field = "sendVideo", "video"
    if kind == "voice" or ext in AUDIO_EXT:
        method, field = "sendVoice", "voice"
    if kind == "sticker" and ext in IMAGE_EXT:
        method, field = "sendPhoto", "photo"
    body, boundary = multipart(fields, [(field, path)])
    tg_call(token, method, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def send_media_group(
    token: str, chat_id: str, paths: list[Path], caption: str, thread_id: str
) -> None:
    media = []
    files: list[tuple[str, Path]] = []
    for i, path in enumerate(paths[:10]):
        attach = f"file{i}"
        ext = path.suffix.lower()
        kind = "photo" if ext in IMAGE_EXT and path.stat().st_size <= PHOTO_MAX else "document"
        if ext in VIDEO_EXT:
            kind = "video"
        item: dict = {"type": kind, "media": f"attach://{attach}"}
        if i == 0 and caption:
            item["caption"] = caption[:MAX_CAPTION]
            item["parse_mode"] = "HTML"
        media.append(item)
        files.append((attach, path))
    fields = {"chat_id": chat_id, "media": json.dumps(media)}
    if thread_id:
        fields["message_thread_id"] = str(thread_id)
    body, boundary = multipart(fields, files)
    tg_call(
        token,
        "sendMediaGroup",
        body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def load_state() -> dict:
    empty = {"last_event_id": 0, "tg_thread_id": "", "chat_id": 0}
    if not STATE_FILE.exists():
        return empty
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty
    data.setdefault("last_event_id", 0)
    data.setdefault("tg_thread_id", "")
    data.setdefault("chat_id", 0)
    return data


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(
            {
                "last_event_id": int(state.get("last_event_id") or 0),
                "tg_thread_id": str(state.get("tg_thread_id") or ""),
                "chat_id": int(state.get("chat_id") or 0),
            }
        ),
        encoding="utf-8",
    )


def format_caption(item: dict, extra: str = "") -> str:
    sender = html.escape(item.get("sender") or "")
    body = html.escape(item.get("body") or "")
    extra = html.escape(extra) if extra else ""
    parts = []
    if sender:
        parts.append(f"<b>{sender}</b>")
    if body:
        parts.append(body)
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def _ready_file(path: str | Path | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    if p.is_file() and p.stat().st_size > 0:
        return p
    return None


def wait_media(item: dict, hexkey: str, timeout: float = 20.0) -> Path | None:
    deadline = time.time() + timeout
    current = item
    while True:
        ready = _ready_file(current.get("media")) or _ready_file(current.get("sticker"))
        if ready:
            return ready
        if current["kind"] == "sticker" and current.get("sticker_id"):
            found = viber_db.sticker_path(int(current["sticker_id"]))
            if found:
                return found
        if time.time() >= deadline:
            return None
        time.sleep(0.4)
        fresh = viber_db.fetch_event(hexkey, int(current["event_id"]))
        if fresh:
            current = fresh


def forward_one(token: str, chat_id: str, thread_id: str, item: dict, hexkey: str) -> bool:
    kind = item["kind"]
    if kind == "skip":
        return False
    caption = format_caption(item)
    if kind == "text":
        if not caption:
            return False
        send_text(token, chat_id, caption, thread_id)
        return True
    media = wait_media(item, hexkey)
    if media:
        send_file(token, chat_id, media, caption, thread_id, kind)
        return True
    extra = {
        "photo": "photo",
        "video": "video",
        "voice": "voice message",
        "document": item.get("filename") or "file",
        "sticker": "sticker",
    }.get(kind, kind)
    text = format_caption(item, extra)
    if text:
        send_text(token, chat_id, text, thread_id)
        return True
    return False


def cmd_setup_key() -> None:
    key = ""
    try:
        key = viber_db.capture_hexkey(timeout_sec=8)
    except RuntimeError as exc:
        key = (load_config().get("VIBER_HEXKEY") or "").strip()
        cached = ROOT / "_found_key.txt"
        if not key and cached.is_file():
            raw = cached.read_text(encoding="utf-8").strip()
            if ":" in raw:
                key = raw.split(":", 1)[1].strip()
        if not key:
            die(str(exc))
        print("key not in Viber memory right now, using the saved one")
    n = viber_db.verify_key(key)
    upsert_config("VIBER_HEXKEY", key)
    print(f"wrote key to config.env, tables in db: {n}")


def cmd_list_chats(cfg: dict[str, str]) -> None:
    key = cfg.get("VIBER_HEXKEY") or ""
    if not key:
        die("no VIBER_HEXKEY — run python bridge.py --setup-key first")
    for chat_id, name in viber_db.list_chats(key):
        print(f"{chat_id:4}  {name}")


def cmd_discover(token: str, topic_needle: str = "") -> None:
    print("Waiting 60s for a Telegram message.")
    print("Send /id in the topic you want, not General.")
    deadline = time.time() + 60
    offset = 0
    needle = (topic_needle or "").strip()
    while time.time() < deadline:
        url = TG_API.format(token=token, method="getUpdates")
        q = urllib.parse.urlencode({"timeout": 25, "offset": offset, "limit": 20})
        req = urllib.request.Request(url + "?" + q)
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.URLError as exc:
            print("network:", exc)
            time.sleep(2)
            continue
        if not payload.get("ok"):
            die(f"telegram: {payload}")
        for upd in payload.get("result") or []:
            offset = max(offset, int(upd["update_id"]) + 1)
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if not chat:
                continue
            name, thread = topic_meta(msg)
            print(
                f"chat_id={chat.get('id')}  type={chat.get('type')}  "
                f"title={chat.get('title') or chat.get('username') or chat.get('first_name')}  "
                f"thread_id={thread}  topic={name or '-'}"
            )
            upsert_config("TG_CHAT_ID", str(chat.get("id")))
            if not thread or thread == 1:
                print("that's General (or no topic). send /id in the real topic.")
                continue
            if needle and name and not topic_match(name, needle):
                print(f"that's {name}, not {needle}. send /id in the right topic.")
                continue
            upsert_config("TG_THREAD_ID", str(thread))
            print(f"topic {name or thread} -> TG_THREAD_ID={thread} saved to config.env")
            return
    die("nothing arrived: bot not in the group, privacy mode in BotFather, or no message")


def cmd_test(token: str, chat_id: str, thread_id: str = "") -> None:
    send_text(token, chat_id, "<b>vibertotg</b>\nbridge is up (SQL, no OCR)", thread_id)
    print("sent a test message")


def run_loop(cfg: dict[str, str]) -> None:
    token = cfg.get("TG_BOT_TOKEN") or ""
    chat_id = cfg.get("TG_CHAT_ID") or ""
    thread_id = (cfg.get("TG_THREAD_ID") or "").strip()
    group = (cfg.get("VIBER_GROUP") or "").strip()
    hexkey = (cfg.get("VIBER_HEXKEY") or "").strip()
    poll = float(cfg.get("POLL_SEC") or 1)
    poll = max(0.5, min(poll, 10))
    if not token or not chat_id:
        die("set TG_BOT_TOKEN and TG_CHAT_ID in config.env")
    if not thread_id:
        die("no TG_THREAD_ID. run python bridge.py --discover and send /id in the topic")
    if not hexkey:
        die("no VIBER_HEXKEY. start Viber Desktop and run python bridge.py --setup-key")
    if not group:
        die("set VIBER_GROUP")
    viber_chat_id, viber_name = viber_db.resolve_chat_id(hexkey, group)
    state = load_state()
    prev_chat = int(state.get("chat_id") or 0)
    state["tg_thread_id"] = thread_id
    if not int(state.get("last_event_id") or 0) or prev_chat != viber_chat_id:
        state["last_event_id"] = viber_db.max_event_id(hexkey, viber_chat_id)
        log(f"starting at tail event_id={state['last_event_id']} (no backfill)")
    state["chat_id"] = viber_chat_id
    save_state(state)
    log(
        f"Viber '{viber_name}' (id {viber_chat_id}) -> TG {chat_id} thread {thread_id}, "
        f"poll {poll}s. Ctrl+C to stop."
    )
    forwarded = 0
    last_beat = time.time()
    while True:
        try:
            items = viber_db.fetch_new(hexkey, viber_chat_id, int(state["last_event_id"]))
        except RuntimeError as exc:
            log(f"db: {exc}")
            time.sleep(2)
            continue
        for item in items:
            state["last_event_id"] = item["event_id"]
            save_state(state)
            if item["kind"] == "skip":
                continue
            try:
                if forward_one(token, chat_id, thread_id, item, hexkey):
                    forwarded += 1
                    log(f"ok event={item['event_id']} {item['kind']}")
            except Exception as exc:
                log(f"send fail event={item['event_id']}: {exc}")
        now = time.time()
        if now - last_beat >= 60:
            log(f"alive forwarded={forwarded} last_event={state['last_event_id']}")
            last_beat = now
        time.sleep(poll)


def main() -> None:
    if sys.platform != "win32":
        die("this build is Windows-only")
    parser = argparse.ArgumentParser(description="Viber group -> Telegram topic (Windows, local DB)")
    parser.add_argument("--setup-key", action="store_true", help="read the DB key from a running Viber")
    parser.add_argument("--list-chats", action="store_true", help="list chats from viber.db")
    parser.add_argument("--discover", action="store_true", help="pick up chat_id and topic id")
    parser.add_argument("--test", action="store_true", help="send a test message to Telegram")
    args = parser.parse_args()
    cfg = load_config()
    token = cfg.get("TG_BOT_TOKEN") or ""
    if args.setup_key:
        cmd_setup_key()
        return
    if args.list_chats:
        cmd_list_chats(cfg)
        return
    if args.discover:
        if not token:
            die("TG_BOT_TOKEN missing from config.env")
        cmd_discover(token, cfg.get("TG_TOPIC") or "")
        return
    if args.test:
        if not token or not cfg.get("TG_CHAT_ID"):
            die("need TG_BOT_TOKEN and TG_CHAT_ID")
        cmd_test(token, cfg["TG_CHAT_ID"], cfg.get("TG_THREAD_ID") or "")
        return
    run_loop(cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
