"""python -m unittest discover -s tests

Stdlib unittest, no network: the gateway is an httpx.MockTransport, Telegram
is a fake bot that records what it was asked to send.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from PIL import Image

import extraction
import followup
import intel
import llm
import main
import pipeline
import storage

CARD = {
    "full_name": "Tan <Wei> Ming",
    "organization": "Acme & Sons",
    "title": "Director",
    "phones": ["+6591234567"],
    "emails": ["wm@acme.example"],
}


class TempDb(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(storage, "DB_PATH", Path(self._dir.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.init_db()


class StorageTests(TempDb):
    def test_saved_card_is_a_new_lead_with_the_cadence_scheduled(self):
        contact = storage.get_contact(storage.save_contact(1, CARD))
        self.assertEqual(contact["phones"], ["+6591234567"])
        self.assertEqual(contact["stage"], "new")
        self.assertFalse(contact["is_vip"])
        for column in ("next_followup_at", "linkedin_nudge_at", "second_touch_at"):
            self.assertGreater(contact[column], pipeline.iso(pipeline.now()))
        self.assertLess(contact["next_followup_at"], contact["linkedin_nudge_at"])
        self.assertLess(contact["linkedin_nudge_at"], contact["second_touch_at"])

    def test_v020_database_is_upgraded_in_place(self):
        path = Path(self._dir.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(storage._SCHEMA)
        conn.execute(
            "INSERT INTO contacts (chat_id, full_name, phones, emails, created_at) VALUES (1, 'Old', '[]', '[]', 'x')"
        )
        conn.commit()
        conn.close()
        with mock.patch.object(storage, "DB_PATH", path):
            storage.init_db()
            storage.init_db()  # idempotent
            old = storage.get_contact(1)
            self.assertEqual((old["full_name"], old["stage"], old["next_followup_at"]), ("Old", "new", None))
            # no follow-up was ever scheduled for it, so it must not start nagging
            self.assertEqual(storage.get_due_followups("9999"), [])

    def test_due_followups_only_open_stages_and_past_dates(self):
        due, later, closed = (storage.save_contact(1, CARD) for _ in range(3))
        storage.update_contact(due, next_followup_at="2000-01-01T00:00:00+00:00")
        storage.update_contact(closed, stage="won", next_followup_at="2000-01-01T00:00:00+00:00")
        now_iso = pipeline.iso(pipeline.now())
        self.assertEqual([c["id"] for c in storage.get_due_followups(now_iso)], [due])
        self.assertEqual(storage.get_due_followups(now_iso, chat_id=2), [])

    def test_contacts_are_scoped_to_the_chat_that_saved_them(self):
        contact_id = storage.save_contact(1, CARD)
        self.assertIsNone(storage.get_contact(contact_id, chat_id=2))
        self.assertFalse(storage.delete_contact(contact_id, chat_id=2))
        self.assertEqual(storage.search_contacts(2, "Acme"), [])
        self.assertEqual(len(storage.search_contacts(1, "acme")), 1)
        self.assertTrue(storage.delete_contact(contact_id, chat_id=1))

    def test_update_refuses_unknown_columns_and_round_trips_json(self):
        contact_id = storage.save_contact(1, CARD)
        with self.assertRaises(ValueError):
            storage.update_contact(contact_id, chat_id=5)
        storage.update_contact(contact_id, intel={"pain_points": [{"pain": "x"}]}, is_vip=True)
        contact = storage.get_contact(contact_id)
        self.assertEqual(contact["intel"]["pain_points"][0]["pain"], "x")
        self.assertTrue(contact["is_vip"])
        self.assertFalse(storage.update_contact(999, notes="x"))


class PipelineTests(TempDb):
    def test_formatters_escape_html_from_the_card(self):
        contact = storage.get_contact(storage.save_contact(1, CARD))
        for text in (pipeline.format_contact(contact), pipeline.format_reminder(contact), pipeline.format_pipeline([contact])):
            self.assertIn("Tan &lt;Wei&gt; Ming", text)
            self.assertNotIn("<Wei>", text)
        self.assertIn("Acme &amp; Sons", pipeline.format_contact(contact))

    def test_closed_stage_schedules_nothing(self):
        self.assertIsNone(pipeline.stage_update_fields("won")["next_followup_at"])
        self.assertIsNotNone(pipeline.stage_update_fields("meeting")["next_followup_at"])

    def test_pipeline_counts_overdue_beyond_the_five_listed(self):
        contacts = [{"full_name": f"P{i}", "stage": "new", "next_followup_at": "2000-01-01T00:00:00+00:00"} for i in range(7)]
        text = pipeline.format_pipeline(contacts)
        self.assertIn("…and 2 more", text)
        self.assertIn("7 follow-ups due", text)


class LlmTests(unittest.IsolatedAsyncioTestCase):
    def _gateway(self, handler):
        patcher = mock.patch.object(llm, "_transport", httpx.MockTransport(handler))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"LLM_GATEWAY_KEY": "tbj_live_card-connect_secretsecret"})
        env.start()
        self.addCleanup(env.stop)

    async def test_request_carries_the_key_and_use_case(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": " hello "}}]})

        self._gateway(handler)
        reply = await llm.vision(llm.USE_CASE_EXTRACT, "sys", "read", b"jpegbytes", "image/jpeg")
        self.assertEqual(reply, "hello")
        self.assertTrue(seen["url"].endswith("/v1/chat/completions"))
        self.assertEqual(seen["headers"]["x-tbj-use-case"], "card-extract")
        self.assertEqual(seen["headers"]["authorization"], "Bearer tbj_live_card-connect_secretsecret")
        image = seen["body"]["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/jpeg;base64,"))
        self.assertNotIn("model", seen["body"])  # the gateway chooses the model

    async def test_gateway_errors_become_llmerror_without_the_key(self):
        self._gateway(lambda request: httpx.Response(403, json={"error": {"code": "not_allowed", "message": "nope"}}))
        with self.assertRaises(llm.LLMError) as caught:
            await llm.complete(llm.USE_CASE_FOLLOWUP, "s", "u")
        self.assertIn("not_allowed", str(caught.exception))
        self.assertNotIn("secretsecret", str(caught.exception))

    async def test_missing_key_fails_before_any_request(self):
        with mock.patch.dict(os.environ, {"LLM_GATEWAY_KEY": ""}):
            with self.assertRaises(llm.LLMError):
                await llm.complete(llm.USE_CASE_FOLLOWUP, "s", "u")

    def test_parse_json_tolerates_fences_and_prose(self):
        self.assertEqual(llm.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.parse_json('Here you go: {"a": {"b": 2}} hope that helps'), {"a": {"b": 2}})
        for bad in ("", "no json here", "[1, 2]"):
            with self.assertRaises(ValueError):
                llm.parse_json(bad)


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _photo(size=(3000, 2000)) -> bytes:
        out = io.BytesIO()
        Image.new("RGB", size, "white").save(out, format="PNG")
        return out.getvalue()

    def test_normalise_card_coerces_shapes_and_drops_junk(self):
        card = extraction.normalise_card(
            {"full_name": "  Ada  ", "phones": "+65 9123 4567", "emails": ["a@b.c", "", None], "title": None, "website": [], "extra": "x"}
        )
        self.assertEqual(card, {"full_name": "Ada", "phones": ["+65 9123 4567"], "emails": ["a@b.c"]})

    async def test_photo_is_downscaled_to_jpeg_and_parsed(self):
        sent = {}

        async def fake_vision(use_case, system, prompt, image_bytes, media_type, max_tokens=0):
            sent.update(use_case=use_case, size=Image.open(io.BytesIO(image_bytes)).size, media_type=media_type)
            return '```json\n{"full_name": "Ada Lim", "emails": ["ada@x.sg"]}\n```'

        with mock.patch.object(llm, "vision", fake_vision):
            card = await extraction.extract_card(self._photo())
        self.assertEqual(card, {"full_name": "Ada Lim", "emails": ["ada@x.sg"]})
        self.assertEqual((sent["use_case"], sent["media_type"]), ("card-extract", "image/jpeg"))
        self.assertEqual(max(sent["size"]), extraction.MAX_EDGE_PX)

    async def test_not_a_card_and_nameless_cards_are_rejected(self):
        for reply in ('{"error": "NOT_A_NAMECARD"}', '{"organization": "Acme"}'):
            with mock.patch.object(llm, "vision", mock.AsyncMock(return_value=reply)):
                with self.assertRaises(extraction.NotANameCard):
                    await extraction.extract_card(self._photo((10, 10)))


class IntelTests(unittest.IsolatedAsyncioTestCase):
    def test_search_results_pick_the_linkedin_profile(self):
        url, snippets = intel.parse_search_results(
            {"organic": [
                {"link": "https://acme.example/team", "title": "Team", "snippet": "Wei Ming leads sales"},
                {"link": "https://sg.linkedin.com/in/weiming?trk=x", "title": "Wei Ming - Acme", "snippet": "Director"},
            ]}
        )
        self.assertEqual(url, "https://sg.linkedin.com/in/weiming")
        self.assertIn("Wei Ming leads sales", snippets)

    async def test_without_search_results_nothing_is_presented_as_found(self):
        reply = json.dumps({"linkedin_url": "https://linkedin.com/in/made-up", "confidence": "high",
                            "data_source": "search_snippets", "pain_points": [], "follow_up_opener": "Hi"})
        with mock.patch.dict(os.environ, {"SERPER_API_KEY": ""}), \
                mock.patch.object(llm, "complete", mock.AsyncMock(return_value=reply)) as complete:
            result = await intel.enrich(CARD)
        self.assertIsNone(result["linkedin_url"])
        self.assertEqual((result["confidence"], result["data_source"]), ("low", "namecard_only"))
        self.assertEqual(complete.call_args.args[0], "card-intel")
        self.assertIn("Tan <Wei> Ming", complete.call_args.args[2])

    def test_intel_message_is_escaped_html(self):
        text = intel.format_intel({"confidence": "low", "pain_points": [{"pain": "A <b> B"}], "linkedin_url": "javascript:x"}, "A & B")
        self.assertIn("A &amp; B", text)
        self.assertIn("A &lt;b&gt; B", text)
        self.assertNotIn("javascript", text)


class FollowupTests(unittest.TestCase):
    def test_context_has_no_dangling_role_and_respects_with_request(self):
        contact = {"full_name": "Ada", "organization": "Acme", "topic": "bots", "request": "an intro"}
        text = followup.contact_context(contact)
        self.assertIn("Role: Acme", text)
        self.assertIn("Their request: an intro", text)
        self.assertNotIn("request", followup.contact_context(contact, with_request=False))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class ReminderTests(TempDb, unittest.IsolatedAsyncioTestCase):
    PAST = "2000-01-01T00:00:00+00:00"

    async def test_due_followup_is_sent_once_then_snoozed(self):
        contact_id = storage.save_contact(7, CARD)
        storage.update_contact(contact_id, next_followup_at=self.PAST)
        bot = FakeBot()
        await main.run_reminders(bot)
        await main.run_reminders(bot)
        self.assertEqual([chat for chat, _ in bot.sent], [7, 7])  # header + card, not repeated
        self.assertIn("Follow up: Tan &lt;Wei&gt; Ming", bot.sent[1][1])
        self.assertGreater(storage.get_contact(contact_id)["next_followup_at"], pipeline.iso(pipeline.now()))

    async def test_day3_and_day14_drafts_are_sent_once(self):
        contact_id = storage.save_contact(7, CARD)
        storage.update_contact(contact_id, linkedin_nudge_at=self.PAST, second_touch_at=self.PAST)
        bot = FakeBot()
        with mock.patch.object(llm, "complete", mock.AsyncMock(return_value="draft <text>")):
            await main.run_reminders(bot)
            await main.run_reminders(bot)
        self.assertEqual(len(bot.sent), 2)
        self.assertIn("Day 3 nudge", bot.sent[0][1])
        self.assertIn("Day 14 check-in", bot.sent[1][1])
        self.assertIn("draft &lt;text&gt;", bot.sent[0][1])
        contact = storage.get_contact(contact_id)
        self.assertIsNone(contact["linkedin_nudge_at"])
        self.assertIsNone(contact["second_touch_at"])

    async def test_second_touch_is_dropped_once_they_have_replied(self):
        contact_id = storage.save_contact(7, CARD)
        storage.update_contact(contact_id, stage="meeting", second_touch_at=self.PAST)
        bot = FakeBot()
        with mock.patch.object(llm, "complete", mock.AsyncMock(return_value="draft")) as complete:
            await main.run_reminders(bot)
        self.assertEqual(bot.sent, [])
        complete.assert_not_called()
        self.assertIsNone(storage.get_contact(contact_id)["second_touch_at"])

    async def test_failed_draft_is_retried_on_the_next_run(self):
        contact_id = storage.save_contact(7, CARD)
        storage.update_contact(contact_id, linkedin_nudge_at=self.PAST)
        bot = FakeBot()
        with mock.patch.object(llm, "complete", mock.AsyncMock(side_effect=llm.LLMError("down"))):
            await main.run_reminders(bot)
        self.assertEqual(bot.sent, [])
        self.assertEqual(storage.get_contact(contact_id)["linkedin_nudge_at"], self.PAST)


class BotWiringTests(unittest.TestCase):
    def test_whatsapp_link(self):
        self.assertEqual(main.whatsapp_link("+65 9123-4567", "Ada Lim"), "https://wa.me/6591234567?text=Hi%20Ada%2C%20great%20meeting%20you%20%E2%80%94%20this%20is%20Julian%21")
        self.assertIsNone(main.whatsapp_link("12345", "Ada"))

    def test_application_builds_with_all_handlers_and_the_daily_job(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CARD_CONNECT_PERSISTENCE_PATH": str(Path(tmp) / "p.pickle")}):
            app = main.build_application("123456:TEST-TOKEN")
            self.assertIn(-1, app.handlers)  # the allow-list gate runs first
            self.assertEqual({job.name for job in app.job_queue.jobs()}, {"daily_reminders", "startup_reminders"})


if __name__ == "__main__":
    unittest.main()
