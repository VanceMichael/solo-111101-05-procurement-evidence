import argparse
import json
from .store import RecordStore
from .capture import capture
from .trace_store import TraceStore
from .trace import run_manifest, trace_receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description='防护用品批次追溯采集')
    parser.add_argument("--database", default="collector.sqlite3")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--capture", metavar="FIXTURE_ROOT")
    parser.add_argument("--route", default='/mail/one')
    parser.add_argument("--observed-at", default="2026-11-11T10:00:00Z")
    parser.add_argument("--run", metavar="SCENARIO", dest="run_scenario")
    parser.add_argument("--output")
    parser.add_argument("--trace", metavar="RECEIPT", help="按入库单号反查验收事实与凭证链")
    args = parser.parse_args(argv)
    if args.run_scenario:
        if not args.output:
            parser.error("--run 需要同时提供 --output")
        result = run_manifest(args.run_scenario, args.database, args.output, args.observed_at)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.trace:
        result = trace_receipt(TraceStore(args.database), args.trace)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.capture:
        print(json.dumps(capture(args.capture, args.route, args.database, args.observed_at), ensure_ascii=False))
        return 0
    if args.init:
        store = RecordStore(args.database)
        store.initialize()
        print(json.dumps({"stored": store.count()}))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
