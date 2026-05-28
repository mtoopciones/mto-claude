"""
YouTube → Summary processor.

Extrae el transcript (o metadatos) de un link de YouTube y genera un resumen
de 45 segundos con Claude. Devuelve un embed listo para DiscordApprover.

Estrategia:
  1. youtube-transcript-api  → subtítulos manuales/automáticos
  2. yt-dlp auto-captions    → subtítulos auto en cualquier idioma
  3. yt-dlp metadata         → título + descripción como fallback final

Dependencias:  pip install youtube-transcript-api yt-dlp
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import os
from typing import Optional, Tuple
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


# ── Estrategia 1: youtube-transcript-api ──────────────────────

async def _get_transcript_api(video_id: str) -> Optional[str]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(
            None,
            lambda: YouTubeTranscriptApi.get_transcript(
                video_id, languages=_TRANSCRIPT_LANGS
            ),
        )
        text = " ".join(e["text"] for e in entries)
        logger.info(f"YouTubeProcessor: transcript via API ({len(text)} chars)")
        return text
    except Exception as e:
        logger.debug(f"YouTubeProcessor: transcript API falló — {e}")
        return None


# ── Estrategia 2 + 3: yt-dlp (auto-captions + metadata) ──────

def _run_ytdlp(args: list) -> Tuple[str, str]:
    import subprocess
    result = subprocess.run(
        ["yt-dlp"] + args,
        capture_output=True, text=True, timeout=30
    )
    return result.stdout, result.stderr


async def _get_transcript_ytdlp(video_id: str) -> Optional[str]:
    """Intenta obtener auto-captions vía yt-dlp y convertirlas a texto plano."""
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        loop = asyncio.get_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            args = [
                "--write-auto-sub", "--skip-download",
                "--sub-langs", "es.*,en.*",
                "--sub-format", "vtt",
                "--output", os.path.join(tmpdir, "%(id)s.%(ext)s"),
                "--no-playlist", "--quiet",
                url,
            ]
            await loop.run_in_executor(None, lambda: _run_ytdlp(args))

            # Buscar archivo .vtt descargado
            for fname in os.listdir(tmpdir):
                if fname.endswith(".vtt"):
                    fpath = os.path.join(tmpdir, fname)
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        raw = f.read()
                    text = _vtt_to_text(raw)
                    if text:
                        logger.info(f"YouTubeProcessor: auto-captions yt-dlp ({len(text)} chars)")
                        return text
    except Exception as e:
        logger.debug(f"YouTubeProcessor: yt-dlp auto-captions falló — {e}")
    return None


def _vtt_to_text(vtt: str) -> str:
    """Extrae texto plano de un archivo VTT eliminando timestamps y duplicados."""
    lines = []
    seen = set()
    for line in vtt.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.startswith("WEBVTT") or line.isdigit():
            continue
        # Quitar etiquetas HTML <...>
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return " ".join(lines)


async def _get_metadata_ytdlp(video_id: str) -> Optional[str]:
    """Obtiene título + descripción del video como fallback."""
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        loop = asyncio.get_event_loop()
        stdout, _ = await loop.run_in_executor(
            None,
            lambda: _run_ytdlp(["--dump-json", "--no-playlist", "--quiet", url]),
        )
        if not stdout.strip():
            return None
        data = json.loads(stdout)
        title = data.get("title", "")
        desc  = (data.get("description", "") or "")[:2000]
        channel = data.get("channel", "")
        result = f"TÍTULO: {title}\nCANAL: {channel}\nDESCRIPCIÓN:\n{desc}"
        logger.info(f"YouTubeProcessor: usando metadatos ({title[:60]})")
        return result
    except Exception as e:
        logger.debug(f"YouTubeProcessor: metadata yt-dlp falló — {e}")
    return None


# ── Función principal ──────────────────────────────────────────

async def get_content(video_id: str) -> Tuple[Optional[str], str]:
    """
    Intenta obtener contenido del video con tres estrategias en cascada.
    Devuelve (texto, source) donde source es 'transcript' | 'captions' | 'metadata' | 'none'.
    """
    text = await _get_transcript_api(video_id)
    if text:
        return text, "transcript"

    text = await _get_transcript_ytdlp(video_id)
    if text:
        return text, "captions"

    text = await _get_metadata_ytdlp(video_id)
    if text:
        return text, "metadata"

    return None, "none"


# ── Generación de resumen ──────────────────────────────────────

async def generate_summary(
    content: str,
    source: str,
    video_url: str,
    anthropic_key: str,
) -> str:
    """Genera un resumen narrable en 45 segundos (100-120 palabras)."""
    import anthropic

    if source == "metadata":
        content_label = "INFORMACIÓN DEL VIDEO (título y descripción)"
        extra = (
            "IMPORTANTE: Solo tienes el título y la descripción, no la transcripción completa. "
            "Genera el resumen basándote en lo disponible, siendo honesto sobre el contenido. "
            "Si la descripción es escasa, céntrate en el título y el tema inferido.\n\n"
        )
    else:
        content_label = "TRANSCRIPCIÓN DEL VIDEO"
        extra = ""

    prompt = (
        "Eres el editor de contenido de MTO Opciones, comunidad de trading de opciones "
        "para hispanohablantes.\n\n"
        f"{content_label}:\n---\n{content[:8000]}\n---\n\n"
        f"{extra}"
        "Genera un RESUMEN diseñado para ser narrado en voz alta en exactamente 45 segundos "
        "(entre 100 y 120 palabras). Requisitos:\n"
        "- La primera frase presenta el tema principal del video\n"
        "- Desarrolla los 2-3 puntos clave más relevantes\n"
        "- Tono directo, claro y cercano para la comunidad trader hispanohablante\n"
        "- Termina con una conclusión o reflexión breve\n"
        "- NO incluyas saludos, presentaciones del ponente ni frases de cierre\n\n"
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


def build_embed(summary: str, video_url: str, source: str) -> dict:
    source_label = {
        "transcript": "transcript completo",
        "captions":   "subtítulos automáticos",
        "metadata":   "título y descripción (sin subtítulos)",
    }.get(source, source)
    return {
        "title": "📺 Resumen de Video YouTube",
        "description": summary,
        "color": 0xFF0000,
        "footer": {"text": f"Fuente: {video_url} · Basado en: {source_label}"},
    }
