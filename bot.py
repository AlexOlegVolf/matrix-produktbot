from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import quote, urlsplit

import aiohttp

LOG = logging.getLogger("produktbot")
MAX_QUERY = 500
MAX_ANSWER = 12000
FAILURE_ANSWER = "Die Produktsuche ist gerade nicht erreichbar. Bitte versuche es später erneut."


class Fatal(Exception):
    """Stop rather than lose data or run with invalid credentials."""


class Retry(Exception):
    def __init__(self, message: str, delay: float = 0):
        super().__init__(message)
        self.delay = delay


class Denied(Exception):
    pass


def allowed(user: str, server: str, bot: str) -> bool:
    if not isinstance(user, str) or not user.startswith("@") or user == bot:
        return False
    local, separator, domain = user[1:].partition(":")
    return bool(local and separator and domain == server)


def room_peer(events: list, bot: str, server: str) -> str:
    """Fail closed: complete current state, not display names or m.direct."""
    state = {(e.get("type"), e.get("state_key")): e.get("content", {}) for e in events}
    if ("m.room.encryption", "") in state:
        raise Denied("Raum ist verschlüsselt")
    if state.get(("m.room.join_rules", ""), {}).get("join_rule") != "invite":
        raise Denied("Raum ist nicht privat")
    visibility = state.get(("m.room.history_visibility", ""), {}).get("history_visibility", "shared")
    if visibility not in {"joined", "invited", "shared"}:
        raise Denied("Raumhistorie ist öffentlich oder unbekannt")
    active = {key: value.get("membership") for (kind, key), value in state.items()
              if kind == "m.room.member" and value.get("membership") in {"join", "invite", "knock"}}
    if len(active) != 2 or active.get(bot) != "join" or set(active.values()) != {"join"}:
        raise Denied("Raum benötigt genau zwei beigetretene Konten")
    peer = next(user for user in active if user != bot)
    if not allowed(peer, server, bot):
        raise Denied("Kein freigegebenes Mitarbeiterkonto")
    return peer


def query_from(event: dict, server: str, bot: str) -> str | None:
    content = event.get("content", {})
    if event.get("type") != "m.room.message" or content.get("msgtype") != "m.text":
        return None
    if not allowed(event.get("sender"), server, bot) or "state_key" in event:
        return None
    if content.get("m.relates_to", {}).get("rel_type") == "m.replace":
        return None
    body = content.get("body")
    return body.strip() if isinstance(body, str) and body.strip() else None


class Store:
    def __init__(self, path: Path, identity: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS rooms (room TEXT PRIMARY KEY, anchor TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs (
            event TEXT PRIMARY KEY, room TEXT NOT NULL, sender TEXT NOT NULL,
            query TEXT NOT NULL, answer TEXT, status TEXT NOT NULL DEFAULT 'pending',
            tries INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS invites (
            room TEXT PRIMARY KEY, sender TEXT NOT NULL,
            tries INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending');
        """)
        old = self.get("identity")
        if old is not None and old != identity:
            self.db.close()
            raise Fatal("Datenordner gehört zu einer anderen Bot-Konfiguration")
        with self.db:
            self.put("identity", identity)
            if self.get("created_ms") is None:
                self.put("created_ms", str(int(time.time() * 1000)))

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def anchor(self, room):
        row = self.db.execute("SELECT anchor FROM rooms WHERE room=?", (room,)).fetchone()
        return row[0] if row else None

    def commit_batch(self, cursor, rooms, jobs, invites):
        # Jobs and cursor must commit together: no acknowledged-but-lost messages.
        with self.db:
            for room, anchor in rooms:
                self.db.execute("INSERT OR REPLACE INTO rooms VALUES (?,?)", (room, anchor))
            self.db.executemany("INSERT INTO jobs(event,room,sender,query) VALUES (?,?,?,?) ON CONFLICT(event) DO NOTHING", jobs)
            self.db.executemany("INSERT INTO invites(room,sender) VALUES (?,?) ON CONFLICT(room) DO NOTHING", invites)
            self.put("since", cursor)

    def due(self, table):
        assert table in {"jobs", "invites"}
        return self.db.execute(f"SELECT * FROM {table} WHERE status='pending' AND due<=? ORDER BY rowid LIMIT 1",
                               (time.time(),)).fetchone()

    def save_answer(self, event, answer):
        with self.db:
            self.db.execute("UPDATE jobs SET answer=?, tries=0 WHERE event=?", (answer, event))

    def finish(self, table, key, status="done"):
        assert table in {"jobs", "invites"}
        col = "event" if table == "jobs" else "room"
        with self.db:
            self.db.execute(f"UPDATE {table} SET status=? WHERE {col}=?", (status, key))
            if table == "jobs":
                self.db.execute("UPDATE jobs SET query='',answer=NULL WHERE event=?", (key,))

    def retry(self, table, row, delay):
        assert table in {"jobs", "invites"}
        col = "event" if table == "jobs" else "room"
        with self.db:
            self.db.execute(f"UPDATE {table} SET tries=tries+1,due=? WHERE {col}=?",
                            (time.time() + delay, row[col]))


class Http:
    def __init__(self, session, config):
        self.session, self.config = session, config

    async def request(self, method, url, *, headers, payload=None, params=None, limit=8_000_000, matrix=True):
        try:
            async with self.session.request(method, url, headers=headers, json=payload, params=params,
                                            allow_redirects=False) as response:
                raw = bytearray()
                async for block in response.content.iter_chunked(65536):
                    raw.extend(block)
                    if len(raw) > limit:
                        raise Retry("Antwort überschreitet die erlaubte Größe")
                if response.status == 401 and matrix:
                    raise Fatal("Matrix-Zugang ungültig; Token durch IT erneuern lassen")
                if response.status == 403 and matrix:
                    raise Denied("Matrix verweigert den Zugriff")
                if not 200 <= response.status < 300:
                    delay = 0
                    if response.status == 429:
                        try:
                            data = json.loads(raw)
                            delay = float(data.get("retry_after_ms", 0)) / 1000
                        except (ValueError, TypeError, AttributeError):
                            pass
                        try:
                            delay = max(delay, float(response.headers.get("Retry-After", 0)))
                        except ValueError:
                            pass
                    raise Retry(f"{'Matrix' if matrix else 'n8n'} HTTP {response.status}", min(max(delay, 0), 86400))
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise Retry("Antwort ist kein gültiges JSON") from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Never log exception URLs, headers or bodies (credentials/user text).
            raise Retry("Netzwerkfehler oder Zeitüberschreitung") from None

    async def matrix(self, method, path, *, payload=None, params=None):
        return await self.request(method, self.config.homeserver + "/_matrix/client/v3" + path,
                                  headers={"Authorization": "Bearer " + self.config.matrix_token},
                                  payload=payload, params=params)

    async def state(self, room):
        result = await self.matrix("GET", f"/rooms/{quote(room, safe='')}/state")
        if not isinstance(result, list):
            raise Retry("Ungültiger Matrix-Raumzustand")
        return result

    async def webhook(self, job):
        payload = {"suchbegriff": job["query"], "room_id": job["room"],
                   "sender": job["sender"], "request_id": job["event"]}
        result = await self.request("POST", self.config.webhook_url,
                                    headers={"X-Bot-Key": self.config.webhook_key},
                                    payload=payload, limit=100_000, matrix=False)
        if not isinstance(result, dict):
            raise Retry("n8n muss ein JSON-Objekt zurückgeben")
        if result.get("room_id") != job["room"] or result.get("request_id") != job["event"]:
            raise Retry("n8n-Antwort gehört nicht zur Anfrage")
        answer = result.get("antwort")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER:
            raise Retry("n8n-Antworttext fehlt oder ist zu lang")
        return answer.strip()

    async def send(self, room, event, answer):
        txn = "produktbot_" + hashlib.sha256(event.encode()).hexdigest()
        result = await self.matrix("PUT", f"/rooms/{quote(room, safe='')}/send/m.room.message/{txn}",
                                   payload={"msgtype": "m.notice", "body": answer})
        if not isinstance(result, dict) or not result.get("event_id"):
            raise Retry("Matrix hat das Senden nicht bestätigt")


class Bot:
    def __init__(self, config, store, http):
        self.c, self.store, self.http = config, store, http

    async def collect_events(self, room, timeline):
        events = timeline.get("events", [])
        if not isinstance(events, list):
            raise Fatal("Ungültige Matrix-Timeline")
        if not timeline.get("limited"):
            return events
        anchor = self.store.anchor(room)
        before = []
        token = timeline.get("prev_batch")
        cutoff = int(self.store.get("created_ms"))
        if anchor and any(e.get("event_id") == anchor for e in events):
            return events
        for _ in range(100):
            if not token:
                if anchor:
                    raise Fatal("Nachrichtenlücke nicht auflösbar; Empfangsstand bleibt erhalten")
                return before + events
            page = await self.http.matrix("GET", f"/rooms/{quote(room, safe='')}/messages",
                                           params={"from": token, "dir": "b", "limit": 100})
            chunk = page.get("chunk")
            if not isinstance(chunk, list):
                raise Retry("Ungültige Matrix-Nachrichtenseite")
            for event in chunk:  # Matrix returns newest first when dir=b.
                if anchor and event.get("event_id") == anchor:
                    return before + events
                if not anchor and event.get("origin_server_ts", cutoff) < cutoff:
                    return before + events
                before.insert(0, event)
            next_token = page.get("end")
            if not chunk or not next_token or next_token == token:
                if anchor:
                    raise Fatal("Nachrichtenlücke nicht auflösbar; Empfangsstand bleibt erhalten")
                return before + events
            token = next_token
        raise Fatal("Mehr als 100 Nachholseiten; IT muss Nachrichtenlücke prüfen")

    async def ingest(self, response):
        if (not isinstance(response, dict) or not isinstance(response.get("next_batch"), str)
                or not response["next_batch"]):
            raise Retry("Matrix liefert keinen Empfangsstand")
        first = self.store.get("since") is None
        rooms, jobs, invites = [], [], []
        cutoff = int(self.store.get("created_ms"))
        for room, info in response.get("rooms", {}).get("invite", {}).items():
            for event in info.get("invite_state", {}).get("events", []):
                if (event.get("type") == "m.room.member" and event.get("state_key") == self.c.bot_user
                        and event.get("content", {}).get("membership") == "invite"
                        and allowed(event.get("sender"), self.c.allowed_server, self.c.bot_user)):
                    invites.append((room, event["sender"]))
        for room, info in response.get("rooms", {}).get("join", {}).items():
            timeline = info.get("timeline", {})
            # First start establishes a baseline; it must not answer old chat history.
            events = timeline.get("events", []) if first else await self.collect_events(room, timeline)
            if events and events[-1].get("event_id"):
                rooms.append((room, events[-1]["event_id"]))
            if first:
                continue
            for event in events:
                text = query_from(event, self.c.allowed_server, self.c.bot_user)
                event_id = event.get("event_id")
                if text is not None and isinstance(event_id, str) and event.get("origin_server_ts", 0) >= cutoff:
                    # Avoid storing excessive user text; worker sends a length hint instead.
                    jobs.append((event_id, room, event["sender"], text[:MAX_QUERY + 1]))
        self.store.commit_batch(response["next_batch"], rooms, jobs, invites)
        if first:
            LOG.info("Empfang bereit. Jetzt neue Suchnachrichten senden.")

    async def invite(self, row):
        room = row["room"]
        path = "/rooms/" + quote(room, safe="")
        # Invites were validated against exact sender server during ingestion.
        await self.http.matrix("POST", path + "/join", payload={})
        try:
            peer = room_peer(await self.http.state(room), self.c.bot_user, self.c.allowed_server)
            if peer != row["sender"]:
                raise Denied("Einladender stimmt nicht mit Gesprächspartner überein")
        except Denied:
            await self.http.matrix("POST", path + "/leave", payload={})
            raise
        self.store.finish("invites", room)
        LOG.info("Berechtigten privaten Chat angenommen.")

    async def job(self, row):
        peer = room_peer(await self.http.state(row["room"]), self.c.bot_user, self.c.allowed_server)
        if peer != row["sender"]:
            raise Denied("Absender gehört nicht mehr zum privaten Chat")
        answer = row["answer"]
        if answer is None:
            if len(row["query"]) > MAX_QUERY:
                answer = f"Bitte beschränke deine Suchanfrage auf {MAX_QUERY} Zeichen."
            else:
                answer = await self.http.webhook(row)
            self.store.save_answer(row["event"], answer)
        # Room may have changed while n8n was processing.
        if room_peer(await self.http.state(row["room"]), self.c.bot_user, self.c.allowed_server) != row["sender"]:
            raise Denied("Gesprächspartner hat sich geändert")
        await self.http.send(row["room"], row["event"], answer)
        self.store.finish("jobs", row["event"])
        LOG.info("Suchanfrage beantwortet.")

    async def work_once(self):
        for table, action in (("invites", self.invite), ("jobs", self.job)):
            row = self.store.due(table)
            if row is None:
                continue
            key = row["event"] if table == "jobs" else row["room"]
            try:
                await action(row)
            except Denied as exc:
                self.store.finish(table, key, "blocked")
                LOG.warning("Nicht bearbeitet: %s.", exc)
            except Retry as exc:
                # An answer may have been saved during this attempt. Count delivery
                # retries against the current row, not the older search snapshot.
                col = "event" if table == "jobs" else "room"
                row = self.store.db.execute(f"SELECT * FROM {table} WHERE {col}=?", (key,)).fetchone()
                if row["tries"] >= 4:
                    # After five failed searches deliver a friendly message, if room remains safe.
                    if table == "jobs" and row["answer"] is None:
                        self.store.save_answer(key, FAILURE_ANSWER)
                        LOG.error("Produktsuche nach fünf Versuchen abgebrochen: %s.", exc)
                    else:
                        self.store.finish(table, key, "failed")
                        LOG.error("Vorgang nach fünf Versuchen angehalten: %s.", exc)
                else:
                    self.store.retry(table, row, max(exc.delay, 2 ** (row["tries"] + 1)))
                    LOG.warning("Erneuter Versuch folgt: %s.", exc)
        await asyncio.sleep(0.2)

    async def receive(self):
        while True:
            params = {"timeout": 30000, "set_presence": "offline",
                      "filter": json.dumps({"room": {"timeline": {"limit": 100},
                                                     "ephemeral": {"types": []}},
                                            "presence": {"types": []}})}
            since = self.store.get("since")
            if since:
                params["since"] = since
            try:
                await self.ingest(await self.http.matrix("GET", "/sync", params=params))
                await asyncio.sleep(0.2)
            except Retry as exc:
                LOG.warning("Empfang wird wiederholt: %s.", exc)
                await asyncio.sleep(max(5, exc.delay))
            except Denied:
                raise Fatal("Matrix verweigert den Nachrichtenempfang") from None

    async def worker(self):
        while True:
            await self.work_once()


class InstanceLock:
    def __init__(self, path):
        self.file = open(path, "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise Fatal("Ein Bot mit diesem Datenordner läuft bereits") from None

    def close(self):
        self.file.close()


def https_url(value, name):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise Fatal(name + " muss eine HTTPS-Adresse ohne Zugangsdaten oder URL-Parameter sein")
    return value.rstrip("/")


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Matrix-Produktbot für unverschlüsselte private Chats")
    for name in ("homeserver", "bot-user", "allowed-server", "webhook-url", "state-dir"):
        parser.add_argument("--" + name, required=True)
    for name in ("matrix-token", "webhook-key"):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--" + name)
        group.add_argument("--" + name + "-file", type=Path)
    args = parser.parse_args(argv)
    args.homeserver = https_url(args.homeserver, "Matrix-Server")
    args.webhook_url = https_url(args.webhook_url, "n8n-Webhook")
    if "/webhook/" not in urlsplit(args.webhook_url).path:
        raise Fatal("n8n Production-URL mit /webhook/ verwenden")
    if not allowed(args.bot_user, args.allowed_server, ""):
        raise Fatal("Bot-ID passt nicht zum erlaubten Matrix-Servernamen")
    for name in ("matrix_token", "webhook_key"):
        file = getattr(args, name + "_file")
        value = file.read_text(encoding="utf-8").strip() if file else getattr(args, name)
        if not value or any(c.isspace() for c in value) or len(value) < 20:
            raise Fatal("Token und Webhook-Schlüssel benötigen mindestens 20 Zeichen ohne Leerzeichen")
        setattr(args, name, value)
    args.state_dir = Path(args.state_dir)
    return args


async def run(config, store):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75), trust_env=False) as session:
        http = Http(session, config)
        who = await http.matrix("GET", "/account/whoami")
        if not isinstance(who, dict) or who.get("user_id") != config.bot_user:
            raise Fatal("Matrix-Token gehört nicht zum konfigurierten Bot-Konto")
        LOG.info("Bot angemeldet. Nur private, unverschlüsselte Mitarbeiter-Chats werden bearbeitet.")
        bot = Bot(config, store, http)
        tasks = [asyncio.create_task(bot.receive()), asyncio.create_task(bot.worker())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lock = store = None
    try:
        config = arguments()
        if os.name != "nt":
            os.umask(0o077)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(config.state_dir / "bot.lock")
        identity = json.dumps([config.homeserver, config.bot_user, config.allowed_server])
        store = Store(config.state_dir / "state.sqlite3", identity)
        asyncio.run(run(config, store))
    except KeyboardInterrupt:
        return 0
    except (Fatal, Retry, Denied) as exc:
        LOG.error("Bot angehalten: %s.", exc)
        return 1
    except Exception as exc:
        LOG.error("Bot angehalten (%s). Konfiguration, Dateirechte und Datenbank prüfen.", type(exc).__name__)
        return 1
    finally:
        if store:
            store.db.close()
        if lock:
            lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
