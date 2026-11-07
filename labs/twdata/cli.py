"""CLI for twdata. Run from labs/:
    python -m twdata.cli fetch daily
    python -m twdata.cli fetch revenue --year 2026 --month 9
    python -m twdata.cli fetch revenue-latest
    python -m twdata.cli fetch margin
    python -m twdata.cli fetch valuation
    python -m twdata.cli fetch institutional --date 20261030
    python -m twdata.cli fetch institutional-tpex
    python -m twdata.cli fetch quotes [--symbols 2330,5483]
    python -m twdata.cli fetch futures
CSVs are written to --out (default data_cache/twdata).
"""
import argparse
import os

from twdata import (daily_close, institutional, institutional_tpex,
                    margin_balance, monthly_revenue, realtime_futures,
                    realtime_quotes, revenue_latest, valuation)


def main():
    ap = argparse.ArgumentParser(prog="twdata")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("kind", choices=["daily", "revenue", "revenue-latest",
                                   "margin", "valuation", "institutional",
                                   "institutional-tpex", "realtime",
                                   "quotes", "futures"])
    f.add_argument("--date", default=None, help="YYYYMMDD (institutional)")
    f.add_argument("--symbols", default="",
                   help="comma-separated stock ids for quotes, e.g. 2330,5483")
    f.add_argument("--year", type=int, default=None)
    f.add_argument("--month", type=int, default=None)
    f.add_argument("--market", default="both", choices=["sii", "otc", "both"])
    f.add_argument("--out", default="data_cache/twdata")
    args = ap.parse_args()

    if args.kind == "daily":
        df, name = daily_close(), "daily"
    elif args.kind == "revenue":
        if not args.year or not args.month:
            ap.error("revenue needs --year and --month")
        df = monthly_revenue(args.year, args.month, args.market)
        name = f"revenue_{args.year}{args.month:02d}"
    elif args.kind == "revenue-latest":
        df, name = revenue_latest(), "revenue_latest"
    elif args.kind == "margin":
        df, name = margin_balance(), "margin"
    elif args.kind == "valuation":
        df, name = valuation(), "valuation"
    elif args.kind == "institutional":
        if not args.date:
            ap.error("institutional needs --date YYYYMMDD")
        df, name = institutional(args.date), f"institutional_{args.date}"
    elif args.kind == "institutional-tpex":
        df, name = institutional_tpex(), "institutional_tpex"
    elif args.kind == "realtime":
        import json as _json
        from twdata.realtime import realtime_dashboard
        print(_json.dumps(realtime_dashboard(), ensure_ascii=False, indent=1))
        return
    else:
        ap.error(f"unknown kind {args.kind}")

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, name + ".csv")
    df.to_csv(path, index=False)
    date = df["date"].iloc[0] if "date" in df.columns and len(df) else "n/a"
    print(f"[twdata] {args.kind}: {len(df)} rows, date={date} -> {path}")


if __name__ == "__main__":
    main()
