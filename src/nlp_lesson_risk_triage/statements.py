"""陈述切分与风险启发。

本模块只负责从课案文本中切出可定位的陈述，并给出*提醒依据*。
启发式结果（风险等级、置信度、触发理由）不是事实证明：陈述的证据
状态只能由教师在工作流中显式变更。
"""

import hashlib
import re

from .text import normalize_text, tokenize

# 句末标点（中英文）作为切分边界；引号、括号等不作为边界。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;]+|\.(?=\s|$)")

# 风险触发器：每条规则给出触发理由，命中越多风险越高。
_RISK_RULES = (
    (re.compile(r"\d"), "包含数字或统计表述，需要教材数据佐证"),
    (re.compile(r"\d{4}\s*年|公元前|世纪|年代"), "包含年代表述，需要教材年表佐证"),
    (re.compile(r"唯一|最|第一|首个|所有|全部|任何|绝不|总是|必然|百分之百"), "使用绝对化表述，需要权威依据界定适用范围"),
    (re.compile(r"[“\"《].+?[”\"》]"), "引用作品或他者表述，需要注明出处"),
    (re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|倍|米|千米|公里|千克|吨|平方公里|摄氏度|度)"), "包含量化度量，需要数据来源"),
    (re.compile(r"研究表明|实验证明|据记载|据统计|据悉|史料显示|调查发现"), "转述研究或史料结论，需要原始来源"),
)

_LEVEL_BY_HITS = {1: "low", 2: "medium"}


def split_sentences(text):
    """把规范化文本切分为 (句子, 起始偏移, 结束偏移)，偏移基于规范化文本。

    分隔符附着在前一句上，保证片段拼接后可以还原原文。
    """
    normalized = normalize_text(text)
    sentences = []
    start = 0
    for match in _SENTENCE_SPLIT_RE.finditer(normalized):
        end = match.end()
        body = normalized[start:end].strip()
        if body:
            offset = normalized.find(body, start)
            sentences.append((body, offset, offset + len(body)))
        start = end
    tail = normalized[start:].strip()
    if tail:
        offset = normalized.find(tail, start)
        sentences.append((tail, offset, offset + len(tail)))
    return sentences


def statement_id(record_id, sentence_text):
    """陈述身份由记录身份与句子内容共同决定。

    句子被改写后内容哈希变化，身份随之变化，旧复核记录无法被套用到
    含义已经变化的新句子上；仅移动位置的句子内容不变，身份保持稳定。
    """
    digest = hashlib.sha256(sentence_text.encode("utf-8")).hexdigest()[:16]
    return f"st-{record_id}-{digest}"


def evaluate_risk(sentence_text):
    """返回风险启发结果。

    命中任何规则即标记为待复核陈述。置信度只是模型侧的提醒强度，
    不构成事实证明，也不会自动解除任何提醒。
    """
    rationales = []
    for pattern, reason in _RISK_RULES:
        if pattern.search(sentence_text):
            rationales.append(reason)
    if not rationales:
        return {
            "flagged": False,
            "risk_level": "none",
            "confidence": 0.0,
            "rationales": [],
        }
    hits = len(rationales)
    level = _LEVEL_BY_HITS.get(hits, "high")
    # 命中一条 0.5，之后每条增加 0.15，封顶 0.95；永远不到 1.0，
    # 因为启发式置信度不能等同于已证实。
    confidence = min(0.5 + 0.15 * (hits - 1), 0.95)
    return {
        "flagged": True,
        "risk_level": level,
        "confidence": round(confidence, 2),
        "rationales": rationales,
    }


def extract_statements(record):
    """从一条课案记录中抽出带定位信息的陈述列表。"""
    statements = []
    for index, (sentence, start, end) in enumerate(split_sentences(record["text"])):
        risk = evaluate_risk(sentence)
        statements.append(
            {
                "statement_id": statement_id(record["record_id"], sentence),
                "index": index,
                "text": sentence,
                "char_start": start,
                "char_end": end,
                "tokens": tokenize(sentence),
                "risk": risk,
                # 新陈述一律未解决；模型置信度不充当证据。
                "status": "unresolved" if risk["flagged"] else "not_applicable",
                "source_ids": [],
                "decision_note": "",
                "decided_at": None,
            }
        )
    return statements
