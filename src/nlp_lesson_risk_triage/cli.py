import argparse
import json
from pathlib import Path

from .repository import RecordRepository
from .workflow import TextWorkflow


def _source_list(raw):
    """解析 --source 描述:定位 参数，可重复传入。"""
    if not raw:
        return []
    result = []
    for item in raw:
        description, sep, locator = item.partition("|")
        if not description.strip():
            raise argparse.ArgumentTypeError("来源说明不能为空")
        result.append(
            {"description": description.strip(),
             "locator": locator.strip() if sep else ""}
        )
    return result


def build_parser():
    parser = argparse.ArgumentParser(description="本地教研文本风险复核工具")
    parser.add_argument("action",
                        choices=("ingest", "list", "queue", "overview",
                                 "confirm", "attach", "unverify", "revoke"))
    parser.add_argument("path", nargs="?", default="fixtures/records.json")
    parser.add_argument("--state", default="var/records.json")
    parser.add_argument("--source-filter", dest="source_filter", default=None,
                        help="按课案来源过滤 list/queue/overview")
    parser.add_argument("--record", help="课案记录 ID（复核操作必填）")
    parser.add_argument("--statement", help="陈述 ID（复核操作必填）")
    parser.add_argument("--source", action="append", default=[],
                        help="教材依据，格式为 说明|定位（页码/章节），可重复传入")
    parser.add_argument("--note", default="", help="教师复核说明")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    workflow = TextWorkflow(RecordRepository(args.state))
    if args.action == "ingest":
        result = workflow.ingest(
            json.loads(Path(args.path).read_text(encoding="utf-8"))
        )
    elif args.action == "list":
        result = workflow.list_records(args.source_filter)
    elif args.action == "queue":
        result = workflow.review_queue(args.source_filter)
    elif args.action == "overview":
        result = workflow.overview(args.source_filter)
    else:
        if not args.record or not args.statement:
            parser.error("复核操作需要 --record 与 --statement")
        if args.action == "confirm":
            result = workflow.confirm_statement(
                args.record, args.statement,
                source_descriptions=_source_list(args.source), note=args.note)
        elif args.action == "attach":
            sources = _source_list(args.source)
            if len(sources) != 1:
                parser.error("attach 需要且仅需要一个 --source")
            result = workflow.attach_source(
                args.record, args.statement,
                sources[0]["description"], sources[0]["locator"])
        elif args.action == "unverify":
            result = workflow.mark_unverifiable(
                args.record, args.statement, note=args.note)
        else:
            result = workflow.revoke_decision(args.record, args.statement)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
