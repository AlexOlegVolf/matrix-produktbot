import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import (Bot, Denied, Fatal, Http, InstanceLock, Retry, Store,
                 allowed, arguments, query_from, room_peer, FAILURE_ANSWER)

BOT = "@produktbot:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"


def config():
    return SimpleNamespace(bot_user=BOT, allowed_server="example.org",
                           homeserver="https://matrix.example.org", matrix_token="m" * 32,
                           webhook_url="https://n8n.example.org/webhook/products", webhook_key="k" * 32)


def state(peer=ALICE, visibility="invited"):
    return [
        {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "invite"}},
        {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": visibility}},
        *[{"type": "m.room.member", "state_key": user, "content": {"membership": "join"}}
          for user in (BOT, peer)],
    ]


def event(id="$e1", sender=ALICE, body="MBN116", **extra):
    return {"type": "m.room.message", "event_id": id, "sender": sender,
            "origin_server_ts": int(time.time() * 1000) + 1000,
            "content": {"msgtype": "m.text", "body": body}, **extra}


def sync(events, token="s2", room="!a:example.org", limited=False):
    return {"next_batch": token, "rooms": {"join": {room: {
        "timeline": {"events": events, "limited": limited, "prev_batch": "p1"}}}}}


class FakeHttp:
    def __init__(self):
        self.states = {"!a:example.org": state(), "!b:example.org": state(BOB)}
        self.sent, self.searches, self.calls, self.pages = [], [], [], []
        self.search_error = self.send_error = None

    async def state(self, room):
        return self.states[room]

    async def webhook(self, job):
        self.searches.append(dict(job))
        if self.search_error:
            raise self.search_error
        return "Produkt " + job["query"]

    async def send(self, room, event_id, answer):
        self.sent.append((room, event_id, answer))
        if self.send_error:
            raise self.send_error

    async def matrix(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/messages"):
            return self.pages.pop(0)
        return {}


class PolicyTests(unittest.TestCase):
    def test_exact_domain_and_new_employee(self):
        self.assertTrue(allowed("@new.person:example.org", "example.org", BOT))
        for user in [BOT, "@x:evil-example.org", "@x:example.org.evil", "@x:elsewhere.org", "@:example.org", None]:
            self.assertFalse(allowed(user, "example.org", BOT))

    def test_room_policy(self):
        self.assertEqual(room_peer(state(), BOT, "example.org"), ALICE)
        self.assertEqual(room_peer(state(visibility="shared"), BOT, "example.org"), ALICE)
        variants = [state("@x:outside.org"), state(visibility="world_readable")]
        variants.append(state() + [{"type": "m.room.encryption", "state_key": "", "content": {}}])
        variants.append(state() + [{"type": "m.room.member", "state_key": BOB, "content": {"membership": "invite"}}])
        public = state(); public[0]["content"]["join_rule"] = "public"; variants.append(public)
        for events in variants:
            with self.subTest(events=events), self.assertRaises(Denied):
                room_peer(events, BOT, "example.org")

    def test_message_types(self):
        self.assertEqual(query_from(event(body="  Gira Tastsensor 4 Komfort  "), "example.org", BOT), "Gira Tastsensor 4 Komfort")
        for e in [event(sender=BOT), event(sender="@x:elsewhere.org"), event(body=" "),
                  event(content={"msgtype": "m.notice", "body": "hi"}),
                  event(content={"msgtype": "m.text", "body": "edit", "m.relates_to": {"rel_type": "m.replace"}}),
                  event(type="m.room.encrypted")]:
            self.assertIsNone(query_from(e, "example.org", BOT))

    def test_config_rejects_bad_urls(self):
        args = ["--homeserver", "https://matrix.example.org", "--bot-user", BOT,
                "--allowed-server", "example.org", "--webhook-url", "https://n8n.example.org/webhook/p",
                "--state-dir", "state", "--matrix-token", "m" * 32, "--webhook-key", "k" * 32]
        self.assertEqual(arguments(args).bot_user, BOT)
        for url in ["http://n8n.example.org/webhook/p", "https://n8n.example.org/webhook-test/p", "https://user:secret@n8n.example.org/webhook/p"]:
            bad = args.copy(); bad[7] = url
            with self.assertRaises(Fatal):
                arguments(bad)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path, "test")
        self.http = FakeHttp()
        self.bot = Bot(config(), self.store, self.http)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def baseline(self):
        await self.bot.ingest(sync([event("$old")], "s1"))

    async def test_baseline_and_dedup_and_restart(self):
        await self.baseline()
        self.assertIsNone(self.store.due("jobs"))
        batch = sync([event()], "s2")
        await self.bot.ingest(batch); await self.bot.ingest(batch)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.store.db.close(); self.store = Store(self.path, "test")
        self.bot.store = self.store
        self.assertEqual(self.store.get("since"), "s2")
        await self.bot.work_once()
        self.assertEqual(len(self.http.sent), 1)
        await self.bot.ingest(batch); await self.bot.work_once()
        self.assertEqual(len(self.http.sent), 1)
        row = self.store.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["query"], ""); self.assertIsNone(row["answer"])

    async def test_two_users_separate_rooms(self):
        await self.baseline()
        await self.bot.ingest(sync([event()], "s2"))
        await self.bot.ingest(sync([event("$e2", BOB, "Gira Tastsensor")], "s3", "!b:example.org"))
        await self.bot.work_once(); await self.bot.work_once()
        self.assertEqual(self.http.sent, [("!a:example.org", "$e1", "Produkt MBN116"),
                                          ("!b:example.org", "$e2", "Produkt Gira Tastsensor")])

    async def test_reject_before_search(self):
        await self.baseline()
        self.http.states["!a:example.org"] += [{"type": "m.room.encryption", "state_key": "", "content": {}}]
        await self.bot.ingest(sync([event()]))
        await self.bot.work_once()
        self.assertFalse(self.http.searches); self.assertFalse(self.http.sent)

    async def test_room_changes_during_search(self):
        await self.baseline(); await self.bot.ingest(sync([event()]))
        original = self.http.webhook
        async def changed(job):
            result = await original(job)
            self.http.states[job["room"]] = state(BOB)
            return result
        self.http.webhook = changed
        await self.bot.work_once()
        self.assertFalse(self.http.sent)

    async def test_answer_cached_after_send_failure(self):
        await self.baseline(); await self.bot.ingest(sync([event()]))
        self.http.send_error = Retry("Matrix HTTP 500")
        await self.bot.work_once()
        self.store.db.execute("UPDATE jobs SET due=0"); self.store.db.commit()
        self.http.send_error = None
        await self.bot.work_once()
        self.assertEqual(len(self.http.searches), 1)
        self.assertEqual(self.http.sent[0], self.http.sent[1])

    async def test_webhook_failure_eventually_gives_notice(self):
        await self.baseline(); await self.bot.ingest(sync([event()]))
        self.http.search_error = Retry("n8n HTTP 500")
        for _ in range(6):
            self.store.db.execute("UPDATE jobs SET due=0"); self.store.db.commit()
            await self.bot.work_once()
        self.assertEqual(len(self.http.searches), 5)
        self.assertEqual(self.http.sent[0][2], FAILURE_ANSWER)

    async def test_limited_timeline_backfills_to_anchor(self):
        await self.baseline()
        self.http.pages = [{"chunk": [event("$e2"), event("$e1"), event("$old")], "end": "p2"}]
        await self.bot.ingest(sync([event("$e3")], limited=True))
        ids = [r[0] for r in self.store.db.execute("SELECT event FROM jobs ORDER BY rowid")]
        self.assertEqual(ids, ["$e1", "$e2", "$e3"])

    async def test_gap_keeps_cursor(self):
        await self.baseline()
        self.http.pages = [{"chunk": []}]
        with self.assertRaises(Fatal):
            await self.bot.ingest(sync([event()], limited=True))
        self.assertEqual(self.store.get("since"), "s1")
        self.assertIsNone(self.store.due("jobs"))

    async def test_invites(self):
        invite = {"type": "m.room.member", "state_key": BOT, "sender": ALICE,
                  "content": {"membership": "invite"}}
        await self.bot.ingest({"next_batch": "s1", "rooms": {"invite": {
            "!a:example.org": {"invite_state": {"events": [invite]}},
            "!evil:example.org": {"invite_state": {"events": [dict(invite, sender="@evil:other.org")]}}}}})
        await self.bot.work_once()
        self.assertEqual(len(self.http.calls), 1)
        self.assertTrue(self.http.calls[0][1].endswith("/join"))
        self.assertIsNone(self.store.due("invites"))

    async def test_long_query_gets_hint_without_api_call(self):
        await self.baseline(); await self.bot.ingest(sync([event(body="x" * 10000)]))
        await self.bot.work_once()
        self.assertFalse(self.http.searches)
        self.assertIn("500", self.http.sent[0][2])

    async def test_cursor_and_jobs_transaction_rollback(self):
        await self.baseline()
        with self.assertRaises(Exception):
            self.store.commit_batch("broken", [], [("$a", "!a", ALICE, "ok"), (None, None, None, None)], [])
        self.assertEqual(self.store.get("since"), "s1")
        self.assertIsNone(self.store.due("jobs"))

    async def test_group_invite_leaves_without_search(self):
        self.http.states["!a:example.org"] += [{"type": "m.room.member", "state_key": BOB,
                                                "content": {"membership": "join"}}]
        self.store.commit_batch("s1", [], [], [("!a:example.org", ALICE)])
        await self.bot.work_once()
        self.assertTrue(self.http.calls[-1][1].endswith("/leave"))
        self.assertFalse(self.http.searches)

    async def test_unknown_new_room_backfills_after_first_start(self):
        await self.baseline()
        old = event("$older", BOB, origin_server_ts=0)
        self.http.pages = [{"chunk": [event("$earlier", BOB), old]}]
        await self.bot.ingest(sync([event("$latest", BOB)], "s3", "!b:example.org", limited=True))
        ids = [r[0] for r in self.store.db.execute("SELECT event FROM jobs ORDER BY rowid")]
        self.assertEqual(ids, ["$earlier", "$latest"])

    async def test_matrix_auth_does_not_discard_pending_job(self):
        await self.baseline(); await self.bot.ingest(sync([event()]))
        self.http.send_error = Fatal("Matrix-Zugang ungültig")
        with self.assertRaises(Fatal):
            await self.bot.work_once()
        self.assertEqual(self.store.due("jobs")["answer"], "Produkt MBN116")

    async def test_empty_sync_token_does_not_reset_checkpoint(self):
        await self.baseline()
        with self.assertRaises(Retry):
            await self.bot.ingest({"next_batch": ""})
        self.assertEqual(self.store.get("since"), "s1")


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.reply = {"antwort": "Ergebnis", "room_id": "!a", "request_id": "$e"}
        self.status = 200
        self.raw_reply = None
        async def handler(request):
            self.requests.append((request.method, request.path, dict(request.headers),
                                  await request.json() if request.can_read_body else None))
            if self.raw_reply is not None:
                return web.Response(text=self.raw_reply, status=self.status)
            return web.json_response(self.reply, status=self.status)
        app = web.Application(); app.router.add_route("*", "/{tail:.*}", handler)
        self.server = TestServer(app); await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.c = config()
        self.c.homeserver = str(self.server.make_url("/" )).rstrip("/")
        self.c.webhook_url = str(self.server.make_url("/webhook/products"))
        self.http = Http(self.session, self.c)
        self.job = {"query": "MBN116", "room": "!a", "sender": ALICE, "event": "$e"}

    async def asyncTearDown(self):
        await self.session.close(); await self.server.close()

    async def test_webhook_contract_and_header_separation(self):
        self.assertEqual(await self.http.webhook(self.job), "Ergebnis")
        _, _, headers, body = self.requests[-1]
        self.assertEqual(headers["X-Bot-Key"], self.c.webhook_key)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(body["request_id"], "$e")
        self.reply = {"event_id": "$sent"}
        await self.http.send("!a", "$e", "Ergebnis")
        _, path, headers, body = self.requests[-1]
        self.assertEqual(headers["Authorization"], "Bearer " + self.c.matrix_token)
        self.assertNotIn("X-Bot-Key", headers)
        self.assertEqual(body["msgtype"], "m.notice")
        await self.http.send("!a", "$e", "Ergebnis")
        self.assertEqual(path, self.requests[-1][1])

    async def test_wrong_room_or_request_rejected(self):
        for key in ("room_id", "request_id"):
            self.reply[key] = "WRONG"
            with self.assertRaises(Retry):
                await self.http.webhook(self.job)

    async def test_empty_or_non_json_webhook_response_rejected(self):
        for raw in ["", "<html>Login</html>"]:
            self.raw_reply = raw
            with self.assertRaises(Retry):
                await self.http.webhook(self.job)

    async def test_array_and_missing_answer_rejected(self):
        for result in [[], {}, {"antwort": "", "room_id": "!a", "request_id": "$e"}]:
            self.reply = result
            with self.assertRaises(Retry):
                await self.http.webhook(self.job)

    async def test_matrix_auth_fatal_webhook_auth_retry(self):
        self.status = 401
        with self.assertRaises(Fatal):
            await self.http.matrix("GET", "/account/whoami")
        with self.assertRaises(Retry):
            await self.http.webhook(self.job)

    async def test_rate_limit_and_redirect(self):
        self.status = 429; self.reply = {"retry_after_ms": 20000}
        with self.assertRaises(Retry) as caught:
            await self.http.webhook(self.job)
        self.assertEqual(caught.exception.delay, 20)
        self.status = 302
        with self.assertRaises(Retry):
            await self.http.webhook(self.job)


class PersistenceTests(unittest.TestCase):
    def test_identity_mismatch_and_single_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db", "one"); store.db.close()
            with self.assertRaises(Fatal):
                Store(Path(tmp) / "test.db", "two")
            lock = InstanceLock(Path(tmp) / "lock")
            try:
                with self.assertRaises(Fatal):
                    InstanceLock(Path(tmp) / "lock")
            finally:
                lock.close()


if __name__ == "__main__":
    unittest.main()
