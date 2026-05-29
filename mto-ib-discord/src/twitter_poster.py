"""
Publicación automática en X (Twitter) para reportes MTO.

Usa OAuth 1.0a (API Key + Secret + Access Token + Access Token Secret)
para publicar tweets en nombre de @MtoOpciones.

Soporte de HILOS: los informes largos se dividen automáticamente en tweets
encadenados (hilo), respetando saltos de párrafo y oraciones.

IMPORTANTE — permisos requeridos:
  Los Access Tokens deben tener permisos "Read and Write".
  Si recibes error 402, ve al X Developer Portal → tu App → Settings →
  User authentication settings → cambia a "Read and Write" y REGENERA
  los Access Tokens. Actualiza config.yaml con los nuevos tokens.

Dependencia:  pip install tweepy
"""

from __future__ import annotations

import asyncio
from typing import List, Optional
from loguru import logger

try:
    import tweepy
    TWEEPY_OK = True
except ImportError:
    TWEEPY_OK = False
    logger.warning("tweepy no instalado — publicación en X desactivada. "
                   "Instala con: pip install tweepy")


# ── Helpers de módulo ─────────────────────────────────────────

def split_for_thread(text: str, max_chars: int = 270) -> List[str]:
    """
    Divide el texto en partes de max_chars caracteres para publicar como hilo.
    Intenta cortar en párrafos, luego oraciones, luego palabras — nunca a mitad.
    Reserva 10 chars para el indicador de posición (1/N) si hay >1 parte.
    """
    if len(text) <= max_chars:
        return [text]

    parts: List[str] = []
    current = ""

    def _flush():
        nonlocal current
        if current.strip():
            parts.append(current.strip())
        current = ""

    paragraphs = text.split("\n\n")
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        candidate = (current + "\n\n" + para).strip() if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            _flush()
            # El párrafo solo ya es demasiado largo → cortar por oraciones
            if len(para) <= max_chars:
                current = para
            else:
                for sent in para.replace(". ", ".\n").split("\n"):
                    sent = sent.strip()
                    if not sent:
                        continue
                    candidate = (current + " " + sent).strip() if current else sent
                    if len(candidate) <= max_chars:
                        current = candidate
                    else:
                        _flush()
                        # La oración sola sigue siendo larga → por palabras
                        if len(sent) <= max_chars:
                            current = sent
                        else:
                            for word in sent.split():
                                candidate = (current + " " + word).strip() if current else word
                                if len(candidate) <= max_chars:
                                    current = candidate
                                else:
                                    _flush()
                                    current = word
    _flush()

    # Añadir indicador de hilo si hay más de un tweet (ej. "1/3")
    if len(parts) > 1:
        total = len(parts)
        parts = [
            f"{p}\n\n{i+1}/{total}"
            if len(f"{p}\n\n{i+1}/{total}") <= 280 else p
            for i, p in enumerate(parts)
        ]

    return parts


class TwitterPoster:
    """Cliente para publicar tweets en @MtoOpciones."""

    def __init__(
        self,
        api_key:             str,
        api_secret:          str,
        access_token:        str,
        access_token_secret: str,
    ):
        self._api_key             = api_key
        self._api_secret          = api_secret
        self._access_token        = access_token
        self._access_token_secret = access_token_secret
        self._client: Optional[tweepy.Client] = None
        if not TWEEPY_OK:
            return
        try:
            # tweepy.Client usa la API v2 de X con OAuth 1.0a User Context
            self._client = tweepy.Client(
                consumer_key=api_key,
                consumer_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_token_secret,
            )
            logger.info("TwitterPoster: cliente X inicializado correctamente")
        except Exception as e:
            logger.error(f"TwitterPoster: error inicializando cliente: {e}")

    # ── API pública ───────────────────────────────────────────

    async def post(self, text: str) -> bool:
        """
        Publica un tweet único. Si supera 280 chars, lo trunca.
        Para informes largos, usa post_thread() en su lugar.
        """
        return await self.post_thread([text])

    async def post_thread(self, parts: List[str]) -> bool:
        """
        Publica un hilo de tweets: cada tweet responde al anterior.
        Devuelve True si al menos el primer tweet se publicó correctamente.
        """
        if not self._client:
            logger.warning("TwitterPoster: cliente no disponible")
            return False

        loop = asyncio.get_event_loop()
        prev_id: Optional[str] = None
        success = 0

        for i, text in enumerate(parts):
            if len(text) > 280:
                text = text[:277] + "…"
            try:
                kwargs: dict = {"text": text}
                if prev_id:
                    # Encadenar como reply al tweet anterior del hilo
                    # Nota: tweepy.Client.create_tweet usa in_reply_to_tweet_id directamente
                    kwargs["in_reply_to_tweet_id"] = prev_id

                resp = await loop.run_in_executor(
                    None,
                    lambda k=kwargs: self._client.create_tweet(**k),
                )
                prev_id = resp.data["id"] if resp.data else None
                success += 1
                logger.info(
                    f"TwitterPoster: tweet {i+1}/{len(parts)} publicado "
                    f"(id={prev_id}): {text[:60]}…"
                )
                # Pequeña pausa entre tweets del hilo para no saturar la API
                if i < len(parts) - 1:
                    await asyncio.sleep(1.5)

            except Exception as e:
                logger.error(f"TwitterPoster: error en tweet {i+1}/{len(parts)}: {e}")
                if i == 0:
                    return False   # primer tweet fallido → abortar

        return success > 0

    async def post_thread_with_media(self, parts: List[str], image_bytes: bytes) -> bool:
        """
        Publica un hilo de tweets con imagen adjunta al primer tweet.
        Usa tweepy v1.1 API para subir el media y la API v2 para publicar los tweets.
        Si el upload de media falla, cae back a post_thread() sin imagen.
        """
        if not self._client or not TWEEPY_OK:
            logger.warning("TwitterPoster: cliente no disponible")
            return False

        loop = asyncio.get_event_loop()
        media_id: Optional[str] = None

        # Intentar subir la imagen con la API v1.1
        try:
            auth = tweepy.OAuth1UserHandler(
                consumer_key=self._api_key,
                consumer_secret=self._api_secret,
                access_token=self._access_token,
                access_token_secret=self._access_token_secret,
            )
            api_v1 = tweepy.API(auth)

            import io as _io
            media = await loop.run_in_executor(
                None,
                lambda: api_v1.media_upload(filename="trade.png", file=_io.BytesIO(image_bytes)),
            )
            media_id = str(media.media_id)
            logger.info(f"TwitterPoster: imagen subida → media_id={media_id}")
        except Exception as e:
            logger.warning(f"TwitterPoster: no se pudo subir imagen para media tweet — {e}. Usando solo texto.")
            return await self.post_thread(parts)

        # Publicar primer tweet con la imagen
        prev_id: Optional[str] = None
        success = 0
        for i, text in enumerate(parts):
            if len(text) > 280:
                text = text[:277] + "…"
            try:
                kwargs: dict = {"text": text}
                if i == 0 and media_id:
                    kwargs["media_ids"] = [media_id]
                if prev_id:
                    kwargs["in_reply_to_tweet_id"] = prev_id
                resp = await loop.run_in_executor(
                    None,
                    lambda k=kwargs: self._client.create_tweet(**k),
                )
                prev_id = resp.data["id"] if resp.data else None
                success += 1
                logger.info(
                    f"TwitterPoster: tweet {i+1}/{len(parts)} con media publicado "
                    f"(id={prev_id}): {text[:60]}…"
                )
                if i < len(parts) - 1:
                    await asyncio.sleep(1.5)
            except Exception as e:
                logger.error(f"TwitterPoster: error en tweet {i+1}/{len(parts)} (media): {e}")
                if i == 0:
                    return False
        return success > 0

    def is_ready(self) -> bool:
        return self._client is not None


def build_from_config(cfg: dict) -> Optional["TwitterPoster"]:
    """
    Crea un TwitterPoster desde la sección 'twitter' del config.yaml.
    Devuelve None si está desactivado o faltan credenciales.
    """
    tw = cfg.get("twitter", {})
    if not tw.get("enabled", False):
        return None
    required = ("api_key", "api_secret", "access_token", "access_token_secret")
    if not all(tw.get(k) for k in required):
        logger.warning("TwitterPoster: credenciales incompletas en config.yaml")
        return None
    return TwitterPoster(
        api_key=tw["api_key"],
        api_secret=tw["api_secret"],
        access_token=tw["access_token"],
        access_token_secret=tw["access_token_secret"],
    )
