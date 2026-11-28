"""Base classes for every button view and modal: they put the right alliance in context before ANY callback runs.

A button pressed hours after a command was typed runs in a fresh task with no alliance in context. Without this, it would
either fail or (worse) act on the wrong alliance's data. `interaction_check` runs before the callback, in the same task, and
decides the alliance from the server the press came from, never from anything stored in the view.
"""
from __future__ import annotations

import logging

import discord

from . import tenantctx

log = logging.getLogger("tunbank.tenantui")


async def enter_alliance(interaction) -> bool:
    """Set the current alliance from the interaction's server. False (after telling the person) when it may not proceed."""
    resolve = tenantctx.resolver()
    if resolve is None:
        return True
    from .tenancy import Refusal
    try:
        tenant = resolve(interaction)
    except Refusal as exc:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(str(exc), ephemeral=True)
            else:
                await interaction.response.send_message(str(exc), ephemeral=True)
        except discord.HTTPException:
            pass
        return False
    tenantctx.set_current(tenant.key)
    return True


class TenantView(discord.ui.View):
    async def interaction_check(self, interaction) -> bool:
        return await enter_alliance(interaction)


class TenantModal(discord.ui.Modal):
    async def interaction_check(self, interaction) -> bool:
        return await enter_alliance(interaction)
