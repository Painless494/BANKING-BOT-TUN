# 26. Games: Spin of Fortune (and the framework for the next ones)

## For members
* `/games spin` or the **🎰 SPIN** button on the panel: one spin per day (staff can change that). The result is shown privately, and
  announced in the channel as staff choose. Prizes are **not** paid instantly: they wait until staff run the payout (`/games history` shows
  "waiting / paid").
* `/games prizes` shows every prize with its real odds. `/games stats`, `/games leaderboard` (this season or all time), `/games history`.
* You need a linked nation (`/nation link`): prizes are paid to it.

## For staff (all settings are also in `/bankset config` → Games)
1. `/gameset defaults` loads the classic wheel into an empty table (same odds as the original bot: 101 tickets, one jackpot).
   **Check the amounts**, then edit with `/gameset prize` (name, weight, what it pays, score, jackpot, on/off) and `/gameset removeprize`.
2. `/gameset show` shows the odds **and what the wheel costs the alliance** (average per spin and worst case, at live prices).
3. `/gameset configure open_games:True` (refuses while the prize table is empty). Also: spins per day, reset hour (UTC), a required role,
   who sees results and where, the animation, and a **daily prize budget in dollars** (once reached, only no-prize results can come up).
4. `/gameset panel` posts the panel with buttons (SPIN / My stats / Leaderboard / Prizes). It keeps working after restarts.
   If results are posted in the **same channel** as the panel, the panel is re-posted below each result so it is always the last message
   (the old copy is removed; several results at once share one move). Switch off with `/gameset configure panel_stays_last:False`.
   It only follows the bot's own results: other people's chat messages can still push it up.
5. `/gameset payouts` lists prizes waiting. `/gameset pay` pays them (see below). `/gameset cancel` cancels an unpaid prize (reason kept).
6. `/gameset newseason` restarts the leaderboard. Nothing is deleted.

## How prizes are paid (money safety)
A spin **never creates or moves money**. It records a prize: what the alliance owes. `/gameset pay` turns prizes into a normal **bulk batch**,
so every existing safeguard applies unchanged: alliance-owned funds only (never members' deposits), your limits, a second approver above the
approval threshold, a preview and a confirmation, re-checks, and at-most-once sending with `/bulk resume` if it stops. Two staff paying at the
same moment cannot pay the same prize twice. A prize shows as **paid** only when its bank transfer really completed; a failed one can be paid
again, one that is paid or in flight cannot. Prizes of frozen accounts are held back. Paid prizes appear as **Game prizes** in `/economy report`.

## What changed from the original spin bot
| Original | Here |
|---|---|
| Prizes were text, paid by hand, then "claimed" by deleting history | Prizes are real records paid through the bank's safe payment pipeline; history is never deleted |
| Prize list, odds, scores, role, channel, text all hard-coded | All configurable; odds are shown to players |
| "Jackpot" meant weight ≤ 1 | An explicit jackpot flag |
| Score guessed from the prize text | A score stored on each prize |
| `250k` was ambiguous | Read as **$250,000** (change it with `/gameset prize` if you meant otherwise) |
| Day = server's local date; a double click could spin twice | Game day is UTC (configurable reset hour); the database itself refuses a second spin |
| `Math.random` | The operating system's secure random source; the odds used are saved with every spin |
| Users identified by Discord name; anyone could spin | Needs a linked, unfrozen member; prizes go to their nation |
| `fullreset` wiped everything | `/gameset newseason`: a fresh leaderboard, nothing deleted |

## Adding the next game (e.g. a lottery)
`tunbank/games.py` is the shared part: prize tables, daily limits, recorded plays with odds, prizes owed, stats, leaderboards. A new game
needs a `GameDef` in `GAMES`, its settings in `config.py` / `config_view.py`, its own play function (using `play_daily()` or recording through
the same tables), and its commands. Prize payments, the staff prize table and `/gameset` need no changes. A lottery that sells tickets for
member balances would additionally use the existing transfer/ledger rules for the ticket price; do not build that around prizes alone.

## Limits
* Only tested with the fake Discord. Try it in a staging server first; the animation edits one message a few times.
* The spin bot's old `spins.db` is not imported (it held names, not nations).
* **Security:** the zip you gave me contains a `.env` with a live Discord bot token. Reset that token in the Discord developer portal.
