"""课案风险复核工作流。

风险判断落实到可定位的陈述及其证据状态：
- 启发式只产生提醒依据，证据状态只能由教师显式变更；
- 陈述身份由句子内容哈希决定，改写后的新句不会继承旧证据；
- 来源独立存储、按引用计数共享，撤销单项确认不会误删其他陈述的依据；
- 总览与待复核队列均从当前持久化状态实时计算，保持一致。
"""

import hashlib
from datetime import datetime, timezone

from .records import TextRecord
from .statements import extract_statements
from .text import annotate, normalize_text

_RISK_WEIGHT = {"high": 3, "medium": 2, "low": 1}
_RESOLVED_STATUSES = ("confirmed", "unverifiable")


def _now():
    return datetime.now(timezone.utc).isoformat()


class TextWorkflow:
    def __init__(self, repository):
        self.repository = repository

    # ------------------------------------------------------------------ 导入

    def ingest(self, values):
        if not isinstance(values, list):
            raise TypeError("导入内容必须是记录列表")
        incoming = [TextRecord.from_dict(value).to_dict() for value in values]
        existing = self.repository.read()
        current = {record["record_id"]: record for record in existing}
        for record in incoming:
            previous = current.get(record["record_id"])
            if previous is None or record["revision"] >= previous["revision"]:
                record["text"] = normalize_text(record["text"])
                record["analysis"] = annotate(record)
                record["statements"] = extract_statements(record)
                record["sources"] = self._carry_review_state(previous, record["statements"])
                current[record["record_id"]] = record
        result = sorted(current.values(), key=lambda row: row["record_id"])
        self.repository.replace(result)
        return {"received": len(incoming), "stored": len(result)}

    @staticmethod
    def _carry_review_state(previous, statements):
        """把内容未变陈述的复核结论与来源带到新版本。

        新抽取的陈述通过内容哈希与旧陈述匹配：句子被改写后哈希变化，
        匹配失败，旧结论与来源不会被套用到含义已变的新句上。
        来源只保留仍被陈述引用的条目，因此多个陈述共用同一来源时，
        只要还有一个陈述引用，该来源就不会被清除。
        """
        if previous is None:
            return []
        prior_statements = {
            item["statement_id"]: item for item in previous.get("statements", [])
        }
        prior_sources = {
            item["source_id"]: item for item in previous.get("sources", [])
        }
        referenced = set()
        for statement in statements:
            old = prior_statements.get(statement["statement_id"])
            if old is None:
                continue
            statement["status"] = old["status"]
            statement["source_ids"] = [
                sid for sid in old["source_ids"] if sid in prior_sources
            ]
            statement["decision_note"] = old.get("decision_note", "")
            statement["decided_at"] = old.get("decided_at")
            referenced.update(statement["source_ids"])
        return [source for source in prior_sources.values()
                if source["source_id"] in referenced]

    # ------------------------------------------------------------------ 读取

    def list_records(self, source=None):
        records = self.repository.read()
        if source:
            records = [record for record in records if record["source"] == source]
        return records

    def _load_record(self, record_id, records=None):
        for record in (records if records is not None else self.repository.read()):
            if record["record_id"] == record_id:
                return record
        raise KeyError(f"未找到课案记录：{record_id}")

    @staticmethod
    def _find_statement(record, statement_id):
        for statement in record["statements"]:
            if statement["statement_id"] == statement_id:
                return statement
        raise KeyError(f"未找到陈述：{statement_id}")

    def review_queue(self, source=None):
        """待复核队列：所有仍处于 unresolved 的风险陈述。

        教师确认或标记无法核实后，陈述立即离开队列；撤销后重新进入。
        """
        queue = []
        for record in self.list_records(source):
            for statement in record["statements"]:
                risk = statement["risk"]
                if not risk["flagged"] or statement["status"] != "unresolved":
                    continue
                queue.append(
                    {
                        "record_id": record["record_id"],
                        "source": record["source"],
                        "revision": record["revision"],
                        "statement_id": statement["statement_id"],
                        "index": statement["index"],
                        "text": statement["text"],
                        "char_start": statement["char_start"],
                        "char_end": statement["char_end"],
                        "risk_level": risk["risk_level"],
                        "confidence": risk["confidence"],
                        "rationales": risk["rationales"],
                        "source_ids": list(statement["source_ids"]),
                    }
                )
        queue.sort(
            key=lambda item: (-_RISK_WEIGHT[item["risk_level"]],
                              item["record_id"], item["index"])
        )
        return queue

    def overview(self, source=None):
        """课案总览：与待复核队列从同一份状态实时计算。"""
        records = self.list_records(source)
        items = []
        for record in records:
            counts = {"flagged": 0, "unresolved": 0, "confirmed": 0,
                      "unverifiable": 0, "risk_score": 0}
            for statement in record["statements"]:
                if not statement["risk"]["flagged"]:
                    continue
                counts["flagged"] += 1
                status = statement["status"]
                if status in counts:
                    counts[status] += 1
                if status == "unresolved":
                    counts["risk_score"] += _RISK_WEIGHT[statement["risk"]["risk_level"]]
            items.append(
                {
                    "record_id": record["record_id"],
                    "source": record["source"],
                    "revision": record["revision"],
                    "total_statements": len(record["statements"]),
                    "flagged": counts["flagged"],
                    "unresolved": counts["unresolved"],
                    "confirmed": counts["confirmed"],
                    "unverifiable": counts["unverifiable"],
                    "risk_score": counts["risk_score"],
                }
            )
        # 总览与队列读取同一份持久化状态，逐记录计数后汇总，避免双份计数漂移。
        totals = {
            "records": len(records),
            "statements": sum(item["total_statements"] for item in items),
            "flagged": sum(item["flagged"] for item in items),
            "unresolved": sum(item["unresolved"] for item in items),
            "confirmed": sum(item["confirmed"] for item in items),
            "unverifiable": sum(item["unverifiable"] for item in items),
            "risk_score": sum(item["risk_score"] for item in items),
        }
        return {"summary": totals, "records": items}

    # -------------------------------------------------------------- 来源管理

    @staticmethod
    def _source_key(description, locator):
        return normalize_text(description).lower() + "|" + normalize_text(locator).lower()

    def _source_id(self, record_id, description, locator):
        digest = hashlib.sha256(
            self._source_key(description, locator).encode("utf-8")
        ).hexdigest()[:12]
        return f"src-{record_id}-{digest}"

    def _ensure_source(self, record, description, locator=""):
        description = normalize_text(description)
        locator = normalize_text(locator)
        if not description:
            raise ValueError("来源说明不能为空")
        key = self._source_key(description, locator)
        for source in record["sources"]:
            if self._source_key(source["description"], source.get("locator", "")) == key:
                return source["source_id"]
        source_id = self._source_id(record["record_id"], description, locator)
        record["sources"].append(
            {
                "source_id": source_id,
                "description": description,
                "locator": locator,
                "added_at": _now(),
            }
        )
        return source_id

    def _prune_sources(self, record):
        """删除不再被任何陈述引用的来源；仍被引用的共享来源保留。"""
        referenced = {
            sid
            for statement in record["statements"]
            for sid in statement["source_ids"]
        }
        record["sources"] = [
            source for source in record["sources"]
            if source["source_id"] in referenced
        ]

    def _attach(self, record, statement, source_ids):
        for sid in source_ids:
            if not any(source["source_id"] == sid for source in record["sources"]):
                raise KeyError(f"未找到来源：{sid}")
            if sid not in statement["source_ids"]:
                statement["source_ids"].append(sid)

    # -------------------------------------------------------------- 教师操作

    def confirm_statement(self, record_id, statement_id, source_descriptions=None,
                          source_ids=None, note=""):
        """教师确认陈述：必须附上至少一条教材依据，模型置信度不能代替确认。"""
        records = self.repository.read()
        record = self._load_record(record_id, records)
        statement = self._find_statement(record, statement_id)
        new_ids = [
            self._ensure_source(record, item["description"], item.get("locator", ""))
            for item in (source_descriptions or [])
        ]
        self._attach(record, statement, list(source_ids or []) + new_ids)
        if not statement["source_ids"]:
            raise ValueError("确认陈述必须提供至少一条教材依据")
        statement["status"] = "confirmed"
        statement["decision_note"] = normalize_text(note)
        statement["decided_at"] = _now()
        self.repository.replace(records)
        return self._decision_view(record, statement)

    def attach_source(self, record_id, statement_id, description, locator=""):
        """为陈述补充来源；补充来源本身不改变证据状态。"""
        records = self.repository.read()
        record = self._load_record(record_id, records)
        statement = self._find_statement(record, statement_id)
        source_id = self._ensure_source(record, description, locator)
        self._attach(record, statement, [source_id])
        self.repository.replace(records)
        return self._decision_view(record, statement)

    def mark_unverifiable(self, record_id, statement_id, note=""):
        """教师标记无法核实：解除该陈述的来源关联并保留说明。"""
        records = self.repository.read()
        record = self._load_record(record_id, records)
        statement = self._find_statement(record, statement_id)
        if not normalize_text(note):
            raise ValueError("标记无法核实必须写明原因，便于后续复核")
        statement["source_ids"] = []
        statement["status"] = "unverifiable"
        statement["decision_note"] = normalize_text(note)
        statement["decided_at"] = _now()
        self._prune_sources(record)
        self.repository.replace(records)
        return self._decision_view(record, statement)

    def revoke_decision(self, record_id, statement_id):
        """撤销确认或无法核实标记，陈述重新进入待复核队列。

        只解除该陈述与来源的关联；若来源仍被同记录的其他陈述引用，
        则保留，其他陈述的依据不受影响。
        """
        records = self.repository.read()
        record = self._load_record(record_id, records)
        statement = self._find_statement(record, statement_id)
        if statement["status"] not in _RESOLVED_STATUSES:
            raise ValueError("该陈述尚无教师结论，无需撤销")
        statement["status"] = (
            "unresolved" if statement["risk"]["flagged"] else "not_applicable"
        )
        statement["source_ids"] = []
        statement["decision_note"] = ""
        statement["decided_at"] = None
        self._prune_sources(record)
        self.repository.replace(records)
        return self._decision_view(record, statement)

    @staticmethod
    def _decision_view(record, statement):
        return {
            "record_id": record["record_id"],
            "statement_id": statement["statement_id"],
            "status": statement["status"],
            "source_ids": list(statement["source_ids"]),
            "decision_note": statement["decision_note"],
            "decided_at": statement["decided_at"],
            "sources": [
                source for source in record["sources"]
                if source["source_id"] in statement["source_ids"]
            ],
        }
