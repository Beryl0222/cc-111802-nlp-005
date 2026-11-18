import tempfile
import unittest
from pathlib import Path

from nlp_lesson_risk_triage.repository import RecordRepository
from nlp_lesson_risk_triage.workflow import TextWorkflow


def make_workflow():
    directory = tempfile.TemporaryDirectory()
    repository = RecordRepository(Path(directory.name) / "records.json")
    return TextWorkflow(repository), directory


def record(record_id, revision, text):
    return {
        "record_id": record_id,
        "source": "school-synthetic",
        "revision": revision,
        "observed_at": "2026-11-18T09:00:00Z",
        "text": text,
        "context": "教研演练",
    }


RISK_TEXT = "据统计，本课共出现12个比喻句。这是普通的课堂提示。所有学生都应完成预习单。"


class StatementExtractionTests(unittest.TestCase):
    def setUp(self):
        self.workflow, self.directory = make_workflow()

    def tearDown(self):
        self.directory.cleanup()

    def test_statements_are_locatable_and_reconstruct_text(self):
        self.workflow.ingest([record("r1", 1, RISK_TEXT)])
        stored = self.workflow.list_records()[0]
        statements = stored["statements"]
        self.assertEqual(len(statements), 3)
        # 偏移可以在原文中精确定位并还原全文。
        for statement in statements:
            self.assertEqual(
                stored["text"][statement["char_start"]:statement["char_end"]],
                statement["text"],
            )
        self.assertEqual(
            "".join(item["text"] for item in statements), stored["text"]
        )
        self.assertEqual([item["index"] for item in statements], [0, 1, 2])

    def test_heuristic_flags_risky_statements_and_keeps_others_clean(self):
        self.workflow.ingest([record("r1", 1, RISK_TEXT)])
        statements = self.workflow.list_records()[0]["statements"]
        flagged = [s for s in statements if s["risk"]["flagged"]]
        self.assertEqual([s["index"] for s in flagged], [0, 2])
        self.assertFalse(statements[1]["risk"]["flagged"])
        self.assertEqual(statements[1]["status"], "not_applicable")
        # 提醒依据对教师可见，置信度严格小于 1，且不构成事实证明。
        first = statements[0]
        self.assertTrue(first["risk"]["rationales"])
        self.assertLess(first["risk"]["confidence"], 1.0)
        # 无论置信度多高，新陈述一律未解决，必须人工复核。
        self.assertEqual(first["status"], "unresolved")
        self.assertIn(first, [s for s in statements if s["status"] == "unresolved"])


class ReviewActionTests(unittest.TestCase):
    def setUp(self):
        self.workflow, self.directory = make_workflow()
        self.workflow.ingest([record("r1", 1, RISK_TEXT)])
        self.statements = self.workflow.list_records()[0]["statements"]
        self.s0 = self.statements[0]["statement_id"]
        self.s2 = self.statements[2]["statement_id"]

    def tearDown(self):
        self.directory.cleanup()

    def test_queue_lists_only_unresolved_with_rationale_visible(self):
        queue = self.workflow.review_queue()
        # 高风险（命中规则更多）的陈述排在队列前面。
        self.assertEqual([item["statement_id"] for item in queue], [self.s0, self.s2])
        item = queue[0]
        self.assertEqual(item["record_id"], "r1")
        self.assertTrue(item["rationales"])
        self.assertIn("confidence", item)
        self.assertEqual(item["source_ids"], [])

    def test_confirm_requires_source_and_updates_queue_and_overview_together(self):
        # 模型置信度不能代替证据：没有来源不允许确认。
        with self.assertRaises(ValueError):
            self.workflow.confirm_statement("r1", self.s0)
        decision = self.workflow.confirm_statement(
            "r1", self.s0,
            source_descriptions=[{"description": "七年级上册教学参考", "locator": "第12页"}],
            note="已核对",
        )
        self.assertEqual(decision["status"], "confirmed")
        self.assertEqual(len(decision["sources"]), 1)
        # 队列与总览来自同一份状态，保持一致。
        queue_ids = [item["statement_id"] for item in self.workflow.review_queue()]
        self.assertNotIn(self.s0, queue_ids)
        overview = self.workflow.overview()
        record_view = next(item for item in overview["records"]
                           if item["record_id"] == "r1")
        self.assertEqual(record_view["confirmed"], 1)
        self.assertEqual(record_view["unresolved"], 1)
        self.assertEqual(overview["summary"]["unresolved"], 1)

    def test_attach_source_does_not_change_status(self):
        decision = self.workflow.attach_source(
            "r1", self.s0, "课程标准2022版", "第8页")
        self.assertEqual(decision["status"], "unresolved")
        self.assertEqual(len(decision["sources"]), 1)
        self.assertIn(self.s0,
                      [item["statement_id"] for item in self.workflow.review_queue()])

    def test_mark_unverifiable_requires_reason_and_leaves_queue(self):
        with self.assertRaises(ValueError):
            self.workflow.mark_unverifiable("r1", self.s0)
        self.workflow.mark_unverifiable("r1", self.s0, note="教材无对应数据")
        self.assertNotIn(self.s0,
                         [item["statement_id"] for item in self.workflow.review_queue()])
        overview = self.workflow.overview()
        self.assertEqual(overview["summary"]["unverifiable"], 1)
        self.assertEqual(overview["summary"]["unresolved"], 1)
        stored = self.workflow.list_records()[0]
        statement = next(s for s in stored["statements"]
                         if s["statement_id"] == self.s0)
        self.assertEqual(statement["source_ids"], [])

    def test_revoke_on_unresolved_is_rejected(self):
        with self.assertRaises(ValueError):
            self.workflow.revoke_decision("r1", self.s0)


class SharedSourceTests(unittest.TestCase):
    def setUp(self):
        self.workflow, self.directory = make_workflow()
        self.workflow.ingest([record("r1", 1, RISK_TEXT)])
        ids = [s["statement_id"] for s in self.workflow.list_records()[0]["statements"]]
        self.s0, self.s2 = ids[0], ids[2]

    def tearDown(self):
        self.directory.cleanup()

    def test_shared_source_is_deduplicated_and_revoke_keeps_other_evidence(self):
        common = {"description": "七年级上册教学参考", "locator": "第12页"}
        self.workflow.confirm_statement("r1", self.s0, source_descriptions=[common])
        self.workflow.confirm_statement("r1", self.s2, source_descriptions=[common])
        stored = self.workflow.list_records()[0]
        # 同一来源只存一份，两个陈述共用其 ID。
        self.assertEqual(len(stored["sources"]), 1)
        shared_id = stored["sources"][0]["source_id"]
        for statement in stored["statements"]:
            if statement["statement_id"] in (self.s0, self.s2):
                self.assertEqual(statement["source_ids"], [shared_id])

        # 撤销其中一项确认：来源仍被另一陈述引用，不得误删。
        self.workflow.revoke_decision("r1", self.s0)
        stored = self.workflow.list_records()[0]
        self.assertEqual([source["source_id"] for source in stored["sources"]],
                         [shared_id])
        other = next(s for s in stored["statements"]
                     if s["statement_id"] == self.s2)
        self.assertEqual(other["source_ids"], [shared_id])
        self.assertEqual(other["status"], "confirmed")
        # 被撤销的陈述回到待复核队列。
        queue_ids = [item["statement_id"] for item in self.workflow.review_queue()]
        self.assertEqual(queue_ids, [self.s0])

        # 最后一个引用解除后，来源才被清除。
        self.workflow.revoke_decision("r1", self.s2)
        self.assertEqual(self.workflow.list_records()[0]["sources"], [])
        self.assertEqual(
            {item["statement_id"] for item in self.workflow.review_queue()},
            {self.s0, self.s2},
        )

    def test_distinct_sources_are_kept_separately(self):
        self.workflow.confirm_statement(
            "r1", self.s0,
            source_descriptions=[{"description": "教学参考", "locator": "第12页"}])
        self.workflow.confirm_statement(
            "r1", self.s2,
            source_descriptions=[{"description": "课程标准", "locator": "第8页"}])
        self.assertEqual(len(self.workflow.list_records()[0]["sources"]), 2)


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.workflow, self.directory = make_workflow()

    def tearDown(self):
        self.directory.cleanup()

    def test_rewritten_sentence_does_not_inherit_old_evidence(self):
        self.workflow.ingest([record("r1", 1, RISK_TEXT)])
        original = self.workflow.list_records()[0]["statements"]
        target = original[0]["statement_id"]
        self.workflow.confirm_statement(
            "r1", target,
            source_descriptions=[{"description": "教学参考", "locator": "第12页"}])

        # 句子被改写：数字与含义都变了，内容哈希变化，应作为全新未解决陈述。
        rewritten = "据统计，本课共出现30个比喻句。这是普通的课堂提示。所有学生都应完成预习单。"
        self.workflow.ingest([record("r1", 2, rewritten)])
        stored = self.workflow.list_records()[0]
        new_first = stored["statements"][0]
        self.assertNotEqual(new_first["statement_id"], target)
        self.assertEqual(new_first["status"], "unresolved")
        self.assertEqual(new_first["source_ids"], [])
        # 旧证据成为无主来源，不得套给新句，随版本迁移被清除。
        self.assertEqual(stored["sources"], [])
        self.assertIn(new_first["statement_id"],
                      [item["statement_id"] for item in self.workflow.review_queue()])

    def test_moved_but_unchanged_sentence_keeps_evidence(self):
        text_a = "据统计，本课共出现12个比喻句。所有学生都应完成预习单。"
        text_b = "所有学生都应完成预习单。据统计，本课共出现12个比喻句。"
        self.workflow.ingest([record("r1", 1, text_a)])
        first_id = self.workflow.list_records()[0]["statements"][0]["statement_id"]
        self.workflow.confirm_statement(
            "r1", first_id,
            source_descriptions=[{"description": "教学参考", "locator": "第12页"}])

        self.workflow.ingest([record("r1", 2, text_b)])
        stored = self.workflow.list_records()[0]
        moved = next(s for s in stored["statements"]
                     if s["statement_id"] == first_id)
        # 内容未变，身份不变，结论与来源随句子迁移到新位置。
        self.assertEqual(moved["index"], 1)
        self.assertEqual(moved["status"], "confirmed")
        self.assertEqual(len(stored["sources"]), 1)
        self.assertEqual(
            stored["text"][moved["char_start"]:moved["char_end"]], moved["text"])


class PersistenceTests(unittest.TestCase):
    def test_decisions_survive_repository_reload(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "records.json"
        workflow = TextWorkflow(RecordRepository(path))
        workflow.ingest([record("r1", 1, RISK_TEXT)])
        sid = workflow.list_records()[0]["statements"][0]["statement_id"]
        workflow.confirm_statement(
            "r1", sid,
            source_descriptions=[{"description": "教学参考", "locator": "第12页"}])
        reloaded = TextWorkflow(RecordRepository(path))
        self.assertEqual(reloaded.overview()["summary"]["confirmed"], 1)
        stored = reloaded.list_records()[0]
        self.assertEqual(stored["statements"][0]["status"], "confirmed")
        directory.cleanup()


if __name__ == "__main__":
    unittest.main()
