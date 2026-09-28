"""翻唱 / 说话 编排层。

把搜歌、TTS、人声分离、音色转换、语音条发送串成完整流程。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from ..music.search import MusicSearchClient, SongInfo
from ..rvc_client import RVCClient

from .mimo_tts import MiMoTTSService
from .net_guard import MAX_REDIRECTS, REDIRECT_STATUS, ExternalURLBlocked, ensure_external_url, resolve_redirect


async def _download_external(session: Any, url: str, *, timeout: Any) -> bytes:
    """下载外部音频直链，逐跳校验协议与落点。

    ``allow_redirects=False`` + 手动跟随，保证每一跳都先过 ``ensure_external_url``，
    否则平台侧一个指向内网/云元数据地址的 302 就会被直接跟随。
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        await ensure_external_url(current)
        async with session.get(current, timeout=timeout, allow_redirects=False) as resp:
            if resp.status in REDIRECT_STATUS:
                current = resolve_redirect(current, resp.headers.get("Location", ""))
                continue
            resp.raise_for_status()
            return await resp.read()
    raise ExternalURLBlocked(f"下载地址跳转次数超过 {MAX_REDIRECTS} 次，已放弃: {url}")


class Pipeline:
    """翻唱与说话编排。

    Args:
        music: 音乐搜索客户端。
        rvc: RVC sidecar 客户端。
        mimo: MiMo TTS 客户端（可为 None，仅说话功能需要）。
        logger: 日志器。
    """

    def __init__(
        self,
        music: MusicSearchClient,
        rvc: RVCClient,
        mimo: MiMoTTSService | None,
        logger: logging.Logger,
    ) -> None:
        self.music = music
        self.rvc = rvc
        self.mimo = mimo
        self.logger = logger

    async def cover_song(
        self,
        query: str,
        sid: str,
        *,
        platform: str = "163",
        search_limit: int = 5,
        uvr_model: str = "HP2_all_vocals",
        uvr_agg: int = 10,
        uvr_weights_dir: str = "",
        with_instrumental: bool = False,
        convert_kwargs: dict[str, Any] | None = None,
    ) -> tuple[SongInfo, bytes]:
        """翻唱：搜歌 → 取链下载 → 分离/换音色/混伴奏，返回 (歌曲信息, wav bytes)。"""
        convert_kwargs = convert_kwargs or {}

        results = await self.music.search(query, platform, limit=search_limit)
        if not results:
            raise RuntimeError(f"未找到「{query}」相关歌曲")

        # 取第一首，后续可由插件层实现选歌交互
        song = results[0]
        audio_url = await self.music.get_song_url(song.song_id, song.platform, song.media_id)
        if not audio_url:
            raise RuntimeError(f"歌曲「{song.display()}」未获取到可播放音频")

        import aiohttp

        async with aiohttp.ClientSession() as session:
            song_audio = await _download_external(
                session, audio_url, timeout=aiohttp.ClientTimeout(total=120)
            )

        self.logger.info("下载整曲完成: %s, %d bytes", song.display(), len(song_audio))
        converted = await self.rvc.cover(
            song_audio,
            sid,
            uvr_model=uvr_model,
            agg=uvr_agg,
            uvr_weights_dir=uvr_weights_dir,
            with_instrumental=with_instrumental,
            **convert_kwargs,
        )
        self.logger.info("翻唱转换完成: %d bytes", len(converted))
        return song, converted

    async def speak(
        self,
        text: str,
        sid: str,
        *,
        voice_id: str = "mimo_default",
        reference_audio_base64: str = "",
        style_instruction: str = "",
        convert: bool = True,
        convert_kwargs: dict[str, Any] | None = None,
    ) -> bytes:
        """说话：MiMo TTS 合成原声 →（可选）RVC 换音色，返回语音 wav bytes。

        Args:
            convert: 是否在 TTS 之后经 RVC 换音色；为 False 时直接返回 TTS 原声。
        """
        convert_kwargs = convert_kwargs or {}
        if self.mimo is None:
            raise RuntimeError("MiMo TTS 未初始化")

        source = await self.mimo.synthesize(
            text,
            voice_id=voice_id,
            reference_audio_base64=reference_audio_base64,
            style_instruction=style_instruction,
        )
        self.logger.info("MiMo TTS 合成完成: %d bytes", len(source))
        if not convert:
            return source
        converted = await self.rvc.convert(source, sid, **convert_kwargs)
        self.logger.info("音色转换完成: %d bytes", len(converted))
        return converted

    @staticmethod
    def to_base64_url(audio: bytes) -> str:
        """转为 base64:// 形式，供语音条发送。"""
        return "base64://" + base64.b64encode(audio).decode("ascii")
