"""Locale packs for the offline extractor.

Every case builds threads from the model objects, the way the other tests do.
The English cases matter as much as the non-English ones: the default pack is
the fallback, and English output must not change at all.
"""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from thread_to_skill import util
from thread_to_skill.cli import main
from thread_to_skill.config import Config
from thread_to_skill.locales import available_locales, detect_locale, get_pack, resolve_locale
from thread_to_skill.locales import de, en, es, ja, zh
from thread_to_skill.models import Message, Thread
from thread_to_skill.offline import offline_distill

PACK_ATTRIBUTES = (
    "NAME", "CUE_ACTION", "CUE_FIX", "CUE_DECISION", "CUE_PITFALL", "CUE_VERIFY",
    "GREETING", "EXTRA_STOPWORDS",
)

ZH_PAIRS = [
    ("Leo", "首先删除缓存，然后运行迁移脚本，最后清理连接池。"),
    ("Nadia", "我们决定使用 PostgreSQL，不再使用 MySQL。"),
    ("Leo", "注意，这个改动会破坏 CI 缓存，不要直接合并。"),
    ("Nadia", "验证：运行 pytest 应当看到全部测试通过。"),
]
JA_PAIRS = [
    ("Aiko", "まず pytest を実行し、その後、キャッシュを削除します。"),
    ("Aiko", "開発チームは PostgreSQL を採用すると決定しました。"),
    ("Kenji", "注意：この変更は CI を壊す。"),
    ("Kenji", "確認：テストを実行して、期待どおりに完了していることを確認してください。"),
]
DE_PAIRS = [
    ("Jonas", "Zuerst aktualisieren wir die Pipeline und dann installieren wir den Fix."),
    ("Jonas", "Wir haben entschieden, dass wir Postgres nehmen."),
    ("Miriam", "Vorsicht, das ist ein Anti-Pattern."),
    ("Miriam", "Stellen wir sicher, dass alle Tests laufen und bestätigen wir das Ergebnis."),
]
ES_PAIRS = [
    ("Ana", "Primero ejecuta la migración y luego agrega el índice."),
    ("Ana", "Decidimos usar Postgres para las colas; no usamos Redis."),
    ("Bruno", "Cuidado, el worker no se reinicia solo."),
    ("Bruno", "Asegúrate de que la prueba pasa; ejecuta la prueba de humo antes del despliegue."),
]
EN_PAIRS = [
    ("Leo", "Our on-call rotation keeps missing handovers. Can we make this boring?"),
    ("Leo", "We should add a 15-minute handover template: open incidents, risky deploys, expiring certs, and who owns each item."),
    ("Nadia", "Decided: the outgoing on-call posts the template in #oncall before 10am, and the incoming on-call replies with explicit acknowledgement."),
    ("Leo", "Gotcha: a thread reply is not acknowledgement. Use the checkmark reaction so we can audit it."),
    ("Nadia", "Verification: every weekday handover has a checkmark within 30 minutes, and the weekly review lists any misses."),
]

# One sentence per category, and each marker must land in its own section.
CUE_MARKERS = {
    "zh": {"procedure": "删除", "decision_rules": "决定", "pitfalls": "注意", "verification": "测试"},
    "ja": {"procedure": "実行", "decision_rules": "決定", "pitfalls": "注意", "verification": "確認"},
    "de": {"procedure": "zuerst", "decision_rules": "entschieden", "pitfalls": "vorsicht", "verification": "bestätigen"},
    "es": {"procedure": "primero", "decision_rules": "decidimos", "pitfalls": "cuidado", "verification": "asegúrate"},
}
LOCALE_PAIRS = {"zh": ZH_PAIRS, "ja": JA_PAIRS, "de": DE_PAIRS, "es": ES_PAIRS}


def build_thread(pairs, *, thread_key="locale"):
    messages = [
        Message(
            id=f"m{index}",
            ts=f"2026-11-01T09:{index:02d}:00Z",
            author=author,
            text=text,
        )
        for index, (author, text) in enumerate(pairs)
    ]
    return Thread.create(
        source="slack",
        channel="engineering",
        thread_key=thread_key,
        messages=messages,
        title="Locale pack check",
    )


def joined(values):
    return "\n".join(str(value) for value in values).lower()


class PackShapeTests(unittest.TestCase):
    def test_available_locales_lists_every_pack(self):
        self.assertEqual(available_locales(), ("en", "zh", "ja", "de", "es"))

    def test_get_pack_returns_the_module_for_a_known_name(self):
        self.assertIs(get_pack("en"), en)
        self.assertIs(get_pack("ja"), ja)

    def test_get_pack_raises_for_an_unknown_name(self):
        with self.assertRaises(KeyError):
            get_pack("klingon")

    def test_every_pack_defines_the_documented_attributes(self):
        for name in available_locales():
            pack = get_pack(name)
            self.assertEqual(pack.NAME, name)
            for attribute in PACK_ATTRIBUTES:
                self.assertTrue(hasattr(pack, attribute), f"{name} lacks {attribute}")
            for cue_attribute in PACK_ATTRIBUTES[1:-1]:
                self.assertIsInstance(getattr(pack, cue_attribute), tuple)
            self.assertIsInstance(pack.EXTRA_STOPWORDS, tuple)

    def test_english_pack_has_no_extra_stop_words(self):
        # The base stop word list in util.py covers English, so its extras stay empty.
        self.assertEqual(en.EXTRA_STOPWORDS, ())

    def test_english_tuples_stay_importable_from_offline(self):
        from thread_to_skill import offline

        self.assertIs(offline.CUE_ACTION, en.CUE_ACTION)
        self.assertIs(offline.CUE_FIX, en.CUE_FIX)
        self.assertIs(offline.CUE_DECISION, en.CUE_DECISION)
        self.assertIs(offline.CUE_PITFALL, en.CUE_PITFALL)
        self.assertIs(offline.CUE_VERIFY, en.CUE_VERIFY)
        self.assertIs(offline.GREETING, en.GREETING)


class CuePickupTests(unittest.TestCase):
    def test_each_pack_fills_its_own_categories(self):
        for name, pairs in LOCALE_PAIRS.items():
            with self.subTest(locale=name):
                result = offline_distill([build_thread(pairs, thread_key=f"{name}-cue")], locale=name)
                sections = {
                    "procedure": [step["step"] for step in result["procedure"]],
                    "decision_rules": [rule["then"] for rule in result["decision_rules"]],
                    "pitfalls": [item["pitfall"] for item in result["pitfalls"]],
                    "verification": result["verification"],
                }
                for section, marker in CUE_MARKERS[name].items():
                    self.assertTrue(
                        any(marker.lower() in text.lower() for text in sections[section]),
                        f"{name} {section} missing {marker}",
                    )
                # Every category came from the pack, so none of them is a fallback.
                self.assertFalse(result["open_questions"])

    def test_chinese_fixture_produces_all_four_sections(self):
        result = offline_distill([build_thread(ZH_PAIRS, thread_key="zh-fixture")], locale="zh")
        self.assertTrue(result["procedure"])
        self.assertTrue(result["decision_rules"])
        self.assertTrue(result["pitfalls"])
        self.assertTrue(result["verification"])
        self.assertFalse(result["open_questions"])
        # Content words survive; the Chinese function words are filtered.
        self.assertIn("postgresql", result["tags"])
        self.assertNotIn("我们", [tag for tag in result["tags"]])

    def test_stopword_filtering_uses_the_active_pack(self):
        tokens = util.tokenize("我们 决定 使用 uv", zh.EXTRA_STOPWORDS)
        self.assertNotIn("我们", tokens)
        self.assertIn("决定", tokens)
        self.assertIn("使用", tokens)
        # English filtering is unchanged when no extras are passed.
        self.assertEqual(util.tokenize("we should the cache uv"), ["cache", "uv"])
        self.assertEqual(
            util.tokenize("wir und der cache", de.EXTRA_STOPWORDS), ["cache"]
        )


class DetectionTests(unittest.TestCase):
    def test_cjk_text_votes_for_its_own_pack(self):
        self.assertEqual(detect_locale(ZH_PAIRS[0][1]), "zh")
        self.assertEqual(detect_locale(JA_PAIRS[0][1]), "ja")

    def test_keyword_vote_needs_the_threshold(self):
        self.assertEqual(detect_locale("\n".join(text for _, text in DE_PAIRS)), "de")
        self.assertEqual(detect_locale("\n".join(text for _, text in ES_PAIRS)), "es")
        # Below the threshold the answer is always English, not a guess.
        self.assertEqual(detect_locale("la"), "en")
        self.assertEqual(detect_locale("wir und"), "en")
        self.assertEqual(detect_locale(""), "en")

    def test_ordinary_english_stays_english(self):
        self.assertEqual(detect_locale("\n".join(text for _, text in EN_PAIRS)), "en")
        fixture = Path(__file__).resolve().parent / "fixtures" / "generic" / "threads.jsonl"
        self.assertEqual(detect_locale(fixture.read_text(encoding="utf-8")), "en")


class ResolveTests(unittest.TestCase):
    def test_auto_and_empty_resolve_through_detection(self):
        self.assertEqual(resolve_locale("", "\n".join(text for _, text in EN_PAIRS)), "en")
        self.assertEqual(resolve_locale("auto", JA_PAIRS[0][1]), "ja")

    def test_an_explicit_name_wins_over_detection(self):
        self.assertEqual(resolve_locale("es", ZH_PAIRS[0][1]), "es")

    def test_an_unknown_name_warns_and_falls_back_to_english(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(resolve_locale("klingon", "hello world"), "en")
        self.assertIn("klingon", stderr.getvalue())
        self.assertIn("en", stderr.getvalue())

    def test_the_english_path_is_identical_for_none_en_and_auto(self):
        thread = build_thread(EN_PAIRS, thread_key="en-identity")
        base = offline_distill([thread])
        self.assertEqual(offline_distill([thread], locale="en"), base)
        self.assertEqual(offline_distill([thread], locale="auto"), base)
        # Not a vacuous comparison: the English path really extracted content.
        self.assertTrue(base["decision_rules"])

    def test_config_reads_the_locale_key_and_defaults_to_auto(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = Config.create(Path(temp.name), provider="offline")
        self.assertEqual(config.locale, "auto")
        self.assertEqual(json.loads((config.home / "config.json").read_text(encoding="utf-8"))["locale"], "auto")
        stored = Config(home=config.home, data={})
        self.assertEqual(stored.locale, "auto")


class CLILocaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".thread2skill"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def ingest_chinese_thread(self) -> str:
        code, output, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        rows = [
            json.dumps(
                {
                    "thread_id": "zh-cli",
                    "id": f"m{index}",
                    "timestamp": f"2026-11-01T09:{index:02d}:00Z",
                    "author": author,
                    "text": text,
                },
                ensure_ascii=False,
            )
            for index, (author, text) in enumerate(ZH_PAIRS)
        ]
        fixture = self.root / "zh_threads.jsonl"
        fixture.write_text("\n".join(rows) + "\n", encoding="utf-8")
        code, output, _ = self.run_cli("ingest", str(fixture), "--json")
        self.assertEqual(code, 0)
        return json.loads(output)["threads"][0]["id"]

    def test_auto_detection_runs_through_the_cli(self):
        thread_id = self.ingest_chinese_thread()
        code, output, _ = self.run_cli("distill", thread_id, "--dry-run", "--json")
        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertTrue(any("决定" in rule["then"] for rule in data["decision_rules"]))

    def test_locale_flag_overrides_auto_detection(self):
        thread_id = self.ingest_chinese_thread()
        code, output, _ = self.run_cli(
            "distill", thread_id, "--provider", "offline", "--locale", "en", "--dry-run", "--json"
        )
        self.assertEqual(code, 0)
        data = json.loads(output)
        # English packs find no cues in Chinese text, so the generic fallbacks apply.
        self.assertFalse(data["decision_rules"])
        self.assertEqual(
            data["procedure"][0]["step"],
            "Re-read the source thread and confirm the agreed approach",
        )
        self.assertTrue(any("No explicit decision was recorded" in q for q in data["open_questions"]))

    def test_cli_rejects_an_unknown_locale(self):
        thread_id = self.ingest_chinese_thread()
        with self.assertRaises(SystemExit):
            self.run_cli("distill", thread_id, "--provider", "offline", "--locale", "klingon", "--dry-run")


if __name__ == "__main__":
    unittest.main()
