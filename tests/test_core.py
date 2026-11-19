"""Yaver - MQTT, mikrofon, LLM gerektirmeyen saf fonksiyonlarin testleri.

Calistirma (proje kokunden, venv aktifken):
    python -m unittest discover tests
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain  # noqa: E402
import common  # noqa: E402
import ear  # noqa: E402
import skills  # noqa: E402


def drain(text):
    """split_sentence'i tampon bitene kadar calistirir."""
    sentences, buffer = [], text
    while True:
        sentence, buffer = brain.split_sentence(buffer)
        if not sentence:
            return sentences, buffer
        sentences.append(sentence)


class SplitSentenceTest(unittest.TestCase):
    def test_normal_sentences_stream_one_by_one(self):
        sentences, rest = drain("Lamba şu anda açık durumda. Başka bir konuda da yardım edebilirim. ")
        self.assertEqual(sentences, ["Lamba şu anda açık durumda.", "Başka bir konuda da yardım edebilirim."])
        self.assertEqual(rest, "")

    def test_short_first_sentence_merges_instead_of_blocking(self):
        sentences, _ = drain("Tamam. Lamba şu anda açık durumda. Başka bir konuda yardım edebilirim. ")
        self.assertEqual(sentences[0], "Tamam. Lamba şu anda açık durumda.")
        self.assertEqual(len(sentences), 2)

    def test_unfinished_sentence_waits(self):
        sentence, rest = brain.split_sentence("Lamba şu anda açık dur")
        self.assertIsNone(sentence)
        self.assertEqual(rest, "Lamba şu anda açık dur")


class HistoryTrimTest(unittest.TestCase):
    def setUp(self):
        brain.history.clear()

    def tearDown(self):
        brain.history.clear()

    def test_history_is_trimmed_in_blocks(self):
        limit, keep = brain.BRAIN["history_turns"], brain.BRAIN["history_trim_to"]
        for i in range(limit):
            brain.add_turn([{"role": "user", "content": str(i)}])
        self.assertEqual(len(brain.history), limit)       # sinira kadar hic atilmaz
        brain.add_turn([{"role": "user", "content": "yeni"}])
        self.assertEqual(len(brain.history), keep)        # asilinca tek seferde keep'e iner
        self.assertEqual(brain.history[-1][0]["content"], "yeni")

    def test_tool_turn_stays_whole(self):
        tool_turn = [{"role": "user", "content": "ledi yak"},
                     {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
                     {"role": "tool", "tool_call_id": "1", "content": "{}"},
                     {"role": "assistant", "content": "Yaktim."}]
        for _ in range(brain.BRAIN["history_turns"] + 1):
            brain.add_turn(list(tool_turn))
        for turn in brain.history:
            self.assertEqual(len(turn), 4)


class RequestBodyTest(unittest.TestCase):
    def test_body_has_model_and_fields(self):
        body = brain.request_body([{"role": "user", "content": "x"}], stream=True)
        self.assertEqual(body["model"], brain.LOCAL["model"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_tokens"], brain.BRAIN["max_tokens"])


class JunkFilterTest(unittest.TestCase):
    def test_hallucinations_are_filtered(self):
        for text in ["Altyazı M.K.", "İzlediğiniz için teşekkürler.", "Teşekkürler.",
                     "Abone olmayı unutmayın", "Thank you."]:
            self.assertTrue(ear.is_junk(text), text)

    def test_real_speech_is_kept(self):
        for text in ["Yaver, masadaki ledi yak.", "Yaver teşekkürler, ışığı kapat"]:
            self.assertFalse(ear.is_junk(text), text)

    def test_normalize(self):
        self.assertEqual(ear.normalize("  IŞIK, Çok   GÜZEL! "), "isik cok guzel")


class TriggerTest(unittest.TestCase):
    def test_trigger_anywhere(self):
        self.assertTrue(ear.contains_trigger("Bugün hava nasıl Yaver?"))
        self.assertTrue(ear.contains_trigger("Yaver'e bir soru"))
        self.assertFalse(ear.contains_trigger("bugün hava nasıl"))


class PayloadTest(unittest.TestCase):
    def msg(self, payload):
        return SimpleNamespace(payload=payload)

    def test_valid_object(self):
        self.assertEqual(common.parse_payload(self.msg(b'{"text": "selam"}')), {"text": "selam"})

    def test_bad_payloads_return_none(self):
        for payload in [b"123", b"[1, 2]", b"not json", b"\xff\xfe", b""]:
            self.assertIsNone(common.parse_payload(self.msg(payload)), payload)

    def test_strip_emoji(self):
        self.assertEqual(common.strip_emoji("Evet, ben Yaver'im. \U0001F60A"), "Evet, ben Yaver'im.")
        self.assertEqual(common.strip_emoji("\U0001F60A"), "")
        self.assertEqual(common.strip_emoji("Işık açık, güneş doğdu."), "Işık açık, güneş doğdu.")

    def test_text_field(self):
        self.assertEqual(common.text_field({"text": "  merhaba "}), "merhaba")
        self.assertEqual(common.text_field({"text": None}), "")
        self.assertEqual(common.text_field({"text": 5}), "")
        self.assertEqual(common.text_field({}), "")

    def test_guard_swallows_exceptions(self):
        @common.guard
        def broken(*args):
            raise ValueError("bozuk")
        # guard hatayi bilerek yazdirir - test ciktisini kirletmesin
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(broken(None, None, None))


class SkillsTest(unittest.TestCase):
    def test_unknown_skill(self):
        result = skills.run_skill(None, "yok_boyle_arac", {})
        self.assertEqual(result["status"], "error")

    def test_wrong_params_do_not_crash(self):
        result = skills.run_skill(None, "led_control", {"yanlis": 1})
        self.assertEqual(result["status"], "error")

    def test_device_schema_lists_registered_leds(self):
        schema = skills.device_param_schema("led")
        self.assertEqual(set(schema["enum"]), set(skills.devices_by_type("led")))


class DeviceCommandTest(unittest.TestCase):
    """type: board kartlari (Yaver Saat) - id eslesmeli cihaz sonucu."""

    class FakeBoardClient:
        """publish edilen komuta, once BASKA bir istegin sonucunu sonra
        dogru id'li sonucu veren sahte kart."""

        def __init__(self, status="ok"):
            self.ctx = None
            self.status = status
            self.sent = []

        def publish(self, topic, payload, retain=False, qos=0):
            import json as _json
            self.sent.append((topic, payload))
            request = _json.loads(payload)
            ack = skills.BOARDS["saat"]["ack_topic"]
            self.ctx.deliver(ack, _json.dumps({"id": "baska", "status": "error"}))
            self.ctx.deliver(ack, _json.dumps({"id": request["id"], "command": request["command"],
                                               "status": self.status, "data": {"sec": 120},
                                               "message": "olmadi"}))

    def make_ctx(self, status="ok"):
        client = self.FakeBoardClient(status)
        ctx = skills.SkillContext(client)
        client.ctx = ctx
        return ctx, client

    def test_parse_device_result(self):
        self.assertIsNone(skills.parse_device_result("bozuk", "a"))
        self.assertIsNone(skills.parse_device_result('{"id": "b"}', "a"))
        self.assertIsNone(skills.parse_device_result("[1]", "a"))
        self.assertEqual(skills.parse_device_result('{"id": "a", "status": "ok"}', "a")["status"], "ok")

    def test_command_matches_own_id(self):
        ctx, client = self.make_ctx()
        result = skills.run_skill(ctx, "device_command", {"device": "saat", "command": "bluetooth_pairing"})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["data"]["data"], {"sec": 120})
        self.assertEqual(client.sent[0][0], skills.BOARDS["saat"]["topic"])

    def test_device_error_is_reported(self):
        ctx, _ = self.make_ctx(status="error")
        result = skills.run_skill(ctx, "device_command", {"device": "saat", "command": "bluetooth_pairing"})
        self.assertEqual(result["status"], "error")
        self.assertIn("olmadi", result["data"])

    def test_offline_device_is_not_sent(self):
        ctx, client = self.make_ctx()
        ctx.deliver(skills.BOARDS["saat"]["online_topic"], "0")
        result = skills.run_skill(ctx, "device_command", {"device": "saat", "command": "bluetooth_pairing"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(client.sent, [])

    def test_unknown_command_rejected(self):
        ctx, client = self.make_ctx()
        result = skills.run_skill(ctx, "device_command", {"device": "saat", "command": "rm_rf"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(client.sent, [])

    def sent_request(self, client):
        import json as _json
        return _json.loads(client.sent[0][1])

    def test_alarm_set_sends_only_its_params(self):
        ctx, client = self.make_ctx()
        result = skills.run_skill(ctx, "saat_alarm", {"action": "set", "time": "07:00", "date": "yarin",
                                                      "days": "haftaici", "id": "a1", "min": None})
        self.assertEqual(result["status"], "ok", result)
        request = self.sent_request(client)
        self.assertEqual(request["command"], "alarm_set")
        # set + days -> set_repeating'e gecer (switch_if_given), date/id/min gitmez
        self.assertEqual(request["params"], {"time": "07:00", "days": "haftaici"})
        self.assertEqual(result["data"]["action"], "set_repeating")

    def test_one_time_alarm_keeps_date(self):
        ctx, client = self.make_ctx()
        skills.run_skill(ctx, "saat_alarm", {"action": "set", "time": "08:00", "date": "yarin"})
        self.assertEqual(self.sent_request(client)["params"], {"time": "08:00", "date": "yarin"})

    def test_music_action_goes_to_params(self):
        ctx, client = self.make_ctx()
        skills.run_skill(ctx, "saat_music", {"action": "next"})
        request = self.sent_request(client)
        self.assertEqual((request["command"], request["params"]), ("music", {"action": "next"}))

    def test_unknown_action_rejected(self):
        ctx, client = self.make_ctx()
        result = skills.run_skill(ctx, "saat_alarm", {"action": "patlat"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(client.sent, [])

    def test_double_stop_is_not_an_error(self):
        # Saat alarmi kendisi durdurdu (alarm_stopped olayi), sonra Yaver'in
        # stop komutu "calan alarm yok" hatasi aldi -> hata sayilmaz.
        skills.recent_events.clear()
        ctx, _ = self.make_ctx(status="error")
        self.assertEqual(skills.run_skill(ctx, "saat_alarm", {"action": "stop"})["status"], "error")
        skills.handle_board_event(ctx, "saat", '{"event": "alarm_stopped", "id": "a1"}')
        result = skills.run_skill(ctx, "saat_alarm", {"action": "stop"})
        self.assertEqual(result["status"], "ok", result)
        skills.recent_events.clear()

    def test_ringing_event_is_announced(self):
        import json as _json
        notes = []

        class Client:
            def publish(self, topic, payload, retain=False, qos=0):
                notes.append((topic, _json.loads(payload)))
        ctx = skills.SkillContext(Client())
        skills.handle_board_event(ctx, "saat", '{"event": "alarm_ringing", "id": "a1", "time": "07:00"}')
        skills.handle_board_event(ctx, "saat", '{"event": "alarm_stopped", "id": "a1"}')  # duyurulmaz
        skills.handle_board_event(ctx, "saat", "bozuk")
        skills.handle_board_event(ctx, "saat", '{"event": 5}')
        self.assertEqual(len(notes), 1)
        topic, packet = notes[0]
        self.assertEqual(topic, skills.SK["topic_event"])
        self.assertIn("07:00", packet["data"]["message"])
        self.assertNotIn("{label}", packet["data"]["message"])
        skills.recent_events.clear()


class ClaudeConversionTest(unittest.TestCase):
    def test_tool_turn_becomes_tool_use_and_merged_results(self):
        messages = [
            {"role": "user", "content": "ikisini de kapat"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "led_control", "arguments": '{"state": "off"}'}},
                {"id": "b", "type": "function", "function": {"name": "led_control", "arguments": "bozuk"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "{}"},
            {"role": "tool", "tool_call_id": "b", "content": "{}"},
            {"role": "assistant", "content": ""},
        ]
        out = brain.to_claude_messages(messages)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user"])  # bos cevap atildi
        self.assertEqual([b["type"] for b in out[1]["content"]], ["tool_use", "tool_use"])
        self.assertEqual(out[1]["content"][0]["input"], {"state": "off"})
        self.assertEqual(out[1]["content"][1]["input"], {})  # bozuk JSON cokertmez
        self.assertEqual([b["tool_use_id"] for b in out[2]["content"]], ["a", "b"])  # tek mesajda

    def test_raw_claude_content_is_sent_back_unchanged(self):
        raw = [{"type": "thinking", "thinking": "", "signature": "s"},
               {"type": "tool_use", "id": "a", "name": "x", "input": {}}]
        out = brain.to_claude_messages([{"role": "assistant", "content": None, "tool_calls": [], "claude_content": raw}])
        self.assertIs(out[0]["content"], raw)

    def test_openai_messages_drop_claude_fields(self):
        out = brain.openai_messages("S", [{"role": "assistant", "content": "x", "claude_content": [1]}])
        self.assertEqual(out, [{"role": "system", "content": "S"}, {"role": "assistant", "content": "x"}])

    def test_tool_schema(self):
        tools = brain.to_claude_tools([{"type": "function", "function": {"name": "t", "description": "d",
                                                                        "parameters": {"type": "object"}}}])
        self.assertEqual(tools, [{"name": "t", "description": "d", "input_schema": {"type": "object"}}])


class ConfirmationTest(unittest.TestCase):
    def test_answers(self):
        self.assertEqual(brain.confirmation_answer("Evet, onaylıyorum."), "yes")
        self.assertEqual(brain.confirmation_answer("Hayır, vazgeç."), "no")
        self.assertEqual(brain.confirmation_answer("evet hayır yapma"), "no")  # karisik = iptal
        self.assertIsNone(brain.confirmation_answer("Hava nasıl?"))

    def test_tool_list_confirm_flag_is_stripped(self):
        tools = [{"type": "function", "function": {"name": "a"}, "confirm": True},
                 {"type": "function", "function": {"name": "b"}}]
        try:
            brain.apply_tool_list(tools)
            self.assertEqual(brain.confirm_tools, {"a"})
            self.assertTrue(all("confirm" not in t for t in brain.available_tools))
        finally:
            brain.apply_tool_list([])


class RepeatFilterTest(unittest.TestCase):
    def setUp(self):
        brain.history.clear()

    def tearDown(self):
        brain.history.clear()

    def test_trailing_filler_is_dropped_but_first_sentence_kept(self):
        brain.add_turn([{"role": "user", "content": "x"},
                        {"role": "assistant", "content": "Bilmiyorum. Lütfen daha net bir şekilde sor."}])
        seen = brain.recent_sentences(3)
        self.assertTrue(brain.is_repeat("Lütfen daha net bir şekilde sor.", seen, 1))
        self.assertFalse(brain.is_repeat("Bilmiyorum.", seen, 0))   # ilk cumle hep soylenir
        self.assertFalse(brain.is_repeat("Yeni bir cümle.", seen, 1))


class AskLoopTest(unittest.TestCase):
    """ask()'in arac dongusu - LLM ve skills sahte."""

    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())  # arac loglari test ciktisini kirletmesin
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        brain.history.clear()
        brain.pending_action.clear()
        brain.apply_tool_list([{"type": "function", "function": {"name": "led_control"}},
                               {"type": "function", "function": {"name": "delegate_task"}, "confirm": True}])
        self.saved = (brain.llm_turn, brain.call_skill, brain.write_history, brain.client_ref[0])
        brain.client_ref[0] = object()
        brain.write_history = lambda: None
        self.skill_calls = []
        brain.call_skill = lambda name, params, on_slow=None: (self.skill_calls.append((name, params))
                                                               or {"status": "ok", "data": {}})

    def tearDown(self):
        brain.llm_turn, brain.call_skill, brain.write_history, brain.client_ref[0] = self.saved
        brain.apply_tool_list([])
        brain.history.clear()
        brain.pending_action.clear()

    @staticmethod
    def call(name, args):
        return {"id": f"id_{name}_{args}", "type": "function", "function": {"name": name, "arguments": args}}

    def script(self, *replies):
        replies = list(replies)
        self.rounds = []

        def fake(system, messages, tools, on_sentence, final_round=False):
            self.rounds.append(final_round)
            text, calls = replies.pop(0)
            if text and not calls:
                on_sentence(text)
            return text, calls, {}
        brain.llm_turn = fake

    def test_two_tools_in_one_round_then_answer(self):
        self.script(("", [self.call("led_control", '{"state": "off", "device": "a"}'),
                          self.call("led_control", '{"state": "off", "device": "b"}')]),
                    ("İkisini de kapattım.", []))
        self.assertEqual(brain.ask("ikisini de kapat"), "İkisini de kapattım.")
        self.assertEqual(len(self.skill_calls), 2)
        self.assertEqual(len(brain.history[-1]), 5)  # soru, karar, 2 sonuc, cevap

    def test_repeated_identical_call_is_not_run_twice(self):
        same = self.call("led_control", '{"state": "on"}')
        self.script(("", [same]), ("", [same]), ("Yaktım.", []))
        brain.ask("ledi yak")
        self.assertEqual(len(self.skill_calls), 1)
        self.assertEqual(self.rounds[-1], True)  # tekrar gorulunce son (aracsiz) adima gecildi

    def test_confirm_tool_waits_for_spoken_yes(self):
        self.script(("", [self.call("delegate_task", '{"task": "derle"}')]), ("Onaylıyor musun?", []))
        brain.ask("projeyi derle")
        self.assertEqual(self.skill_calls, [])            # onaysiz calismadi
        self.assertEqual(brain.pending_action["name"], "delegate_task")
        self.script(("Başlattım.", []))
        brain.ask("evet")
        self.assertEqual(self.skill_calls, [("delegate_task", {"task": "derle"})])
        self.assertFalse(brain.pending_action)

    def test_confirm_tool_cancelled_by_no(self):
        self.script(("", [self.call("delegate_task", '{"task": "derle"}')]), ("Onaylıyor musun?", []))
        brain.ask("projeyi derle")
        self.script(("Tamam, iptal ettim.", []))
        brain.ask("hayır vazgeç")
        self.assertEqual(self.skill_calls, [])


class ClaudeStreamTest(unittest.TestCase):
    """claude_turn() + ask(): sahte HTTP sunucusu (internet/anahtar gerekmez)."""

    @staticmethod
    def sse(events):
        import json as _json
        return "".join(f"event: {e['type']}\ndata: {_json.dumps(e)}\n\n" for e in events).encode()

    def test_tool_round_echoes_thinking_and_keeps_history_clean(self):
        try:
            import anthropic
            import httpx2
        except ImportError:
            self.skipTest("anthropic paketi yok")
        import json as _json
        start = {"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "x", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}}
        end = lambda reason: [{"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None},
                               "usage": {"output_tokens": 1}}, {"type": "message_stop"}]
        streams = [
            [start, {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
             {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}},
             {"type": "content_block_stop", "index": 0},
             {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "get_datetime", "input": {}}},
             {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
             {"type": "content_block_stop", "index": 1}] + end("tool_use"),
            [start, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
             {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Saat on dört beş."}},
             {"type": "content_block_stop", "index": 0}] + end("end_turn"),
        ]
        requests = []

        def handler(request):
            requests.append(_json.loads(request.content))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=self.sse(streams.pop(0)))

        saved = (brain.PROVIDER, brain.claude_ref[0], brain.client_ref[0], brain.call_skill, brain.write_history)
        brain.PROVIDER = "claude"
        brain.claude_ref[0] = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        brain.client_ref[0] = object()
        brain.call_skill = lambda n, p, on_slow=None: {"status": "ok", "data": {"saat": "14:05"}}
        brain.write_history = lambda: None
        brain.history.clear()
        brain.apply_tool_list([{"type": "function", "function": {"name": "get_datetime", "parameters": {"type": "object"}}}])
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                reply = brain.ask("saat kaç")
        finally:
            (brain.PROVIDER, brain.claude_ref[0], brain.client_ref[0], brain.call_skill, brain.write_history) = saved
            brain.apply_tool_list([])
            turn = brain.history.pop()
        self.assertEqual(reply, "Saat on dört beş.")
        self.assertNotIn("temperature", requests[0])
        self.assertEqual(requests[1]["messages"][1]["content"][0], {"type": "thinking", "thinking": "", "signature": "SIG"})
        self.assertEqual(requests[1]["messages"][2]["content"][0]["type"], "tool_result")
        self.assertFalse(any("claude_content" in m for m in turn))  # thinking gecmise yazilmaz


class SkillHelpersTest(unittest.TestCase):
    def test_number_to_turkish(self):
        cases = {0: "sıfır", 7: "yedi", 14: "on dört", 100: "yüz", 101: "yüz bir", 1000: "bin",
                 2026: "iki bin yirmi altı", 1999: "bin dokuz yüz doksan dokuz", -5: "eksi beş"}
        for n, words in cases.items():
            self.assertEqual(skills.number_to_turkish(n), words)

    def test_describe_now(self):
        import time as _time
        now = _time.mktime((2026, 11, 16, 14, 5, 0, 0, 0, -1))
        info = skills.describe_now(now)
        self.assertEqual(info["gun"], "Pazartesi")
        self.assertEqual(info["tarih"], "16 Kasım 2026")
        self.assertEqual(info["saat"], "14:05")
        self.assertEqual(info["saat_yazi"], "on dört beş")

    def test_safe_eval(self):
        self.assertEqual(skills.safe_eval("17*23"), 391)
        self.assertEqual(skills.safe_eval("(120-35)/5"), 17)
        self.assertEqual(skills.safe_eval("2,5*2"), 5)
        for bad in ["__import__('os')", "a+1", "2**100000", "[1]"]:
            with self.assertRaises(Exception):
                skills.safe_eval(bad)

    def test_publish_and_wait_gets_ack(self):
        class FakeClient:
            def publish(self, topic, payload, retain=False, qos=0):
                ctx.deliver("ack", payload)   # cihaz aninda cevap veriyor
        ctx = skills.SkillContext(FakeClient())
        self.assertEqual(ctx.publish_and_wait("cmd", "on", "ack", 1.0), "on")

    def test_led_without_ack_is_error(self):
        ctx = skills.SkillContext(None)  # kimse cevap vermez
        device = next(iter(skills.devices_by_type("led")))
        saved = skills.SK["device_ack_timeout_sec"]
        skills.CONFIG["devices"][device]["ack_timeout_sec"] = 0.05
        try:
            result = skills.run_skill(ctx, "led_control", {"device": device, "state": "on"})
        finally:
            skills.CONFIG["devices"][device].pop("ack_timeout_sec")
            skills.SK["device_ack_timeout_sec"] = saved
        self.assertEqual(result["status"], "error")

    def test_confirm_flag_in_tool_list(self):
        defs = {t["function"]["name"]: t for t in skills.tool_definitions()}
        self.assertNotIn("confirm", defs["get_datetime"])


class EarTextTest(unittest.TestCase):
    def test_stop_command_needs_trigger_and_stop_word(self):
        self.assertTrue(ear.is_stop_command("Yaver dur."))
        self.assertTrue(ear.is_stop_command("Yaver, sus artık"))
        self.assertFalse(ear.is_stop_command("Dur bakalım, düşüneyim."))     # tetik yok
        self.assertFalse(ear.is_stop_command("Yaver bugün hava nasıl?"))     # dur kelimesi yok
        self.assertFalse(ear.is_stop_command("Yaver durumu nasıl?"))         # 'durumu' != 'dur'

    def test_followup_window(self):
        self.assertTrue(ear.accept_utterance("evet", False, now=10, followup_deadline=12))
        self.assertFalse(ear.accept_utterance("evet", False, now=13, followup_deadline=12))
        self.assertTrue(ear.accept_utterance("Yaver saat kaç", False, now=13, followup_deadline=12))


class SharedHelpersTest(unittest.TestCase):
    def test_modules_do_not_shadow_common_helpers(self):
        # ear.py'nin kendi mqtt_connect()'i common'dakini ezmisti, ear acilista
        # TypeError ile coktu (yasanmis) - ayni isim tekrar tanimlanmasin.
        for module in (brain, ear, skills):
            for name in ("mqtt_client", "mqtt_connect", "parse_payload", "guard", "load_config"):
                if hasattr(module, name):
                    self.assertIs(getattr(module, name), getattr(common, name), f"{module.__name__}.{name}")


class PathTest(unittest.TestCase):
    def test_relative_paths_resolve_to_project_root(self):
        self.assertEqual(common.resolve_path("voices/x.onnx"), common.ROOT / "voices" / "x.onnx")
        absolute = Path(__file__).resolve()
        self.assertEqual(common.resolve_path(str(absolute)), absolute)


if __name__ == "__main__":
    unittest.main()
