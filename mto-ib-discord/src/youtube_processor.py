"""
YouTube → Summary processor.

Extrae el transcript de un link de YouTube y genera un resumen
de 45 segundos con Claude. Devuelve un embed listo para DiscordApprover.

Dependencia:  pip install youtube-transcript-api
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional
from loguru import logger

_YT_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?'
    r'(?:youtube\.com/(?:watch\?[^&\s]*v=|shorts/|embed/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})'
)

_TRANSCRIPT_LANGS = ["es", "es-ES", "es-MX", "es-419", "en", "en-US"]


def extract_video_id(text: str) -> Optional[str]:
    m = _YT_PATTERN.search(text)
    return m.group(1) if m else None


async def get_transcript(video_id: str) -> Optional[str]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(
            None,
            lambda: YouTubeTranscriptApi.get_transcript(
                video_id, languages=_TRANSCRIPT_LANGS
            ),
        )
        return " ".join(e["text"] for e in entries)
    except Exception as e:
        logger.warning(f"YouTubeProcessor: sin transcript para {video_id}: {e}")
        return None


async def generate_summary(
    transcript: str,
    video_url: str,
    anthropic_key: str,
) -> str:
    """Genera un resumen narrable en voz alta en 45 segundos (100-120 palabras)."""
    import anthropic

    prompt = (
        "Eres el editor de contenido de MTO Opciones, comunidad de trading de opciones "
        "para hispanohablantes.\n\n"
        f"TRANSCRIPCIÓN DEL VIDEO:\n---\n{transcript[:8000]}\n---\n\n"
        "Genera un RESUMEN diseñado para ser narrado en voz alta en exactamente 45 segundos "
        "(entre 100 y 120 palabras). Requisitos:\n"
        "- La primera frase presenta el tema principal del video\n"
        "- Desarrolla los 2-3 puntos clave más relevantes\n"
        "- Tono directo, claro y cercano para la comunidad trader hispanohablante\n"
        "- Termina con una conclusión o reflexión breve\n"
        "- NO incluyas saludos, presentaciones del ponente ni frases de cierre del tipo "
        "  'si te ha gustado suscríbete'\n\n"
        "Devuelve SOLO el texto del resumen, sin títulos ni etiquetas."
    )

    client = anthropic.Anthropic(api_key=anthropic_key)
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.messages.create(
            model="claude-opus-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        ),
    )
    return response.content[0].text.strip()


def build_embed(summary: str, video_url: str) -> dict:
    return {
        "title": "📺 Resumen de Video YouTube",
        "description": summary,
        "color": 0xFF0000,
        "footer": {"text": f"Fuente: {video_url}"},
    }
