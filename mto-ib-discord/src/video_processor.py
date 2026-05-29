"""
Video Processor — Crea un reel de 45 segundos a partir de un vídeo de YouTube.

Pipeline:
  1. Descarga el vídeo con yt-dlp (usando cookies para evitar bot detection)
  2. Obtiene el transcript con timestamps exactos
  3. Claude selecciona los mejores segmentos (~45s total)
  4. ffmpeg corta y pega los fragmentos elegidos
  5. Devuelve bytes del vídeo MP4 resultante

Dependencias: yt-dlp, ffmpeg, youtube-transcript-api
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from typing import List, Optional, Tuple
from loguru import logger

_COOKIES_FILE = "data/youtube_cookies.txt"
_MAX_VIDEO_BYTES = 50 * 1024 * 1024   # 50 MB límite Discord


# ── Helpers ───────────────────────────────────────────────────

def _run_cmd(args: list, timeout: int = 120) -> Tuple[str, str, int]:
    import subprocess
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout, result.stderr, result.returncode


async def _run_async(args: list, timeout: int = 300) -> Tuple[str, str, int]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _run_cmd(args, timeout))


# ── Descarga de vídeo ─────────────────────────────────────────

async def download_video(video_id: str, output_path: str) -> bool:
    """
    Descarga el vídeo de YouTube en calidad ≤720p.
    Usa cookies si están disponibles.
    Retorna True si la descarga fue exitosa.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--format", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
        "--merge-output-format", "mp4",
        "--output", output_path,
        "--no-warnings",
        "--quiet",
    ]

    # Añadir cookies si existen
    if os.path.exists(_COOKIES_FILE):
        cmd += ["--cookies", _COOKIES_FILE]
        logger.debug("VideoProcessor: usando cookies de YouTube")

    cmd.append(url)

    stdout, stderr, code = await _run_async(cmd, timeout=300)

    if code != 0:
        logger.warning(f"VideoProcessor: yt-dlp error (code={code}): {stderr[:200]}")
        return False

    if not os.path.exists(output_path):
        logger.warning(f"VideoProcessor: archivo no creado en {output_path}")
        return False

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info(f"VideoProcessor: vídeo descargado ({size_mb:.1f} MB) → {output_path}")
    return True


# ── Transcript con timestamps ─────────────────────────────────

async def get_transcript_with_timestamps(video_id: str) -> Optional[List[dict]]:
    """
    Devuelve lista de {text, start, duration} o None si no hay transcript.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(
            None,
            lambda: YouTubeTranscriptApi.get_transcript(
                video_id, languages=["es", "es-ES", "es-MX", "es-419", "en", "en-US"]
            )
        )
        return entries  # [{text, start, duration}, ...]
    except Exception as e:
        logger.warning(f"VideoProcessor: sin transcript para {video_id}: {e}")
        return None


# ── Selección de segmentos con Claude ─────────────────────────

async def select_best_segments(
    transcript_entries: List[dict],
    video_id: str,
    anthropic_key: str,
    target_seconds: int = 45,
) -> Optional[List[dict]]:
    """
    Claude analiza el transcript con timestamps y elige los mejores
    fragmentos que sumen ~target_seconds segundos.

    Retorna lista de {start, end, reason} ordenada cronológicamente.
    """
    import anthropic

    # Preparar transcript para Claude (con timestamps)
    lines = []
    for e in transcript_entries:
        start = e["start"]
        end   = start + e["duration"]
        lines.append(f"[{start:.1f}s - {end:.1f}s] {e['text']}")
    transcript_text = "\n".join(lines)

    # Duración total del vídeo
    total_duration = transcript_entries[-1]["start"] + transcript_entries[-1]["duration"] if transcript_entries else 0

    prompt = (
        "Eres editor de vídeo para MTO Opciones, comunidad de trading de opciones.\n\n"
        f"TRANSCRIPT CON TIMESTAMPS (vídeo de {total_duration:.0f}s total):\n"
        f"---\n{transcript_text[:12000]}\n---\n\n"
        f"Tu tarea: seleccionar los fragmentos más IMPACTANTES e INFORMATIVOS del vídeo "
        f"que en total sumen entre {target_seconds - 5} y {target_seconds + 5} segundos.\n\n"
        "CRITERIOS DE SELECCIÓN:\n"
        "- Prioriza el GANCHO inicial (primeros 5-10 segundos si son buenos)\n"
        "- Elige los momentos con más VALOR educativo o impacto emocional\n"
        "- Los fragmentos deben tener SENTIDO por sí solos (frase completa)\n"
        "- Evita saludos, despedidas, o frases de relleno\n\n"
        f"Selecciona entre 3 y 6 fragmentos. Cada uno debe ser de 5-15 segundos.\n\n"
        "Responde SOLO con JSON válido:\n"
        '{"segments": [{"start": 12.5, "end": 24.0, "reason": "gancho inicial impactante"}, ...]}'
    )

    client = anthropic.Anthropic(api_key=anthropic_key)
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.messages.create(
            model="claude-opus-4-5",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    data = json.loads(raw.strip())
    segments = data.get("segments", [])

    if not segments:
        return None

    # Ordenar cronológicamente
    segments.sort(key=lambda s: s["start"])
    total = sum(s["end"] - s["start"] for s in segments)
    logger.info(f"VideoProcessor: {len(segments)} segmentos seleccionados ({total:.1f}s total)")
    return segments


# ── Cortar y unir con ffmpeg ──────────────────────────────────

async def create_highlight_reel(
    video_path: str,
    segments: List[dict],
    output_path: str,
) -> bool:
    """
    Usa ffmpeg para cortar los segmentos y concatenarlos.
    Retorna True si el proceso fue exitoso.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Cortar cada segmento
        segment_files = []
        for i, seg in enumerate(segments):
            start    = seg["start"]
            duration = seg["end"] - seg["start"]
            seg_path = os.path.join(tmpdir, f"seg_{i:02d}.mp4")

            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", video_path,
                "-t", str(duration),
                "-c:v", "libx264",
                "-c:a", "aac",
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                "-preset", "fast",
                "-crf", "23",
                "-movflags", "+faststart",
                "-loglevel", "error",
                seg_path,
            ]
            stdout, stderr, code = await _run_async(cmd)
            if code != 0:
                logger.error(f"VideoProcessor: ffmpeg corte {i} error: {stderr[:200]}")
                # Fallback: sin reescalar
                cmd_simple = [
                    "ffmpeg", "-y",
                    "-ss", str(start),
                    "-i", video_path,
                    "-t", str(duration),
                    "-c:v", "copy", "-c:a", "copy",
                    "-loglevel", "error",
                    seg_path,
                ]
                _, stderr2, code2 = await _run_async(cmd_simple)
                if code2 != 0:
                    logger.error(f"VideoProcessor: ffmpeg corte simple {i} error: {stderr2[:200]}")
                    continue

            if os.path.exists(seg_path):
                segment_files.append(seg_path)

        if not segment_files:
            logger.error("VideoProcessor: ningún segmento se pudo cortar")
            return False

        # 2. Crear lista de concatenación
        list_path = os.path.join(tmpdir, "concat_list.txt")
        with open(list_path, "w") as f:
            for sp in segment_files:
                f.write(f"file '{sp}'\n")

        # 3. Concatenar
        cmd_concat = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            "-movflags", "+faststart",
            "-loglevel", "error",
            output_path,
        ]
        stdout, stderr, code = await _run_async(cmd_concat)
        if code != 0:
            logger.error(f"VideoProcessor: ffmpeg concat error: {stderr[:200]}")
            return False

        if not os.path.exists(output_path):
            return False

        size_mb = os.path.getsize(output_path) / 1024 / 1024
        logger.info(f"VideoProcessor: reel creado ({size_mb:.1f} MB) → {output_path}")
        return True


# ── También genera el guión del reel ──────────────────────────

async def generate_reel_script(
    transcript_entries: List[dict],
    segments: List[dict],
    video_id: str,
    anthropic_key: str,
) -> str:
    """
    Genera el guión/caption del reel en el estilo de MTO Opciones:
    hook + puntos clave + insight + pregunta final.
    """
    import anthropic

    # Texto de los segmentos seleccionados
    selected_text = []
    for seg in segments:
        for e in transcript_entries:
            if e["start"] >= seg["start"] and e["start"] <= seg["end"]:
                selected_text.append(e["text"])
    segments_content = " ".join(selected_text)

    # Contexto completo para el gancho
    full_text = " ".join(e["text"] for e in transcript_entries[:50])  # primeros ~2min

    prompt = (
        "Eres el editor de contenido de MTO Opciones (@mtoopciones), comunidad de trading "
        "de opciones para hispanohablantes.\n\n"
        f"CONTENIDO DEL REEL (fragmentos seleccionados):\n---\n{segments_content}\n---\n\n"
        f"CONTEXTO ADICIONAL (inicio del vídeo):\n---\n{full_text[:1500]}\n---\n\n"
        "Genera el CAPTION completo para el reel de Instagram en el estilo de @mtoopciones.\n\n"
        "ESTRUCTURA OBLIGATORIA (basada en el estilo del canal):\n"
        "1. HOOK (1 línea): Pregunta o afirmación impactante que describe el contenido. "
        "   Formato: emoji + '¿[pregunta]?' o 'Lo que nadie te dice sobre [tema]'\n"
        "2. CONTEXTO (1-2 líneas): Qué encontrará el espectador si ve el reel\n"
        "3. PUNTOS CLAVE (3-5 bullets con 🔹): Los conceptos principales del fragmento\n"
        "4. INSIGHT (1-2 líneas): La reflexión o conclusión más importante\n"
        "5. CTA (1 línea): Invitación a ver el reel o responder\n"
        "6. PREGUNTA FINAL (1 línea con 👇): Pregunta directa para engagement\n\n"
        "Tono: educativo, directo, cercano. Sin palabras en inglés innecesarias.\n"
        "Máximo 300 palabras en total.\n\n"
        "Devuelve SOLO el caption listo para publicar, sin títulos ni metaetiquetas."
    )

    client = anthropic.Anthropic(api_key=anthropic_key)
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
    )
    return response.content[0].text.strip()


# ── Pipeline principal ────────────────────────────────────────

async def process_youtube_to_reel(
    video_id: str,
    anthropic_key: str,
    target_seconds: int = 45,
) -> dict:
    """
    Pipeline completo: YouTube → reel de 45 segundos.

    Retorna dict con:
      - video_bytes: bytes del MP4 (o None si falla)
      - script: caption del reel
      - segments: lista de segmentos usados
      - error: mensaje de error (o None)
    """
    result = {"video_bytes": None, "script": "", "segments": [], "error": None}

    with tempfile.TemporaryDirectory(prefix="mto_reel_") as tmpdir:
        video_path  = os.path.join(tmpdir, f"{video_id}.mp4")
        output_path = os.path.join(tmpdir, f"{video_id}_reel.mp4")

        # 1. Descargar vídeo
        logger.info(f"VideoProcessor: descargando {video_id}...")
        ok = await download_video(video_id, video_path)
        if not ok:
            result["error"] = "No se pudo descargar el vídeo (comprueba las cookies de YouTube)"
            return result

        # 2. Obtener transcript con timestamps
        logger.info("VideoProcessor: obteniendo transcript...")
        transcript = await get_transcript_with_timestamps(video_id)
        if not transcript:
            result["error"] = "El vídeo no tiene subtítulos disponibles para seleccionar segmentos"
            return result

        # 3. Claude selecciona los mejores segmentos
        logger.info("VideoProcessor: seleccionando segmentos con Claude...")
        segments = await select_best_segments(transcript, video_id, anthropic_key, target_seconds)
        if not segments:
            result["error"] = "No se pudieron identificar segmentos adecuados"
            return result
        result["segments"] = segments

        # 4. Crear el reel con ffmpeg
        logger.info(f"VideoProcessor: creando reel ({len(segments)} segmentos)...")
        ok = await create_highlight_reel(video_path, segments, output_path)
        if not ok:
            result["error"] = "Error al cortar y unir los segmentos con ffmpeg"
            return result

        # 5. Leer bytes del vídeo resultante
        size = os.path.getsize(output_path)
        if size > _MAX_VIDEO_BYTES:
            result["error"] = f"Vídeo demasiado grande ({size/1024/1024:.0f} MB > 50 MB)"
            return result

        with open(output_path, "rb") as f:
            result["video_bytes"] = f.read()

        # 6. Generar guión/caption del reel
        logger.info("VideoProcessor: generando guión del reel...")
        result["script"] = await generate_reel_script(transcript, segments, video_id, anthropic_key)

    logger.info(f"VideoProcessor: reel listo ({len(result['video_bytes'])/1024/1024:.1f} MB)")
    return result
