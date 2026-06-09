"""
Publicación automática en la Facebook Page de MTO Opciones.

Usa la Graph API de Meta con un Page Access Token para publicar
posts de texto e imagen en la página MTO Opciones.
"""

from __future__ import annotations

from typing import Optional

import aiohttp
from loguru import logger

GRAPH_URL = "https://graph.facebook.com/v21.0"


class FacebookPoster:
    """Cliente para publicar en la Facebook Page MTO Opciones."""

    def __init__(self, page_access_token: str, page_id: str = ""):
        self._token   = page_access_token
        self._page_id = page_id

    # ── Setup ─────────────────────────────────────────────────

    async def setup(self) -> bool:
        """Verifica la conexión con la Facebook Page."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{GRAPH_URL}/{self._page_id}",
                    params={"fields": "id,name", "access_token": self._token}
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"Facebook setup error {resp.status}: {text[:200]}")
                        return False
                    data = await resp.json()
                    name = data.get("name", "?")
                    logger.info(
                        f"FacebookPoster: conectado a pagina '{name}' "
                        f"(id={self._page_id})"
                    )
                    return True
        except Exception as e:
            logger.error(f"FacebookPoster setup error: {e}")
            return False

    def is_ready(self) -> bool:
        return bool(self._page_id and self._token)

    # ── API pública ───────────────────────────────────────────

    async def post_text(self, message: str) -> bool:
        """Publica un post de texto en la Facebook Page."""
        if not self.is_ready():
            logger.warning("FacebookPoster: no configurado, post ignorado")
            return False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{GRAPH_URL}/{self._page_id}/feed",
                    params={
                        "message":      message,
                        "access_token": self._token,
                    }
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200 or "id" not in data:
                        logger.error(f"Facebook post error: {data}")
                        return False
                    logger.info(
                        f"FacebookPoster: ✅ post publicado "
                        f"(id={data['id']}, msg={message[:50]}…)"
                    )
                    return True
        except Exception as e:
            logger.error(f"FacebookPoster post_text error: {e}")
            return False

    async def post_image(self, image_url: str, caption: str = "") -> bool:
        """Publica una imagen con caption en la Facebook Page."""
        if not self.is_ready():
            logger.warning("FacebookPoster: no configurado, post ignorado")
            return False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{GRAPH_URL}/{self._page_id}/photos",
                    params={
                        "url":          image_url,
                        "caption":      caption,
                        "access_token": self._token,
                    }
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200 or "id" not in data:
                        logger.error(f"Facebook photo error: {data}")
                        return False
                    logger.info(
                        f"FacebookPoster: ✅ imagen publicada (id={data['id']})"
                    )
                    return True
        except Exception as e:
            logger.error(f"FacebookPoster post_image error: {e}")
            return False


def build_from_config(cfg: dict) -> Optional["FacebookPoster"]:
    fb = cfg.get("facebook", {})
    if not fb.get("enabled", False):
        return None
    token   = fb.get("page_access_token", "")
    page_id = fb.get("page_id", "")
    if not token or not page_id:
        logger.warning("FacebookPoster: page_access_token o page_id no configurado")
        return None
    return FacebookPoster(page_access_token=token, page_id=page_id)
