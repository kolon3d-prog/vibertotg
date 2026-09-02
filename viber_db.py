"""Read this PC's Viber Desktop viber.db via Viber's own SEE-enabled qsqlite plugin."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = ROOT / "qt-plugins"
STICKERS = Path(os.environ.get("APPDATA", "")) / "ViberPC" / "data" / "stickers"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PRAGMA_NEEDLE = "PRAGMA hexkey='".encode("utf-16le")

# MessageType in Messages / MessageInfo
TYPE_TEXT = 1
TYPE_PHOTO = 2
TYPE_VIDEO = 3
TYPE_STICKER = 4
TYPE_RICH = 8
TYPE_LINK = 9
TYPE_FILEISH = 11
TYPE_PIN = 15
SKIP_TYPES = {0, 66, 72, 77}

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def viber_install() -> Path:
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Viber"
    if not (p / "plugins" / "sqldrivers" / "qsqlite.dll").is_file():
        raise FileNotFoundError(f"Viber Desktop not found: {p}")
    return p


def find_viber_db() -> Path:
    root = Path(os.environ.get("APPDATA", "")) / "ViberPC"
    dbs = [p for p in root.glob("*/viber.db") if p.is_file()]
    if not dbs:
        raise FileNotFoundError(f"no viber.db under {root}")
    return max(dbs, key=lambda p: p.stat().st_size)


def ensure_plugin() -> Path:
    src = viber_install() / "plugins" / "sqldrivers" / "qsqlite.dll"
    dest_dir = PLUGIN_ROOT / "sqldrivers"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "qsqlite.dll"
    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dest)
    return PLUGIN_ROOT


def _qt():
    ensure_plugin()
    os.environ["QT_PLUGIN_PATH"] = str(PLUGIN_ROOT)
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.setLibraryPaths([str(PLUGIN_ROOT)])
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    from PySide6.QtSql import QSqlDatabase, QSqlQuery

    return app, QSqlDatabase, QSqlQuery


def open_db(hexkey: str):
    app, QSqlDatabase, QSqlQuery = _qt()
    name = "vibertotg"
    if QSqlDatabase.contains(name):
        QSqlDatabase.removeDatabase(name)
    db = QSqlDatabase.addDatabase("QSQLITE", name)
    db.setConnectOptions("QSQLITE_OPEN_URI;QSQLITE_OPEN_READONLY;QSQLITE_BUSY_TIMEOUT=8000")
    uri = find_viber_db().resolve().as_uri() + f"?mode=ro&hexkey={hexkey}"
    db.setDatabaseName(uri)
    if not db.open():
        raise RuntimeError(db.lastError().text() or "failed to open viber.db")
    return db, QSqlQuery(db)


def query_rows(q, sql: str, binds: list | None = None) -> list[dict]:
    if binds:
        q.prepare(sql)
        for i, val in enumerate(binds, 1):
            q.bindValue(i - 1, val)
        ok = q.exec()
    else:
        ok = q.exec(sql)
    if not ok:
        raise RuntimeError(q.lastError().text() or sql)
    rows: list[dict] = []
    rec = q.record()
    cols = [rec.fieldName(i) for i in range(rec.count())]
    while q.next():
        rows.append({c: q.value(i) for i, c in enumerate(cols)})
    return rows


def list_chats(hexkey: str) -> list[tuple[int, str]]:
    db, q = open_db(hexkey)
    try:
        rows = query_rows(q, "SELECT ChatID, Name FROM ChatInfo ORDER BY ChatID")
    finally:
        db.close()
    out = []
    for row in rows:
        name = str(row["Name"] or "").strip()
        if name:
            out.append((int(row["ChatID"]), name))
    return out


def resolve_chat_id(hexkey: str, needle: str) -> tuple[int, str]:
    chats = list_chats(hexkey)
    if not needle:
        raise RuntimeError("empty VIBER_GROUP")
    fold = needle.casefold()
    exact = [(i, n) for i, n in chats if n.casefold() == fold]
    if exact:
        return exact[0]
    hits = [(i, n) for i, n in chats if fold in n.casefold()]
    if not hits:
        names = ", ".join(n for _, n in chats[:20])
        raise RuntimeError(f"Viber chat '{needle}' not found. have: {names}")
    hits.sort(key=lambda x: len(x[1]))
    return hits[0]


def sticker_path(sticker_id: int) -> Path | None:
    if not sticker_id:
        return None
    name = f"{int(sticker_id):08d}.png"
    if not STICKERS.is_dir():
        return None
    for path in STICKERS.rglob(name):
        if path.is_file():
            return path
    return None


def _parse_info(raw) -> dict:
    if not raw:
        return {}
    text = str(raw).strip()
    if not text.startswith("{"):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _as_path(raw) -> Path | None:
    if not raw:
        return None
    text = str(raw).strip().strip('"')
    if not text:
        return None
    return Path(text)


EVENT_SELECT = """
SELECT e.EventID, e.TimeStamp, e.Direction, e.Type AS EventType,
       e.ChatID, e.ContactID,
       m.Type AS MessageType, m.Body, m.PayloadPath, m.ThumbnailPath,
       m.StickerID, m.PttID, m.Duration, m.Info,
       c.Name AS ContactName, c.ClientName,
       d.TempFileName
FROM Events e
JOIN Messages m ON m.EventID = e.EventID
LEFT JOIN Contact c ON c.ContactID = e.ContactID
LEFT JOIN DownloadFile d ON d.EventID = e.EventID
"""


def classify(row: dict) -> dict:
    event_type = int(row.get("EventType") or 0)
    msg_type = int(row.get("MessageType") or 0)
    body = str(row.get("Body") or "").strip()
    info = _parse_info(row.get("Info") or row.get("MessageInfo"))
    payload = _as_path(row.get("PayloadPath"))
    thumb = _as_path(row.get("ThumbnailPath"))
    tmp = _as_path(row.get("TempFileName"))
    media = payload or tmp
    kind = "skip"
    if event_type == 3 or msg_type in SKIP_TYPES:
        kind = "skip"
    elif msg_type == TYPE_PHOTO:
        kind = "photo"
    elif msg_type == TYPE_VIDEO:
        kind = "video"
    elif msg_type == TYPE_STICKER:
        kind = "sticker"
    elif msg_type == TYPE_FILEISH:
        if "audio_ptt" in info:
            kind = "voice"
        elif (info.get("fileInfo") or {}).get("ContentType") == "FILE" or (
            info.get("fileInfo") or {}
        ).get("FileExt"):
            kind = "document"
        elif media:
            kind = "document"
        elif body:
            kind = "text"
        else:
            kind = "skip"
    elif msg_type in (TYPE_TEXT, TYPE_RICH, TYPE_LINK, TYPE_PIN):
        kind = "text" if body else "skip"
    elif body:
        kind = "text"
    sender = str(row.get("ClientName") or row.get("ContactName") or "").strip()
    if int(row.get("Direction") or 0) == 1:
        sender = sender or "me"
    sticker_id = int(row.get("StickerID") or 0)
    duration = int(row.get("Duration") or 0)
    file_info = info.get("fileInfo") or {}
    return {
        "event_id": int(row["EventID"]),
        "kind": kind,
        "sender": sender,
        "body": body,
        "media": str(media) if media else "",
        "thumb": str(thumb) if thumb else "",
        "sticker_id": sticker_id,
        "sticker": str(sticker_path(sticker_id) or "") if kind == "sticker" else "",
        "duration_ms": duration,
        "filename": str(file_info.get("FileName") or ""),
        "ext": str(file_info.get("FileExt") or ""),
        "msg_type": msg_type,
    }


def fetch_new(hexkey: str, chat_id: int, after_event_id: int) -> list[dict]:
    db, q = open_db(hexkey)
    try:
        cid = int(chat_id)
        after = int(after_event_id)
        rows = query_rows(
            q,
            EVENT_SELECT
            + f" WHERE e.ChatID = {cid} AND e.EventID > {after} ORDER BY e.EventID",
        )
    finally:
        db.close()
    return [classify(r) for r in rows]


def fetch_event(hexkey: str, event_id: int) -> dict | None:
    db, q = open_db(hexkey)
    try:
        rows = query_rows(q, EVENT_SELECT + f" WHERE e.EventID = {int(event_id)}")
    finally:
        db.close()
    return classify(rows[0]) if rows else None


def max_event_id(hexkey: str, chat_id: int) -> int:
    db, q = open_db(hexkey)
    try:
        rows = query_rows(
            q,
            f"SELECT COALESCE(MAX(EventID), 0) AS m FROM Events WHERE ChatID = {int(chat_id)}",
        )
    finally:
        db.close()
    return int(rows[0]["m"] if rows else 0)


def viber_pids() -> list[int]:
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out: list[int] = []
    if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
        while True:
            if pe.szExeFile.lower() == "viber.exe":
                out.append(int(pe.th32ProcessID))
            if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(snap)
    return out


def _scan_pragma_keys(pids: list[int]) -> list[str]:
    read = kernel32.ReadProcessMemory
    read.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    vq = kernel32.VirtualQueryEx
    vq.restype = ctypes.c_size_t
    found: list[str] = []
    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            continue
        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        try:
            while True:
                if not vq(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    break
                base = mbi.BaseAddress or 0
                size = mbi.RegionSize or 0
                prot = mbi.Protect or 0
                nxt = base + size
                if nxt <= addr:
                    break
                readable = mbi.State == MEM_COMMIT and (prot & 0xEE) and not (prot & 0x101)
                if readable and 0 < size <= 64 * 1024 * 1024:
                    buf = (ctypes.c_char * size)()
                    nread = ctypes.c_size_t(0)
                    if read(handle, ctypes.c_void_p(base), buf, size, ctypes.byref(nread)) and nread.value:
                        blob = bytes(buf[: nread.value])
                        start = 0
                        while True:
                            i = blob.find(PRAGMA_NEEDLE, start)
                            if i < 0:
                                break
                            sl = blob[i : i + 400]
                            if len(sl) % 2:
                                sl = sl[:-1]
                            text = sl.decode("utf-16le", "replace")
                            if text.startswith("PRAGMA hexkey='"):
                                key = text.split("PRAGMA hexkey='", 1)[1].split("'", 1)[0]
                                if len(key) >= 32 and key not in found:
                                    found.append(key)
                            start = i + 2
                addr = nxt
                if addr >= 0x7FFFFFFFFFFF:
                    break
        finally:
            kernel32.CloseHandle(handle)
    return found


def capture_hexkey(timeout_sec: float = 8.0) -> str:
    """Read PRAGMA hexkey='...' that this Viber Desktop already built (UTF-16)."""
    deadline = time.time() + timeout_sec
    last_err = "Viber.exe is not running"
    while time.time() <= deadline:
        pids = viber_pids()
        if not pids:
            last_err = "Viber.exe is not running — start Viber Desktop"
            time.sleep(1)
            continue
        found = _scan_pragma_keys(pids)
        if found:
            return found[0]
        last_err = "key not in memory yet"
        time.sleep(0.6)
    raise RuntimeError(
        f"{last_err}. restart Viber Desktop and run python bridge.py --setup-key again"
    )


def verify_key(hexkey: str) -> int:
    db, q = open_db(hexkey)
    try:
        rows = query_rows(q, "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'")
    finally:
        db.close()
    n = int(rows[0]["n"] if rows else 0)
    if n < 3:
        raise RuntimeError("key rejected — sqlite_master is empty")
    return n
