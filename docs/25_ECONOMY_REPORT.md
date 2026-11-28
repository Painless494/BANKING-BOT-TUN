# 25. Economy report (revenue and expenditure)

`/economy report` shows what the **alliance** earned and spent in a period. `/economy auto` posts it for you every week or month.

## What counts
| | Counted? |
|---|---|
| Tax collected every turn (the same records the per-turn tax summary adds up) | **Revenue** |
| Alliance donations (`#ignore`) | Revenue **only if you switch it on** (`/economy auto donations:On`, or the `donations` option of one report) |
| Members' deposits | Never revenue |
| Loan repayments | Never revenue (listed under "Not counted") |
| Grants, raws, bulk payments, any payment staff sent from **alliance funds** | **Expenditure**, grouped by what it was |
| Money sent outside TUN Bank that staff reviewed as alliance money (`/bank review` → alliance) | Expenditure |
| Members' withdrawals (through TUN Bank, or made outside it and charged to the member) | Never expenditure |
| Moves between our own banks (main ↔ offshore) | Never expenditure |
| Money that left the bank and nobody has classified yet | **Not counted. Shown as "Needs review".** Use `/bank review`, then run the report again |

The report never guesses. Cash is exact; resources are valued at current market prices, like the other TUN Bank reports. It also shows the
previous period (the month before for a month, the same number of days before otherwise) so you can see the trend.

## Using it
* `/economy report` – pick **last 7 / 30 days**, **this / last week** (Monday to Sunday, UTC), **this / last month**, or **custom** (`start:2026-10-01 end:2026-10-31`).
  `donations:` overrides the saved donation setting for that one report. `detail:True` attaches a spreadsheet file with every line.
  Needs the `bank_view_alliance_holdings` permission (Admins have it).
* `/economy auto` – shows the schedule. Change it with `weekly`, `monthly`, `donations`, `channel` (needs MINISTER).
  * Weekly: every Monday, the week that just ended. Monthly: on the 1st, the month that just ended. Posted one hour after the period ends.
  * Switching a schedule on **never posts an old report**: the first one is the next period to finish. Each period is posted once.
  * Posts to the channel you choose, else the ECON log. With no channel nothing is posted and nothing is lost.
* The same settings are in `/bankset config` under *Alerts & channels*.

## Colour trade bloc bonus: is it in the turn report?
The bot has **no colour-bloc logic anywhere**. The per-turn "Tax Collection — Turn Complete" message is the sum of the tax records PnW gives
the bot (`taxrecs`), and nothing else. So the bonus appears in it only if PnW itself includes it inside those tax records. A bonus that PnW
pays to nations directly is the nations' income, never reaches the alliance bank and is not in the turn report or this report.
I could not confirm from here how PnW treats it. **To check:** compare one turn's total in the bot with the alliance's own tax records in
the game for the same turn; if they match to the cent, the bonus is (or is not) in PnW's figures and the bot reports exactly what PnW does.

## Limits
* Only data the bot has recorded: if a period starts before the bot's first record, the report says so.
* The shared offshore: payouts there that belong to other alliances' shares may show up under "Needs review" until classified.
* Not tested against real Discord or live PnW data.
