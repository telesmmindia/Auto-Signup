"""Refresh column D (BALANCE) on a tournament sheet from the live site.

Read-only against the casino -- one HTTP login + getBalance per account, the
same call diagnose_account() uses, no browser and no bets. A failed read is
LEFT ALONE rather than written as blank or zero, same rule balance_checker
follows: an error must never wipe a known-good figure.
"""
import os, sys, time
_env = sys.argv[sys.argv.index("--env") + 1] if "--env" in sys.argv else None
import main as m  # noqa
if _env:
    from dotenv import load_dotenv
    load_dotenv(_env, override=True)
import tournament_runner as R  # noqa

SPACING = float(os.environ.get("REFRESH_SPACING", "5"))
site = os.environ.get("BOT_SITE_URL") or m.SITE_URL
proxies = R.current_proxies()

ws = R.open_worksheet()
roster = R.roster_from_sheet(ws)
print(f"site {site}: reading {len(roster)} balances, {SPACING:.0f}s apart\n")

results = []
for i, r in enumerate(roster):
    if i:
        time.sleep(SPACING)
    proxy = proxies[i % len(proxies)]
    try:
        res = m.http_check_account_balance(r["username"], r["password"],
                                           site_url=site, proxy=proxy)
    except Exception as exc:
        res = {"ok": False, "messages": [str(exc)[:80]]}
    bal = res.get("balance") if res.get("ok") else None
    why = ("blocked at the edge -- not a real answer"
           if res.get("infra_block") else
           "; ".join(str(x) for x in (res.get("messages") or []))[:60])
    results.append((r, bal, why))
    print(f"   {r['username']:24s} {'' if bal is None else bal:>10}"
          f"  {'' if bal is not None else why}")

wrote = 0
for r, bal, _why in results:
    if bal is None or not r.get("_row"):
        continue
    try:
        ws.update(range_name=f"D{r['_row']}", values=[[bal]])
        wrote += 1
    except Exception as exc:
        print(f"   (could not write row {r['_row']}: {exc})")

print(f"\nwrote {wrote} balance(s); "
      f"{sum(1 for _r, b, _w in results if b is None)} left untouched")
funded = sorted([(b, r["username"]) for r, b, _w in results if b],
                reverse=True)
if funded:
    total = sum(b for b, _u in funded)
    print(f"\nholding money ({total:,} in total):")
    for b, u in funded:
        print(f"   {u:24s} {b:>10,}  ({100.0 * b / total:.0f}% of the pot)")
