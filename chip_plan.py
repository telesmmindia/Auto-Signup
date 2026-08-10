"""Turning a rupee amount into a sequence of chip clicks.

Shared by tournament.py (knockout stakes, baccarat) and main.run_paired_hedge
(hedge stakes, Stock Market Live). It lives in its own module because
tournament.py imports main, so main cannot import tournament -- and both need
the same solver. Pure arithmetic: no Playwright, no site knowledge.

The solver is exact rather than greedy. Greedy looks fine on small stakes but
wastes badly once the click budget binds (it turned 99,900 into 92,500, a 7%
remainder). The exact answer costs a few thousand integer operations.
"""
from __future__ import annotations


def plan_stake(target, chips, table_min=0, table_max=None, max_clicks=8):
    """Largest stake reachable in <= `max_clicks` chip clicks, not exceeding
    `target` or `table_max`.

    Returns (stake, [chip values, largest first]). stake is 0 (and the plan
    empty) when nothing at or above `table_min` is reachable.

    Rounds DOWN, always. Overshooting would either be rejected by the table or
    stake money the other side can't match, and an unmatched stake is not a
    hedge. Callers that need an EXACT amount must compare `stake` to `target`
    themselves and refuse the difference (run_paired_hedge does).
    """
    budget = int(target) if table_max is None else min(int(target), int(table_max))
    if budget < table_min:
        return 0, []

    units = sorted({int(c) for c in chips if c and c > 0})
    if not units:
        return 0, []
    step = units[0]                       # smallest chip == the granularity
    if any(c % step for c in units):
        # Non-divisible rail: fall back to plain greedy rather than silently
        # mis-solving. Not expected on the rails seen so far (baccarat
        # 100/500/2500/10k/50k/100k, stock market 10/50/100/200/500/2500).
        plan, remaining = [], budget
        for chip in sorted(units, reverse=True):
            while remaining >= chip and len(plan) < max_clicks:
                plan.append(chip)
                remaining -= chip
        stake = sum(plan)
        return (stake, plan) if stake >= table_min else (0, [])

    cap = budget // step
    coins = [c // step for c in units]
    # cost[v] = fewest chips summing to exactly v (in `step` units).
    INF = max_clicks + 1
    cost = [INF] * (cap + 1)
    pick = [0] * (cap + 1)
    cost[0] = 0
    for v in range(1, cap + 1):
        for c in coins:
            if c <= v and cost[v - c] + 1 < cost[v]:
                cost[v] = cost[v - c] + 1
                pick[v] = c
    # Largest reachable total within the click budget.
    best = max((v for v in range(cap, -1, -1) if cost[v] <= max_clicks),
               default=0)

    plan, v = [], best
    while v > 0:
        plan.append(pick[v] * step)
        v -= pick[v]
    plan.sort(reverse=True)

    stake = sum(plan)
    if stake < table_min:
        return 0, []
    return stake, plan


def group_plan(plan):
    """Collapse a chip plan into [(chip, count), ...], largest chip first.

    Selecting a chip is far slower than clicking a spot (the click has to be
    verified against [data-role="selected-chip"]), so a plan of
    50000+10000+10000+10000 is placed as two selections and four clicks, not
    four selections and four clicks. Inside a ~10-15s betting window that
    difference matters."""
    grouped = []
    for chip in plan:
        if grouped and grouped[-1][0] == chip:
            grouped[-1][1] += 1
        else:
            grouped.append([chip, 1])
    return [(c, n) for c, n in grouped]
