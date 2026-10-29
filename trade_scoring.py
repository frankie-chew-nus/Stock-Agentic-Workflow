"""Deterministic BUY-signal profitability scoring, separate from classification accuracy.

Assumptions: signal at as-of close, exit at the 20th subsequent stock trading
close, equal notional per signal, no reinvestment, 0.6% round-trip cost.
Use adjusted closes for both stock and STI, matching the existing outcome
classifier. Overlapping signals are separate hypothetical positions.
"""
from __future__ import annotations

import pandas as pd


def score_buy_signals(ledger: list[dict], stock: pd.DataFrame,
                      sti: pd.DataFrame, horizon: int = 20,
                      round_trip_cost_pct: float = 0.6) -> list[dict]:
    if horizon < 1 or round_trip_cost_pct < 0:
        raise ValueError("Invalid horizon or trading cost")
    s = stock["adj_close"].sort_index()
    b = sti["adj_close"].sort_index()
    results = []
    for record in ledger:
        if record.get("action_final") != "BUY":
            continue
        as_of = pd.Timestamp(record["as_of"])
        eligible = s.index[s.index <= as_of]
        if eligible.empty:
            continue
        start = eligible[-1]
        start_pos = s.index.get_loc(start)
        if start_pos + horizon >= len(s):
            results.append({"as_of": str(as_of.date()), "complete": False})
            continue
        end = s.index[start_pos + horizon]
        if start not in b.index or end not in b.index:
            results.append({"as_of": str(as_of.date()), "complete": False,
                            "reason": "missing_benchmark_close"})
            continue
        stock_gross = (float(s.loc[end]) / float(s.loc[start]) - 1) * 100
        benchmark = (float(b.loc[end]) / float(b.loc[start]) - 1) * 100
        net = stock_gross - round_trip_cost_pct
        results.append({
            "as_of": str(as_of.date()), "entry_date": str(start.date()),
            "exit_date": str(end.date()), "complete": True,
            "stock_gross_pct": round(stock_gross, 6),
            "stock_net_pct": round(net, 6),
            "sti_pct": round(benchmark, 6),
            "excess_net_pct": round(net - benchmark, 6),
        })
    return results


def summarize_buy_signals(scored: list[dict]) -> dict:
    completed = [r for r in scored if r.get("complete")]
    if not completed:
        return {"buy_signals": len(scored), "completed": 0,
                "mean_net_pct": None, "mean_sti_pct": None,
                "mean_excess_pct": None, "win_rate": None}
    n = len(completed)
    return {
        "buy_signals": len(scored), "completed": n,
        "mean_net_pct": sum(r["stock_net_pct"] for r in completed) / n,
        "mean_sti_pct": sum(r["sti_pct"] for r in completed) / n,
        "mean_excess_pct": sum(r["excess_net_pct"] for r in completed) / n,
        "win_rate": sum(r["stock_net_pct"] > 0 for r in completed) / n,
    }
