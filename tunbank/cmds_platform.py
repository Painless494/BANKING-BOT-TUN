"""/platform (the platform owner, home server only) and /setup (an alliance's own admins, in their own server).

Neither group touches an alliance's accounting. /platform manages the registry (which alliance may use the bot, where, and who
administers it). /setup lets an alliance's admins give the bot their own PnW keys, which are tested and then stored ENCRYPTED.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands

from . import alerts as A
from .credentials import KEY_RE, CredentialError
from .pnw import PnWClient, PnWRejected, PnWUncertain
from .tenancy import TenancyError, parse_ids

log = logging.getLogger("tunbank.platform")


def make_pnw_client(settings):
    """Separate so tests can replace it. A throwaway client used only to test keys before they are stored."""
    return PnWClient(settings)


async def _say(interaction, text: str):
    if interaction.response.is_done():
        await interaction.followup.send(text[:1900], ephemeral=True)
    else:
        await interaction.response.send_message(text[:1900], ephemeral=True)


class SetupKeysModal(discord.ui.Modal):
    """Deliberately a plain Modal (not a TenantModal): the alliance has no services yet, so there is nothing to switch to.
    It checks the server and the admin itself. The keys are typed here and never echoed back."""

    def __init__(self, bot, tenant_key: str):
        super().__init__(title="Your alliance's PnW keys")
        self.bot, self.tenant_key = bot, tenant_key
        mk = lambda label, ph, req: discord.ui.TextInput(label=label, placeholder=ph, required=req, max_length=64,   # noqa: E731
                                                         style=discord.TextStyle.short)
        self.add_item(mk("Alliance API key", "A key from your PnW account page", True))
        self.add_item(mk("Bank bot key", "The bot key of your banking nation", True))
        self.add_item(mk("API key of that banking nation", "Leave empty if it is the same as the first key", False))

    async def on_submit(self, interaction):
        tenants = self.bot.tenants
        t = tenants.for_guild(getattr(interaction, "guild_id", None))
        if t is None or t.is_home or t.key != self.tenant_key or t.status != "ACTIVE" or interaction.user.id not in t.admin_ids:
            return await _say(interaction, "You are not allowed to set this alliance's keys here.")
        api, bot_key, bot_api = (str(c.value or "").strip() for c in self.children)
        bot_api = bot_api or api
        for label, v in (("Alliance API key", api), ("Bank bot key", bot_key), ("API key of the banking nation", bot_api)):
            if not KEY_RE.match(v):
                return await _say(interaction, f"**{label}** does not look like a PnW key (letters and digits only). Nothing was saved.")
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        keys = {"api_key": api, "bot_key": bot_key, "bot_key_api_key": bot_api}
        import dataclasses
        trial = dataclasses.replace(t.settings, pnw_api_key=api, pnw_bot_key=bot_key, pnw_bot_key_api_key=bot_api)
        from .credentials import register_secret
        for v in keys.values():
            register_secret(v)
        client = make_pnw_client(trial)
        try:
            members = await client.fetch_alliance_members()
        except (PnWRejected, PnWUncertain) as exc:
            return await _say(interaction, f"PnW did not accept those keys for alliance {t.alliance_id}, so nothing was saved: {str(exc)[:300]}")
        except Exception:  # noqa: BLE001
            log.exception("key test failed for alliance %s", t.alliance_id)
            return await _say(interaction, "The keys could not be tested right now. Nothing was saved. Try again in a minute.")
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        if not members:
            return await _say(interaction, f"PnW returned no members for alliance {t.alliance_id}. Check the alliance ID with the platform owner. Nothing was saved.")
        try:
            tenants.platform.save_keys(t.alliance_id, keys, f"{interaction.user.id}")
        except (CredentialError, TenancyError) as exc:
            return await _say(interaction, str(exc))
        row = tenants.platform.by_alliance(t.alliance_id)
        tenants.load_row(row)
        await self.bot.sync_alliance_servers(t.guild_id)
        await _say(interaction, f"Keys accepted ({len(members)} members found) and stored encrypted. Your bank is now active. "
                                "Next: `/bankset setrole` to map your staff roles, then `/bankset setlogchannel`, then `/bankset config`.")


def register(platform_grp, setup_grp, bot):
    tenants, platform = bot.tenants, bot.platform
    home_guild = bot.settings.guild_id

    async def owner_here(interaction) -> bool:
        """/platform commands: the platform owner, in the home server, nobody else."""
        if getattr(interaction, "guild_id", None) != home_guild:
            await _say(interaction, "Platform commands only work in the home server.")
            return False
        if not platform.is_owner(interaction.user.id):
            await _say(interaction, "Only the platform owner can do that.")
            return False
        return True

    def card_line(r: dict) -> str:
        t = tenants.for_alliance(r["alliance_id"])
        keys = "keys set" if r["keys_enc"] else "no keys yet"
        live = "running" if t is not None and t.ready else "not running"
        return f"**{r['name']}** · alliance `{r['alliance_id']}` · server `{r['guild_id']}` · {r['status']} · {keys} · {live} · {len(parse_ids(r['admin_ids']))} admin(s)"

    @platform_grp.command(name="add", description="Owner: approve a new alliance and the Discord server it will use")
    @app_commands.describe(alliance_id="The Politics & War alliance ID", guild_id="The alliance's Discord server ID (the bot must be invited there)",
                           name="A short name for the alliance", admins="Discord user IDs of its admins, comma separated")
    async def add(interaction: discord.Interaction, alliance_id: int, guild_id: str, name: str, admins: str):
        if not await owner_here(interaction):
            return
        try:
            if not guild_id.strip().isdigit():
                raise TenancyError("The server ID must be a number.")
            ids = parse_ids(admins)
            if not ids:
                raise TenancyError("Give at least one admin's Discord user ID: they will set up the alliance's keys.")
            row = platform.add(alliance_id=alliance_id, guild_id=int(guild_id), name=name, admin_ids=ids, actor=str(interaction.user.id))
            tenants.load_row(row)
        except TenancyError as exc:
            return await _say(interaction, str(exc))
        await bot.sync_alliance_servers(int(guild_id))
        await _say(interaction, f"Approved **{row['name']}** (alliance {alliance_id}). Its admins can now run `/setup keys` in server {guild_id}. "
                                f"Its database was created at `data/tenants/{alliance_id}/`.")

    @platform_grp.command(name="list", description="Owner: every alliance, its status and whether it is running")
    async def list_(interaction: discord.Interaction):
        if not await owner_here(interaction):
            return
        rows = platform.rows()
        c = A.Card("🏛️ Alliances", "\n".join(card_line(r) for r in rows) or "None yet. Add one with `/platform add`.", A.BLUE)
        c.add("Home", f"alliance `{bot.settings.alliance_id}` (TUN) · server `{home_guild}`")
        await interaction.response.send_message(embed=A.to_embed(c), ephemeral=True)

    async def change_status(interaction, alliance_id: int, status: str):
        if not await owner_here(interaction):
            return
        try:
            row = platform.set_status(alliance_id, status, str(interaction.user.id))
            tenants.load_row(row)
        except TenancyError as exc:
            return await _say(interaction, str(exc))
        await _say(interaction, f"Alliance {alliance_id} is now **{status}**. " + (
            "Its commands, buttons and background work stop immediately. Nothing is deleted." if status == "SUSPENDED" else "It is back in service."))

    @platform_grp.command(name="suspend", description="Owner: stop an alliance's bank immediately (nothing is deleted)")
    async def suspend(interaction: discord.Interaction, alliance_id: int):
        await change_status(interaction, alliance_id, "SUSPENDED")

    @platform_grp.command(name="resume", description="Owner: put a suspended alliance back in service")
    async def resume(interaction: discord.Interaction, alliance_id: int):
        await change_status(interaction, alliance_id, "ACTIVE")

    @platform_grp.command(name="admins", description="Owner: replace the list of Discord users who administer an alliance")
    @app_commands.describe(alliance_id="The alliance", admins="Discord user IDs, comma separated (replaces the current list)")
    async def admins_cmd(interaction: discord.Interaction, alliance_id: int, admins: str):
        if not await owner_here(interaction):
            return
        try:
            ids = parse_ids(admins)
            if not ids:
                raise TenancyError("An alliance needs at least one admin.")
            row = platform.set_admins(alliance_id, ids, str(interaction.user.id))
            tenants.load_row(row)
        except TenancyError as exc:
            return await _say(interaction, str(exc))
        await _say(interaction, f"Alliance {alliance_id} now has {len(ids)} admin(s).")

    # ------------------------------------------------------------------------------------------------ /setup
    def my_alliance(interaction):
        return tenants.for_guild(getattr(interaction, "guild_id", None))

    @setup_grp.command(name="keys", description="Alliance admin: give the bot your alliance's PnW keys (typed in a private form)")
    async def keys(interaction: discord.Interaction):
        t = my_alliance(interaction)
        if t is None or t.is_home:
            return await _say(interaction, "The home alliance's keys are set in the bot's environment, not here." if t is not None
                              else "This server is not registered.")
        if interaction.user.id not in t.admin_ids:
            return await _say(interaction, "Only this alliance's admins (set by the platform owner) can set its keys.")
        await interaction.response.send_modal(SetupKeysModal(bot, t.key))

    @setup_grp.command(name="status", description="Alliance admin: is your alliance's bank set up?")
    async def status(interaction: discord.Interaction):
        t = my_alliance(interaction)
        if t is None:
            return await _say(interaction, "This server is not registered.")
        if interaction.user.id not in t.admin_ids:
            return await _say(interaction, "Only this alliance's admins can see its setup.")
        c = A.Card("🔧 Setup", f"{t.name} · alliance `{t.alliance_id}`", A.BLUE)
        c.add("Status", t.status, True)
        c.add("PnW keys", "stored (encrypted)" if t.has_keys else "**not set yet** — run `/setup keys`", True)
        c.add("Bank", "running" if t.ready else "not running", True)
        await interaction.response.send_message(embed=A.to_embed(c), ephemeral=True)
