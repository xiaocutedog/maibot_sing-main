"""RVC sidecar HTTP 客户端。

通过 aiohttp 调用 sidecar 的 /health、/models、/separate、/convert、/cover、/shutdown
端点。所有请求都带上 ``X-Sidecar-Token`` 鉴权头，避免同主机其它进程直接调用推理接口。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import aiohttp

# 令牌握手盐值：与 sidecar/server.py 的 _AUTH_PROOF_SALT 必须一致
_AUTH_PROOF_SALT = b"maibot-sing-sidecar-v1"


class RVCSidecarError(RuntimeError):
    """sidecar 调用失败。"""


class RVCClient:
    """RVC sidecar 客户端。"""

    def __init__(self, base_url: str, logger: logging.Logger | None = None, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.logger = logger or logging.getLogger(__name__)
        self._session: aiohttp.ClientSession | None = None
        self._headers = {"X-Sidecar-Token": self.token} if self.token else {}

    def auth_proof(self) -> str:
        """本客户端令牌对应的握手校验值，用于确认端口上的服务确实持有同一令牌。"""
        if not self.token:
            return ""
        return hmac.new(self.token.encode("utf-8"), _AUTH_PROOF_SALT, hashlib.sha256).hexdigest()[:16]

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # convert 单次推理可能耗时数分钟，超时给足
            timeout = aiohttp.ClientTimeout(total=600)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def health(self) -> dict[str, Any]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/health", headers=self._headers or None) as resp:
            if resp.status != 200:
                raise RVCSidecarError(f"sidecar 健康检查失败: {resp.status}")
            return await resp.json()

    async def shutdown(self) -> None:
        """请求 sidecar 自行退出（仅持有正确令牌的调用方可用）。

        用于回收本插件此前拉起、但进程句柄已丢失的旧版本 sidecar——
        不用给未知 PID 发信号，避免误杀同主机其它进程。
        """
        session = await self._get_session()
        async with session.post(f"{self.base_url}/shutdown", headers=self._headers or None) as resp:
            if resp.status != 200:
                raise RVCSidecarError(f"请求 sidecar 退出失败: {resp.status}")

    async def list_models(self) -> list[str]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/models", headers=self._headers or None) as resp:
            if resp.status != 200:
                raise RVCSidecarError(f"获取模型列表失败: {resp.status}")
            data = await resp.json()
            return list(data.get("models", []))

    async def separate(
        self,
        audio: bytes,
        model: str = "HP2_all_vocals",
        agg: int = 10,
        uvr_weights_dir: str = "",
    ) -> bytes:
        """人声分离，返回人声 wav bytes。

        ``uvr_weights_dir`` 非空时，sidecar 会临时切换到该目录读取 UVR 模型，
        从而支持使用外部 Ultimate Vocal Remover 的模型文件。
        """
        session = await self._get_session()
        params: dict[str, str] = {"model": model, "agg": str(agg)}
        if uvr_weights_dir:
            params["uvr_weights_dir"] = uvr_weights_dir
        async with session.post(
            f"{self.base_url}/separate",
            params=params,
            data=audio,
            headers={**self._headers, "Content-Type": "application/octet-stream"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RVCSidecarError(f"人声分离失败: {resp.status} {body[:300]}")
            return await resp.read()

    async def convert(
        self,
        audio: bytes,
        sid: str,
        *,
        spk_id: int = 0,
        f0_up_key: int = 0,
        f0_method: str = "rmvpe",
        index_rate: float = 0.75,
        filter_radius: int = 3,
        resample_sr: int = 0,
        rms_mix_rate: float = 0.25,
        protect: float = 0.33,
    ) -> bytes:
        """音色转换，返回换音色后的 wav bytes。

        sid 为模型文件名（传给 get_vc），spk_id 为模型内说话人索引（传给 vc_single）。
        """
        params = {
            "sid": sid,
            "spk_id": str(spk_id),
            "f0_up_key": str(f0_up_key),
            "f0_method": f0_method,
            "index_rate": str(index_rate),
            "filter_radius": str(filter_radius),
            "resample_sr": str(resample_sr),
            "rms_mix_rate": str(rms_mix_rate),
            "protect": str(protect),
        }
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/convert",
            params=params,
            data=audio,
            headers={**self._headers, "Content-Type": "application/octet-stream"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RVCSidecarError(f"音色转换失败: {resp.status} {body[:300]}")
            return await resp.read()

    async def cover(
        self,
        audio: bytes,
        sid: str,
        *,
        uvr_model: str = "HP2_all_vocals",
        agg: int = 10,
        uvr_weights_dir: str = "",
        trim: bool = True,
        with_instrumental: bool = False,
        auto_key: bool = False,
        sample_audio: str = "",
        auto_key_offset: int = 0,
        auto_key_max: int = 5,
        spk_id: int = 0,
        f0_up_key: int = 0,
        f0_method: str = "rmvpe",
        index_rate: float = 0.75,
        filter_radius: int = 3,
        resample_sr: int = 0,
        rms_mix_rate: float = 0.25,
        protect: float = 0.33,
    ) -> bytes:
        """翻唱一站式：分离 →（可选裁剪静音）→ 换音色 →（可选混伴奏）。

        一次 HTTP 调用完成整条链路，返回最终 wav bytes。
        ``trim`` 仅对纯人声输出生效；混伴奏时人声保留整段以对齐伴奏时间轴。
        ``auto_key`` 开启且提供 ``sample_audio`` 时，由 sidecar 按示例音频音高
        自动计算变调，覆盖 ``f0_up_key``。
        """
        params = {
            "sid": sid,
            "uvr_model": uvr_model,
            "agg": str(agg),
            "trim": "1" if trim else "0",
            "with_instrumental": "1" if with_instrumental else "0",
            "spk_id": str(spk_id),
            "f0_up_key": str(f0_up_key),
            "f0_method": f0_method,
            "index_rate": str(index_rate),
            "filter_radius": str(filter_radius),
            "resample_sr": str(resample_sr),
            "rms_mix_rate": str(rms_mix_rate),
            "protect": str(protect),
        }
        if auto_key and sample_audio:
            params["auto_key"] = "1"
            params["sample_audio"] = sample_audio
            params["auto_key_offset"] = str(auto_key_offset)
            params["auto_key_max"] = str(auto_key_max)
        if uvr_weights_dir:
            params["uvr_weights_dir"] = uvr_weights_dir
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/cover",
            params=params,
            data=audio,
            headers={**self._headers, "Content-Type": "application/octet-stream"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RVCSidecarError(f"翻唱转换失败: {resp.status} {body[:300]}")
            return await resp.read()
