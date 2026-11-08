"""User-facing memory commands: inspect, disable and forget."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from discord_bot.utils import split_message

if TYPE_CHECKING:
    from discord_bot.services import Services

logger = logging.getLogger(__name__)

MAX_FACTS_SHOWN = 30


class MemoryCog(commands.Cog, name="memory"):
    def __init__(self, bot: commands.Bot, services: Services) -> None:
        self.bot = bot
        self.services = services

    async def _dm_guard(self, ctx: commands.Context) -> bool:
        if not isinstance(ctx.channel, discord.DMChannel):
            await ctx.send("Память доступна только в личных сообщениях с ботом.")
            return False
        return True

    async def _send_text(self, ctx: commands.Context, text: str) -> None:
        for chunk in split_message(text, max_len=1900):
            await ctx.send(chunk)

    @commands.command(name="memory")
    async def memory_cmd(
        self,
        ctx: commands.Context,
        *,
        args: str | None = None,
    ) -> None:
        if not await self._dm_guard(ctx):
            return

        parts = (args or "").strip().split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        user_id = str(ctx.author.id)

        if action in {"on", "off"}:
            enabled = action == "on"
            ok = await self.services.memory.store.set_user_memory(user_id, enabled)
            if not ok:
                return await ctx.send("Не удалось сохранить настройку, попробуй позже.")
            if enabled:
                return await ctx.send("Память включена. Буду запоминать важное.")
            return await ctx.send(
                "Память выключена. Новые сообщения не сохраняются, "
                "старые можно удалить командой `!forget all`."
            )

        if action in {"forget", "delete"}:
            target = parts[1].strip() if len(parts) > 1 else ""
            return await self._forget(ctx, target)

        await self._show(ctx, user_id)

    @commands.command(name="forget")
    async def forget_cmd(
        self,
        ctx: commands.Context,
        *,
        target: str | None = None,
    ) -> None:
        if not await self._dm_guard(ctx):
            return
        await self._forget(ctx, (target or "").strip())

    async def _show(self, ctx: commands.Context, user_id: str) -> None:
        store = self.services.memory.store
        enabled = await store.user_memory_enabled(user_id)
        persona = await store.get_persona(user_id)
        facts = await store.list_facts(
            user_id,
            include_private=True,
            guild_id=None,
        )

        lines = [
            f"**Память:** {'включена' if enabled else 'выключена'}",
            f"**Фактов:** {len(facts)}",
        ]

        persona_lines: list[str] = []
        raw_persona = persona.get("persona")
        if isinstance(raw_persona, dict):
            for key, value in raw_persona.items():
                text = str(value).strip()
                if text:
                    persona_lines.append(f"- {key}: {text}")
        hint = str(persona.get("prompt_hint") or "").strip()
        if hint:
            persona_lines.append(f"- заметка: {hint}")
        if persona_lines:
            lines.append("")
            lines.append("**Стиль общения:**")
            lines.extend(persona_lines)

        if facts:
            lines.append("")
            lines.append("**Что помню:**")
            for fact in facts[:MAX_FACTS_SHOWN]:
                short = (fact.id or "")[:8]
                lines.append(f"`{short}` [{fact.visibility}] {fact.content}")
            if len(facts) > MAX_FACTS_SHOWN:
                lines.append(f"… и ещё {len(facts) - MAX_FACTS_SHOWN}")

        lines.append("")
        lines.append(
            "Управление: `!memory off`, `!memory on`, "
            "`!forget <id>`, `!forget all`."
        )
        await self._send_text(ctx, "\n".join(lines))

    async def _forget(self, ctx: commands.Context, target: str) -> None:
        store = self.services.memory.store
        user_id = str(ctx.author.id)

        if not target:
            return await ctx.send(
                "Использование: `!forget <id>` или `!forget all`. "
                "ID можно посмотреть в `!memory`."
            )

        if target.lower() in {"all", "всё", "все"}:
            ok = await store.delete_user_facts(user_id)
            if not ok:
                return await ctx.send("Не удалось удалить, попробуй позже.")
            return await ctx.send("Удалено всё, что я помнил о тебе.")

        facts = await store.list_facts(
            user_id,
            include_private=True,
            guild_id=None,
        )
        matches = [
            fact
            for fact in facts
            if fact.id and (fact.id == target or fact.id.startswith(target))
        ]
        if not matches:
            return await ctx.send("Такого факта нет. Посмотри список в `!memory`.")
        if len(matches) > 1:
            return await ctx.send("Подходит несколько фактов, укажи ID точнее.")

        fact = matches[0]
        ok = await store.delete_fact(user_id, str(fact.id))
        if not ok:
            return await ctx.send("Не удалось удалить, попробуй позже.")
        return await ctx.send(f"Забыл: {fact.content}")
