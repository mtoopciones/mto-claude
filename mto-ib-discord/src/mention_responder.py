"""
MentionResponder — Sistema de respuesta a menciones de @Mto_Toni.

Flujo:
  1. El bot detecta cualquier mensaje en el servidor que mencione a Mto_Toni
  2. Publica en el canal de menciones (webhook) un embed con:
       - El mensaje original y su autor
       - El canal donde fue mencionado
       - Una respuesta sugerida por Claude
       - Dos botones:
           💬 Contestar directamente  → publica la respuesta en el canal original
           ✏️ Modificar + publicar    → abre modal de edición, aprende de los cambios

Sistema de aprendizaje:
  - Cada vez que se modifica una respuesta, Claude analiza el cambio
  - Las lecciones se guardan en data/mention_learnings.json
  - Se aplican automáticamente en las siguientes respuestas sugeridas
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import ui
from loguru import logger

MADRID_ZONE         = ZoneInfo("Europe/Madrid")
_LEARNINGS_FILE     = "data/mention_learnings.json"
_MAX_LEARNINGS      = 30
_MAX_MODAL_CHARS    = 3900
_TONI_DISPLAY_NAME  = "Mto_Toni"
_MARIO_DISPLAY_NAME = "Mto_Mario"


async def _post_as_toni(
    channel: discord.TextChannel,
    text: str,
    reply_to_msg_id: Optional[int] = None,
    toni_avatar_url: str = "",
    display_name: str = _TONI_DISPLAY_NAME,
) -> bool:
    """
    Publica un mensaje en el canal como 'Mto_Toni' usando un webhook temporal.
    Si no puede crear el webhook (sin permisos), cae al bot normal.
    Devuelve True si se publicó via webhook (aparece como Mto_Toni).
    """
    try:
        # Buscar si ya existe un webhook con ese nombre en el canal
        existing = await channel.webhooks()
        wh = next((w for w in existing if w.name == display_name), None)

        if wh is None:
            # Crear webhook con el nombre y avatar del usuario
            avatar_bytes = None
            if toni_avatar_url:
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(toni_avatar_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                            if r.status == 200:
                                avatar_bytes = await r.read()
                except Exception:
                    pass
            wh = await channel.create_webhook(
                name=display_name,
                avatar=avatar_bytes,
                reason=f"MTO Bot — respuestas como {display_name}",
            )

        # Publicar (con referencia al mensaje original si es posible)
        kwargs: dict = {"username": display_name, "content": text}
        if toni_avatar_url:
            kwargs["avatar_url"] = toni_avatar_url
        if reply_to_msg_id:
            # Los webhooks no soportan reply directo, pero añadimos referencia visual
            try:
                ref_msg = await channel.fetch_message(reply_to_msg_id)
                author  = ref_msg.author.display_name
                preview = (ref_msg.content or "")[:80]
                kwargs["content"] = f"> **{author}:** {preview}\n\n{text}"
            except Exception:
                pass

        await wh.send(**kwargs)
        return True

    except discord.Forbidden:
        # Sin permisos para gestionar webhooks → usar bot normal
        if reply_to_msg_id:
            try:
                ref = await channel.fetch_message(reply_to_msg_id)
                await ref.reply(text)
                return False
            except Exception:
                pass
        await channel.send(text)
        return False
    except Exception as e:
        logger.warning(f"_post_as_toni: error — {e}")
        await channel.send(text)
        return False


# ── Modal: editar respuesta ────────────────────────────────────────

class MentionEditModal(ui.Modal, title="✏️ Editar respuesta"):
    text_input = ui.TextInput(
        label="Edita la respuesta antes de publicar",
        style=discord.TextStyle.paragraph,
        max_length=_MAX_MODAL_CHARS,
        required=True,
    )

    def __init__(
        self,
        suggested_text:  str,
        original_text:   str,
        target_channel:  discord.TextChannel,
        reply_to_msg_id: Optional[int],
        toni_avatar_url:  str = "",
        mario_avatar_url: str = "",
        analyze_cb:      Optional[Callable] = None,
    ):
        super().__init__()
        self.text_input.default  = suggested_text[:_MAX_MODAL_CHARS]
        self.original_text       = original_text
        self.suggested_text      = suggested_text
        self.target_channel      = target_channel
        self.reply_to_msg_id     = reply_to_msg_id
        self.toni_avatar_url     = toni_avatar_url
        self.mario_avatar_url    = mario_avatar_url
        self.analyze_cb          = analyze_cb

    async def on_submit(self, interaction: discord.Interaction):
        edited = self.text_input.value.strip()

        # Aprender de la edición si hubo cambios (fire-and-forget)
        if self.analyze_cb and edited.strip() != self.suggested_text.strip():
            asyncio.ensure_future(
                self.analyze_cb(self.original_text, self.suggested_text, edited)
            )

        # Mostrar vista de elección de autor (ephemeral)
        view = MentionPublishAsView(
            text             = edited,
            target_channel   = self.target_channel,
            reply_to_msg_id  = self.reply_to_msg_id,
            toni_avatar_url  = self.toni_avatar_url,
            mario_avatar_url = self.mario_avatar_url,
        )
        await interaction.response.send_message(
            f"**Texto editado** — ¿quién publica?```{edited[:300]}```",
            view=view,
            ephemeral=True,
        )


# ── View: elegir autor tras editar ────────────────────────────────

class MentionPublishAsView(ui.View):
    """Vista ephemeral que aparece después de editar, para elegir quién publica."""

    def __init__(
        self,
        text:             str,
        target_channel:   discord.TextChannel,
        reply_to_msg_id:  Optional[int],
        toni_avatar_url:  str = "",
        mario_avatar_url: str = "",
    ):
        super().__init__(timeout=120)
        self.text             = text
        self.target_channel   = target_channel
        self.reply_to_msg_id  = reply_to_msg_id
        self.toni_avatar_url  = toni_avatar_url
        self.mario_avatar_url = mario_avatar_url

    async def _publish(
        self, interaction: discord.Interaction,
        display_name: str, avatar_url: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await _post_as_toni(
                self.target_channel, self.text,
                self.reply_to_msg_id, avatar_url,
                display_name=display_name,
            )
            await interaction.followup.send(
                f"✅ Publicado como {display_name}.", ephemeral=True
            )
            # Intentar deshabilitar botones (puede fallar en mensajes efímeros)
            try:
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(view=self)
            except Exception:
                pass
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @ui.button(label="💬 Publicar como Mto_Toni", style=discord.ButtonStyle.green)
    async def btn_toni(self, interaction: discord.Interaction, button: ui.Button):
        await self._publish(interaction, _TONI_DISPLAY_NAME, self.toni_avatar_url)

    @ui.button(label="💬 Publicar como Mto_Mario", style=discord.ButtonStyle.green)
    async def btn_mario(self, interaction: discord.Interaction, button: ui.Button):
        await self._publish(interaction, _MARIO_DISPLAY_NAME, self.mario_avatar_url)


# ── View: botones de acción ────────────────────────────────────────

class MentionView(ui.View):

    def __init__(
        self,
        suggested_reply:  str,
        original_message: str,
        target_channel:   discord.TextChannel,
        reply_to_msg_id:  Optional[int],
        toni_avatar_url:  str = "",
        mario_avatar_url: str = "",
        analyze_cb:       Optional[Callable] = None,
    ):
        super().__init__(timeout=None)
        self.suggested_reply  = suggested_reply
        self.original_message = original_message
        self.target_channel   = target_channel
        self.reply_to_msg_id  = reply_to_msg_id
        self.toni_avatar_url  = toni_avatar_url
        self.mario_avatar_url = mario_avatar_url
        self.analyze_cb       = analyze_cb

    async def _publish_and_disable(
        self, interaction: discord.Interaction,
        display_name: str, avatar_url: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await _post_as_toni(
                self.target_channel, self.suggested_reply,
                self.reply_to_msg_id, avatar_url,
                display_name=display_name,
            )
            await interaction.followup.send(
                f"✅ Respuesta publicada como {display_name}.", ephemeral=True
            )
            try:
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(view=self)
            except Exception:
                pass
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @ui.button(label="💬 Publicar como Mto_Toni", style=discord.ButtonStyle.green, row=0)
    async def btn_toni(self, interaction: discord.Interaction, button: ui.Button):
        await self._publish_and_disable(interaction, _TONI_DISPLAY_NAME, self.toni_avatar_url)

    @ui.button(label="💬 Publicar como Mto_Mario", style=discord.ButtonStyle.green, row=0)
    async def btn_mario(self, interaction: discord.Interaction, button: ui.Button):
        await self._publish_and_disable(interaction, _MARIO_DISPLAY_NAME, self.mario_avatar_url)

    @ui.button(label="✏️ Modificar + publicar", style=discord.ButtonStyle.blurple, row=1)
    async def btn_edit(self, interaction: discord.Interaction, button: ui.Button):
        modal = MentionEditModal(
            suggested_text   = self.suggested_reply,
            original_text    = self.original_message,
            target_channel   = self.target_channel,
            reply_to_msg_id  = self.reply_to_msg_id,
            toni_avatar_url  = self.toni_avatar_url,
            mario_avatar_url = self.mario_avatar_url,
            analyze_cb       = self.analyze_cb,
        )
        await interaction.response.send_modal(modal)


# ── Clase principal ────────────────────────────────────────────────

class MentionResponder:
    """
    Detecta menciones a Mto_Toni y sugiere respuestas con Claude.
    Se integra con el bot de Discord del DiscordApprover.
    """

    def __init__(self, cfg: dict, toni_user_id: int):
        self.toni_user_id      = toni_user_id
        mario_id_str           = cfg.get("discord", {}).get("mario_user_id", "")
        self.mario_user_id     = int(mario_id_str) if mario_id_str else None
        disc                   = cfg.get("discord", {})
        self.toni_mention_webhook  = disc.get("toni_mention_webhook",  disc.get("mention_webhook", ""))
        self.mario_mention_webhook = disc.get("mario_mention_webhook", disc.get("mention_webhook", ""))
        self.anthropic_key     = cfg.get("anthropic", {}).get("api_key", "")

        self._learnings: List[dict] = []
        self._load_learnings()

    # ── Persistencia de lecciones ──────────────────────────────────

    def _load_learnings(self) -> None:
        try:
            if os.path.exists(_LEARNINGS_FILE):
                with open(_LEARNINGS_FILE, encoding="utf-8") as f:
                    self._learnings = json.load(f)
                logger.info(f"MentionResponder: {len(self._learnings)} lecciones cargadas")
        except Exception as e:
            logger.warning(f"MentionResponder: no se pudo cargar learnings: {e}")
            self._learnings = []

    def _save_learnings(self) -> None:
        try:
            os.makedirs(os.path.dirname(_LEARNINGS_FILE), exist_ok=True)
            with open(_LEARNINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._learnings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"MentionResponder: no se pudo guardar learnings: {e}")

    def _format_learnings(self) -> str:
        if not self._learnings:
            return ""
        recent = sorted(self._learnings, key=lambda x: x.get("date", ""), reverse=True)[:_MAX_LEARNINGS]
        lines  = [f"- {l.get('lesson', '')}" for l in recent if l.get("lesson")]
        if not lines:
            return ""
        return "\n\nLECCIONES DE RESPUESTAS ANTERIORES (aplícalas):\n" + "\n".join(lines)

    # ── Generación de respuesta con Claude ─────────────────────────

    async def _generate_reply(self, question: str, author_name: str, channel_name: str) -> str:
        if not self.anthropic_key:
            return f"Hola {author_name}! Gracias por tu mensaje. Te respondo en breve."

        import anthropic
        learnings = self._format_learnings()

        prompt = (
            "Eres Toni Faura, director financiero y fundador de MTO Opciones "
            "(comunidad de trading de opciones para hispanohablantes).\n\n"
            f"Un miembro del servidor Discord te ha mencionado en el canal #{channel_name}.\n\n"
            f"MENSAJE DE {author_name.upper()}:\n{question}\n\n"
            "Escribe una respuesta directa, cercana y profesional en nombre de Toni. "
            "Sé conciso (máximo 3-4 frases). "
            "Si es una pregunta técnica sobre opciones, responde con precisión. "
            "Si es un saludo o comentario, responde de forma cálida. "
            "No uses emojis excesivos."
            f"{learnings}\n\n"
            "Devuelve SOLO el texto de la respuesta, listo para publicar."
        )

        client = anthropic.Anthropic(api_key=self.anthropic_key)
        loop   = asyncio.get_event_loop()
        resp   = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                model="claude-opus-4-5",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
        )
        return resp.content[0].text.strip()

    # ── Aprendizaje de ediciones ────────────────────────────────────

    async def _analyze_and_learn(
        self, original_question: str, suggested: str, edited: str
    ) -> None:
        if not self.anthropic_key:
            return
        try:
            import anthropic

            prompt = (
                "Analiza la diferencia entre la respuesta SUGERIDA por el bot y la versión "
                "EDITADA por Toni Faura para una mención en Discord.\n\n"
                f"PREGUNTA ORIGINAL:\n{original_question}\n\n"
                f"RESPUESTA SUGERIDA:\n{suggested}\n\n"
                f"RESPUESTA EDITADA POR TONI:\n{edited}\n\n"
                "Extrae 1-3 lecciones concretas y accionables sobre el estilo, tono "
                "o contenido que Toni prefiere. Responde SOLO con JSON:\n"
                '{"lessons": ["lección 1", "lección 2", ...]}'
            )

            client   = anthropic.Anthropic(api_key=self.anthropic_key)
            loop     = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.messages.create(
                    model="claude-opus-4-5",
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed  = json.loads(raw.strip())
            lessons = parsed.get("lessons", [])

            today = datetime.now().strftime("%Y-%m-%d")
            for lesson in lessons:
                self._learnings.append({"date": today, "lesson": lesson})
            # Máximo 100 lecciones
            if len(self._learnings) > 100:
                self._learnings = self._learnings[-100:]
            self._save_learnings()
            logger.info(f"MentionResponder: {len(lessons)} lección(es) aprendida(s)")

        except Exception as e:
            logger.error(f"MentionResponder: error aprendizaje: {e}")

    # ── Publicar en canal de menciones ─────────────────────────────

    def _webhooks_for_mention(self, message: discord.Message) -> List[str]:
        """Devuelve la lista de webhooks destino según a quién se menciona."""
        mentioned_ids = {u.id for u in message.mentions}
        webhooks = []
        if self.toni_user_id in mentioned_ids and self.toni_mention_webhook:
            webhooks.append(self.toni_mention_webhook)
        if self.mario_user_id and self.mario_user_id in mentioned_ids and self.mario_mention_webhook:
            webhooks.append(self.mario_mention_webhook)
        return webhooks

    async def handle_mention(
        self, message: discord.Message, bot_client: discord.Client
    ) -> None:
        """Procesa una mención y publica en el/los canal(es) de menciones con botones."""
        webhooks = self._webhooks_for_mention(message)
        if not webhooks:
            logger.warning("MentionResponder: webhook no configurado")
            return

        # Si se menciona a los dos, publicar en ambos canales en paralelo
        if len(webhooks) > 1:
            await asyncio.gather(*[
                self._handle_mention_to_webhook(message, bot_client, wh)
                for wh in webhooks
            ])
            return

        await self._handle_mention_to_webhook(message, bot_client, webhooks[0])

    async def _handle_mention_to_webhook(
        self, message: discord.Message, bot_client: discord.Client, webhook: str
    ) -> None:
        """Publica la notificación de mención en un canal concreto (via webhook)."""
        author_name  = message.author.display_name
        channel_name = message.channel.name if hasattr(message.channel, "name") else "DM"
        content      = message.content or "(mensaje sin texto)"
        jump_url     = message.jump_url

        logger.info(f"MentionResponder: mención de {author_name} en #{channel_name}")

        # Generar respuesta sugerida
        suggested = await self._generate_reply(content, author_name, channel_name)

        # Obtener el canal original para que los botones puedan publicar allí
        target_channel = message.channel

        # Obtener avatares de Toni y Mario para los webhooks
        guild = message.guild
        toni_member  = guild.get_member(self.toni_user_id)  if guild else None
        mario_member = guild.get_member(self.mario_user_id) if guild and self.mario_user_id else None
        toni_avatar  = str(toni_member.display_avatar.url)  if toni_member  and toni_member.display_avatar  else ""
        mario_avatar = str(mario_member.display_avatar.url) if mario_member and mario_member.display_avatar else ""

        # Crear view con botones
        view = MentionView(
            suggested_reply  = suggested,
            original_message = content,
            target_channel   = target_channel,
            reply_to_msg_id  = message.id,
            toni_avatar_url  = toni_avatar,
            mario_avatar_url = mario_avatar,
            analyze_cb       = self._analyze_and_learn,
        )

        # Embed con el contexto
        now_str = datetime.now(MADRID_ZONE).strftime("%d/%m/%Y %H:%M")
        embed = discord.Embed(
            title       = f"📣 Mención de @{author_name}",
            description = content[:2000],
            color       = 0xF39C12,
            timestamp   = datetime.utcnow(),
        )
        embed.add_field(name="📍 Canal", value=f"#{channel_name}", inline=True)
        embed.add_field(name="🔗 Ir al mensaje", value=f"[Ver en Discord]({jump_url})", inline=True)
        embed.add_field(
            name  = "💡 Respuesta sugerida",
            value = suggested[:1020],
            inline= False,
        )
        embed.set_footer(text=f"MTO Bot · {now_str}")
        if message.author.avatar:
            embed.set_thumbnail(url=message.author.avatar.url)

        # Enviar al canal de menciones via webhook (para el embed)
        # Y enviar el mensaje interactivo con botones al canal via bot
        try:
            # Intentar obtener el canal del webhook
            channel_id = await self._get_webhook_channel_id(webhook)
            mention_ch = None

            if channel_id:
                # Primero intentar get_channel (caché), luego fetch_channel (API)
                mention_ch = bot_client.get_channel(channel_id)
                if mention_ch is None:
                    try:
                        mention_ch = await bot_client.fetch_channel(channel_id)
                    except Exception as e_fetch:
                        logger.warning(f"MentionResponder: no se pudo acceder al canal {channel_id}: {e_fetch}")

            if mention_ch:
                # Enviar embed + botones en un solo mensaje via bot
                await mention_ch.send(
                    content="**📣 Nueva mención — elige cómo responder:**",
                    embed=embed,
                    view=view,
                )
            else:
                # Fallback: solo el embed via webhook (sin botones)
                logger.warning("MentionResponder: sin acceso al canal — enviando solo embed via webhook")
                async with aiohttp.ClientSession() as s:
                    await s.post(
                        webhook,
                        json={
                            "content": "**📣 Nueva mención** (da al bot permisos en este canal para ver los botones de respuesta)",
                            "embeds": [embed.to_dict()],
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    )

        except Exception as e:
            logger.error(f"MentionResponder: error publicando mención: {e}")

    async def _get_webhook_channel_id(self, webhook_url: str) -> Optional[int]:
        """Obtiene el channel_id del webhook de menciones."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    webhook_url,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return int(data.get("channel_id", 0)) or None
        except Exception:
            pass
        return None
