# 27. Bank DMs to members: who gets what

Members get a DM when their account changes. You now control that in three layers. **Nothing changes until you change a setting:**
everything is on and members cannot opt out, exactly as before.

| Layer | Where | Default |
|---|---|---|
| Master switch: every bank DM | `/nation dmalerts all_dms:` (or `/bankset config` → Alerts & channels) | on |
| Per kind (alliance-wide) | `/nation dmalerts deposits: withdrawals: transfers: staff_changes:` | all on |
| Members may switch kinds off for themselves | `/nation dmalerts members_choose:True`, then members use `/nation alerts` | off |

The kinds: **Deposits** (a deposit was credited) · **Withdrawals** (money left the account: by the member, by staff, or outside TUN Bank) ·
**Transfers** (balance transfers between members) · **Staff changes** (adjustments, reserved or released funds, an overdue loan settled
from a deposit). `/nation dmalerts` with no options just shows the current state.

* A member can never mute the notice that money left their account **outside TUN Bank**; only staff can, through the Withdrawals switch.
* The decision is made when the DM is about to be sent. A DM that was skipped is **not** sent later if you switch it back on. The record
  of it stays in the notification list with the reason ("withdrawals DMs are switched off by staff (dm_withdrawals)").
* Switching DMs off never affects the money: the movement and its record happen first and separately.
* Members choose with `/nation alerts` (for example `/nation alerts withdrawals:False`); with no options it shows what they get.
* Staff-only control per member (muting one specific person) is not built.
* Direct DMs about loans or reserved funds come under "Staff changes"; DMs with no category (none exist today) follow only the master switch.
