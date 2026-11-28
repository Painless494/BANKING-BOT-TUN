# 24. Several alliances in one bot: how it works and how to add one

## What you get
One bot, one Railway service, one Discord application. Each alliance has **its own database file** (`data/tenants/<alliance id>/tunbank.db`),
its own settings, admins, roles, ledger, hash chains, emergency lock, backups and encrypted PnW keys. TUN is the **home** alliance and is
stored and behaves exactly as before: `data/tunbank.db`, the keys in the environment variables.

Multi-alliance mode is **off unless you switch it on**. Without `MULTI_ALLIANCE=1` the bot is the single-alliance bot it always was
(same commands, no `/platform`, no `/setup`, no `platform.db`).

## Switching it on (owner, once)
1. Make sure `GUILD_ID` (the home server) and `CREDENTIAL_ENCRYPTION_KEY` are set. Keep that key safe: **lose it and every stored alliance key
   and member key is unreadable.** Back it up separately from the database.
2. Add the variable `MULTI_ALLIANCE=1` and redeploy. On first start the bot creates `data/platform.db`, copies your existing game-rule
   corrections and project-catalogue history into it (once; nothing is removed from the home database) and syncs commands to every server.
3. Run `python scripts/check_startup.py` **before** deploying. Its second half builds the bot in this mode.

## Adding an alliance
1. The alliance invites the bot to its own Discord server (a server can belong to only one alliance).
2. In **your home server**: `/platform add alliance_id:<PnW id> guild_id:<server id> name:<short name> admins:<Discord user ids>`.
   This creates the alliance's database and registers `/setup`, `/bank`, … in its server. Nothing is running yet: it has no keys.
3. An alliance admin runs `/setup keys` in **their** server. It opens a private form (the keys are never shown in chat). The bot tests them
   against PnW (`alliances → members`), and only if PnW accepts them and returns members does it store them, **encrypted**.
4. The alliance admins then do the usual first-time set-up: `/bankset setrole`, `/bankset setlogchannel`, `/bankset config`.

Other commands (all owner-only, home server only): `/platform list`, `/platform suspend`, `/platform resume`, `/platform admins`.
Suspending stops that alliance's commands, buttons and background loops at once and deletes nothing. Registry rows can never be deleted.

## Who can do what
| | Platform owner (`OWNER_DISCORD_IDS`) | Alliance admin (listed in `/platform add` / `/platform admins`) |
|---|---|---|
| Approve, suspend, resume alliances; change admins | yes (home server) | no |
| Set the alliance's PnW keys (`/setup keys`) | no | yes, in their server |
| Run `/bankset …` and all banking in an alliance | **only in the home alliance** | yes, in their own alliance only |
| `/grantset rule`, `/grantset projectimport` | yes | no (the rules and catalogue are shared by every alliance) |

The platform owner is **not** automatically an admin of another alliance, and an admin of one alliance has no rights in another.

## What the bot refuses
Unknown servers, direct messages, suspended alliances, and alliances without keys (except `/setup`). Autocomplete is refused silently.
A button or form pressed later takes its alliance from the server it came from, never from anything remembered in the message.
Alerts for an alliance are only ever posted to channels **inside that alliance's own server**.

## Backups, verification, upgrades
* The daily backup loop writes one backup per alliance (`data/tenants/<id>/backups/`) and one of the registry (`data/platform-backups/`).
* `/bankset backup` inside an alliance backs up that alliance only.
* `python scripts/verify_all.py [data_dir]` verifies the home database, `platform.db` and every alliance file. Run it on backups, or with
  the bot stopped.
* Migrations 001–017 are shared and untouched. `018_tenants.sql` lives in `tunbank/platform_migrations/` and is applied **only** to
  `platform.db`; alliance databases and the home database never get it.
* `scripts/rehearse_upgrade.py <backup.db>` still rehearses one database file: run it once per file.

## Not built yet (be aware)
* Alliances other than home have **no offshore**: the main bank only.
* One shared PnW rate-limit budget: each alliance has its own client, loops run alliance after alliance. Fine for a few alliances; watch
  PnW errors if you add many.
* Restoring a backup into an alliance (`apply_pending_restore`) is only wired for the home database.
* Not tested against real Discord: the tests use the fake Discord. Run `scripts/check_startup.py` and a staging server before going live.
