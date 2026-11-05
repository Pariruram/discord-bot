"""``!models`` — global free-model switcher with buttons."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot.llm.catalog import (
    ModelOption,
    available_free_models,
    provider_is_available,
)
from bot.llm.models import ModelRef, resolve_model_ref
from bot.llm.providers import build_providers

if TYPE_CHECKING:
    from bot.services import Services

logger = logging.getLogger(__name__)

PAGE_SIZE = 20


def build_models_embed(
    current: ModelRef,
    options: list[ModelOption],
) -> discord.Embed:
    embed = discord.Embed(
        title="Free-модели",
        description="Выбор меняет модель **для всех участников**.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Сейчас", value=f"`{current.label}`", inline=False)

    if current.key not in {option.key for option in options}:
        embed.add_field(
            name="⚠️ Текущая модель",
            value="Не входит в free-список. Выберите другую.",
            inline=False,
        )

    model_lines = [
        f"• **{option.label}** — {option.description}" for option in options
    ]
    model_text = "\n".join(model_lines)
    if model_text and len(model_text) <= 1024:
        embed.add_field(name="Модели", value=model_text, inline=False)

    embed.add_field(
        name="Подсказка",
        value="Нажмите кнопку с моделью. `OR` = OpenRouter.",
        inline=False,
    )
    embed.set_footer(
        text="Только free-тарифы с настроенным ключом. Лимиты могут меняться."
    )
    return embed


class ModelButtonsView(discord.ui.View):
    def __init__(
        self,
        services: Services,
        options: list[ModelOption],
        current_key: str,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=180)
        self.services = services
        self.options = options
        self.current_key = current_key
        max_page = max(0, (len(options) - 1) // PAGE_SIZE)
        self.page = max(0, min(page, max_page))
        self._build()

    def _build(self) -> None:
        self.clear_items()
        start = self.page * PAGE_SIZE
        page_options = self.options[start : start + PAGE_SIZE]

        for index, option in enumerate(page_options):
            is_current = option.key == self.current_key
            button = discord.ui.Button(
                label=option.label[:80],
                style=(
                    discord.ButtonStyle.success
                    if is_current
                    else discord.ButtonStyle.secondary
                ),
                disabled=is_current,
                row=index // 5,
            )
            button.callback = self._model_callback(option)
            self.add_item(button)

        total_pages = max(1, (len(self.options) + PAGE_SIZE - 1) // PAGE_SIZE)
        reset_row = 4 if total_pages > 1 else min(4, (len(page_options) - 1) // 5 + 1)
        if total_pages > 1:
            previous = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.secondary,
                row=4,
                disabled=self.page == 0,
            )
            previous.callback = self._previous_page
            self.add_item(previous)

            following = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.secondary,
                row=4,
                disabled=self.page >= total_pages - 1,
            )
            following.callback = self._next_page
            self.add_item(following)

        reset = discord.ui.Button(
            label="Сбросить",
            style=discord.ButtonStyle.danger,
            row=reset_row,
        )
        reset.callback = self._reset
        self.add_item(reset)

    def _model_callback(self, option: ModelOption):
        async def callback(interaction: discord.Interaction) -> None:
            await self._switch(interaction, option)

        return callback

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        await self._show_page(interaction, self.page - 1)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        await self._show_page(interaction, self.page + 1)

    async def _show_page(
        self,
        interaction: discord.Interaction,
        page: int,
    ) -> None:
        view = ModelButtonsView(
            self.services,
            self.options,
            self.current_key,
            page=page,
        )
        await interaction.response.edit_message(view=view)

    async def _switch(
        self,
        interaction: discord.Interaction,
        option: ModelOption,
    ) -> None:
        settings = await self.services.settings.get_all()
        providers = build_providers(settings)
        spec = providers.get(option.provider)

        if spec is None or not provider_is_available(spec):
            await interaction.response.send_message(
                f"Провайдер `{option.provider}` больше недоступен: нет ключа.",
                ephemeral=True,
            )
            return

        before = resolve_model_ref(settings, providers)
        saved = await self.services.model_switcher.apply(
            option.provider,
            option.model,
            option.utility_model,
        )
        logger.info(
            "Global model changed by %s (%s): %s -> %s",
            interaction.user,
            interaction.user.id,
            before.label,
            option.key,
        )

        fresh_settings = await self.services.settings.get_all()
        fresh_providers = build_providers(fresh_settings)
        current = resolve_model_ref(fresh_settings, fresh_providers)
        embed = build_models_embed(current, self.options)
        view = ModelButtonsView(
            self.services,
            self.options,
            current.key,
            page=self.page,
        )

        if saved:
            message = f"✅ Модель для всех: `{option.key}`"
        else:
            message = (
                f"⚠️ Модель применена: `{option.key}`, но не сохранилась в БД. "
                "После рестарта вернётся старая."
            )
        await interaction.response.send_message(message, ephemeral=True)

        if interaction.message is not None:
            try:
                await interaction.message.edit(embed=embed, view=view)
            except discord.HTTPException:
                logger.exception("Не удалось обновить сообщение !models")

    async def _reset(self, interaction: discord.Interaction) -> None:
        settings = await self.services.settings.get_all()
        providers = build_providers(settings)
        current = resolve_model_ref(settings, providers)
        spec = providers.get(current.provider)

        target: ModelOption | None = None
        if spec is not None:
            target = next(
                (
                    option
                    for option in self.options
                    if option.provider == current.provider
                    and option.model == spec.default_model
                ),
                None,
            )
        if target is None:
            target = next(
                (option for option in self.options if option.provider == current.provider),
                None,
            )
        if target is None:
            target = self.options[0]

        await self._switch(interaction, target)


class ModelsCog(commands.Cog, name="models"):
    def __init__(self, bot: commands.Bot, services: Services) -> None:
        self.bot = bot
        self.services = services

    @commands.command(name="models")
    async def models_cmd(self, ctx: commands.Context) -> None:
        settings = await self.services.settings.get_all()
        providers = build_providers(settings)
        options = available_free_models(providers)

        if not options:
            return await ctx.send(
                "Нет доступных free-моделей. Добавьте ключ Gemini, Groq или "
                "OpenRouter в переменные окружения."
            )

        current = resolve_model_ref(settings, providers)
        embed = build_models_embed(current, options)
        view = ModelButtonsView(self.services, options, current.key)
        await ctx.send(embed=embed, view=view)
