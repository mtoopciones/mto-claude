"""
Publicación automática en Instagram para reportes MTO.

Usa la Instagram Graph API con un User Access Token generado
desde Meta for Developers para publicar en @mtoopciones.

IMPORTANTE — Instagram NO permite posts de solo texto en el feed.
  Todos los posts deben ser imágenes o vídeos.
  Este módulo genera una imagen branded (1080x1080) con el contenido
  del informe usando Pillow y la sube a imgbb.com para obtener una URL
  pública que Instagram pueda descargar.

  Para habilitar la subida de imágenes, configura en config.yaml:
    instagram:
      imgbb_api_key: "TU-CLAVE-IMGBB"    ← obtener gratis en https://api.imgbb.com/

  Los hashtags se publican como el PRIMER COMENTARIO del post (estrategia
  recomendada para maximizar alcance sin saturar la descripción visual).

Dependencias:  pip install Pillow aiohttp
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import textwrap
from datetime import datetime, timedelta
from typing import Optional, Tuple

import aiohttp
from loguru import logger

GRAPH_URL         = "https://graph.instagram.com/v21.0"
TOKEN_EXPIRY_DAYS = 60   # los tokens de Instagram duran 60 días
RENEW_BEFORE_DAYS = 10   # renovar cuando queden menos de 10 días

# Separador que usa discord_approver para marcar dónde empiezan los hashtags
_HASHTAG_SEP = "===HASHTAGS==="

# Colores del tema MTO Opciones
_BG_COLOR    = (15,  23,  42)    # navy oscuro
_ACCENT_CLR  = (250, 176,  5)    # dorado
_TEXT_CLR    = (241, 245, 249)   # blanco suave
_SUB_CLR     = (148, 163, 184)   # gris claro
_LINE_CLR    = (30,  41,  59)    # línea separadora

# Fuentes DejaVu (disponibles en Ubuntu/Debian sin configuración)
_FONT_BOLD   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REG    = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


class InstagramPoster:
    """Cliente para publicar en Instagram @mtoopciones via Graph API."""

    def __init__(
        self,
        app_id:         str,
        app_secret:     str,
        access_token:   str,
        token_file:     str = "data/instagram_token.json",
        imgbb_api_key:  str = "",
        logo_url:       str = "",
    ):
        self._app_id        = app_id
        self._app_secret    = app_secret
        self._access_token  = access_token
        self._token_file    = token_file
        self._imgbb_api_key = imgbb_api_key
        self._logo_url      = logo_url
        self._logo_img      = None          # cache en memoria
        self._ig_user_id:   Optional[str]      = None
        self._token_expiry: Optional[datetime] = None
        self._load_token_from_file()

    # ── Persistencia del token ────────────────────────────────

    def _load_token_from_file(self) -> None:
        try:
            if os.path.exists(self._token_file):
                with open(self._token_file, "r") as f:
                    data = json.load(f)
                self._access_token = data.get("access_token", self._access_token)
                expiry_str = data.get("expiry")
                if expiry_str:
                    self._token_expiry = datetime.fromisoformat(expiry_str)
                logger.info(
                    f"Instagram: token cargado del archivo "
                    f"(expira {self._token_expiry.strftime('%d/%m/%Y') if self._token_expiry else '?'})"
                )
        except Exception as e:
            logger.debug(f"Instagram: no se pudo cargar token guardado: {e}")

    def _save_token_to_file(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._token_file), exist_ok=True)
            with open(self._token_file, "w") as f:
                json.dump({
                    "access_token": self._access_token,
                    "expiry": self._token_expiry.isoformat() if self._token_expiry else None,
                    "updated_at": datetime.now().isoformat(),
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Instagram: error guardando token: {e}")

    async def refresh_token_if_needed(self) -> bool:
        """Renueva el token si quedan menos de 10 días para que caduque."""
        if self._token_expiry is None:
            # Primera vez: establecer expiración a 60 días desde ahora
            self._token_expiry = datetime.now() + timedelta(days=TOKEN_EXPIRY_DAYS)
            self._save_token_to_file()

        days_left = (self._token_expiry - datetime.now()).days
        if days_left > RENEW_BEFORE_DAYS:
            logger.debug(f"Instagram: token OK, expira en {days_left} días")
            return True

        logger.info(f"Instagram: renovando token (quedan {days_left} días)…")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{GRAPH_URL}/refresh_access_token",
                    params={
                        "grant_type":   "ig_refresh_token",
                        "access_token": self._access_token,
                    }
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"Instagram token refresh error {resp.status}: {text[:200]}")
                        return False
                    data = await resp.json()
                    self._access_token = data["access_token"]
                    expires_in = data.get("expires_in", TOKEN_EXPIRY_DAYS * 86400)
                    self._token_expiry = datetime.now() + timedelta(seconds=expires_in)
                    self._save_token_to_file()
                    logger.info(
                        f"Instagram: ✅ token renovado, expira el "
                        f"{self._token_expiry.strftime('%d/%m/%Y')}"
                    )
                    return True
        except Exception as e:
            logger.error(f"Instagram token refresh exception: {e}")
            return False

    # ── Setup ─────────────────────────────────────────────────

    async def setup(self) -> bool:
        """Obtiene el IG User ID y verifica la conexión."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{GRAPH_URL}/me",
                    params={
                        "fields": "id,username",
                        "access_token": self._access_token,
                    }
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"Instagram setup error {resp.status}: {text[:200]}")
                        return False
                    data = await resp.json()
                    self._ig_user_id = data.get("id")
                    username = data.get("username", "?")
                    logger.info(
                        f"InstagramPoster: conectado como @{username} "
                        f"(id={self._ig_user_id})"
                    )
                    return True
        except Exception as e:
            logger.error(f"InstagramPoster setup error: {e}")
            return False

    def is_ready(self) -> bool:
        return bool(self._ig_user_id and self._access_token)

    # ── API pública ───────────────────────────────────────────

    async def post_image(self, image_url: str, caption: str = "") -> bool:
        """
        Publica una imagen en Instagram.
        image_url debe ser una URL pública accesible (https).
        caption puede incluir hashtags.
        """
        if not self.is_ready():
            logger.warning("InstagramPoster: no configurado, post ignorado")
            return False
        if len(caption) > 2200:
            caption = caption[:2197] + "..."
        try:
            async with aiohttp.ClientSession() as s:
                # Paso 1: crear el container de media
                async with s.post(
                    f"{GRAPH_URL}/{self._ig_user_id}/media",
                    params={
                        "image_url":    image_url,
                        "caption":      caption,
                        "access_token": self._access_token,
                    }
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200 or "id" not in data:
                        logger.error(f"Instagram media container error: {data}")
                        return False
                    container_id = data["id"]

                # Paso 2: publicar el container
                await asyncio.sleep(2)   # Meta recomienda esperar antes de publicar
                async with s.post(
                    f"{GRAPH_URL}/{self._ig_user_id}/media_publish",
                    params={
                        "creation_id":  container_id,
                        "access_token": self._access_token,
                    }
                ) as resp2:
                    data2 = await resp2.json()
                    if resp2.status != 200 or "id" not in data2:
                        logger.error(f"Instagram publish error: {data2}")
                        return False
                    post_id = data2["id"]
                    logger.info(
                        f"InstagramPoster: ✅ imagen publicada "
                        f"(id={post_id}, caption={caption[:50]}…)"
                    )
                    return True
        except Exception as e:
            logger.error(f"InstagramPoster post_image error: {e}")
            return False

    # ── Logo MTO ─────────────────────────────────────────────

    def _get_logo_img(self):
        """
        Carga y cachea el logo de MTO Opciones.
        Estrategia:
          1. Carga desde data/mto_logo.png (ya descargado al arrancar).
          2. Si no existe, descarga desde logo_url con SSL permisivo.
          3. Detecta y elimina fondo blanco/gris si lo hubiera.
        Devuelve imagen RGBA lista para pegar, o None si falla.
        """
        if self._logo_img is not None:
            return self._logo_img
        try:
            from PIL import Image as _PIL, ImageChops

            logo_file = "data/mto_logo.png"
            if os.path.exists(logo_file):
                logo = _PIL.open(logo_file).convert("RGBA")
            elif self._logo_url:
                import urllib.request, ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode    = ssl.CERT_NONE
                req = urllib.request.Request(
                    self._logo_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                    raw = r.read()
                logo = _PIL.open(io.BytesIO(raw)).convert("RGBA")
                try:
                    os.makedirs("data", exist_ok=True)
                    logo.save(logo_file)
                except Exception:
                    pass
            else:
                return None

            # Detectar y eliminar fondo blanco / casi-blanco (si lo hubiera)
            corner = logo.getpixel((4, 4))
            if corner[3] > 200 and corner[0] > 230 and corner[1] > 230 and corner[2] > 230:
                r_ch, g_ch, b_ch, a_ch = logo.split()
                r_m = r_ch.point(lambda x: 255 if x > 228 else 0)
                g_m = g_ch.point(lambda x: 255 if x > 228 else 0)
                b_m = b_ch.point(lambda x: 255 if x > 228 else 0)
                white_mask = ImageChops.multiply(ImageChops.multiply(r_m, g_m), b_m)
                inv_mask   = white_mask.point(lambda x: 255 - x)
                logo.putalpha(ImageChops.multiply(a_ch, inv_mask))

            # Redimensionar a 78 px de alto preservando aspecto
            target_h = 78
            target_w = int(target_h * logo.width / logo.height)
            logo = logo.resize((target_w, target_h), _PIL.LANCZOS)

            self._logo_img = logo
            logger.info(f"InstagramPoster: logo OK ({target_w}×{target_h})")
            return logo

        except Exception as e:
            logger.warning(f"InstagramPoster: error cargando logo: {e}")
            return None

    # ── QR code ───────────────────────────────────────────────

    def _get_qr_img(self, size: int = 80):
        """
        Devuelve una imagen RGBA con el QR de www.mtoopciones.com.
        Usa caché en data/qr_mto.png; descarga desde api.qrserver.com si no existe.
        """
        qr_file = "data/qr_mto.png"
        try:
            from PIL import Image as _PIL
            if os.path.exists(qr_file):
                qr = _PIL.open(qr_file).convert("RGBA")
                return qr.resize((size, size), _PIL.LANCZOS)
            import urllib.request, ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            # Fondo blanco + módulos naranjas (colores MTO)
            qr_url = (
                "https://api.qrserver.com/v1/create-qr-code/"
                "?size=160x160"
                "&data=https%3A%2F%2Fwww.mtoopciones.com"
                "&bgcolor=ffffff"
                "&color=121212"
                "&qzone=1"
            )
            req = urllib.request.Request(qr_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                raw = r.read()
            qr = _PIL.open(io.BytesIO(raw)).convert("RGBA")
            try:
                os.makedirs("data", exist_ok=True)
                qr.save(qr_file)
            except Exception:
                pass
            return qr.resize((size, size), _PIL.LANCZOS)
        except Exception as e:
            logger.warning(f"InstagramPoster: error generando QR: {e}")
            return None

    # ── Parseo de datos de mercado ────────────────────────────

    def _parse_market_data(self, text: str) -> dict:
        """
        Extrae índices de mercado y operaciones del texto del informe.
        Soporta múltiples formatos generados por Claude.
        """
        data: dict = {"indices": [], "closes": [], "opens": [], "rolls": []}

        NUM = r'[\d.,]+'
        PCT = r'[+\-][\d.,]+\s*%'

        # Buscar datos de índices LÍNEA A LÍNEA para evitar falsos positivos
        # en el texto introductorio (p.ej. "S&P 500 plano (+0.02%)...")
        for name, pat in [
            ("S&P 500", r'S&P\s*500'),
            ("NASDAQ",  r'[Nn]asdaq'),
            ("VIX",     r'VIX'),
        ]:
            for line in text.split('\n'):
                # Eliminar emojis/símbolos de flecha antes de parsear
                clean_line = re.sub(r'[*_`▲▼◆►◄·]', '', line).strip()
                m = re.match(rf'{pat}\s+({NUM})(?:\s+({PCT}))?', clean_line)
                if m:
                    value  = m.group(1)
                    change = (m.group(2) or "").strip()
                    data["indices"].append({
                        "name":     name,
                        "value":    value,
                        "change":   change,
                        "positive": change.startswith('+') if change else None,
                    })
                    break

        section = None
        for line in text.split('\n'):
            clean = re.sub(r'[*_`]', '', line).strip()
            cl    = clean.lower()
            if re.search(r'\bcierre',   cl): section = 'closes'; continue
            if re.search(r'\bapertura', cl): section = 'opens';  continue
            if re.search(r'\broll\b',   cl): section = 'rolls';  continue
            if re.search(r'\bmercado|\bíndice|\bwall|\bprimas?\b', cl): section = None; continue
            if section and clean:
                # Quitar bullets/emojis al inicio
                item = re.sub(
                    r'^[\s•·*\-–—\U0001F534\U0001F7E2\U0001F504\U0001F4B0]+',
                    '', clean
                ).strip()
                if not item:
                    continue
                m2 = re.match(r'([A-Z]{2,6})\s*[·\-–—]\s*(.+)', item)
                if m2:
                    data[section].append({
                        "symbol":  m2.group(1),
                        "account": m2.group(2).strip(),
                    })
                elif re.match(r'^[A-Z]{2,6}$', item):
                    data[section].append({"symbol": item, "account": ""})

        # ── Primas ────────────────────────────────────────────────────
        primas: dict = {}
        cob = re.search(r'[Cc]obradas?\s*[:：]\s*([+\-]?\$?[\d.,]+)', text)
        pag = re.search(r'[Pp]agadas?\s*[:：]\s*([+\-]?\$?[\d.,]+)',  text)
        net = re.search(r'[Nn]eto?\s*[:：]\s*([+\-]?\$?[\d.,]+)',     text)
        if cob: primas["cobradas"] = cob.group(1)
        if pag: primas["pagadas"]  = pag.group(1)
        if net: primas["neto"]     = net.group(1)
        data["primas"] = primas

        # ── Agenda macro (pre-mercado) ─────────────────────────────────
        macro_events: list = []
        in_agenda = False
        for line in text.split('\n'):
            clean = re.sub(r'[*_`]', '', line).strip()
            cl    = clean.lower()
            # Activar sección agenda
            if re.search(r'\bagenda\b|\bmacro\b|\beventos?\b', cl) and \
               not re.search(r'\bfuturos?\b|\bíndice', cl):
                in_agenda = True
                continue
            # Desactivar sección al llegar a otra cabecera
            if in_agenda and re.search(
                r'\bfuturos?\b|\btitular\b|\bnoticias?\b|\bmercados?\b'
                r'|\bcierres?\b|\baperturas?\b|\brolls?\b|\bprimas?\b', cl
            ):
                in_agenda = False
                continue
            if in_agenda and clean:
                item = re.sub(r'^[•·\-\*📅📆\s]+', '', clean).strip()
                if item and len(item) > 4:
                    macro_events.append(item)
        data["macro_events"] = macro_events[:6]

        # ── Titular de noticias (pre-mercado) ──────────────────────────
        headline = ""
        # Buscar entre comillas tipográficas o normales
        hl_m = re.search(r'["“«](.*?)["”»]', text)
        if hl_m:
            headline = hl_m.group(1).strip()[:140]
        else:
            # Fallback: línea debajo de sección TITULAR
            in_titular = False
            for line in text.split('\n'):
                clean = re.sub(r'[*_`]', '', line).strip()
                if re.search(r'\btitular\b', clean.lower()):
                    in_titular = True
                    continue
                if in_titular and clean:
                    headline = clean[:140]
                    break
        data["headline"] = headline

        return data

    # ── Generación de imagen visual branded ───────────────────

    def _generate_report_image(
        self, title: str, body: str, report_type: str = "postmarket"
    ) -> Optional[bytes]:
        """
        Imagen 1080×1080 con layout adaptado al tipo de informe:

        POST-MERCADO (report_type="postmarket"):
          Cabecera · MERCADOS · OPERACIONES MTO · PRIMAS · Footer

        PRE-APERTURA (report_type="premarket"):
          Cabecera · FUTUROS · AGENDA MACRO · TITULAR · Footer

        Paleta de colores inspirada en el logo MTO (naranja, azul, verde/rojo).
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
            from datetime import date as _date

            W, H = 1080, 1080
            PAD  = 50

            # ── Paleta (colores acordes con logo MTO) ────────────────
            BG      = (  8,  13,  28)   # navy muy oscuro
            CARD_BG = ( 18,  28,  52)   # fondo tarjeta
            ACCENT  = (250, 150,  20)   # naranja cálido (logo)
            BLUE    = ( 20, 155, 230)   # azul/cyan (logo) — VIX / rolls
            GREEN   = ( 52, 211, 153)   # positivo / cobradas
            RED     = (248, 113, 113)   # negativo / pagadas
            TEXT    = (241, 245, 249)   # blanco suave
            SUB     = ( 88, 108, 132)   # gris subtítulo
            DARK    = (  4,   8,  18)   # footer oscuro
            DIV     = ( 28,  42,  70)   # línea divisoria

            img  = Image.new("RGB", (W, H), BG)
            draw = ImageDraw.Draw(img)

            def f(path: str, size: int):
                try:    return ImageFont.truetype(path, size)
                except: return ImageFont.load_default()

            # ── Tipo de informe ───────────────────────────────────────
            is_premarket = (report_type == "premarket")

            # ── Datos del informe ─────────────────────────────────────
            mdata        = self._parse_market_data(body)
            indices      = mdata.get("indices",      [])
            closes       = mdata.get("closes",       [])
            opens        = mdata.get("opens",        [])
            rolls        = mdata.get("rolls",        [])
            primas       = mdata.get("primas",       {})
            macro_events = mdata.get("macro_events", [])
            headline     = mdata.get("headline",     "")

            # ── Fecha en español ─────────────────────────────────────
            d = _date.today()
            MONTHS = ["ene","feb","mar","abr","may","jun",
                      "jul","ago","sep","oct","nov","dic"]
            date_str = f"{d.day} {MONTHS[d.month - 1]} {d.year}"

            # ══════════════════════════════════════════════════════════
            # ZONA 1 — CABECERA  (0..145)
            # ══════════════════════════════════════════════════════════
            draw.rectangle([0, 0, W, 8], fill=ACCENT)

            logo = self._get_logo_img()
            if logo:
                ly = 8 + (128 - logo.height) // 2
                img.paste(logo, (PAD, ly), logo)
                tx = PAD + logo.width + 18
            else:
                BX, BY, BR = 92, 72, 42
                draw.ellipse([BX-BR, BY-BR, BX+BR, BY+BR], fill=ACCENT)
                bb_m = draw.textbbox((0, 0), "MTO", font=f(_FONT_BOLD, 22))
                draw.text(
                    (BX-(bb_m[2]-bb_m[0])//2, BY-(bb_m[3]-bb_m[1])//2-1),
                    "MTO", font=f(_FONT_BOLD, 22), fill=BG,
                )
                tx = BX + BR + 18

            draw.text((tx, 26), "MTO OPCIONES",
                      font=f(_FONT_BOLD, 40), fill=ACCENT)
            subtitle = "PRE-APERTURA" if is_premarket else "CIERRE DE MERCADO"
            draw.text((tx, 78), f"{subtitle}  ·  {date_str}",
                      font=f(_FONT_REG, 20), fill=SUB)

            draw.rectangle([PAD, 142, W-PAD, 145], fill=DIV)

            # ══════════════════════════════════════════════════════════
            # ZONA 2 — MERCADOS  (155..400)
            # ══════════════════════════════════════════════════════════
            y = 155
            zone2_title = "FUTUROS" if is_premarket else "MERCADOS"
            draw.text((PAD, y), zone2_title,
                      font=f(_FONT_BOLD, 22), fill=ACCENT)
            y += 34   # 189

            while len(indices) < 3:
                indices.append({"name":"—","value":"—","change":"","positive":None})
            indices = indices[:3]

            CARD_GAP = 18
            CARD_W   = (W - 2*PAD - 2*CARD_GAP) // 3   # ≈ 313
            CARD_H   = 198

            for i, idx in enumerate(indices):
                cx = PAD + i*(CARD_W + CARD_GAP)
                cy = y

                draw.rounded_rectangle(
                    [cx, cy, cx+CARD_W, cy+CARD_H], radius=14, fill=CARD_BG
                )

                # Franja lateral
                stripe = (
                    GREEN if idx.get("positive") is True
                    else RED  if idx.get("positive") is False
                    else BLUE
                )
                draw.rounded_rectangle(
                    [cx, cy, cx+6, cy+CARD_H], radius=3, fill=stripe
                )

                # Nombre
                draw.text((cx+20, cy+14), idx["name"],
                          font=f(_FONT_REG, 19), fill=SUB)

                # Valor (grande)
                draw.text((cx+20, cy+44), idx.get("value","—"),
                          font=f(_FONT_BOLD, 34), fill=TEXT)

                # Flecha + % (PROMINENTES — mismo tamaño que el valor)
                chg = idx.get("change", "")
                if chg:
                    chg_clr  = GREEN if idx.get("positive") else RED
                    arrow    = "▲" if idx.get("positive") else "▼"
                    chg_text = f"{arrow} {chg}"
                    draw.text((cx+20, cy+106), chg_text,
                              font=f(_FONT_BOLD, 32), fill=chg_clr)
                else:
                    # VIX sin % → indicador neutro
                    draw.text((cx+20, cy+110), "índice vol.",
                              font=f(_FONT_REG, 19), fill=SUB)

            y += CARD_H + 20   # ≈ 407

            draw.rectangle([PAD, y, W-PAD, y+2], fill=DIV)
            y += 14   # ≈ 423

            # ══════════════════════════════════════════════════════════
            # ZONA 3 — OPERACIONES MTO  o  AGENDA MACRO
            # ══════════════════════════════════════════════════════════

            if is_premarket:
                # ── PRE-MERCADO: Agenda macro ─────────────────────────
                draw.text((PAD, y), "AGENDA MACRO",
                          font=f(_FONT_BOLD, 26), fill=ACCENT)
                y += 42

                AGD_TOP = y
                n_ev    = max(len(macro_events), 1)
                agd_h   = min(max(90, 24 + n_ev * 46 + 16), 260)
                AGD_BOT = AGD_TOP + agd_h

                draw.rounded_rectangle(
                    [PAD, AGD_TOP, W-PAD, AGD_BOT], radius=14, fill=CARD_BG
                )

                ay = AGD_TOP + 20
                if macro_events:
                    for ev in macro_events:
                        if ay > AGD_BOT - 18: break
                        # Bullet azul
                        draw.ellipse([PAD+22, ay+7, PAD+32, ay+17], fill=BLUE)
                        for sub in textwrap.wrap(ev, width=50):
                            if ay > AGD_BOT - 14: break
                            draw.text((PAD+44, ay), sub,
                                      font=f(_FONT_REG, 22), fill=TEXT)
                            ay += 30
                        ay += 6
                else:
                    draw.text((PAD+24, AGD_TOP+28),
                              "Sin eventos macro destacados hoy",
                              font=f(_FONT_REG, 22), fill=SUB)

                y = AGD_BOT + 16
                draw.rectangle([PAD, y, W-PAD, y+2], fill=DIV)
                y += 14

            else:
                # ── POST-MERCADO: Operaciones MTO ─────────────────────
                draw.text((PAD, y), "OPERACIONES MTO",
                          font=f(_FONT_BOLD, 26), fill=ACCENT)
                y += 42

                OPS_TOP = y

                def _ops_h() -> int:
                    h = 22
                    for sec in [closes, opens, rolls]:
                        if sec:
                            h += 30 + 14
                            h += len(sec) * 38 + 10
                    return max(90, h + 20)

                ops_h      = min(_ops_h(), 260)
                OPS_BOTTOM = OPS_TOP + ops_h

                draw.rounded_rectangle(
                    [PAD, OPS_TOP, W-PAD, OPS_BOTTOM], radius=14, fill=CARD_BG
                )

                oy = OPS_TOP + 22

                def _draw_sec(label: str, items: list, badge_fill: tuple) -> None:
                    nonlocal oy
                    if not items or oy > OPS_BOTTOM - 40:
                        return
                    sec_f = f(_FONT_BOLD, 19)
                    bb    = draw.textbbox((0, 0), label, font=sec_f)
                    bw    = (bb[2]-bb[0]) + 26
                    bx    = PAD + 22
                    draw.rounded_rectangle(
                        [bx, oy, bx+bw, oy+30], radius=7, fill=badge_fill
                    )
                    draw.text((bx+13, oy+6), label, font=sec_f, fill=BG)
                    oy += 44
                    for item in items:
                        if oy > OPS_BOTTOM - 14: break
                        sym = item.get("symbol", "")
                        acc = item.get("account", "")
                        sf  = f(_FONT_BOLD, 23)
                        af  = f(_FONT_REG,  19)
                        draw.text((PAD+38, oy), sym, font=sf, fill=TEXT)
                        if acc:
                            sw = draw.textbbox((0, 0), sym, font=sf)[2]
                            draw.text((PAD+38+sw+14, oy+3), f"· {acc}",
                                      font=af, fill=SUB)
                        oy += 38
                    oy += 10

                _draw_sec("CIERRES",   closes, RED)
                _draw_sec("APERTURAS", opens,  GREEN)
                _draw_sec("ROLLS",     rolls,  BLUE)

                if not closes and not opens and not rolls:
                    draw.text((PAD+24, OPS_TOP+26),
                              "Sin operaciones registradas hoy",
                              font=f(_FONT_REG, 22), fill=SUB)

                y = OPS_BOTTOM + 16
                draw.rectangle([PAD, y, W-PAD, y+2], fill=DIV)
                y += 14

            # ══════════════════════════════════════════════════════════
            # ZONA 4 — PRIMAS (post) / TITULAR (pre)
            # ══════════════════════════════════════════════════════════

            if is_premarket:
                # ── PRE-MERCADO: Titular de noticias ──────────────────
                if headline:
                    draw.text((PAD, y), "TITULAR",
                              font=f(_FONT_BOLD, 26), fill=ACCENT)
                    y += 40

                    # Altura dinámica según longitud del titular
                    wrapped_hl = textwrap.wrap(headline, width=50)
                    TIT_H = 32 + len(wrapped_hl) * 30 + 24
                    TIT_H = max(TIT_H, 90)

                    draw.rounded_rectangle(
                        [PAD, y, W-PAD, y+TIT_H], radius=14, fill=CARD_BG
                    )

                    # Comilla de apertura (decorativa)
                    draw.text((PAD+18, y+10), "“",
                              font=f(_FONT_BOLD, 42), fill=BLUE)

                    hy = y + 18
                    for line in wrapped_hl:
                        draw.text((PAD+60, hy), line,
                                  font=f(_FONT_REG, 22), fill=TEXT)
                        hy += 30

                    y += TIT_H + 16

            else:
                # ── POST-MERCADO: Primas cobradas / pagadas / neto ────
                cobradas_v = primas.get("cobradas", "")
                pagadas_v  = primas.get("pagadas",  "")
                neto_v     = primas.get("neto",     "")

                if cobradas_v or pagadas_v or neto_v:
                    draw.text((PAD, y), "PRIMAS",
                              font=f(_FONT_BOLD, 26), fill=ACCENT)
                    y += 40

                    PRI_H = 105
                    draw.rounded_rectangle(
                        [PAD, y, W-PAD, y+PRI_H], radius=14, fill=CARD_BG
                    )

                    cols = []
                    if cobradas_v: cols.append(("Cobradas", cobradas_v, GREEN))
                    if pagadas_v:  cols.append(("Pagadas",  pagadas_v,  RED))
                    if neto_v:
                        net_col = GREEN if (neto_v.startswith('+') or
                                            not neto_v.startswith('-')) else RED
                        cols.append(("Neto", neto_v, net_col))

                    col_w = (W - 2*PAD) // max(len(cols), 1)
                    for j, (lbl, val, clr) in enumerate(cols):
                        cx_p = PAD + j * col_w
                        draw.text((cx_p+22, y+16), lbl,
                                  font=f(_FONT_REG, 19), fill=SUB)
                        draw.text((cx_p+22, y+44), val,
                                  font=f(_FONT_BOLD, 32), fill=clr)

                    y += PRI_H + 16

            # ══════════════════════════════════════════════════════════
            # ZONA 5 — FOOTER  (con QR en esquina inferior derecha)
            # ══════════════════════════════════════════════════════════
            FOOTER_H = 110   # más alto para dar hueco al QR
            draw.rectangle([0, H-FOOTER_H, W, H], fill=DARK)
            draw.rectangle([0, H-FOOTER_H, W, H-FOOTER_H+4], fill=ACCENT)

            # Texto a la izquierda
            draw.text((PAD, H-FOOTER_H+18), "@mtoopciones",
                      font=f(_FONT_BOLD, 24), fill=ACCENT)
            draw.text((PAD, H-FOOTER_H+50),
                      "trading de opciones en español",
                      font=f(_FONT_REG, 19), fill=SUB)
            draw.text((PAD, H-FOOTER_H+75),
                      "www.mtoopciones.com",
                      font=f(_FONT_REG, 17), fill=(88, 108, 132))

            # QR en la esquina inferior derecha
            QR_SIZE = 90
            qr_img  = self._get_qr_img(size=QR_SIZE)
            if qr_img:
                # Marco blanco de 4 px alrededor del QR
                from PIL import Image as _PIL_qr
                qr_x = W - PAD - QR_SIZE - 4
                qr_y = H - FOOTER_H + (FOOTER_H - QR_SIZE) // 2
                # Fondo blanco
                qr_bg = _PIL_qr.new("RGB", (QR_SIZE + 8, QR_SIZE + 8), (255, 255, 255))
                img.paste(qr_bg, (qr_x - 4, qr_y - 4))
                img.paste(qr_img.convert("RGB"), (qr_x, qr_y))

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return buf.getvalue()

        except Exception as e:
            logger.error(f"InstagramPoster: error generando imagen: {e}")
            return None

    # ── Subida de imagen a imgbb ──────────────────────────────

    async def _upload_to_imgbb(self, image_bytes: bytes) -> Optional[str]:
        """
        Sube la imagen a imgbb.com y devuelve la URL pública permanente.
        Requiere imgbb_api_key en config.yaml (gratis en https://api.imgbb.com/).
        """
        if not self._imgbb_api_key:
            logger.warning(
                "InstagramPoster: sin imgbb_api_key → imagen no se puede subir. "
                "Configura 'instagram.imgbb_api_key' en config.yaml "
                "(cuenta gratuita en https://api.imgbb.com/)"
            )
            return None
        b64 = base64.b64encode(image_bytes).decode("ascii")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.imgbb.com/1/upload",
                    data={"key": self._imgbb_api_key, "image": b64},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    if data.get("success"):
                        url = data["data"]["url"]
                        logger.debug(f"InstagramPoster: imagen subida a imgbb → {url}")
                        return url
                    logger.error(f"InstagramPoster: imgbb error: {data}")
        except Exception as e:
            logger.error(f"InstagramPoster: imgbb upload exception: {e}")
        return None

    # ── Añadir comentario a un post ───────────────────────────

    async def add_comment(self, media_id: str, text: str) -> bool:
        """
        Añade un comentario a un post publicado.
        Se usa para poner los hashtags como primer comentario (mejor práctica).
        """
        if not self.is_ready() or not media_id:
            return False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{GRAPH_URL}/{media_id}/comments",
                    params={
                        "message":      text[:2200],
                        "access_token": self._access_token,
                    },
                ) as resp:
                    data = await resp.json()
                    if resp.status == 200 and "id" in data:
                        logger.info(f"InstagramPoster: hashtags publicados como comentario (id={data['id']})")
                        return True
                    logger.error(f"InstagramPoster: add_comment error: {data}")
        except Exception as e:
            logger.error(f"InstagramPoster: add_comment exception: {e}")
        return False

    # ── Publicar imagen + comentario con hashtags ─────────────

    async def _post_image_for_report(
        self, image_url: str, caption: str, hashtags: str
    ) -> bool:
        """
        Publica una imagen con la caption dada.
        Si hay hashtags, los añade como primer comentario.
        Devuelve True si se publicó correctamente.
        """
        if not self.is_ready():
            logger.warning("InstagramPoster: no configurado")
            return False
        if len(caption) > 2200:
            caption = caption[:2197] + "…"
        try:
            async with aiohttp.ClientSession() as s:
                # Paso 1: crear container de imagen
                async with s.post(
                    f"{GRAPH_URL}/{self._ig_user_id}/media",
                    params={
                        "image_url":    image_url,
                        "caption":      caption,
                        "access_token": self._access_token,
                    },
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200 or "id" not in data:
                        logger.error(f"InstagramPoster: media container error: {data}")
                        return False
                    container_id = data["id"]

                # Paso 2: publicar el container
                await asyncio.sleep(3)
                async with s.post(
                    f"{GRAPH_URL}/{self._ig_user_id}/media_publish",
                    params={
                        "creation_id":  container_id,
                        "access_token": self._access_token,
                    },
                ) as resp2:
                    data2 = await resp2.json()
                    if resp2.status != 200 or "id" not in data2:
                        logger.error(f"InstagramPoster: publish error: {data2}")
                        return False
                    post_id = data2["id"]
                    logger.info(f"InstagramPoster: imagen publicada (id={post_id})")

            # Paso 3: añadir hashtags como primer comentario
            if hashtags:
                await asyncio.sleep(2)
                await self.add_comment(post_id, hashtags)

            return True

        except Exception as e:
            logger.error(f"InstagramPoster: _post_image_for_report error: {e}")
            return False

    async def post_text(self, text: str, report_type: str = "postmarket") -> bool:
        """
        Publica en Instagram generando una imagen branded con el contenido.
        (Instagram no permite posts de texto puro en el feed.)

        report_type: "postmarket" (cierre) | "premarket" (pre-apertura)
        El texto debe contener el separador '===HASHTAGS===' para separar
        el contenido de los hashtags. Los hashtags se publican como comentario.
        """
        if not self.is_ready():
            logger.warning("InstagramPoster: no configurado, post ignorado")
            return False

        # ── Separar caption de hashtags ───────────────────────────────
        if _HASHTAG_SEP in text:
            caption_raw, _, hashtags = text.partition(_HASHTAG_SEP)
            caption_raw = caption_raw.strip()
            hashtags    = hashtags.strip()
        else:
            # Fallback: último bloque de líneas que empiece por '#'
            lines = text.split("\n")
            hi = next(
                (i for i, l in enumerate(lines) if l.strip().startswith("#")),
                len(lines),
            )
            caption_raw = "\n".join(lines[:hi]).strip()
            hashtags    = "\n".join(lines[hi:]).strip()

        # ── Generar imagen ────────────────────────────────────────────
        title = (caption_raw.split("\n")[0] if caption_raw else "MTO Opciones")[:80]
        image_bytes = self._generate_report_image(title, caption_raw, report_type)
        if not image_bytes:
            logger.error("InstagramPoster: no se pudo generar imagen del informe")
            return False

        # ── Subir imagen a imgbb ──────────────────────────────────────
        image_url = await self._upload_to_imgbb(image_bytes)
        if not image_url:
            return False

        # ── Publicar + comentario con hashtags ────────────────────────
        return await self._post_image_for_report(image_url, caption_raw, hashtags)


def build_from_config(cfg: dict) -> Optional["InstagramPoster"]:
    """
    Crea un InstagramPoster desde la sección 'instagram' del config.yaml.
    Devuelve None si está desactivado o faltan credenciales.

    Campos opcionales en config.yaml:
      instagram:
        imgbb_api_key: "TU-CLAVE"   ← necesario para publicar en el feed
                                      (gratis en https://api.imgbb.com/)
    """
    ig = cfg.get("instagram", {})
    if not ig.get("enabled", False):
        return None
    required = ("app_id", "app_secret", "access_token")
    if not all(ig.get(k) for k in required):
        logger.warning("InstagramPoster: credenciales incompletas en config.yaml")
        return None
    return InstagramPoster(
        app_id        = ig["app_id"],
        app_secret    = ig["app_secret"],
        access_token  = ig["access_token"],
        imgbb_api_key = ig.get("imgbb_api_key", ""),
        logo_url      = cfg.get("discord", {}).get("logo_url", ""),
    )
