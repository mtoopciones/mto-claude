"""
DiscordApprover — Sistema de revisión y aprobación de informes.

Flujo:
  1. Bot genera informe → lo publica en canal de revisión CON 5 botones
  2. Usuario revisa y clica:
       ✅ Publicar en Discord    → publica limpio en el webhook de destino
       🌐 Publicar en Redes     → intro + contenido + hashtags × red social
       📢 Discord + Redes       → ambas opciones a la vez
       ✏️  Editar y publicar     → modal de edición → elige destino
                                  + aprende de los cambios realizados
       🔄 Volver a generar      → Claude reescribe (aplicando lecciones previas)

Mejoras en publicación social:
  - Intro atractiva generada por Claude para cada red
  - Hashtags optimizados por red (Twitter 2-3, Instagram 20-25, Facebook 3-5)
  - Log detallado en logs/social_posts.log

Sistema de aprendizaje editorial:
  - Al editar y publicar Claude analiza qué cambió y por qué
  - Las lecciones se guardan en data/editorial_learnings.json
  - En regeneraciones y futuras publicaciones se aplican automáticamente
  - Log de cambios aprendidos en logs/editorial_changes.log

Los botones son PERSISTENTES: sobreviven reinicios del bot porque:
  - Cada botón tiene un custom_id estático
  - El estado (publish_webhook + embeds) se guarda en data/pending_reviews.json
  - En _on_ready el bot re-edita el mensaje con botones frescos

Requiere:
  - discord.bot_token      en config.yaml
  - discord.review_webhook en config.yaml
  - anthropic.api_key      en config.yaml (para regenerar, generar intros y aprender)
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from datetime import datetime
from typing import Callable, Dict, List, Optional

import aiohttp
import discord
from discord import ui
from loguru import logger


# ── Constantes ────────────────────────────────────────────────────

_MAX_MODAL_CHARS   = 3900    # límite seguro para TextInput de Discord
_MAX_TWITTER       = 280
_MAX_INSTAGRAM     = 2200
_MAX_FACEBOOK      = 63000
_PENDING_FILE      = "data/pending_reviews.json"
_LEARNINGS_FILE    = "data/editorial_learnings.json"
_SOCIAL_LOG_FILE   = "logs/social_posts.log"
_EDIT_LOG_FILE     = "logs/editorial_changes.log"
_MAX_LEARNINGS     = 25      # lecciones máximas por tipo que se inyectan en el prompt

# Reglas de hashtags por red social
_HASHTAG_RULES = {
    "twitter":   "2-3 hashtags al final (menos es más en Twitter/X; más reduce engagement)",
    "instagram": "20-25 hashtags al final en un bloque separado (más hashtags = mayor alcance en Instagram)",
    "facebook":  "3-5 hashtags al final (Facebook penaliza el exceso de hashtags)",
}

# Sugerencias de hashtags base (se pasan a Claude como referencia)
_HASHTAG_SUGGESTIONS = {
    "twitter": [
        "#Opciones", "#Trading", "#MercadosFinancieros",
        "#Bolsa", "#SP500", "#Inversión",
    ],
    "instagram": [
        "#Opciones", "#Trading", "#MercadosFinancieros", "#Bolsa", "#Inversión",
        "#TradingOpciones", "#AnálisisMercado", "#MTOOpciones", "#FinanzasPersonales",
        "#Economía", "#WallStreet", "#SP500", "#Nasdaq", "#TradingLatam",
        "#EstrategiaOpciones", "#OpcionesFinancieras", "#InversiónLatam",
        "#TradingEspaña", "#OpcionesAcciones", "#EducaciónFinanciera",
        "#InversiónInteligente", "#BolsaDeValores", "#MercadoFinanciero",
        "#ComunidadTrader", "#TradingEducation",
    ],
    "facebook": [
        "#Opciones", "#Trading", "#MercadosFinancieros", "#Inversión",
        "#FinanzasPersonales", "#MTOOpciones", "#TradingOpciones",
    ],
}

# Fallback de hashtags sin Claude
_HASHTAG_DEFAULTS = {
    "twitter":   "#Opciones #Trading #MercadosFinancieros",
    "instagram": (
        "#Opciones #Trading #MercadosFinancieros #Bolsa #Inversión "
        "#TradingOpciones #AnálisisMercado #MTOOpciones #FinanzasPersonales "
        "#Economía #WallStreet #SP500 #Nasdaq #TradingLatam "
        "#EstrategiaOpciones #OpcionesFinancieras #InversiónLatam "
        "#TradingEspaña #OpcionesAcciones #EducaciónFinanciera #InversiónInteligente"
    ),
    "facebook":  "#Opciones #Trading #MercadosFinancieros #Inversión #FinanzasPersonales",
}


# ── Helpers ───────────────────────────────────────────────────────

def _embeds_to_text(embeds: List[dict]) -> str:
    """Extrae texto plano de una lista de embeds Discord para editar/publicar en redes."""
    parts = []
    for e in embeds:
        if e.get("author", {}).get("name"):
            parts.append(e["author"]["name"])
        if e.get("title"):
            parts.append(e["title"])
        if e.get("description"):
            parts.append(e["description"])
        for field in e.get("fields", []):
            if field.get("name") and field.get("value"):
                parts.append(f"**{field['name']}**\n{field['value']}")
    return "\n\n".join(parts)


def _to_discord_embeds(raw: List[dict]) -> List[discord.Embed]:
    return [discord.Embed.from_dict(e) for e in raw]


def _write_log(log_file: str, lines: List[str]) -> None:
    """Añade líneas al log especificado (crea dirs si no existen)."""
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"[{ts}] {line}\n")
    except Exception as e:
        logger.warning(f"Log write error ({log_file}): {e}")


# ── View: publicar tras editar (ephemeral, no necesita persistencia) ──

class PublishAfterEditView(ui.View):
    def __init__(
        self,
        edited_embeds:      List[dict],
        publish_discord_cb: Callable,
        publish_social_cb:  Callable,
    ):
        super().__init__(timeout=600)
        self.edited_embeds      = edited_embeds
        self.publish_discord_cb = publish_discord_cb
        self.publish_social_cb  = publish_social_cb

    @ui.button(label="✅ Publicar en Discord", style=discord.ButtonStyle.green)
    async def pub_discord(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.publish_discord_cb(self.edited_embeds)
            await interaction.edit_original_response(content="✅ Publicado en Discord.", view=None, embed=None)
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Error: {e}", view=None)

    @ui.button(label="🌐 Publicar en Redes", style=discord.ButtonStyle.blurple)
    async def pub_social(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.publish_social_cb(self.edited_embeds)
            await interaction.edit_original_response(content=f"🌐 {result}", view=None, embed=None)
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Error: {e}", view=None)

    @ui.button(label="📢 Publicar en Todo", style=discord.ButtonStyle.green)
    async def pub_all(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.publish_discord_cb(self.edited_embeds)
            result = await self.publish_social_cb(self.edited_embeds)
            await interaction.edit_original_response(
                content=f"✅ Publicado en Discord y Redes. {result}", view=None, embed=None
            )
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Error: {e}", view=None)


# ── Modal: editar texto ────────────────────────────────────────────

class EditModal(ui.Modal, title="✏️ Editar contenido"):
    text_input = ui.TextInput(
        label="Edita el texto antes de publicar",
        style=discord.TextStyle.paragraph,
        max_length=_MAX_MODAL_CHARS,
        required=True,
    )

    def __init__(
        self,
        current_text:       str,
        original_text:      str,      # texto original SIN editar (para comparar)
        original_embeds:    List[dict],
        publish_discord_cb: Callable,
        publish_social_cb:  Callable,
        analyze_cb:         Optional[Callable] = None,  # async(original, edited, report_type) → str
        report_type:        str = "",
    ):
        super().__init__()
        self.text_input.default  = current_text[:_MAX_MODAL_CHARS]
        self.original_text       = original_text
        self.original_embeds     = original_embeds
        self.publish_discord_cb  = publish_discord_cb
        self.publish_social_cb   = publish_social_cb
        self.analyze_cb          = analyze_cb
        self.report_type         = report_type

    async def on_submit(self, interaction: discord.Interaction):
        edited_text = self.text_input.value

        # ── Análisis de cambios + aprendizaje ────────────────────────
        if self.analyze_cb and edited_text.strip() != self.original_text.strip():
            asyncio.ensure_future(
                self.analyze_cb(self.original_text, edited_text, self.report_type)
            )

        # Reconstruir un embed simple con el texto editado
        base = self.original_embeds[0] if self.original_embeds else {}
        edited_embed = {
            "description": edited_text,
            "color":       base.get("color", 0x2ECC71),
        }
        if base.get("author"):
            edited_embed["author"] = base["author"]
        if base.get("title"):
            edited_embed["title"] = base["title"]

        view = PublishAfterEditView(
            edited_embeds=[edited_embed],
            publish_discord_cb=self.publish_discord_cb,
            publish_social_cb=self.publish_social_cb,
        )
        await interaction.response.send_message(
            "**Vista previa — ¿dónde publicar?**",
            embed=discord.Embed.from_dict(edited_embed),
            view=view,
            ephemeral=True,
        )


# ── View principal: PERSISTENTE con custom_ids estáticos ──────────

class PersistentReviewView(ui.View):
    """
    Vista con 5 botones para aprobar/rechazar informes.
    Usa custom_id estáticos + estado guardado en disco para sobrevivir
    reinicios del bot (re-registrada en _on_ready con add_view).

    Fila 0: ✅ Discord | 🌐 Redes | 📢 Discord + Redes
    Fila 1: ✏️ Editar  | 🔄 Regenerar
    """

    def __init__(self, approver: "DiscordApprover"):
        super().__init__(timeout=None)   # sin expiración
        self.approver = approver

    def _ctx(self, message_id: int) -> Optional[dict]:
        """Obtiene el contexto almacenado para este mensaje."""
        return self.approver._pending_reviews.get(str(message_id))

    # ── Botón 1: Publicar en Discord ──────────────────────────────

    @ui.button(label="✅ Publicar en Discord", style=discord.ButtonStyle.green,
               row=0, custom_id="review_btn_discord")
    async def btn_discord(self, interaction: discord.Interaction, button: ui.Button):
        ctx = self._ctx(interaction.message.id)
        if not ctx:
            await interaction.response.send_message(
                "❌ Esta revisión expiró (el bot se reinició después de enviarla). "
                "Espera el próximo informe programado.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await _post_webhook(ctx["publish_webhook"], {"embeds": ctx["embeds"]})
            await interaction.message.edit(
                content=ctx["header"] + "\n\n✅ **Publicado en Discord**",
                view=None,
            )
            await interaction.followup.send("✅ Publicado correctamente en Discord.", ephemeral=True)
            self.approver._remove_pending(str(interaction.message.id))
            logger.info(f"Approver: '{ctx['report_type']}' publicado en Discord ✅")
            _write_log(_SOCIAL_LOG_FILE, [
                f"DISCORD | {ctx['report_type']} | ✅ OK",
            ])
        except Exception as e:
            await interaction.followup.send(f"❌ Error al publicar: {e}", ephemeral=True)

    # ── Botón 2: Publicar en Redes ────────────────────────────────

    @ui.button(label="🌐 Publicar en Redes", style=discord.ButtonStyle.blurple,
               row=0, custom_id="review_btn_social")
    async def btn_social(self, interaction: discord.Interaction, button: ui.Button):
        ctx = self._ctx(interaction.message.id)
        if not ctx:
            await interaction.response.send_message(
                "❌ Esta revisión expiró (el bot se reinició). Espera el próximo informe.",
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.approver._publish_social(ctx["embeds"], ctx["report_type"], message_id=str(interaction.message.id))
            await interaction.message.edit(
                content=ctx["header"] + f"\n\n🌐 **Publicado en Redes** — {result}",
                view=None,
            )
            await interaction.followup.send(f"🌐 {result}", ephemeral=True)
            self.approver._remove_pending(str(interaction.message.id))
        except Exception as e:
            await interaction.followup.send(f"❌ Error al publicar en redes: {e}", ephemeral=True)

    # ── Botón 3: Publicar en Discord + Redes ─────────────────────

    @ui.button(label="📢 Discord + Redes", style=discord.ButtonStyle.green,
               row=0, custom_id="review_btn_all")
    async def btn_all(self, interaction: discord.Interaction, button: ui.Button):
        ctx = self._ctx(interaction.message.id)
        if not ctx:
            await interaction.response.send_message(
                "❌ Esta revisión expiró (el bot se reinició). Espera el próximo informe.",
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await _post_webhook(ctx["publish_webhook"], {"embeds": ctx["embeds"]})
            result = await self.approver._publish_social(ctx["embeds"], ctx["report_type"], message_id=str(interaction.message.id))
            await interaction.message.edit(
                content=ctx["header"] + f"\n\n📢 **Publicado en Discord y Redes** — {result}",
                view=None,
            )
            await interaction.followup.send(
                f"✅ Publicado en Discord y Redes. {result}", ephemeral=True
            )
            self.approver._remove_pending(str(interaction.message.id))
            logger.info(f"Approver: '{ctx['report_type']}' publicado en Discord y Redes ✅")
            _write_log(_SOCIAL_LOG_FILE, [
                f"DISCORD | {ctx['report_type']} | ✅ OK",
                f"REDES   | {ctx['report_type']} | {result}",
            ])
        except Exception as e:
            await interaction.followup.send(f"❌ Error al publicar: {e}", ephemeral=True)

    # ── Botón 4: Editar ───────────────────────────────────────────

    @ui.button(label="✏️ Editar y publicar", style=discord.ButtonStyle.grey,
               row=1, custom_id="review_btn_edit")
    async def btn_edit(self, interaction: discord.Interaction, button: ui.Button):
        ctx = self._ctx(interaction.message.id)
        if not ctx:
            await interaction.response.send_message(
                "❌ Esta revisión expiró.", ephemeral=True
            )
            return
        msg_id_str   = str(interaction.message.id)
        original_txt = _embeds_to_text(ctx["embeds"])

        async def pub_discord(edited_embeds: List[dict]) -> None:
            await _post_webhook(ctx["publish_webhook"], {"embeds": edited_embeds})
            self.approver._remove_pending(msg_id_str)
            logger.info(f"Approver: '{ctx['report_type']}' editado y publicado en Discord ✅")
            _write_log(_SOCIAL_LOG_FILE, [f"DISCORD | {ctx['report_type']} | ✅ OK (editado)"])

        async def pub_social(edited_embeds: List[dict]) -> str:
            return await self.approver._publish_social(edited_embeds, ctx["report_type"], message_id=msg_id_str)

        modal = EditModal(
            current_text       = original_txt[:_MAX_MODAL_CHARS],
            original_text      = original_txt,
            original_embeds    = ctx["embeds"],
            publish_discord_cb = pub_discord,
            publish_social_cb  = pub_social,
            analyze_cb         = self.approver._analyze_and_learn_from_edit,
            report_type        = ctx["report_type"],
        )
        await interaction.response.send_modal(modal)

    # ── Botón 5: Regenerar ────────────────────────────────────────

    @ui.button(label="🔄 Volver a generar", style=discord.ButtonStyle.red,
               row=1, custom_id="review_btn_regen")
    async def btn_regen(self, interaction: discord.Interaction, button: ui.Button):
        ctx = self._ctx(interaction.message.id)
        if not ctx:
            await interaction.response.send_message(
                "❌ Esta revisión expiró.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("⏳ Generando nueva versión con IA...", ephemeral=True)
        try:
            new_embeds = await self.approver._regenerate(ctx["embeds"], ctx["report_type"])
            self.approver._update_pending_embeds(str(interaction.message.id), new_embeds)
            new_view = PersistentReviewView(approver=self.approver)
            await interaction.message.edit(
                content=ctx["header"] + "\n\n🔄 *Nueva versión generada — revisa y aprueba:*",
                embeds=_to_discord_embeds(new_embeds),
                view=new_view,
            )
        except Exception as e:
            logger.error(f"Approver: error al regenerar ({ctx['report_type']}): {e}")
            await interaction.followup.send(f"❌ Error al regenerar: {e}", ephemeral=True)


# ── Clase principal ────────────────────────────────────────────────

class DiscordApprover:
    """
    Gestiona el canal de revisión de informes con botones interactivos y persistentes.
    Incluye generación de intros/hashtags para redes sociales y aprendizaje editorial.
    """

    def __init__(
        self,
        cfg:              dict,
        facebook_poster  = None,
        instagram_poster = None,
        twitter_poster   = None,
    ):
        discord_cfg = cfg.get("discord", {})
        self.bot_token      = discord_cfg.get("bot_token", "")
        self.review_webhook = discord_cfg.get("review_webhook", "")
        self.log_webhook    = discord_cfg.get("log_webhook", "")

        anthropic_cfg = cfg.get("anthropic", {})
        self.anthropic_key = anthropic_cfg.get("api_key", "")

        self.facebook_poster  = facebook_poster
        self.instagram_poster = instagram_poster
        self.twitter_poster   = twitter_poster

        self._channel_id: Optional[int] = None

        # Estado persistente de revisiones en curso
        self._pending_reviews: Dict[str, dict] = {}
        self._load_pending()

        # Imágenes en memoria para publicación social (no se persisten en JSON)
        self._pending_images: Dict[str, bytes] = {}

        # imgbb API key para subir imágenes
        self._imgbb_api_key: str = cfg.get("instagram", {}).get("imgbb_api_key", "")

        # Lecciones editoriales aprendidas de ediciones anteriores
        self._learnings: Dict[str, List[dict]] = {}
        self._load_learnings()

        self.youtube_channel_id: Optional[int] = (
            int(discord_cfg["youtube_channel_id"])
            if discord_cfg.get("youtube_channel_id")
            else None
        )
        self.youtube_publish_webhook: str = (
            discord_cfg.get("youtube_publish_webhook", "")
            or self.review_webhook
        )

        intents = discord.Intents.default()
        intents.message_content = True   # privileged intent — activar en Discord Dev Portal
        self.client = discord.Client(intents=intents)

        _self = self
        async def on_ready() -> None:
            await _self._on_ready()
        self.client.event(on_ready)

        async def on_message(message: discord.Message) -> None:
            await _self._on_message(message)
        self.client.event(on_message)

    # ── Estado persistente: revisiones pendientes ──────────────────

    def _load_pending(self) -> None:
        try:
            if os.path.exists(_PENDING_FILE):
                with open(_PENDING_FILE, encoding="utf-8") as f:
                    self._pending_reviews = json.load(f)
                if self._pending_reviews:
                    logger.info(
                        f"Discord Approver: {len(self._pending_reviews)} revisión(es) "
                        "pendiente(s) restauradas desde disco"
                    )
        except Exception as e:
            logger.warning(f"Discord Approver: no se pudo cargar pending_reviews: {e}")
            self._pending_reviews = {}

    def _save_pending(self) -> None:
        try:
            os.makedirs(os.path.dirname(_PENDING_FILE), exist_ok=True)
            with open(_PENDING_FILE, "w", encoding="utf-8") as f:
                json.dump(self._pending_reviews, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Discord Approver: no se pudo guardar pending_reviews: {e}")

    def _add_pending(self, message_id: str, data: dict) -> None:
        self._pending_reviews[message_id] = data
        self._save_pending()

    def _remove_pending(self, message_id: str) -> None:
        self._pending_reviews.pop(message_id, None)
        self._pending_images.pop(message_id, None)
        self._save_pending()

    def _update_pending_embeds(self, message_id: str, new_embeds: List[dict]) -> None:
        if message_id in self._pending_reviews:
            self._pending_reviews[message_id]["embeds"] = new_embeds
            self._save_pending()

    # ── Estado persistente: lecciones editoriales ─────────────────

    def _load_learnings(self) -> None:
        try:
            if os.path.exists(_LEARNINGS_FILE):
                with open(_LEARNINGS_FILE, encoding="utf-8") as f:
                    self._learnings = json.load(f)
                total = sum(len(v) for v in self._learnings.values())
                if total:
                    logger.info(f"Discord Approver: {total} lección(es) editorial(es) cargadas")
        except Exception as e:
            logger.warning(f"Discord Approver: no se pudo cargar learnings: {e}")
            self._learnings = {}

    def _save_learnings(self) -> None:
        try:
            os.makedirs(os.path.dirname(_LEARNINGS_FILE), exist_ok=True)
            with open(_LEARNINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._learnings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Discord Approver: no se pudo guardar learnings: {e}")

    def _format_learnings_for_prompt(self, report_type: str) -> str:
        """Devuelve las lecciones como texto para inyectar en el prompt de Claude."""
        items = self._learnings.get(report_type, [])
        if not items:
            return ""
        # Ordenar por fecha desc, tomar las más recientes
        recent = sorted(items, key=lambda x: x.get("date", ""), reverse=True)[:_MAX_LEARNINGS]
        lines = [f"- [{l.get('type','general')}] {l.get('lesson','')}" for l in recent]
        return (
            "\n\nLECCIONES APRENDIDAS DE EDICIONES ANTERIORES — aplícalas todas:\n"
            + "\n".join(lines)
        )

    # ── Eventos del bot ────────────────────────────────────────────

    async def _on_ready(self) -> None:
        """
        Al conectar, re-activar las revisiones pendientes editando el mensaje
        original con botones frescos (garantiza funcionamiento tras reinicio).
        """
        logger.info(f"Discord Approver: bot listo como {self.client.user}")
        if not self._pending_reviews:
            return

        channel = self.client.get_channel(self._channel_id) if self._channel_id else None
        reactivated = 0
        for msg_id_str in list(self._pending_reviews.keys()):
            try:
                view = PersistentReviewView(approver=self)
                self.client.add_view(view, message_id=int(msg_id_str))

                if channel:
                    try:
                        msg = await channel.fetch_message(int(msg_id_str))
                        ctx = self._pending_reviews[msg_id_str]
                        fresh_view = PersistentReviewView(approver=self)
                        await msg.edit(
                            content=ctx["header"] + "\n\n🔄 *(botones reactivados tras reinicio)*",
                            view=fresh_view,
                        )
                        reactivated += 1
                    except Exception as e_edit:
                        logger.warning(
                            f"Discord Approver: no se pudo reactivar mensaje {msg_id_str}: {e_edit}"
                        )
                        self._remove_pending(msg_id_str)
            except Exception as e:
                logger.warning(f"Discord Approver: error re-registrando {msg_id_str}: {e}")

        if reactivated:
            logger.info(
                f"Discord Approver: {reactivated} revisión(es) reactivada(s) con botones frescos"
            )

    # ── Listener de mensajes: YouTube ─────────────────────────────

    async def _on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.youtube_channel_id and message.channel.id != self.youtube_channel_id:
            return
        if not self.youtube_channel_id:
            return  # feature desactivada si no hay canal configurado
        await self._handle_youtube_message(message)

    async def _handle_youtube_message(self, message: discord.Message) -> None:
        from .youtube_processor import extract_video_id, get_content, generate_summary, build_embed

        video_id = extract_video_id(message.content)
        if not video_id:
            return

        video_url = f"https://youtu.be/{video_id}"
        logger.info(f"YouTubeProcessor: link detectado — {video_url}")

        try:
            await message.add_reaction("⏳")
        except Exception:
            pass

        content, source = await get_content(video_id)
        if not content:
            try:
                await message.remove_reaction("⏳", self.client.user)
                await message.reply(
                    "❌ No se pudo obtener información de este video. "
                    "Puede ser privado, restringido por edad, o no tener subtítulos ni descripción."
                )
            except Exception:
                pass
            return

        if not self.anthropic_key:
            try:
                await message.remove_reaction("⏳", self.client.user)
                await message.reply("❌ No hay API key de Anthropic configurada (`anthropic.api_key`).")
            except Exception:
                pass
            return

        try:
            summary = await generate_summary(content, source, video_url, self.anthropic_key)
            embed = build_embed(summary, video_url, source)

            # ── Intentar generar el reel de 45s ──────────────────────
            reel_bytes = None
            reel_script = ""
            try:
                from .video_processor import process_youtube_to_reel
                await message.reply("🎬 Generando reel de 45s… esto puede tardar 1-2 minutos")
                reel_result = await process_youtube_to_reel(video_id, self.anthropic_key)
                if reel_result.get("video_bytes"):
                    reel_bytes  = reel_result["video_bytes"]
                    reel_script = reel_result.get("script", "")
                    logger.info(f"YouTubeProcessor: reel generado ({len(reel_bytes)/1024/1024:.1f} MB)")
                elif reel_result.get("error"):
                    logger.warning(f"YouTubeProcessor: reel no generado — {reel_result['error']}")
                    await message.reply(f"⚠️ Reel no generado: {reel_result['error']}\nSe publicará solo el resumen.")
            except Exception as e_reel:
                logger.warning(f"YouTubeProcessor: error generando reel — {e_reel}")

            try:
                await message.remove_reaction("⏳", self.client.user)
                await message.add_reaction("✅")
            except Exception:
                pass

            # Si hay reel, usarlo como guión principal del embed
            if reel_script:
                embed["description"] = reel_script
                embed["title"] = "🎬 Reel de 45s listo para publicar"
                embed["footer"]["text"] = f"Fuente: {video_url} · Reel generado automáticamente"

            header = f"{'🎬' if reel_bytes else '📺'} **{'Reel' if reel_bytes else 'Video'} listo para publicar** — <{video_url}>"

            await self.post_for_review(
                embeds=[embed],
                report_type="youtube",
                publish_webhook=self.youtube_publish_webhook,
                header=header,
                social_image_bytes=reel_bytes,   # el vídeo se usará en redes sociales
            )

            # Enviar el vídeo directamente al canal de revisión si está disponible
            if reel_bytes and self._channel_id:
                channel = self.client.get_channel(self._channel_id)
                if channel:
                    await channel.send(
                        content=f"🎬 **Reel 45s** — {video_url}",
                        file=discord.File(io.BytesIO(reel_bytes), filename="reel_45s.mp4"),
                    )
            logger.info(f"YouTubeProcessor: resumen publicado para revisión — {video_url}")

        except Exception as e:
            logger.error(f"YouTubeProcessor: error procesando {video_url}: {e}")
            try:
                await message.remove_reaction("⏳", self.client.user)
                await message.reply(f"❌ Error al procesar el video: {e}")
            except Exception:
                pass

    async def start(self) -> None:
        if not self.bot_token:
            logger.warning("Discord Approver: sin bot_token — sistema de revisión desactivado")
            return
        self._channel_id = await self._fetch_channel_id(self.review_webhook)
        if not self._channel_id:
            logger.error("Discord Approver: no se pudo obtener el channel_id del review_webhook")
            return
        asyncio.ensure_future(self.client.start(self.bot_token))
        logger.info(f"Discord Approver: iniciado — canal de revisión {self._channel_id}")

    # ── API pública ────────────────────────────────────────────────

    async def post_for_review(
        self,
        embeds:           List[dict],
        report_type:      str,
        publish_webhook:  str,
        header:           str = "📋 **Informe listo para revisión**",
        target_channel_id: Optional[int] = None,
        card_image_bytes:  Optional[bytes] = None,
        social_image_bytes: Optional[bytes] = None,
    ) -> None:
        channel_id = target_channel_id if target_channel_id is not None else self._channel_id
        if not channel_id:
            logger.error("Discord Approver: canal de revisión no configurado — publicando directamente")
            await _post_webhook(publish_webhook, {"embeds": embeds})
            return

        channel = self.client.get_channel(channel_id)
        if channel is None:
            logger.error(f"Discord Approver: canal {channel_id} no encontrado en caché")
            await _post_webhook(publish_webhook, {"embeds": embeds})
            return

        view = PersistentReviewView(approver=self)
        try:
            send_kwargs: dict = {
                "content": header,
                "view": view,
            }
            if card_image_bytes is not None:
                send_kwargs["file"] = discord.File(io.BytesIO(card_image_bytes), filename="trade.png")
            else:
                send_kwargs["embeds"] = _to_discord_embeds(embeds)

            msg = await channel.send(**send_kwargs)
            self._add_pending(str(msg.id), {
                "report_type":     report_type,
                "publish_webhook": publish_webhook,
                "embeds":          embeds,
                "header":          header,
            })
            if social_image_bytes is not None:
                self._pending_images[str(msg.id)] = social_image_bytes
            logger.info(f"Discord Approver: '{report_type}' publicado para revisión (msg {msg.id})")
        except Exception as e:
            logger.error(f"Discord Approver: error posteando revisión: {e}")
            await _post_webhook(publish_webhook, {"embeds": embeds})

    # ── Generación de texto para redes sociales ────────────────────

    async def _generate_social_post(
        self, embeds: List[dict], report_type: str, network: str
    ) -> str:
        """
        Genera texto optimizado para la red social indicada:
        intro atractiva + contenido + hashtags específicos por red.
        Usa Claude si hay api_key; si no, texto base + hashtags por defecto.
        """
        base_text = _embeds_to_text(embeds)

        if not self.anthropic_key:
            # Fallback sin Claude
            return f"{base_text}\n\n{_HASHTAG_DEFAULTS.get(network, '')}"

        import anthropic

        # Para YouTube: el resumen ya es el contenido, adapt a red social directamente
        if report_type == "youtube":
            network_tone = {
                "twitter":   "directo e impactante, como si estuvieras compartiendo un hallazgo valioso",
                "instagram": "cercano y visual, usa emojis con moderación, invita a ver el video",
                "facebook":  "reflexivo y accesible, invita al debate sobre el tema del video",
            }.get(network, "atractivo y conciso")
        elif report_type == "operacion":
            network_tone = {
                "twitter":   "directo e impactante, genera curiosidad sobre la operación",
                "instagram": "cercano y visual, invita a unirse a la comunidad",
                "facebook":  "profesional y accesible, invita a conocer más sobre la estrategia",
            }.get(network, "atractivo y directo")
        else:
            network_tone = {
                "twitter":   "directo, impactante, cada tweet del hilo debe funcionar solo (el hilo puede tener varios tweets)",
                "instagram": "cercano, visual, usa emojis con moderación, invita a seguir la cuenta",
                "facebook":  "profesional pero accesible, más extenso, invita al debate o comentarios",
            }.get(network, "profesional y cercano")

        hashtag_suggestions = " ".join(_HASHTAG_SUGGESTIONS.get(network, []))
        learnings = self._format_learnings_for_prompt(report_type)

        # Para Instagram: separador especial + estructura según tipo de informe
        ig_format_note = ""
        if network == "instagram":
            ig_format_note = (
                "\n\nFORMATO OBLIGATORIO para Instagram:\n"
                "- Escribe primero la INTRODUCCIÓN y el CONTENIDO adaptado (sin hashtags)\n"
                "- Luego añade una línea que diga exactamente: ===HASHTAGS===\n"
                "- Debajo pon los 20-25 hashtags en un bloque continuo\n"
            )
            if report_type == "premarket":
                ig_format_note += (
                    "\nESTRUCTURA OBLIGATORIA del CONTENIDO para informe PRE-APERTURA:\n"
                    "1. Sección 'FUTUROS' con UNA línea por índice en este formato EXACTO "
                    "(el parser de imagen lo requiere — no cambies el formato ni añadas $ delante):\n"
                    "   S&P 500   [valor numérico]   [+X.XX%]\n"
                    "   Nasdaq    [valor numérico]   [+X.XX%]\n"
                    "   VIX       [valor numérico]\n"
                    "2. Sección 'AGENDA MACRO' con los eventos del día (una línea por evento, con •)\n"
                    "   Si no hay eventos: escribe '• Sin eventos macro relevantes hoy'\n"
                    "3. Si hay titular de noticias relevante: sección 'TITULAR' con el texto entre comillas\n"
                )
            elif report_type == "postmarket":
                ig_format_note += (
                    "\nESTRUCTURA OBLIGATORIA del CONTENIDO para informe CIERRE DE MERCADO:\n"
                    "1. Sección 'MERCADOS' con UNA línea por índice en este formato EXACTO "
                    "(el parser de imagen lo requiere — no cambies el formato ni añadas $ delante):\n"
                    "   S&P 500   [valor numérico]   [+X.XX%]\n"
                    "   Nasdaq    [valor numérico]   [+X.XX%]\n"
                    "   VIX       [valor numérico]\n"
                    "2. Sección 'OPERACIONES MTO' con las operaciones del día (apertura/cierre/roll)\n"
                    "   Si no hay operaciones: escribe '• Sin operaciones registradas hoy'\n"
                )

        # Instrucciones adicionales específicas por tipo de informe
        operacion_extra = ""
        if report_type == "operacion":
            operacion_extra = (
                "\n\nINSTRUCCIONES ESPECIALES PARA PUBLICACIÓN DE OPERACIÓN:\n"
                "Esta publicación muestra una operación real de nuestra cartera. El texto debe:\n"
                "1. VARIAR en cada publicación — no uses siempre las mismas frases de inicio\n"
                "2. Mencionar si es APERTURA o CIERRE (léelo del embed: 'APERTURA DE OPERACION' o 'CIERRE DE OPERACION')\n"
                "3. Para APERTURA: destacar la prima cobrada (campo 'Prima +/- comision') y el ticker con $\n"
                "   Ej: 'Nueva operación abierta en $TICKER — hemos cobrado $XXX de prima'\n"
                "4. Para CIERRE: destacar el resultado (campo 'Resultado') positivo o negativo\n"
                "   Ej: 'Cerramos $TICKER con +$XXX de beneficio' / 'Cerramos $TICKER asumiendo -$XXX'\n"
                "5. Mencionar que esto ocurre en nuestro canal PRIVADO de Discord\n"
                "6. SIEMPRE terminar con esta frase (o variante): "
                "'Escanea el QR de la imagen o visita mtoopciones.com para unirte a la comunidad'\n"
                "7. NO incluyas strikes, DTE, buying power ni detalles técnicos — solo lo esencial\n"
                "8. Tono: cercano, como compartiendo un logro con la comunidad\n"
            )

        # Regla de cashtags diferenciada por red
        if network == "twitter":
            _cashtag_rule = (
                "REGLA CASHTAGS para Twitter/X: Twitter permite MÁXIMO 1 cashtag ($SÍMBOLO) por tweet — "
                "si pones más de uno el tweet falla con error 403. "
                "USA $ SOLO para el ticker principal de la operación (ej: $CELH, $SOFI, $IBIT). "
                "Para índices escribe el nombre COMPLETO SIN $: 'S&P 500', 'Nasdaq', 'VIX', 'SPX'. "
                "NUNCA pongas $SPX, $NDX, $QQQ, $SPY ni $VIX — violan la norma de Twitter.\n\n"
            )
        else:
            _cashtag_rule = (
                "REGLA CASHTAGS: Usa $TICKER para acciones y ETFs operados (ej: $CELH, $SOFI, $IBIT). "
                "Para índices escribe el nombre completo SIN $: 'S&P 500', 'Nasdaq', 'VIX' "
                "(el parser de la imagen de Instagram necesita esos nombres exactos para mostrar los datos).\n\n"
            )

        prompt = (
            f"Eres el editor de contenido de MTO Opciones, comunidad de trading de opciones "
            f"para hispanohablantes (España y Latinoamérica).\n\n"
            f"INFORME ORIGINAL:\n---\n{base_text}\n---\n\n"
            f"Tu tarea: crear el texto completo para publicar en {network.upper()}.\n\n"
            f"{operacion_extra}"
            f"{_cashtag_rule}"
            f"FORMATO OBLIGATORIO:\n"
            f"1. INTRODUCCIÓN: 1-2 frases que enganchen ({network_tone})\n"
            f"2. CONTENIDO: el informe adaptado levemente al tono de {network} "
            f"(mantén TODOS los datos y porcentajes exactos, sin inventar nada)\n"
            f"3. HASHTAGS: {_HASHTAG_RULES.get(network, '3-5 hashtags')}\n"
            f"{ig_format_note}\n"
            f"Hashtags de referencia para nuestro sector (selecciona y adapta los más relevantes):\n"
            f"{hashtag_suggestions}\n"
            f"{learnings}\n\n"
            f"Devuelve SOLO el texto final listo para copiar y pegar en {network}. "
            f"Sin explicaciones, sin metaetiquetas."
        )

        client = anthropic.Anthropic(api_key=self.anthropic_key)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1200 if network == "twitter" else 3000,
                messages=[{"role": "user", "content": prompt}],
            ),
        )
        return response.content[0].text.strip()

    # ── Publicación en redes sociales ──────────────────────────────

    async def _publish_social(self, embeds: List[dict], report_type: str, message_id: Optional[str] = None) -> str:
        """
        Publica en Twitter, Instagram y Facebook con intro + hashtags generados por IA.
        Registra cada publicación en logs/social_posts.log.
        """
        results   = []
        log_lines = []
        today     = datetime.now().strftime("%Y-%m-%d")

        # Recuperar imagen social si está disponible en memoria
        social_image_bytes: Optional[bytes] = None
        if message_id and report_type == "operacion":
            social_image_bytes = self._pending_images.get(message_id)

        # ── Twitter (hilo) ────────────────────────────────────────────
        if self.twitter_poster:
            try:
                from .twitter_poster import split_for_thread
                text  = await self._generate_social_post(embeds, report_type, "twitter")
                parts = split_for_thread(text, max_chars=270)
                if report_type == "operacion" and social_image_bytes is not None:
                    ok = await self.twitter_poster.post_thread_with_media(parts, social_image_bytes)
                else:
                    ok = await self.twitter_poster.post_thread(parts)
                status = "✅" if ok else "❌"
                n_tweets = len(parts)
                results.append(f"Twitter: {status} ({n_tweets} tweet{'s' if n_tweets > 1 else ''})")
                log_lines.append(
                    f"TWITTER | {report_type} | {status} | hilo {n_tweets}t | {text[:80].replace(chr(10),' ')}..."
                )
                logger.info(f"Approver social: Twitter '{report_type}' {status} ({n_tweets} tweets)")
            except Exception as e:
                results.append(f"Twitter: ❌ ({e})")
                log_lines.append(f"TWITTER | {report_type} | ❌ ERROR | {e}")
                logger.error(f"Approver social: Twitter error — {e}")

        # ── Instagram ────────────────────────────────────────────────
        if self.instagram_poster:
            try:
                text = await self._generate_social_post(embeds, report_type, "instagram")
                text = text[:_MAX_INSTAGRAM]
                if report_type == "operacion" and social_image_bytes is not None:
                    image_url = await self._upload_to_imgbb(social_image_bytes)
                    if image_url:
                        ok = await self.instagram_poster.post_image(image_url, caption=text)
                    else:
                        ok = await self.instagram_poster.post_text(text, report_type=report_type)
                else:
                    ok = await self.instagram_poster.post_text(text, report_type=report_type)
                status = "✅" if ok else "❌"
                results.append(f"Instagram: {status}")
                log_lines.append(
                    f"INSTAGRAM | {report_type} | {status} | {text[:80].replace(chr(10),' ')}..."
                )
                logger.info(f"Approver social: Instagram '{report_type}' {status}")
            except Exception as e:
                results.append(f"Instagram: ❌ ({e})")
                log_lines.append(f"INSTAGRAM | {report_type} | ❌ ERROR | {e}")
                logger.error(f"Approver social: Instagram error — {e}")

        # ── Facebook ─────────────────────────────────────────────────
        if self.facebook_poster:
            try:
                text = await self._generate_social_post(embeds, report_type, "facebook")
                text = text[:_MAX_FACEBOOK]
                if report_type == "operacion" and social_image_bytes is not None:
                    image_url = await self._upload_to_imgbb(social_image_bytes)
                    if image_url:
                        ok = await self.facebook_poster.post_image(image_url, caption=text)
                    else:
                        ok = await self.facebook_poster.post_text(text)
                else:
                    ok = await self.facebook_poster.post_text(text)
                status = "✅" if ok else "❌"
                results.append(f"Facebook: {status}")
                log_lines.append(
                    f"FACEBOOK | {report_type} | {status} | {text[:80].replace(chr(10),' ')}..."
                )
                logger.info(f"Approver social: Facebook '{report_type}' {status}")
            except Exception as e:
                results.append(f"Facebook: ❌ ({e})")
                log_lines.append(f"FACEBOOK | {report_type} | ❌ ERROR | {e}")
                logger.error(f"Approver social: Facebook error — {e}")

        if log_lines:
            _write_log(_SOCIAL_LOG_FILE, log_lines)

        asyncio.ensure_future(self._log_social_to_discord(report_type, results))

        result_str = " · ".join(results) if results else "Sin redes configuradas"
        return result_str

    # ── Log de publicaciones en redes al canal log-bot ────────────

    async def _log_social_to_discord(
        self, report_type: str, results: list
    ) -> None:
        """
        Envía un embed al canal log-bot con el resultado de cada publicación
        en redes sociales (✅/❌ por red, timestamp, tipo de informe).
        """
        if not self.log_webhook or not results:
            return

        ok_count  = sum(1 for r in results if "✅" in r)
        total     = len(results)
        all_ok    = ok_count == total
        some_ok   = ok_count > 0

        color = 0x2ECC71 if all_ok else (0xF39C12 if some_ok else 0xE74C3C)
        icon  = "✅" if all_ok else ("⚠️" if some_ok else "❌")

        lines = "\n".join(f"• {r}" for r in results)
        embed = {
            "title":       f"{icon}  Redes Sociales — {report_type}",
            "description": lines,
            "color":       color,
            "timestamp":   datetime.utcnow().isoformat() + "Z",
            "footer":      {"text": "MTO Bot · publicación automática"},
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    self.log_webhook,
                    json={"embeds": [embed]},
                ) as resp:
                    if resp.status not in (200, 204):
                        text = await resp.text()
                        logger.warning(
                            f"Approver: log_webhook HTTP {resp.status}: {text[:100]}"
                        )
        except Exception as e:
            logger.warning(f"Approver: error enviando log a Discord: {e}")

    # ── Aprendizaje editorial ──────────────────────────────────────

    async def _analyze_and_learn_from_edit(
        self, original: str, edited: str, report_type: str
    ) -> None:
        """
        Analiza qué cambió el usuario al editar el informe y guarda las lecciones.
        Se ejecuta en background (fire-and-forget) para no bloquear la UI.
        """
        if not self.anthropic_key:
            logger.debug("Approver learnings: sin api_key de Anthropic, análisis omitido")
            return

        try:
            import anthropic

            prompt = (
                "Analiza las diferencias entre el texto ORIGINAL generado por el bot "
                "y la versión EDITADA por el usuario para un informe de tipo "
                f"'{report_type}' de MTO Opciones (comunidad de trading de opciones).\n\n"
                f"TEXTO ORIGINAL:\n---\n{original}\n---\n\n"
                f"TEXTO EDITADO:\n---\n{edited}\n---\n\n"
                "Identifica TODOS los cambios y clasifícalos. Para cada cambio genera "
                "una lección concreta y accionable que el bot deba aplicar en el futuro.\n\n"
                "Tipos de lección:\n"
                "- data: corrección de datos (cotizaciones, %, valores)\n"
                "- style: cambio de estilo, tono o vocabulario\n"
                "- removal: contenido o fuente que se eliminó y no se debe volver a incluir\n"
                "- addition: algo que el usuario añadió que debería incluirse por defecto\n\n"
                "Responde ÚNICAMENTE con un JSON válido con esta estructura:\n"
                '{"lessons": [{"type": "...", "lesson": "descripción concreta y accionable"}], '
                '"summary": "resumen en 2-3 frases de qué se cambió y por qué"}'
            )

            client = anthropic.Anthropic(api_key=self.anthropic_key)
            loop   = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.messages.create(
                    model="claude-opus-4-5",
                    max_tokens=1500,
                    messages=[{"role": "user", "content": prompt}],
                ),
            )
            raw = response.content[0].text.strip()

            # Parsear JSON (Claude puede añadir markdown ```json ... ```)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed   = json.loads(raw.strip())
            lessons  = parsed.get("lessons", [])
            summary  = parsed.get("summary", "")

            # Guardar lecciones
            today    = datetime.now().strftime("%Y-%m-%d")
            bucket   = self._learnings.setdefault(report_type, [])
            for lesson in lessons:
                bucket.append({
                    "date":   today,
                    "type":   lesson.get("type", "general"),
                    "lesson": lesson.get("lesson", ""),
                })
            # Limitar a 100 lecciones por tipo (descartar las más antiguas)
            if len(bucket) > 100:
                self._learnings[report_type] = bucket[-100:]
            self._save_learnings()

            # Log de cambios en archivo
            log_lines = [
                f"TIPO: {report_type} | {len(lessons)} lección(es) aprendida(s)",
                f"RESUMEN: {summary}",
            ] + [f"  [{l.get('type')}] {l.get('lesson')}" for l in lessons]
            _write_log(_EDIT_LOG_FILE, log_lines)

            logger.info(
                f"Approver learnings: {len(lessons)} lección(es) guardada(s) "
                f"para '{report_type}' — {summary[:100]}"
            )

            # ── Notificación al canal de log ──────────────────────────
            if self.log_webhook and lessons:
                await self._notify_learning(report_type, summary, lessons)

        except Exception as e:
            logger.error(f"Approver learnings: error analizando edición — {e}")

    async def _notify_learning(
        self, report_type: str, summary: str, lessons: list
    ) -> None:
        """Envía al canal log-bot un embed explicando qué detectó y qué mejora aplicará."""
        _TYPE_EMOJI = {
            "style":    "🎨",
            "data":     "📊",
            "removal":  "🗑️",
            "addition": "➕",
            "general":  "💡",
        }
        _TYPE_LABEL = {
            "style":    "Estilo / tono",
            "data":     "Datos / cifras",
            "removal":  "Contenido eliminado",
            "addition": "Contenido añadido",
            "general":  "General",
        }

        total_prev = sum(
            len(v) for v in self._learnings.values()
        )

        # Agrupar lecciones por tipo
        by_type: dict = {}
        for l in lessons:
            t = l.get("type", "general")
            by_type.setdefault(t, []).append(l.get("lesson", ""))

        fields = []
        for t, items in by_type.items():
            emoji = _TYPE_EMOJI.get(t, "💡")
            label = _TYPE_LABEL.get(t, t.capitalize())
            value = "\n".join(f"• {i}" for i in items)[:1020]
            fields.append({"name": f"{emoji} {label}", "value": value, "inline": False})

        fields.append({
            "name": "📚 Lecciones acumuladas",
            "value": f"`{total_prev}` lecciones guardadas para publicaciones futuras de tipo `{report_type}`",
            "inline": False,
        })

        embed = {
            "title": f"🧠 Aprendizaje editorial — `{report_type}`",
            "description": (
                f"**He detectado {len(lessons)} cambio(s) en tu edición** y los he guardado "
                f"como reglas que aplicaré automáticamente en próximas publicaciones.\n\n"
                f"**Resumen:** {summary}"
            ),
            "color": 0x9B59B6,
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "footer": {"text": "MTO Bot · aprendizaje editorial automático"},
        }

        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    self.log_webhook,
                    json={"embeds": [embed]},
                ) as resp:
                    if resp.status not in (200, 204):
                        text = await resp.text()
                        logger.warning(f"Approver learning notify: HTTP {resp.status}: {text[:100]}")
        except Exception as e:
            logger.warning(f"Approver learning notify: error — {e}")

    # ── Regeneración con IA ────────────────────────────────────────

    async def _regenerate(self, current_embeds: List[dict], report_type: str) -> List[dict]:
        """Reescribe el informe con Claude aplicando las lecciones editoriales aprendidas."""
        if not self.anthropic_key:
            raise RuntimeError("No hay api_key de Anthropic en config.yaml (sección 'anthropic:')")

        import anthropic

        current_text = _embeds_to_text(current_embeds)
        learnings    = self._format_learnings_for_prompt(report_type)

        prompt = (
            "Eres un experto en análisis de mercados financieros y opciones. "
            "Perteneces al equipo editorial de MTO Opciones, una comunidad de traders "
            "hispanohablantes. Tengo el siguiente informe de mercado ya redactado. "
            "Reescríbelo con un estilo diferente: más dinámico, frases más cortas, "
            "tono más directo y cercano, pero manteniendo TODOS los datos y porcentajes exactos. "
            "No inventes datos, solo cambia la forma de expresarlos.\n\n"
            f"INFORME ACTUAL:\n{current_text}"
            f"{learnings}"
        )

        client = anthropic.Anthropic(api_key=self.anthropic_key)
        loop   = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                model="claude-opus-4-5",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            ),
        )
        new_text = response.content[0].text

        new_embeds = [dict(e) for e in current_embeds]
        if new_embeds:
            new_embeds[0] = dict(new_embeds[0])
            new_embeds[0]["description"] = new_text[:4000]

        return new_embeds

    # ── Helpers ────────────────────────────────────────────────────

    async def _upload_to_imgbb(self, image_bytes: bytes) -> Optional[str]:
        """
        Sube image_bytes a imgbb.com y devuelve la display_url pública.
        Requiere imgbb_api_key en config.yaml (gratis en https://api.imgbb.com/).
        """
        if not self._imgbb_api_key:
            logger.warning(
                "DiscordApprover: sin imgbb_api_key — imagen no se puede subir a imgbb. "
                "Configura 'instagram.imgbb_api_key' en config.yaml"
            )
            return None
        try:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.imgbb.com/1/upload",
                    data={"key": self._imgbb_api_key, "image": b64},
                ) as resp:
                    data = await resp.json()
                    if data.get("success"):
                        url = data["data"]["display_url"]
                        logger.debug(f"DiscordApprover: imagen subida a imgbb → {url}")
                        return url
                    logger.error(f"DiscordApprover: imgbb error: {data}")
        except Exception as e:
            logger.error(f"DiscordApprover: imgbb upload exception: {e}")
        return None

    async def _fetch_channel_id(self, webhook_url: str) -> Optional[int]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(webhook_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return int(data.get("channel_id", 0)) or None
                    logger.warning(f"Approver: webhook API devolvió {resp.status}")
        except Exception as e:
            logger.error(f"Approver: error obteniendo channel_id: {e}")
        return None


# ── Utilidad de posting ────────────────────────────────────────────

async def _post_webhook(url: str, payload: dict) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                raise RuntimeError(f"Webhook HTTP {resp.status}: {text[:200]}")



