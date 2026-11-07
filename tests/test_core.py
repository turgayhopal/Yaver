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
