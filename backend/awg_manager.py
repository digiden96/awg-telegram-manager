#!/usr/bin/python3
"""Transaction-safe AmneziaWG peer, usage and access-policy manager."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from zoneinfo import ZoneInfo

STATE = Path(os.environ.get("AWG_MANAGER_STATE", "/var/lib/awg-manager"))
MANAGER_SETTINGS = Path(os.environ.get("AWG_MANAGER_SETTINGS", "/etc/awg-manager/config.json"))
SETTINGS = json.loads(MANAGER_SETTINGS.read_text()) if MANAGER_SETTINGS.exists() else {}
DB = STATE / "manager.db"
LOCK = STATE / "manager.lock"
AWG_CONFIG = Path(SETTINGS.get("config_path", os.environ.get("AWG_MANAGER_CONFIG", "/etc/amnezia/amneziawg/awg0.conf")))
AWG = SETTINGS.get("awg", os.environ.get("AWG_MANAGER_AWG", "/usr/local/bin/awg"))
AWG_QUICK = SETTINGS.get("quick", os.environ.get("AWG_MANAGER_QUICK", "/usr/local/bin/awg-quick"))
INTERFACE = SETTINGS.get("interface", os.environ.get("AWG_MANAGER_INTERFACE", "awg0"))
TIMEZONE = ZoneInfo(os.environ.get("AWG_MANAGER_TIMEZONE", "Europe/Moscow"))
NETWORK = ipaddress.ip_network(SETTINGS.get("network", os.environ.get("AWG_MANAGER_NETWORK", "10.66.66.0/24")))
SERVER_IP = ipaddress.ip_address(SETTINGS.get("server_ip", os.environ.get("AWG_MANAGER_SERVER_IP", "10.66.66.1")))


def server_endpoint() -> str:
    try:
        endpoint = str(json.loads(MANAGER_SETTINGS.read_text())["endpoint"]).strip()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("Не задан endpoint в /etc/awg-manager/config.json") from error
    if not endpoint or len(endpoint) > 255 or any(char.isspace() for char in endpoint):
        raise ValueError("Некорректный endpoint")
    return endpoint


def now_ts() -> int:
    return int(time.time())


def run(args: list[str], text: str | None = None) -> str:
    return subprocess.run(args, input=text, text=True, check=True,
                          capture_output=True, timeout=25).stdout.strip()


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def connect() -> sqlite3.Connection:
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(DB, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS peers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        note TEXT NOT NULL DEFAULT '',
        private_key TEXT NOT NULL,
        public_key TEXT NOT NULL UNIQUE,
        preshared_key TEXT NOT NULL,
        address TEXT NOT NULL UNIQUE,
        manual_enabled INTEGER NOT NULL DEFAULT 1,
        effective_enabled INTEGER NOT NULL DEFAULT 1,
        block_reason TEXT,
        created_at INTEGER NOT NULL,
        expires_at INTEGER,
        quota_bytes INTEGER,
        quota_period TEXT NOT NULL DEFAULT 'lifetime',
        quota_period_key TEXT,
        quota_used INTEGER NOT NULL DEFAULT 0,
        total_rx INTEGER NOT NULL DEFAULT 0,
        total_tx INTEGER NOT NULL DEFAULT 0,
        last_rx INTEGER NOT NULL DEFAULT 0,
        last_tx INTEGER NOT NULL DEFAULT 0,
        last_handshake INTEGER NOT NULL DEFAULT 0,
        deleted_at INTEGER
      );
      CREATE TABLE IF NOT EXISTS schedules (
        id TEXT PRIMARY KEY,
        peer_id TEXT NOT NULL REFERENCES peers(id),
        action TEXT NOT NULL CHECK(action IN ('enable','disable')),
        cron TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_fired_minute INTEGER,
        created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        peer_id TEXT,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL,
        notified_at INTEGER
      );
      CREATE INDEX IF NOT EXISTS events_pending ON events(notified_at, id);
    """)
    os.chmod(DB, 0o600)
    return connection


@contextlib.contextmanager
def locked():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open("a") as stream:
        os.chmod(LOCK, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def validate_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value):
        raise ValueError("Имя: 1–32 латинских символа, цифры, дефис или подчёркивание")
    return value


def find_peer(connection: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM peers WHERE deleted_at IS NULL AND (id=? OR name=? COLLATE NOCASE)",
        (ref, ref)).fetchone()
    if row is None:
        raise ValueError("Клиент не найден")
    return row


def awg_stats() -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    output = run([AWG, "show", INTERFACE, "dump"])
    for line in output.splitlines()[1:]:
        columns = line.split("\t")
        if len(columns) >= 7:
            stats[columns[0]] = {"handshake": int(columns[4]), "rx": int(columns[5]), "tx": int(columns[6])}
    return stats


def period_key(period: str, stamp: int) -> str:
    local = dt.datetime.fromtimestamp(stamp, TIMEZONE)
    if period == "day":
        return local.strftime("%Y-%m-%d")
    if period == "month":
        return local.strftime("%Y-%m")
    return "lifetime"


def sample_usage(connection: sqlite3.Connection, stamp: int) -> None:
    stats = awg_stats()
    for peer in connection.execute("SELECT * FROM peers WHERE deleted_at IS NULL"):
        current = stats.get(peer["public_key"])
        key = period_key(peer["quota_period"], stamp)
        used = peer["quota_used"] if peer["quota_period_key"] == key else 0
        if current is None:
            connection.execute("UPDATE peers SET quota_period_key=?,quota_used=?,last_rx=0,last_tx=0 WHERE id=?", (key, used, peer["id"]))
            continue
        delta_rx = current["rx"] - peer["last_rx"] if current["rx"] >= peer["last_rx"] else current["rx"]
        delta_tx = current["tx"] - peer["last_tx"] if current["tx"] >= peer["last_tx"] else current["tx"]
        connection.execute("""
          UPDATE peers SET total_rx=total_rx+?, total_tx=total_tx+?, quota_used=?,
            quota_period_key=?, last_rx=?, last_tx=?, last_handshake=? WHERE id=?
        """, (delta_rx, delta_tx, used + delta_rx + delta_tx, key, current["rx"], current["tx"],
              current["handshake"], peer["id"]))


def cron_values(field: str, minimum: int, maximum: int, sunday_alias: bool = False) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        base, slash, step_text = part.partition("/")
        step = int(step_text) if slash else 1
        if step < 1 or step > maximum - minimum + 1:
            raise ValueError("Некорректный шаг cron")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            start, end = int(left), int(right)
        else:
            start = end = int(base)
        if start < minimum or start > maximum or end < minimum or end > maximum or end < start:
            raise ValueError("Значение cron вне диапазона")
        values.update(value % 7 if sunday_alias else value for value in range(start, end + 1, step))
    return values


def parse_cron(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    fields = expression.strip().split()
    if len(fields) != 5:
        raise ValueError("Cron должен содержать 5 полей: минута час день месяц день-недели")
    return (cron_values(fields[0], 0, 59), cron_values(fields[1], 0, 23),
            cron_values(fields[2], 1, 31), cron_values(fields[3], 1, 12),
            cron_values(fields[4], 0, 7, True))


def cron_matches_parsed(expression: str, parsed: tuple[set[int], set[int], set[int], set[int], set[int]], moment: dt.datetime) -> bool:
    minutes, hours, days, months, weekdays = parsed
    cron_weekday = (moment.weekday() + 1) % 7
    fields = expression.strip().split()
    day_match = moment.day in days
    weekday_match = cron_weekday in weekdays
    calendar_match = (day_match or weekday_match) if fields[2] != "*" and fields[4] != "*" else day_match and weekday_match
    return moment.minute in minutes and moment.hour in hours and moment.month in months and calendar_match


def cron_matches(expression: str, moment: dt.datetime) -> bool:
    return cron_matches_parsed(expression, parse_cron(expression), moment)


def next_cron(expression: str, count: int = 5) -> list[str]:
    parsed = parse_cron(expression)
    cursor = dt.datetime.now(TIMEZONE).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    found: list[str] = []
    for _ in range(527040):
        if cron_matches_parsed(expression, parsed, cursor):
            found.append(cursor.isoformat())
            if len(found) == count:
                return found
        cursor += dt.timedelta(minutes=1)
    raise ValueError("Cron не срабатывает в пределах года")


def evaluate(connection: sqlite3.Connection, stamp: int) -> bool:
    local = dt.datetime.fromtimestamp(stamp, TIMEZONE)
    minute = stamp // 60
    for schedule in connection.execute("SELECT * FROM schedules WHERE enabled=1"):
        if schedule["last_fired_minute"] == minute or not cron_matches(schedule["cron"], local):
            continue
        enabled = 1 if schedule["action"] == "enable" else 0
        connection.execute("UPDATE peers SET manual_enabled=? WHERE id=?", (enabled, schedule["peer_id"]))
        connection.execute("UPDATE schedules SET last_fired_minute=? WHERE id=?", (minute, schedule["id"]))
        connection.execute("INSERT INTO events(created_at,peer_id,kind,detail) VALUES(?,?,?,?)",
                           (stamp, schedule["peer_id"], "schedule", schedule["action"]))
    changed = False
    for peer in connection.execute("SELECT * FROM peers WHERE deleted_at IS NULL"):
        reason = None
        if not peer["manual_enabled"]:
            reason = "manual"
        elif peer["expires_at"] is not None and stamp >= peer["expires_at"]:
            reason = "expired"
        elif peer["quota_bytes"] is not None and peer["quota_used"] >= peer["quota_bytes"]:
            reason = "quota"
        effective = 0 if reason else 1
        if effective != peer["effective_enabled"] or reason != peer["block_reason"]:
            connection.execute("UPDATE peers SET effective_enabled=?, block_reason=? WHERE id=?",
                               (effective, reason, peer["id"]))
            connection.execute("INSERT INTO events(created_at,peer_id,kind,detail) VALUES(?,?,?,?)",
                               (stamp, peer["id"], "access", reason or "enabled"))
            changed = True
    return changed


def interface_template() -> str:
    return AWG_CONFIG.read_text().split("[Peer]", 1)[0].rstrip()


def render_config(connection: sqlite3.Connection) -> str:
    blocks = [interface_template()]
    for peer in connection.execute(
            "SELECT * FROM peers WHERE deleted_at IS NULL AND effective_enabled=1 ORDER BY address"):
        preshared = f"PresharedKey = {peer['preshared_key']}\n" if peer["preshared_key"] else ""
        blocks.append(f"[Peer]\n# id={peer['id']} name={peer['name']}\nPublicKey = {peer['public_key']}\n"
                      f"{preshared}AllowedIPs = {peer['address']}/32")
    return "\n\n".join(blocks) + "\n"


def apply_config(connection: sqlite3.Connection) -> None:
    previous = AWG_CONFIG.read_text()
    replacement = render_config(connection)
    if replacement == previous:
        return
    atomic_write(STATE / "config.previous", previous)
    atomic_write(AWG_CONFIG, replacement)
    try:
        stripped = run([AWG_QUICK, "strip", str(AWG_CONFIG)])
        run([AWG, "syncconf", INTERFACE, "/dev/stdin"], stripped + "\n")
        (STATE / "config.previous").unlink(missing_ok=True)
    except Exception:
        atomic_write(AWG_CONFIG, previous)
        run([AWG, "syncconf", INTERFACE, "/dev/stdin"], run([AWG_QUICK, "strip", str(AWG_CONFIG)]) + "\n")
        raise


def migrate_legacy(connection: sqlite3.Connection, source: Path) -> dict:
    if not source.exists():
        return {"migrated": 0}
    state = json.loads(source.read_text())
    migrated = 0
    for name, peer in state.items():
        validate_name(name)
        exists = connection.execute("SELECT 1 FROM peers WHERE public_key=?", (peer["public"],)).fetchone()
        if exists:
            continue
        connection.execute("""INSERT INTO peers
          (id,name,private_key,public_key,preshared_key,address,manual_enabled,effective_enabled,created_at)
          VALUES(?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex[:16], name, peer["private"], peer["public"], peer["psk"],
              peer["ip"], int(peer.get("enabled", True)), int(peer.get("enabled", True)), now_ts()))
        migrated += 1
    return {"migrated": migrated}


def import_server_config(connection: sqlite3.Connection, source: Path) -> dict:
    """Import server-side peers without inventing unavailable client private keys."""
    if not source.is_file():
        raise ValueError("Конфигурация AWG не найдена")
    content = source.read_text()
    sections = re.findall(r"(?m)^\s*\[([^\]]+)\]", content)
    if not sections or sections[0] != "Interface" or sections.count("Interface") != 1 or set(sections) - {"Interface", "Peer"}:
        raise ValueError("Expected one Interface followed by Peer sections")
    if re.search(r"(?im)^\s*SaveConfig\s*=\s*true", content):
        raise ValueError("SaveConfig=true is not supported; disable it before import")
    blocks = re.split(r"(?m)^\s*\[Peer\]\s*$", content)[1:]
    imported = 0
    for block in blocks:
        fields: dict[str, str] = {}
        name = None
        for raw in block.splitlines():
            line = raw.strip()
            match = re.search(r"(?:^|\s)name=([A-Za-z0-9][A-Za-z0-9_-]{0,31})(?:\s|$)", line)
            if line.startswith("#") and match:
                name = match.group(1)
            elif "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                if key.strip() in fields:
                    raise ValueError("Duplicate peer parameter; import cancelled")
                fields[key.strip()] = value.strip()
        public = fields.get("PublicKey")
        allowed = fields.get("AllowedIPs", "").strip()
        if set(fields) - {"PublicKey", "PresharedKey", "AllowedIPs"}:
            raise ValueError("Peer has unsupported settings; import cancelled")
        if not public or not allowed:
            raise ValueError("Peer is missing PublicKey or AllowedIPs")
        try:
            network = ipaddress.ip_network(allowed, strict=False)
        except ValueError as error:
            raise ValueError("Only one IPv4 /32 per peer is supported") from error
        if network.version != 4 or network.prefixlen != 32 or network.network_address == SERVER_IP or network.network_address not in NETWORK:
            raise ValueError("Peer address is outside the supported client subnet")
        if connection.execute("SELECT 1 FROM peers WHERE public_key=?", (public,)).fetchone():
            continue
        base = name or f"peer-{str(network.network_address).replace('.', '-')}"
        candidate = base
        suffix = 2
        while connection.execute("SELECT 1 FROM peers WHERE name=? COLLATE NOCASE", (candidate,)).fetchone():
            candidate = f"{base[:27]}-{suffix}"
            suffix += 1
        connection.execute("""INSERT INTO peers
          (id,name,private_key,public_key,preshared_key,address,manual_enabled,effective_enabled,created_at)
          VALUES(?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex[:16], validate_name(candidate), "", public,
              fields.get("PresharedKey", ""), str(network.network_address), 1, 1, now_ts()))
        imported += 1
    return {"imported": imported, "discovered": len(blocks)}


def public_peer(row: sqlite3.Row) -> dict:
    result = {key: row[key] for key in ("id", "name", "note", "address", "manual_enabled",
        "effective_enabled", "block_reason", "created_at", "expires_at", "quota_bytes",
        "quota_period", "quota_used", "total_rx", "total_tx", "last_handshake")}
    result["exportable"] = bool(row["private_key"])
    return result


def export_config(peer: sqlite3.Row) -> str:
    if not peer["private_key"]:
        raise ValueError("Приватный ключ импортированного клиента неизвестен; перевыпусти конфигурацию")
    fields = {}
    for line in interface_template().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    public = run([AWG, "pubkey"], fields["PrivateKey"] + "\n")
    obfuscation = "\n".join(f"{key} = {value}" for key, value in fields.items() if key in {
        "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4",
        "HeaderProtectionKey", "ContentPaddingAddition"})
    allowed = "0.0.0.0/0"
    config = (f"[Interface]\nPrivateKey = {peer['private_key']}\nAddress = {peer['address']}/32\n"
              f"DNS = 1.1.1.1\nMTU = 1280\n{obfuscation}\n\n[Peer]\nPublicKey = {public}\n"
              f"PresharedKey = {peer['preshared_key']}\nEndpoint = {server_endpoint()}\n"
              f"AllowedIPs = {allowed}\nPersistentKeepalive = 25\n")
    return json.dumps({"id": peer["id"], "name": peer["name"], "config": config,
                       "routing": "full"})


def notify_pending(connection: sqlite3.Connection) -> None:
    config_path = STATE / "notifications.json"
    if not config_path.exists():
        return
    config = json.loads(config_path.read_text())
    token = Path(config["token_file"]).read_text().strip()
    for event in connection.execute("""SELECT events.*, peers.name FROM events
      LEFT JOIN peers ON peers.id=events.peer_id WHERE notified_at IS NULL ORDER BY events.id LIMIT 20"""):
        names = {"manual": "заблокирован вручную", "expired": "заблокирован: срок истёк",
                 "quota": "заблокирован: квота исчерпана", "enabled": "доступ разрешён",
                 "enable": "расписание разрешило доступ", "disable": "расписание запретило доступ"}
        detail = names.get(event["detail"], event["detail"])
        payload = {"chat_id": config["chat_id"], "text": f"VPN · {event['name'] or event['peer_id']}\n{detail}"}
        if config.get("thread_id"):
            payload["message_thread_id"] = config["thread_id"]
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            response = json.load(urllib.request.urlopen(request, timeout=15))
            if response.get("ok"):
                connection.execute("UPDATE events SET notified_at=? WHERE id=?", (now_ts(), event["id"]))
        except Exception:
            break


def command(args: argparse.Namespace) -> object:
    with locked(), connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if args.command == "migrate-legacy":
            result = migrate_legacy(connection, Path(args.source))
            evaluate(connection, now_ts())
            apply_config(connection)
        elif args.command in ("import-config", "check-import"):
            result = import_server_config(connection, Path(args.source))
            if args.command == "check-import":
                connection.rollback()
                return result
            # Import only populates the database; the daemon applies later.
        elif args.command == "list":
            sample_usage(connection, now_ts())
            result = [public_peer(row) for row in connection.execute(
                "SELECT * FROM peers WHERE deleted_at IS NULL ORDER BY name COLLATE NOCASE")]
        elif args.command == "get":
            sample_usage(connection, now_ts())
            result = public_peer(find_peer(connection, args.peer))
            result["schedules"] = [dict(row) for row in connection.execute(
                "SELECT id,action,cron,enabled FROM schedules WHERE peer_id=? ORDER BY created_at", (result["id"],))]
        elif args.command == "add":
            name = validate_name(args.name)
            used = {row[0] for row in connection.execute("SELECT address FROM peers WHERE deleted_at IS NULL")}
            address = next((str(ip) for ip in NETWORK.hosts() if ip != SERVER_IP and str(ip) not in used), None)
            if not address:
                raise ValueError("Нет свободных VPN-адресов")
            private = run([AWG, "genkey"])
            peer_id = uuid.uuid4().hex[:16]
            connection.execute("""INSERT INTO peers
              (id,name,private_key,public_key,preshared_key,address,created_at) VALUES(?,?,?,?,?,?,?)""",
              (peer_id, name, private, run([AWG, "pubkey"], private + "\n"), run([AWG, "genpsk"]), address, now_ts()))
            evaluate(connection, now_ts())
            apply_config(connection)
            result = public_peer(find_peer(connection, peer_id))
        elif args.command == "rename":
            peer = find_peer(connection, args.peer)
            connection.execute("UPDATE peers SET name=? WHERE id=?", (validate_name(args.name), peer["id"]))
            apply_config(connection)
            result = public_peer(find_peer(connection, peer["id"]))
        elif args.command == "enable":
            peer = find_peer(connection, args.peer)
            sample_usage(connection, now_ts())
            connection.execute("UPDATE peers SET manual_enabled=? WHERE id=?", (1 if args.value == "true" else 0, peer["id"]))
            evaluate(connection, now_ts())
            apply_config(connection)
            result = public_peer(find_peer(connection, peer["id"]))
        elif args.command == "expiry":
            peer = find_peer(connection, args.peer)
            expiry = None if args.value == "none" else int(dt.datetime.fromisoformat(args.value).astimezone(dt.timezone.utc).timestamp())
            connection.execute("UPDATE peers SET expires_at=? WHERE id=?", (expiry, peer["id"]))
            evaluate(connection, now_ts())
            apply_config(connection)
            result = public_peer(find_peer(connection, peer["id"]))
        elif args.command == "quota":
            peer = find_peer(connection, args.peer)
            sample_usage(connection, now_ts())
            quota = None if args.bytes == "none" else int(args.bytes)
            if quota is not None and quota < 1048576:
                raise ValueError("Минимальная квота 1 МБ")
            connection.execute("UPDATE peers SET quota_bytes=?,quota_period=?,quota_used=0,quota_period_key=? WHERE id=?",
                               (quota, args.period, period_key(args.period, now_ts()), peer["id"]))
            evaluate(connection, now_ts())
            apply_config(connection)
            result = public_peer(find_peer(connection, peer["id"]))
        elif args.command == "schedule-add":
            peer = find_peer(connection, args.peer)
            next_runs = next_cron(args.cron)
            schedule_id = uuid.uuid4().hex[:12]
            connection.execute("INSERT INTO schedules(id,peer_id,action,cron,created_at) VALUES(?,?,?,?,?)",
                               (schedule_id, peer["id"], args.action, args.cron, now_ts()))
            result = {"id": schedule_id, "next": next_runs}
        elif args.command == "schedule-delete":
            peer = find_peer(connection, args.peer)
            deleted = connection.execute("DELETE FROM schedules WHERE id=? AND peer_id=?", (args.schedule, peer["id"])).rowcount
            if not deleted:
                raise ValueError("Расписание не найдено")
            result = {"deleted": args.schedule}
        elif args.command == "delete":
            peer = find_peer(connection, args.peer)
            if args.confirm != "CONFIRM":
                raise ValueError("Требуется подтверждение CONFIRM")
            sample_usage(connection, now_ts())
            connection.execute("UPDATE peers SET effective_enabled=0,deleted_at=? WHERE id=?", (now_ts(), peer["id"]))
            connection.execute("DELETE FROM schedules WHERE peer_id=?", (peer["id"],))
            apply_config(connection)
            result = {"deleted": peer["id"], "name": peer["name"]}
            connection.execute("DELETE FROM peers WHERE id=?", (peer["id"],))
        elif args.command == "reissue":
            peer = find_peer(connection, args.peer)
            if args.confirm != "CONFIRM":
                raise ValueError("Требуется подтверждение CONFIRM")
            sample_usage(connection, now_ts())
            private = run([AWG, "genkey"])
            connection.execute("""UPDATE peers SET private_key=?,public_key=?,preshared_key=?,
              last_rx=0,last_tx=0,last_handshake=0 WHERE id=?""",
              (private, run([AWG, "pubkey"], private + "\n"), run([AWG, "genpsk"]), peer["id"]))
            apply_config(connection)
            result = public_peer(find_peer(connection, peer["id"]))
        elif args.command == "export":
            result = json.loads(export_config(find_peer(connection, args.peer)))
        elif args.command == "tick":
            sample_usage(connection, now_ts())
            changed = evaluate(connection, now_ts())
            apply_config(connection)
            notify_pending(connection)
            result = {"ok": True, "changed": changed}
        elif args.command == "next-cron":
            result = {"next": next_cron(args.cron)}
        else:
            raise ValueError("Неизвестная операция")
        connection.commit()
        return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    migration = commands.add_parser("migrate-legacy"); migration.add_argument("source")
    config_import = commands.add_parser("import-config"); config_import.add_argument("source")
    check_import = commands.add_parser("check-import"); check_import.add_argument("source")
    commands.add_parser("list")
    get = commands.add_parser("get"); get.add_argument("peer")
    add = commands.add_parser("add"); add.add_argument("name")
    rename = commands.add_parser("rename"); rename.add_argument("peer"); rename.add_argument("name")
    enable = commands.add_parser("enable"); enable.add_argument("peer"); enable.add_argument("value", choices=["true", "false"])
    expiry = commands.add_parser("expiry"); expiry.add_argument("peer"); expiry.add_argument("value")
    quota = commands.add_parser("quota"); quota.add_argument("peer"); quota.add_argument("bytes"); quota.add_argument("period", choices=["lifetime", "day", "month"])
    schedule = commands.add_parser("schedule-add"); schedule.add_argument("peer"); schedule.add_argument("action", choices=["enable", "disable"]); schedule.add_argument("cron")
    schedule_delete = commands.add_parser("schedule-delete"); schedule_delete.add_argument("peer"); schedule_delete.add_argument("schedule")
    delete = commands.add_parser("delete"); delete.add_argument("peer"); delete.add_argument("confirm")
    reissue = commands.add_parser("reissue"); reissue.add_argument("peer"); reissue.add_argument("confirm")
    export = commands.add_parser("export"); export.add_argument("peer")
    commands.add_parser("tick")
    next_parser = commands.add_parser("next-cron"); next_parser.add_argument("cron")
    return root


def daemon() -> None:
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped:
        try:
            command(argparse.Namespace(command="tick"))
        except Exception as error:
            print(f"tick failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        for _ in range(10):
            if stopped:
                break
            time.sleep(1)


if __name__ == "__main__":
    os.umask(0o077)
    if os.geteuid() != 0:
        print(json.dumps({"error": "root helper required"}))
        raise SystemExit(1)
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "daemon":
            daemon()
        else:
            print(json.dumps(command(parser().parse_args()), ensure_ascii=False))
    except (ValueError, sqlite3.IntegrityError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({"error": "Операция не выполнена; подробности в журнале сервера"}, ensure_ascii=False))
        raise
