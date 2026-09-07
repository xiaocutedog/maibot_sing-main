"""MiMo TTS 服务 — 说话功能的基础 TTS，合成"原声"供 RVC 换音色。

精简自 maibot-mimotts-voice 的 tts_service.py，只保留预置音色与
音色复刻两种合成能力，返回音频 bytes（wav）。

依赖: aiohttp。
"""

from __future__ import annotations

import gc
import logging
from typing import Any

import aiohttp


class MiMoTTSService:
    """MiMo TTS 服务。"""

    DEFAULT_API_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
    MODEL_PRESET = "mimo-v2.5-tts"
    MODEL_CLONE = "mimo-v2.5-tts-voiceclone"

    def __init__(self, api_key: str, api_base_url: str = "", logger: logging.Logger | None = None) -> None:
        self.api_key = api_key.strip() if api_key else ""
        self.api_base_url = api_base_url.strip() if api_base_url else self.DEFAULT_API_BASE_URL
        self.logger = logger or logging.getLogger(__name__)
        self._session: aiohttp.ClientSession | None = None

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "mimo_default",
        reference_audio_base64: str = "",
        style_instruction: str = "",
        audio_format: str = "wav",
    ) -> bytes:
        """合成语音，返回音频 bytes。

        reference_audio_base64 为空时走预置音色（voice_id），否则走音色复刻。
        """
        if not self.api_key:
            raise RuntimeError("MiMo API Key 未配置")

        messages: list[dict[str, str]] = []
        messages.append({"role": "user", "content": style_instruction if style_instruction else ""})
        messages.append({"role": "assistant", "content": text})

        if reference_audio_base64:
            model = self.MODEL_CLONE
            voice_str = (
                reference_audio_base64
                if reference_audio_base64.startswith("data:")
                else f"data:audio/wav;base64,{reference_audio_base64}"
            )
        else:
            model = self.MODEL_PRESET
            voice_str = voice_id

        payload = {
            "model": model,
            "modalities": ["text", "audio"],
            "messages": messages,
            "audio": {"format": audio_format, "voice": voice_str},
        }
        headers = {"api-key": self.api_key, "Content-Type": "application/json"}

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.api_base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"MiMo API 失败: {response.status} - {error_text[:200]}")

                result = await response.json()
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"MiMo 网络错误: {exc}") from exc
        finally:
            del payload, messages
            gc.collect()

        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError("MiMo 无返回结果")
        audio_data = choices[0].get("message", {}).get("audio", {})
        audio_base64 = audio_data.get("data", "")
        if not audio_base64:
            raise RuntimeError("MiMo 无音频数据")

        import base64

        return base64.b64decode(audio_base64)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
