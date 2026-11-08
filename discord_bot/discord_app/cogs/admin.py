"""Owner-only administration commands."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from discord_bot.llm.catalog import validate_model
from discord_bot.llm.models import parse_model_switch, resolve_model_ref
from discord_bot.llm.providers import (
    BUILTIN_PROVIDERS,
    build_providers,
    custom_provider_dicts,
    is_valid_provider_name,
    spec_from_dict,
)
from discord_bot.prompts import DEFAULT_SYSTEM_PROMPT
from discord_bot.tools.base import enabled_tool_names
from discord_bot.utils import as_bool, parse_json_list, safe_int, split_message

if TYPE_CHECKING:
    from discord_bot.services import Services

logger = logging.getLogger(__name__)

ALLOWED_SETTINGS = {
    "provider",
    "chat_model",
    "model",
    "utility_model",
    "fast_model",
    "gemini_model",
    "gemini_fast_model",
    "groq_model",
    "groq_fast_model",
    "max_history",
    "max_message_length",
    "max_tokens",
    "enabled_tools",
    "global_system_prompt",
    "memory_enabled",
    "memory_retention_days",
    "max_facts_per_user",
    "max_context_facts",
    "session_gap_minutes",
    "worker_poll_seconds",
    "catch_up_limit",
    "allowed_channel_ids",
}

MODEL_SETTING_KEYS = {
    "chat_model",
    "model",
    "utility_model",
    "fast_model",
    "gemini_model",
    "gemini_fast_model",
    "groq_model",
    "groq_fast_model",
}

NUMERIC_SETTINGS = {
    "max_history": (1, 100),
    "max_message_length": (200, 2000),
    "max_tokens": (200, 4000),
    "memory_retention_days": (1, 3650),
    "max_facts_per_user": (10, 1000),
    "max_context_facts": (1, 200),
    "session_gap_minutes": (1, 1440),
    "worker_poll_seconds": (5, 3600),
    "catch_up_limit": (0, 500),
}


class AdminCog(commands.Cog, name="admin"):
    def __init__(self, bot: commands.Bot, services: Services) -> None:
        self.bot = bot
        self.services = services

    async def _guard(self, ctx: commands.Context) -> bool:
        if not isinstance(ctx.channel, discord.DMChannel):
            await ctx.send("Нет доступа.")
            return False
        owners = await self.services.settings.get_owners()
        if str(ctx.author.id) not in owners:
            await ctx.send("Нет доступа.")
            return False
        return True

    async def _send_text(self, ctx: commands.Context, text: str) -> None:
        for chunk in split_message(text, max_len=1900):
            await ctx.send(chunk)

    def _provider_for_model_key(
        self,
        key: str,
        settings: dict[str, str],
        providers,
    ) -> str | None:
        if key in {"gemini_model", "gemini_fast_model"}:
            return "gemini"
        if key in {"groq_model", "groq_fast_model"}:
            return "groq"
        if key in {"chat_model", "model", "utility_model", "fast_model"}:
            return resolve_model_ref(settings, providers).provider
        return None

    def _validate_model(self, provider: str | None, model: str) -> str | None:
        return validate_model(provider, model)

    @commands.command(name="owners")
    async def owners_cmd(self, ctx: commands.Context) -> None:
        if not await self._guard(ctx):
            return
        owners = await self.services.settings.get_owners()
        await ctx.send(f"**Владельцы:** {owners}")

    @commands.command(name="owner")
    async def owner_cmd(
        self,
        ctx: commands.Context,
        action: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if not await self._guard(ctx):
            return

        owners = await self.services.settings.get_owners()

        if action == "add" and user_id and user_id.isdigit():
            if user_id in owners:
                return await ctx.send(f"Уже владелец: `{user_id}`")
            owners.append(user_id)
            await self.services.settings.save_owners(owners)
            return await ctx.send(f"Добавлен: `{user_id}`")

        if action == "remove" and user_id and user_id in owners:
            if len(owners) <= 1:
                return await ctx.send("Нельзя удалить последнего владельца.")
            owners.remove(user_id)
            await self.services.settings.save_owners(owners)
            return await ctx.send(f"Удален: `{user_id}`")

        await ctx.send("Использование: `!owner add <id>` или `!owner remove <id>`")

    @commands.command(name="settings")
    async def settings_cmd(self, ctx: commands.Context) -> None:
        if not await self._guard(ctx):
            return
        settings = await self.services.settings.get_all()
        await self._send_text(
            ctx,
            "```json\n" + json.dumps(settings, indent=2, ensure_ascii=False) + "\n```",
        )

    @commands.command(name="set")
    async def set_cmd(
        self,
        ctx: commands.Context,
        key: str | None = None,
        *,
        value: str | None = None,
    ) -> None:
        if not await self._guard(ctx):
            return
        if not key or value is None or not value.strip():
            return await self._send_text(
                ctx,
                "Использование: `!set <key> <value>`\n"
                f"Ключи: {', '.join(sorted(ALLOWED_SETTINGS))}",
            )

        key = key.strip()
        value = value.strip()
        if key not in ALLOWED_SETTINGS:
            return await ctx.send("Неверный ключ.")

        settings = await self.services.settings.get_all()
        providers = build_providers(settings)

        if key in MODEL_SETTING_KEYS:
            provider = self._provider_for_model_key(key, settings, providers)
            problem = self._validate_model(provider, value)
            if problem:
                return await ctx.send(problem)

        if key == "provider":
            provider = value.lower()
            if provider not in providers:
                return await ctx.send(
                    "Неизвестный провайдер. Доступны: "
                    + ", ".join(sorted(providers))
                )
            spec = providers[provider]
            await self.services.settings.set("provider", provider)
            await self.services.settings.set("chat_model", spec.default_model)
            await self.services.settings.set(
                "utility_model", spec.default_utility_model
            )
            return await ctx.send(
                f"Провайдер: `{provider}`, модель: `{spec.default_model}`"
            )

        if key in NUMERIC_SETTINGS:
            minimum, maximum = NUMERIC_SETTINGS[key]
            number = safe_int(value, minimum - 1)
            if not minimum <= number <= maximum:
                return await ctx.send(
                    f"`{key}` должно быть числом от {minimum} до {maximum}."
                )
            await self.services.settings.set(key, str(number))
            return await ctx.send(f"`{key}` изменен на `{number}`.")

        if key == "memory_enabled":
            lowered = value.lower()
            if lowered not in {"true", "false", "1", "0", "on", "off", "yes", "no"}:
                return await ctx.send("Ожидается `true` или `false`.")
            await self.services.settings.set(key, "true" if as_bool(value) else "false")
            return await ctx.send(f"`{key}` изменен.")

        if key == "allowed_channel_ids":
            if value.startswith("["):
                parsed = parse_json_list(value, default=None)
                if not isinstance(parsed, list):
                    return await ctx.send("Ожидается JSON-список или список через запятую.")
                names = [str(item).strip() for item in parsed if str(item).strip()]
            else:
                names = [item.strip() for item in value.split(",") if item.strip()]
            if any(not item.isdigit() for item in names):
                return await ctx.send("ID каналов должны быть числами.")
            await self.services.settings.set(
                key, json.dumps(names, ensure_ascii=False)
            )
            return await ctx.send(
                f"Каналы: {names or 'все, где упомянули бота'}"
            )

        if key == "enabled_tools":
            if value.startswith("["):
                parsed = parse_json_list(value, default=None)
                if not isinstance(parsed, list):
                    return await ctx.send("Ожидается JSON-список или список через запятую.")
                names = [str(item) for item in parsed]
            else:
                names = [item.strip() for item in value.split(",") if item.strip()]

            unknown = [name for name in names if name not in self.services.tools.names()]
            if unknown:
                return await ctx.send(
                    "Неизвестные инструменты: " + ", ".join(unknown)
                )
            await self.services.settings.set(
                key, json.dumps(names, ensure_ascii=False)
            )
            return await ctx.send(f"Включенные инструменты: {names or 'нет'}")

        await self.services.settings.set(key, value)
        await ctx.send(f"`{key}` изменен.")

    @commands.command(name="system")
    async def system_cmd(
        self,
        ctx: commands.Context,
        *,
        arg: str | None = None,
    ) -> None:
        if not await self._guard(ctx):
            return

        if not arg:
            prompt = await self.services.settings.get(
                "global_system_prompt", DEFAULT_SYSTEM_PROMPT
            )
            return await self._send_text(ctx, f"```\n{prompt}\n```")

        if arg.lower().startswith("set "):
            new_prompt = arg[4:].strip()
            if not new_prompt:
                return await ctx.send("Пустой prompt не сохранён.")
            await self.services.settings.set("global_system_prompt", new_prompt)
            return await ctx.send("Prompt обновлен.")

        if arg.lower() == "reset":
            await self.services.settings.set(
                "global_system_prompt", DEFAULT_SYSTEM_PROMPT
            )
            return await ctx.send("Сброшено.")

        await ctx.send("Использование: `!system`, `!system set <текст>` или `!system reset`")

    @commands.command(name="model")
    async def model_cmd(
        self,
        ctx: commands.Context,
        *,
        value: str | None = None,
    ) -> None:
        if not await self._guard(ctx):
            return

        settings = await self.services.settings.get_all()
        providers = build_providers(settings)
        current = resolve_model_ref(settings, providers)

        if not value:
            lines = [
                f"**Текущая модель:** `{current.label}`",
                f"Utility: `{current.utility_model}`",
                "",
                "**Провайдеры:**",
            ]
            for name, spec in providers.items():
                marker = "→ " if name == current.provider else "  "
                lines.append(
                    f"{marker}`{name}` — default: `{spec.default_model}`"
                )
            lines.append("")
            lines.append(
                "Переключение (глобально для всех): `!model provider:model` "
                "или `!model model`"
            )
            return await self._send_text(ctx, "\n".join(lines))

        provider, model = parse_model_switch(value, providers)
        if provider is not None:
            spec = providers[provider]
            selected = model or spec.default_model
            problem = self._validate_model(provider, selected)
            if problem:
                return await ctx.send(problem)
            await self.services.settings.set("provider", provider)
            await self.services.settings.set("chat_model", selected)
            await self.services.settings.set(
                "utility_model", spec.default_utility_model
            )
            return await ctx.send(
                f"Модель переключена: `{provider}:{selected}`"
            )

        if not model:
            return await ctx.send("Пустое имя модели.")
        problem = self._validate_model(current.provider, model)
        if problem:
            return await ctx.send(problem)
        await self.services.settings.set("chat_model", model)
        await ctx.send(f"Модель: `{current.provider}:{model}`")

    @commands.command(name="provider")
    async def provider_cmd(
        self,
        ctx: commands.Context,
        action: str | None = None,
        name: str | None = None,
        base_url: str | None = None,
        key_env: str | None = None,
        model: str | None = None,
        utility_model: str | None = None,
    ) -> None:
        if not await self._guard(ctx):
            return

        settings = await self.services.settings.get_all()
        providers = build_providers(settings)
        custom = custom_provider_dicts(settings)

        if not action or action.lower() == "list":
            lines = ["**Провайдеры:**"]
            active = resolve_model_ref(settings, providers).provider
            for provider_name, spec in providers.items():
                kind = "custom" if provider_name not in BUILTIN_PROVIDERS else "builtin"
                marker = "→ " if provider_name == active else "  "
                lines.append(
                    f"{marker}`{provider_name}` ({kind}) — {spec.base_url}"
                )
            lines.append(
                "\nДобавить: `!provider add <name> <base_url> <KEY_ENV|-> <model> [utility_model]`"
            )
            lines.append("Удалить: `!provider remove <name>`")
            return await self._send_text(ctx, "\n".join(lines))

        action = action.lower()

        if action == "add":
            if not name or not base_url or not key_env or not model:
                return await ctx.send(
                    "Использование: `!provider add <name> <base_url> <KEY_ENV|-> <model> [utility_model]`"
                )
            provider_name = name.strip().lower()
            if not is_valid_provider_name(provider_name) or provider_name in BUILTIN_PROVIDERS:
                return await ctx.send("Некорректное или занятое имя провайдера.")
            if any(str(item.get("name", "")).lower() == provider_name for item in custom):
                return await ctx.send("Провайдер с таким именем уже существует.")

            record = {
                "name": provider_name,
                "base_url": base_url.strip(),
                "api_key_env": "" if key_env.strip() in {"-", "none", "no"} else key_env.strip(),
                "default_model": model.strip(),
            }
            if utility_model:
                record["default_utility_model"] = utility_model.strip()

            if spec_from_dict(record) is None:
                return await ctx.send(
                    "Не удалось добавить провайдера: проверь base_url и модель."
                )

            custom.append(record)
            await self.services.settings.set(
                "providers_json", json.dumps(custom, ensure_ascii=False)
            )
            return await ctx.send(f"Провайдер `{provider_name}` добавлен.")

        if action == "remove":
            if not name:
                return await ctx.send("Использование: `!provider remove <name>`")
            provider_name = name.strip().lower()
            if provider_name in BUILTIN_PROVIDERS:
                return await ctx.send("Встроенного провайдера удалить нельзя.")
            remaining = [
                item
                for item in custom
                if str(item.get("name", "")).lower() != provider_name
            ]
            if len(remaining) == len(custom):
                return await ctx.send("Провайдер не найден.")

            await self.services.settings.set(
                "providers_json", json.dumps(remaining, ensure_ascii=False)
            )
            if resolve_model_ref(settings, providers).provider == provider_name:
                fallback = BUILTIN_PROVIDERS["gemini"]
                await self.services.settings.set("provider", fallback.name)
                await self.services.settings.set("chat_model", fallback.default_model)
                await self.services.settings.set(
                    "utility_model", fallback.default_utility_model
                )
            return await ctx.send(f"Провайдер `{provider_name}` удален.")

        await ctx.send("Использование: `!provider list|add|remove`")

    @commands.command(name="tools")
    async def tools_cmd(
        self,
        ctx: commands.Context,
        action: str | None = None,
        name: str | None = None,
    ) -> None:
        if not await self._guard(ctx):
            return

        settings = await self.services.settings.get_all()
        registry = self.services.tools
        enabled = enabled_tool_names(settings.get("enabled_tools"), registry)

        if not action:
            if not registry.names():
                return await ctx.send("Инструменты не зарегистрированы.")
            lines = ["**Инструменты:**"]
            for tool_name in registry.names():
                mark = "✅" if tool_name in enabled else "⛔"
                lines.append(f"{mark} `{tool_name}`")
            lines.append("\nУправление: `!tools enable <name>` / `!tools disable <name>`")
            return await self._send_text(ctx, "\n".join(lines))

        action = action.lower()
        if action not in {"enable", "disable"}:
            return await ctx.send("Использование: `!tools enable|disable <name>`")
        if not name or name not in registry.names():
            return await ctx.send(
                "Неизвестный инструмент. Доступны: " + ", ".join(registry.names())
            )

        if action == "enable" and name not in enabled:
            enabled.append(name)
        elif action == "disable" and name in enabled:
            enabled.remove(name)

        await self.services.settings.set(
            "enabled_tools", json.dumps(enabled, ensure_ascii=False)
        )
        await ctx.send(f"Инструменты: {enabled or 'нет'}")
