"""MaiBot 翻唱 + 语音插件。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ActivationType, ToolParameterInfo, ToolParamType

from .music.search import MusicSearchClient, MusicSearchError, SongInfo
from .rvc_client import RVCClient, RVCSidecarError
from .services.mimo_tts import MiMoTTSService
from .services.pipeline import Pipeline


# 语音发送的软截止：超过该时长仍未返回，先按失败上报（bot 会说"发不出去"），
# 但发送请求不会被取消，交由后台观察任务继续等待最终结果
_VOICE_SEND_DEADLINE_S = 180
# 语音发送底层 RPC 的真实等待上限（仅在后台观察时生效，需远大于软截止）
_VOICE_SEND_RPC_TIMEOUT_MS = 3_600_000
# 后台观察一次"结果未知"语音发送的最长时间
_LATE_VOICE_WATCH_S = 1_800
# base64 兜底受 16MB 传输帧上限约束（base64 膨胀 4/3），超限文件跳过兜底
_B64_FALLBACK_MAX_BYTES = 8 * 1024 * 1024
# 翻唱去重窗口：同一会话同一首歌在该时间内只执行一次管线、只发一条语音
_COVER_DEDUP_WINDOW_S = 600
# 期望的 sidecar 代码版本；sidecar/server.py 的 SIDECAR_VERSION 递增时同步修改。
# 端口上已有服务版本不匹配（残留旧代码进程）时，插件会终止它并重新拉起
EXPECTED_SIDECAR_VERSION = "4"


# ===== 配置模型 =====


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.2.0", description="配置版本")


class RVCConfig(PluginConfigBase):
    """RVC sidecar 配置（可迁移核心：改 rvc_root 即可迁移）。"""

    __ui_label__ = "RVC 声音转换"
    __ui_order__ = 1

    rvc_root: str = Field(default="D:/RVC20240604Nvidia", description="RVC 安装根目录")
    python_path: str = Field(default="", description="RVC Python 解释器路径，留空自动用 {rvc_root}/runtime/python.exe")
    port: int = Field(default=7898, description="sidecar 监听端口（避开 7897 WebUI）")
    auto_start: bool = Field(default=True, description="插件加载时自动拉起 sidecar（端口已有服务则复用）")
    default_model: str = Field(default="", description="默认音色模型（assets/weights 下的 .pth 文件名，含扩展名）")
    f0_method: str = Field(default="rmvpe", description="音高提取算法: pm/harvest/crepe/rmvpe")
    f0_up_key: int = Field(default=0, description="变调（半音数，升八度 12，降八度 -12）")
    model_keys: dict[str, int] = Field(
        default_factory=dict,
        description="按模型自动变调映射：模型文件名(含 .pth) → 变调半音数。命中时覆盖 f0_up_key",
    )
    index_rate: float = Field(default=0.5, description="检索特征占比 (0~1)，过高会产生金属感/沙哑")
    filter_radius: int = Field(default=3, description="harvest 中值滤波半径（>=3 可削弱哑音）")
    resample_sr: int = Field(default=0, description="后处理重采样至最终采样率，0 为不重采样")
    rms_mix_rate: float = Field(default=0.25, description="音量包络融合比例 (0~1)")
    protect: float = Field(default=0.4, description="清辅音保护力度 (0~0.5)，过低会口齿不清")
    uvr_model: str = Field(default="HP2_all_vocals", description="UVR5 人声分离模型名（assets/uvr5_weights 下，不含 .pth）")
    uvr_agg: int = Field(default=10, description="人声提取激进程度 (0~20)")
    uvr_weights_dir: str = Field(
        default="",
        description="外部 UVR5 权重目录（如 Ultimate Vocal Remover 的 VR_Models 绝对路径），留空用 RVC 自带 uvr5_weights",
    )
    auto_key: bool = Field(
        default=False,
        description="自动变调：按示例音频与歌曲人声的音高差自动计算变调半音数（model_keys 命中时不生效）",
    )
    auto_key_offset: int = Field(
        default=0,
        description="自动变调微调（半音）：在自动计算结果上额外升/降，正=升、负=降",
    )
    auto_key_max: int = Field(
        default=5,
        ge=1,
        description="自动变调幅度上限（半音绝对值）：变调过大音色会发哑，高音哑就调低此值（如 3）",
    )
    sample_audio: str = Field(
        default="",
        description="音色示例音频路径（该音色的唱歌片段，作为自动变调的基准音高）",
    )
    voice_cache_dir: str = Field(
        default="",
        description="语音条本地缓存目录（NapCat 可见的绝对路径），留空用插件运行时目录",
    )
    voice_cache_retention_days: int = Field(
        default=5,
        ge=0,
        description="翻唱语音缓存文件保留天数（按文件修改时间计算，0=永久保留）",
    )


class MiMoConfig(PluginConfigBase):
    """MiMo TTS 配置（说话功能的基础 TTS）。"""

    __ui_label__ = "MiMo TTS"
    __ui_order__ = 2

    api_key: str = Field(default="", description="MiMo API Key")
    api_base_url: str = Field(default="https://api.xiaomimimo.com/v1", description="MiMo API 地址")
    voice_mode: str = Field(default="preset", description="语音模式: 'preset'(预置音色) 或 'clone'(音色复刻)")
    preset_voice: str = Field(default="mimo_default", description="预置音色 ID（仅 preset 模式生效）")
    reference_audio: str = Field(default="", description="音色复刻参考音频文件路径（仅 clone 模式生效）")
    rvc_after_tts: bool = Field(
        default=True,
        description="说话功能：TTS 输出后再经 RVC 换音色；关闭则直接发送 TTS 原声",
    )


class MusicConfig(PluginConfigBase):
    """音乐搜索配置。"""

    __ui_label__ = "音乐搜索"
    __ui_order__ = 3

    default_platform: str = Field(default="163", description="默认音乐平台: 163(网易云) 或 qq(QQ音乐)")
    search_limit: int = Field(default=5, description="搜索结果数量")
    netease_account: str = Field(default="", description="网易云账号（手机号或邮箱），填写后自动密码登录")
    netease_password: str = Field(default="", description="网易云密码")
    netease_countrycode: str = Field(default="86", description="网易云手机号区号（手机号登录时生效）")
    netease_music_u: str = Field(default="", description="网易云 MUSIC_U 登录凭证（可选，账号密码登录的回退）")
    netease_csrf: str = Field(default="", description="网易云 __csrf 令牌（可选，与 MUSIC_U 配对）")
    qq_uin: str = Field(default="", description="QQ音乐 uin（可选，扫码登录的回退）")
    qq_key: str = Field(default="", description="QQ音乐 qqmusic_key（可选，扫码登录的回退）")


class ComponentConfig(PluginConfigBase):
    """组件开关。"""

    __ui_label__ = "组件"
    __ui_order__ = 4

    command_enabled: bool = Field(default=True, description="启用命令（/翻唱、/说、/音色列表、/qq音乐登录、/163logintest、/qqlogintest）")
    tool_enabled: bool = Field(default=True, description="启用工具（LLM 自主触发）")


class SingPluginConfig(PluginConfigBase):
    """翻唱插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    rvc: RVCConfig = Field(default_factory=RVCConfig)
    mimo: MiMoConfig = Field(default_factory=MiMoConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    components: ComponentConfig = Field(default_factory=ComponentConfig)


class SingPlugin(MaiBotPlugin):
    """翻唱 + 语音插件。"""

    config_model = SingPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._music: MusicSearchClient | None = None
        self._rvc: RVCClient | None = None
        self._mimo: MiMoTTSService | None = None
        self._pipeline: Pipeline | None = None
        self._sidecar_proc: asyncio.subprocess.Process | None = None
        # 待选歌曲状态: stream_id -> (结果列表, 平台, 时间戳)
        self._pending: dict[str, tuple[list[SongInfo], str, float]] = {}
        self._pending_lock = asyncio.Lock()
        # 结果未知的语音发送的后台观察任务
        self._late_voice_watchers: set[asyncio.Task[None]] = set()
        # QQ 扫码登录后台任务
        self._qq_login_task: asyncio.Task[None] | None = None
        # 网易云扫码登录后台任务
        self._netease_login_task: asyncio.Task[None] | None = None
        # 翻唱去重：进行中/刚完成的任务 (stream, query, model, 带伴奏) -> 运行信息
        self._cover_runs: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
        # 翻唱语音缓存定期清理任务
        self._cache_cleanup_task: asyncio.Task[None] | None = None

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        self.ctx.logger.info("翻唱插件加载中...")
        self._ensure_config_exists()

        # 每次启动先清理一次过期缓存（放在最前，避免被 sidecar 启动等待等慢步骤推迟）
        self._cache_cleanup_task = asyncio.create_task(self._voice_cache_cleanup_loop())

        # 初始化 RVC sidecar 客户端
        port = self.config.rvc.port
        self._rvc = RVCClient(f"http://127.0.0.1:{port}", logger=self.ctx.logger)

        # 自动拉起 sidecar（或复用已有服务）
        if self.config.rvc.auto_start:
            await self._start_sidecar()

        # 初始化音乐搜索与 MiMo TTS
        self._music = self._build_music_client()
        await self._restore_music_logins()
        self._mimo = MiMoTTSService(
            api_key=self.config.mimo.api_key,
            api_base_url=self.config.mimo.api_base_url,
            logger=self.ctx.logger,
        )
        self._pipeline = Pipeline(self._music, self._rvc, self._mimo, self.ctx.logger)
        self.ctx.logger.info("翻唱插件加载完成")

    async def on_unload(self) -> None:
        if self._music is not None:
            await self._music.close()
            self._music = None
        if self._mimo is not None:
            await self._mimo.close()
            self._mimo = None
        if self._rvc is not None:
            await self._rvc.close()
            self._rvc = None
        await self._stop_sidecar()
        self._pending.clear()
        for watcher in self._late_voice_watchers:
            watcher.cancel()
        self._late_voice_watchers.clear()
        if self._qq_login_task is not None:
            self._qq_login_task.cancel()
            self._qq_login_task = None
        if self._netease_login_task is not None:
            self._netease_login_task.cancel()
            self._netease_login_task = None
        if self._cache_cleanup_task is not None:
            self._cache_cleanup_task.cancel()
            self._cache_cleanup_task = None
        self.ctx.logger.info("翻唱插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self.ctx.logger.info("翻唱插件配置已更新，重建客户端")
        # 重建音乐与 MiMo 客户端（RVC 客户端仅端口可能变化，也重建）
        if self._music is not None:
            await self._music.close()
        if self._mimo is not None:
            await self._mimo.close()
        if self._rvc is not None:
            await self._rvc.close()
        await self._stop_sidecar()

        self._rvc = RVCClient(f"http://127.0.0.1:{self.config.rvc.port}", logger=self.ctx.logger)
        if self.config.rvc.auto_start:
            await self._start_sidecar()
        self._music = self._build_music_client()
        await self._restore_music_logins()
        self._mimo = MiMoTTSService(
            api_key=self.config.mimo.api_key,
            api_base_url=self.config.mimo.api_base_url,
            logger=self.ctx.logger,
        )
        self._pipeline = Pipeline(self._music, self._rvc, self._mimo, self.ctx.logger)
        # 保留期等缓存配置可能已变化，立即按新配置清理一次
        self._cleanup_voice_cache()

    # ===== sidecar 管理 =====

    def _ensure_config_exists(self) -> None:
        """如果插件目录下不存在 config.toml，则从 config.example.toml 复制生成。"""
        import shutil

        plugin_dir = Path(__file__).parent
        config_path = plugin_dir / "config.toml"
        example_path = plugin_dir / "config.example.toml"
        if config_path.exists():
            return
        if example_path.exists():
            shutil.copy2(example_path, config_path)
            self.ctx.logger.info("已从 config.example.toml 生成 config.toml")
        else:
            self.ctx.logger.warning("未找到 config.example.toml，请手动创建 config.toml")

    def _resolve_python_path(self) -> str:
        """解析 RVC Python 解释器路径。"""
        rvc_root = self.config.rvc.rvc_root
        configured = self.config.rvc.python_path.strip()
        if configured:
            return configured
        return str(Path(rvc_root) / "runtime" / "python.exe")

    def _sidecar_script_path(self) -> str:
        return str(Path(__file__).parent / "sidecar" / "server.py")

    async def _start_sidecar(self) -> None:
        """拉起 sidecar；端口已有服务时版本匹配则复用，残留旧代码进程则终止后重启。"""
        rvc = self._rvc
        if rvc is None:
            return
        port = self.config.rvc.port

        # 先探测端口是否已有服务
        try:
            health = await rvc.health()
        except Exception:
            health = None

        if health and health.get("status") == "ready":
            version = str(health.get("version", "") or "")
            if version == EXPECTED_SIDECAR_VERSION:
                self.ctx.logger.info("复用已运行的 sidecar: %s", port)
                return
            # 版本不匹配：端口上是旧代码的残留进程，终止后重新拉起
            self.ctx.logger.warning(
                "端口 %s 上的 sidecar 代码版本过旧（%s != %s），终止后重新拉起",
                port, version or "未知", EXPECTED_SIDECAR_VERSION,
            )
            await self._terminate_stale_sidecar(health)

        rvc_root = self.config.rvc.rvc_root
        python_exe = self._resolve_python_path()
        script = self._sidecar_script_path()

        if not Path(rvc_root).exists():
            self.ctx.logger.error("RVC 根目录不存在: %s", rvc_root)
            return
        if not Path(python_exe).exists():
            self.ctx.logger.error("RVC Python 解释器不存在: %s（请检查 rvc_root 或 python_path）", python_exe)
            return

        self.ctx.logger.info("拉起 sidecar: %s %s --rvc-root %s --port %s", python_exe, script, rvc_root, port)
        try:
            self._sidecar_proc = await asyncio.create_subprocess_exec(
                python_exe,
                script,
                "--rvc-root",
                rvc_root,
                "--port",
                str(port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            self.ctx.logger.error("拉起 sidecar 失败: %s", exc)
            return

        # 等待 sidecar 就绪（模型加载可能较慢，但服务本身几秒内可响应 /health）
        for _ in range(60):
            if self._sidecar_proc is not None and self._sidecar_proc.returncode is not None:
                self.ctx.logger.error("sidecar 进程提前退出，退出码: %s", self._sidecar_proc.returncode)
                return
            try:
                health = await rvc.health()
                if health.get("status") == "ready":
                    self.ctx.logger.info("sidecar 已就绪: %s", port)
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        self.ctx.logger.warning("等待 sidecar 就绪超时（60s），可能仍在加载模型")

    async def _terminate_stale_sidecar(self, health: dict[str, Any]) -> None:
        """终止版本过旧的 sidecar 进程并等待端口释放。"""
        proc = self._sidecar_proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
        else:
            # 进程不是本实例拉起的（如上次 MaiBot 异常退出遗留），按 /health 里的 PID 结束
            pid = health.get("pid")
            try:
                if pid:
                    os.kill(int(pid), 9)
            except Exception as exc:
                self.ctx.logger.warning("结束旧 sidecar 进程（pid=%s）失败: %s", pid, exc)
        self._sidecar_proc = None
        # 等旧进程释放端口
        for _ in range(10):
            try:
                await self._rvc.health()
                await asyncio.sleep(0.5)
            except Exception:
                return

    async def _ensure_sidecar_ready(self) -> None:
        """发起转换前确保 sidecar 存活；崩溃或被结束后自动重新拉起。"""
        if self._rvc is None:
            return
        try:
            health = await self._rvc.health()
            if health.get("status") == "ready":
                return
        except Exception:
            pass
        self.ctx.logger.warning("sidecar 未就绪，自动重新拉起")
        await self._start_sidecar()

    async def _stop_sidecar(self) -> None:
        proc = self._sidecar_proc
        self._sidecar_proc = None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

    def _build_music_client(self) -> MusicSearchClient:
        netease_cookie: dict[str, str] = {}
        if self.config.music.netease_music_u:
            netease_cookie["MUSIC_U"] = self.config.music.netease_music_u
        if self.config.music.netease_csrf:
            netease_cookie["__csrf"] = self.config.music.netease_csrf
        qq_cookie: dict[str, str] = {}
        if self.config.music.qq_uin:
            qq_cookie["uin"] = self.config.music.qq_uin
        if self.config.music.qq_key:
            qq_cookie["qqmusic_key"] = self.config.music.qq_key
        return MusicSearchClient(netease_cookie=netease_cookie, qq_cookie=qq_cookie)

    # ===== 音乐平台登录 =====

    def _login_cache_path(self) -> Path:
        return Path(self.ctx.paths.runtime_dir) / "music_login_cache.json"

    def _load_login_cache(self) -> dict[str, Any]:
        """读取平台登录态缓存文件（避免每次重启都重新登录）。"""
        try:
            with open(self._login_cache_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_login_cache(self, section: str, value: dict[str, Any]) -> None:
        try:
            data = self._load_login_cache()
            data[section] = value
            path = self._login_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.ctx.logger.warning("保存音乐登录缓存失败: %s", exc)

    async def _restore_music_logins(self) -> None:
        """插件加载/重建客户端后恢复网易云与 QQ 的登录态。"""
        if self._music is None:
            return
        cfg = self.config.music
        cache = self._load_login_cache()

        # QQ：优先用扫码登录缓存，其次退回配置里的 uin/key（已在客户端初始化）
        qq_cache = cache.get("qq") if isinstance(cache.get("qq"), dict) else {}
        if (
            not cfg.qq_uin.strip()
            and not cfg.qq_key.strip()
            and qq_cache.get("uin")
            and qq_cache.get("qqmusic_key")
        ):
            self._music.apply_qq_cookies({k: str(v) for k, v in qq_cache.items()})
            self.ctx.logger.info("已恢复 QQ 音乐缓存登录态: uin=%s", qq_cache["uin"])

        # 网易云：恢复持久化的设备 ID 与匿名 token（扫码接口需要，避免重复注册被限频）
        device = cache.get("netease_device") if isinstance(cache.get("netease_device"), dict) else {}
        if device.get("device_id"):
            self._music.set_netease_device(str(device["device_id"]), str(device.get("anon_token") or ""))

        # 网易云：配置了账号密码时自动登录（缓存有效则复用，失效自动重登）
        try:
            result = await self._ensure_netease_login()
            if result:
                self.ctx.logger.info("%s", result)
        except Exception as exc:
            self.ctx.logger.warning("网易云自动登录失败（将退回 cookie/未登录模式）: %s", exc)

    async def _ensure_netease_login(self, force: bool = False) -> str:
        """确保网易云处于登录态，返回结果说明。

        优先复用缓存登录态（含扫码登录的，先经接口校验），失效或 force 时：
        配置了账号密码则密码登录，否则提示重新扫码。
        """
        if self._music is None:
            return ""
        cfg = self.config.music
        account, password = cfg.netease_account.strip(), cfg.netease_password.strip()
        cache = self._load_login_cache().get("netease", {})
        cache_cookies = (cache.get("cookies") or {}).get("MUSIC_U")
        # 缓存登录态可复用：toml 账号匹配，或来自扫码登录（account 标记 qr）
        cache_matches = bool(cache_cookies) and (
            str(cache.get("account", "")) == account
            or str(cache.get("account", "")).startswith("qr")
            or str(cache.get("account", "")) == "cookie"
        )
        if not force and cache_matches:
            cookies = {k: str(v) for k, v in cache["cookies"].items()}
            self._music.apply_netease_cookies(cookies)
            try:
                profile = await self._music.get_netease_profile()
                self.ctx.logger.info("复用网易云缓存登录态: %s", profile["nickname"])
                return f"网易云登录正常：{profile['nickname']}"
            except Exception:
                self.ctx.logger.warning("网易云缓存登录态已失效，尝试重新登录")
        if not account or not password:
            if cache_cookies:
                return "网易云缓存登录态已失效，请发 /网易云音乐登录 重新扫码"
            return ""
        cookies = await self._music.login_netease(account, password, cfg.netease_countrycode)
        self._save_login_cache("netease", {"account": account, "cookies": cookies})
        profile = await self._music.get_netease_profile()
        self.ctx.logger.info("网易云自动登录成功: %s", account)
        return f"网易云登录成功：{profile['nickname']}"

    # ===== 工具方法 =====

    def _convert_kwargs(self, sid: str = "") -> dict[str, Any]:
        cfg = self.config.rvc
        f0_up_key, _ = self._resolve_f0_up_key(sid)
        return {
            "f0_up_key": f0_up_key,
            "f0_method": cfg.f0_method,
            "index_rate": cfg.index_rate,
            "filter_radius": cfg.filter_radius,
            "resample_sr": cfg.resample_sr,
            "rms_mix_rate": cfg.rms_mix_rate,
            "protect": cfg.protect,
        }

    def _resolve_f0_up_key(self, sid: str) -> tuple[int, bool]:
        """解析变调半音数，返回 (半音数, 是否命中 model_keys 手动映射)。

        每个 RVC 模型都有其训练时最合适的音域，翻唱时按模型自动适配变调。
        ``rvc.model_keys`` 键为模型文件名（含 .pth），值为该模型的变调半音数；
        手动映射优先于自动变调。
        """
        model = (sid or "").strip()
        if model:
            key = self.config.rvc.model_keys.get(model)
            if key is not None:
                return int(key), True
        return int(self.config.rvc.f0_up_key), False

    def _resolve_model(self, sid: str) -> str:
        """解析音色模型名，未指定时用配置默认值。"""
        model = sid.strip() if sid else self.config.rvc.default_model.strip()
        if not model:
            raise RuntimeError("未指定音色模型，请通过 -v 参数指定或在配置中设置 rvc.default_model")
        return model

    def _resolve_platform(self, platform: str) -> str:
        p = platform.strip().lower()
        if p in ("163", "qq"):
            return p
        if p in ("网易", "netease", "网易云音乐"):
            return "163"
        if p in ("qq音乐", "qqmusic"):
            return "qq"
        default = self.config.music.default_platform.strip().lower()
        return default if default in ("163", "qq") else "163"

    def _voice_cache_dir(self) -> Path:
        """翻唱语音缓存目录：配置了 voice_cache_dir 用之，否则用运行时目录。"""
        raw = self.config.rvc.voice_cache_dir.strip()
        return Path(raw).expanduser() if raw else Path(self.ctx.paths.runtime_dir)

    def _cleanup_voice_cache(self) -> int:
        """清理超过保留期的翻唱语音缓存文件，返回删除数量。"""
        retention_days = self.config.rvc.voice_cache_retention_days
        if retention_days <= 0:
            return 0
        cache_dir = self._voice_cache_dir()
        if not cache_dir.is_dir():
            return 0
        cutoff = time.time() - retention_days * 86400
        removed = 0
        for path in cache_dir.glob("sing_*.wav"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue  # 文件可能正被发送流程占用，跳过下次再清
        if removed:
            self.ctx.logger.info("已清理 %d 个超过 %d 天的翻唱语音缓存", removed, retention_days)
        return removed

    async def _voice_cache_cleanup_loop(self) -> None:
        """启动时清理一次，之后每 24 小时巡检一次。"""
        while True:
            try:
                self._cleanup_voice_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("清理翻唱语音缓存失败")
            await asyncio.sleep(24 * 3600)

    async def _send_voice(self, audio: bytes, stream_id: str) -> bool:
        """发送语音条（wav bytes）。

        翻唱整曲 wav 可达数十 MB，base64 后远超 16MB 传输帧上限，
        因此落地为本地文件，通过 voiceurl（文件路径）交给 NapCat 读取。

        兜底策略：仅当上一种方式明确返回 False（宿主确认未发出）才尝试下一种；
        调用超软截止仍未返回说明结果未知——消息可能仍在宿主侧投递中，
        此时严禁重发，否则语音条会发两遍；改为后台观察，若最终送达则补发
        一条文字说明（bot 此前已说"发不出去"）。
        """
        cache_dir = self._voice_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"sing_{uuid.uuid4().hex}.wav"
        cache_path.write_bytes(audio)

        # 生成标准 file URI（Windows 为 file:///D:/...），NapCat 侧可直接读取本地文件
        file_reference = "file:///" + cache_path.resolve().as_posix().lstrip("/")

        # 首选 voiceurl（文件路径，无大小限制）
        outcome = await self._send_custom_voice("voiceurl", {"url": file_reference}, stream_id)
        if outcome != "failed":
            return outcome == "sent"

        if len(audio) > _B64_FALLBACK_MAX_BYTES:
            self.ctx.logger.warning(
                "voiceurl 发送失败且音频过大（%d bytes），base64 兜底必超传输帧上限，放弃",
                len(audio),
            )
            return False

        # 兜底：走 base64 语音段（仅小文件可行）
        b64_url = "base64://" + base64.b64encode(audio).decode("ascii")
        for custom_type in ("record", "voice"):
            outcome = await self._send_custom_voice(custom_type, {"file": b64_url}, stream_id)
            if outcome == "sent":
                return True
            if outcome == "unknown":
                return False
            # "failed" → 尝试下一种
        return False

    async def _send_custom_voice(self, custom_type: str, data: dict[str, Any], stream_id: str) -> str:
        """发送一条语音消息，返回结果：``"sent"`` / ``"failed"`` / ``"unknown"``。

        底层 RPC 的真实超时给得很长（``_VOICE_SEND_RPC_TIMEOUT_MS``），
        插件侧用 ``asyncio.wait`` 做软截止——它不会取消发送任务；超软截止
        后按 ``"unknown"`` 处理，发送任务留在后台由观察任务继续等待结果。

        注意：宿主侧的发送一旦发出就无法撤回，所以 ``"unknown"`` 既可能
        最终送达也可能失败，唯一正确的做法是绝不重发，只观察。
        """
        send_task = asyncio.create_task(
            self.ctx.send.custom(
                custom_type,
                data,
                stream_id,
                timeout_ms=_VOICE_SEND_RPC_TIMEOUT_MS,
            )
        )
        done, _ = await asyncio.wait({send_task}, timeout=_VOICE_SEND_DEADLINE_S)
        if send_task not in done:
            self.ctx.logger.warning(
                "%s 语音发送超过 %ds 未返回，先按失败上报，后台继续确认结果",
                custom_type,
                _VOICE_SEND_DEADLINE_S,
            )
            self._spawn_late_voice_watcher(send_task, stream_id)
            return "unknown"
        try:
            return "sent" if send_task.result() else "failed"
        except Exception as exc:
            self.ctx.logger.warning("%s 语音发送结果未知: %s", custom_type, exc)
            return "unknown"

    def _spawn_late_voice_watcher(self, send_task: "asyncio.Task[bool]", stream_id: str) -> None:
        """后台观察一次结果未知的语音发送，确认送达后补发文字说明。"""

        async def _watch() -> None:
            try:
                done, _ = await asyncio.wait({send_task}, timeout=_LATE_VOICE_WATCH_S)
                if not done:
                    self.ctx.logger.warning("语音发送观察超过 %ds 仍未返回，放弃确认", _LATE_VOICE_WATCH_S)
                    return
                try:
                    sent = bool(send_task.result())
                except Exception:
                    return  # 最终确认失败，此前已如实上报，无需处理
                if sent:
                    self.ctx.logger.info("迟到确认：语音已送达 %s，补发提示", stream_id)
                    try:
                        await self.ctx.send.text("刚才那条语音其实发出去啦～", stream_id)
                    except Exception as exc:
                        self.ctx.logger.warning("补发语音送达提示失败: %s", exc)
            except asyncio.CancelledError:
                pass
            finally:
                self._late_voice_watchers.discard(asyncio.current_task())

        watcher = asyncio.create_task(_watch())
        self._late_voice_watchers.add(watcher)

    def _find_stream_id(self, stream_id: str, kwargs: dict[str, Any]) -> str:
        sid = str(stream_id or "").strip() or str(kwargs.get("stream_id", "") or "")
        if sid:
            return sid
        msg = kwargs.get("message", {})
        if isinstance(msg, dict):
            sid = str(msg.get("stream_id", "") or "")
            if sid:
                return sid
        return ""

    # ===== 命令 =====

    @Command(
        "翻唱",
        description="用克隆音色翻唱歌曲（搜歌 → 人声分离 → 换音色）",
        pattern=r"^(?P<pfx>\S)翻唱(?:\s+(?P<model>-v\s+\S+|\S+))?\s*(?P<query>.+)$",
        # 完整流程（搜歌+下载+分离+转换+发送）远超宿主默认 60s RPC 超时，
        # 超时会导致宿主判定失败而插件仍在后台把语音发出（bot 说失败但语音照发）
        timeout_ms=900_000,
    )
    async def handle_cover_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        matched = kwargs.get("matched_groups")
        if not isinstance(matched, dict):
            matched = {}
        query = str(matched.get("query", "") or "").strip()
        model = str(matched.get("model", "") or "").strip()
        # 去掉 -v 前缀
        model = re.sub(r"^-v\s*", "", model).strip()

        if not query:
            await self.ctx.send.text("用法：/翻唱 <歌名> [-v 模型名]", stream_id)
            return False, "缺少歌名", True

        try:
            model = self._resolve_model(model)
            song, audio, reused = await self._run_cover_dedup(query, model, stream_id)
            if reused:
                await self.ctx.send.text(f"这首歌正在翻唱中，语音稍后发送：「{song.display()}」", stream_id)
                return True, f"翻唱进行中: {song.display()}", True
        except Exception as exc:
            self.ctx.logger.exception("翻唱失败: %s", query)
            await self.ctx.send.text(f"翻唱失败：{exc}", stream_id)
            return False, str(exc), True

        ok = await self._send_voice(audio, stream_id)
        if ok:
            await self.ctx.send.text(f"已用克隆音色翻唱「{song.display()}」", stream_id)
        else:
            await self.ctx.send.text(f"翻唱完成但语音发送失败：「{song.display()}」", stream_id)
        return ok, f"翻唱: {song.display()}", True

    @Command(
        "说",
        description="用克隆音色说话（MiMo TTS → 换音色）",
        pattern=r"^(?P<pfx>\S)说\s+(?P<text>.+)$",
        timeout_ms=600_000,
    )
    async def handle_speak_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        matched = kwargs.get("matched_groups")
        if not isinstance(matched, dict):
            matched = {}
        text = str(matched.get("text", "") or "").strip()
        if not text:
            await self.ctx.send.text("用法：/说 <文本>", stream_id)
            return False, "缺少文本", True

        try:
            model = self._resolve_model("")
            audio = await self._run_speak(text, model, stream_id)
        except Exception as exc:
            self.ctx.logger.exception("说话失败")
            await self.ctx.send.text(f"说话失败：{exc}", stream_id)
            return False, str(exc), True

        ok = await self._send_voice(audio, stream_id)
        return ok, f"说: {text}", True

    @Command(
        "音色列表",
        description="列出可用 RVC 音色模型",
        pattern=r"^(?P<pfx>\S)音色列表$",
    )
    async def handle_list_models(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._rvc is None:
            await self.ctx.send.text("RVC 客户端未初始化", stream_id)
            return False, "RVC 客户端未初始化", True
        try:
            models = await self._rvc.list_models()
        except Exception as exc:
            self.ctx.logger.exception("获取音色列表失败")
            await self.ctx.send.text(f"获取音色列表失败：{exc}", stream_id)
            return False, str(exc), True
        if not models:
            await self.ctx.send.text("未找到音色模型（请检查 rvc_root 下的 assets/weights）", stream_id)
            return False, "无模型", True
        lines = ["可用音色模型："] + [f"  {name}" for name in models]
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, f"列出 {len(models)} 个模型", True

    @Command(
        "qq音乐登录",
        description="发起 QQ 音乐扫码登录，bot 发送二维码，手机 QQ 扫码确认即可（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)qq音乐登录\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_qq_music_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        self._start_qq_qrcode_login(stream_id)
        return True, "QQ 扫码登录已发起", True

    @Command(
        "网易云音乐登录",
        description="发起网易云扫码登录，bot 发送二维码，网易云音乐 App 扫码确认即可（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)网易云音乐登录\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_netease_music_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        self._start_netease_qrcode_login(stream_id)
        return True, "网易云扫码登录已发起", True

    @Command(
        "163cookie",
        description="用网易云 MUSIC_U cookie 登录（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)163cookie(?:\s+(?P<cookie>.+))?\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_netease_cookie_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        matched = kwargs.get("matched_groups")
        cookie_value = str(matched.get("cookie", "") or "").strip() if isinstance(matched, dict) else ""
        if not cookie_value:
            await self.ctx.send.text(
                "用法：/163cookie <MUSIC_U 值>\n"
                "获取：电脑浏览器登录 music.163.com → F12 → 应用/Storage → Cookie → 复制 MUSIC_U 的值\n"
                "（也可整段粘贴含 MUSIC_U=xxx; __csrf=yyy 的 cookie 字符串）", stream_id)
            return False, "缺少 cookie", True
        if self._music is None:
            await self.ctx.send.text("音乐客户端未初始化", stream_id)
            return False, "未初始化", True
        music_u, csrf = cookie_value, ""
        m = re.search(r"MUSIC_U=([^;]+)", cookie_value)
        if m:
            music_u = m.group(1).strip()
            c = re.search(r"__csrf=([^;]+)", cookie_value)
            csrf = c.group(1).strip() if c else ""
        self._music.apply_netease_cookies({"MUSIC_U": music_u, "__csrf": csrf})
        try:
            profile = await self._music.get_netease_profile()
        except Exception as exc:
            await self.ctx.send.text(f"❌ cookie 校验失败（可能已过期）：{exc}", stream_id)
            return False, str(exc), True
        self._save_login_cache("netease", {"account": "cookie", "cookies": self._music.get_netease_cookies()})
        await self.ctx.send.text(f"✅ 网易云登录正常：{profile['nickname']}", stream_id)
        return True, f"网易云 cookie 登录: {profile['nickname']}", True

    @Command(
        "163logintest",
        description="测试网易云登录态，成功时显示账号昵称（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)163logintest\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_netease_login_test(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._music is None:
            await self.ctx.send.text("音乐客户端未初始化", stream_id)
            return False, "未初始化", True
        try:
            profile = await self._music.get_netease_profile()
        except Exception as exc:
            await self.ctx.send.text(f"❌ 网易云登录态异常：{exc}", stream_id)
            return False, str(exc), True
        nickname = profile["nickname"] or "（昵称未知，但登录态有效）"
        await self.ctx.send.text(f"✅ 网易云登录正常：{nickname}", stream_id)
        return True, f"网易云登录正常: {nickname}", True

    @Command(
        "qqlogintest",
        description="测试 QQ 音乐登录态，成功时显示账号昵称（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)qqlogintest\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_qq_login_test(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._music is None:
            await self.ctx.send.text("音乐客户端未初始化", stream_id)
            return False, "未初始化", True
        try:
            profile = await self._music.get_qq_profile()
        except Exception as exc:
            await self.ctx.send.text(f"❌ QQ 音乐登录态异常：{exc}", stream_id)
            return False, str(exc), True
        nickname = profile["nickname"] or "（昵称未知，但登录态有效）"
        await self.ctx.send.text(f"✅ QQ 音乐登录正常：{nickname}", stream_id)
        return True, f"QQ 音乐登录正常: {nickname}", True

    # ===== QQ 扫码登录流程 =====

    def _start_qq_qrcode_login(self, stream_id: str) -> None:
        if self._music is None:
            asyncio.create_task(self.ctx.send.text("音乐客户端未初始化", stream_id))
            return
        if self._qq_login_task is not None and not self._qq_login_task.done():
            self._qq_login_task.cancel()
        self._qq_login_task = asyncio.create_task(self._run_qq_qrcode_login(stream_id))

    async def _run_qq_qrcode_login(self, stream_id: str) -> None:
        """发起 QQ 扫码登录：发二维码 → 轮询状态 → 确认后换票并缓存登录态。"""
        music = self._music
        if music is None:
            return
        try:
            png, qrsig = await music.qq_qrcode_start()
        except Exception as exc:
            await self.ctx.send.text(f"获取 QQ 登录二维码失败：{exc}", stream_id)
            return
        try:
            await self.ctx.send.image(base64.b64encode(png).decode("ascii"), stream_id)
        except Exception as exc:
            self.ctx.logger.warning("二维码图片发送失败: %s", exc)
            await self.ctx.send.text(f"二维码图片发送失败：{exc}", stream_id)
            return
        await self.ctx.send.text("请用手机 QQ 扫描二维码，并在手机上点击确认登录（3 分钟内有效）", stream_id)

        scanned_notified = False
        waited = 0.0
        interval = 2.5
        while waited < 180.0:
            await asyncio.sleep(interval)
            waited += interval
            try:
                state, extra = await music.qq_qrcode_poll(qrsig)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.ctx.send.text(f"查询扫码状态失败：{exc}", stream_id)
                return
            if state == "scanned" and not scanned_notified:
                scanned_notified = True
                await self.ctx.send.text("已扫码，请在手机上确认登录", stream_id)
            elif state == "expired":
                await self.ctx.send.text("二维码已过期，请重新发送 /音乐登录 qq", stream_id)
                return
            elif state == "success":
                uin, sigx = extra.split("|", 1)
                try:
                    cookies = await music.qq_qrcode_finish(uin, sigx)
                except Exception as exc:
                    await self.ctx.send.text(f"QQ 登录失败：{exc}", stream_id)
                    return
                self._save_login_cache("qq", cookies)
                await self.ctx.send.text(f"QQ 音乐登录成功（uin={cookies['uin']}），登录态已保存", stream_id)
                return
        await self.ctx.send.text("等待扫码超时，请重新发送 /qq音乐登录", stream_id)

    # ===== 网易云扫码登录流程 =====

    def _start_netease_qrcode_login(self, stream_id: str) -> None:
        if self._music is None:
            asyncio.create_task(self.ctx.send.text("音乐客户端未初始化", stream_id))
            return
        if self._netease_login_task is not None and not self._netease_login_task.done():
            self._netease_login_task.cancel()
        self._netease_login_task = asyncio.create_task(self._run_netease_qrcode_login(stream_id))

    async def _run_netease_qrcode_login(self, stream_id: str) -> None:
        """发起网易云扫码登录：发二维码 → 轮询状态 → 确认后保存登录态。"""
        music = self._music
        if music is None:
            return
        try:
            png, unikey = await music.netease_qrcode_start()
        except Exception as exc:
            await self.ctx.send.text(f"获取网易云登录二维码失败：{exc}", stream_id)
            return
        # 持久化设备 ID 与匿名 token（下次启动复用，避免匿名注册限频）
        device = music.get_netease_device()
        if device.get("anon_token"):
            self._save_login_cache("netease_device", device)
        try:
            await self.ctx.send.image(base64.b64encode(png).decode("ascii"), stream_id)
        except Exception as exc:
            self.ctx.logger.warning("二维码图片发送失败: %s", exc)
            await self.ctx.send.text(f"二维码图片发送失败：{exc}", stream_id)
            return
        token_note = (
            "" if music.get_netease_device().get("anon_token")
            else "（注意：本次二维码缺少环境令牌，确认时若提示环境异常，请改用 /163cookie 登录）"
        )
        await self.ctx.send.text(
            "请打开网易云音乐 App，用 App 内的「扫一扫」扫描此二维码（不要用 QQ/微信扫一扫或相机），"
            "扫描后在 App 弹出的页面点击【确认登录】。二维码 5 分钟内有效" + token_note, stream_id,
        )

        scanned_notified = False
        scanned_reminded = False
        waited = 0.0
        interval = 2.0
        # 轮询直到服务器判定二维码过期（800）或授权成功（803）。
        # unikey 的真实有效期由网易云服务器控制（实测 > 3 分钟），
        # 固定提前退出会导致"手机已确认但 bot 已停止监听"的错位
        while waited < 600.0:
            await asyncio.sleep(interval)
            waited += interval
            try:
                state = await music.netease_qrcode_poll(unikey)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.ctx.send.text(f"查询扫码状态失败：{exc}", stream_id)
                return
            if state == "scanned" and not scanned_notified:
                scanned_notified = True
                await self.ctx.send.text("已扫码，请在手机上点击【确认登录】完成授权", stream_id)
            if scanned_notified and waited > 60 and not scanned_reminded:
                scanned_reminded = True
                await self.ctx.send.text(
                    "仍未收到确认。请确认手机上已点击【确认登录】按钮；"
                    "若扫码后打开的是登录页而不是确认页，请改用网易云音乐 App 内的「扫一扫」重新扫描", stream_id)
            if state == "expired":
                await self.ctx.send.text(
                    "二维码已过期，请重新发送 /网易云音乐登录，扫码后请尽快在手机上点击【确认登录】", stream_id)
                return
            if state == "success":
                self._save_login_cache("netease", {"account": "qr", "cookies": music.get_netease_cookies()})
                nickname = ""
                try:
                    nickname = (await music.get_netease_profile())["nickname"]
                except Exception:
                    pass
                msg = "网易云扫码登录成功"
                if nickname:
                    msg += f"：{nickname}"
                await self.ctx.send.text(msg + "，登录态已保存", stream_id)
                return
        await self.ctx.send.text("等待扫码超时（10 分钟），请重新发送 /网易云音乐登录", stream_id)

    # ===== Tool =====

    @Tool(
        "cover_song",
        description=(
            "用克隆音色翻唱一首歌（bot 亲自开口唱）。仅当用户想让 bot 自己唱时调用，"
            "典型说法：「我想听你唱XX」「你唱一首XX」「翻唱XX」「用你的声音唱XX」。"
            "本工具会搜索歌曲、分离人声、用克隆音色替换，发送语音条。"
            "注意：用户只是想听这首歌的原唱/原曲时（如「放一首XX」「发一首XX」「来一首XX的歌」"
            "「放XX听听」），不要调用本工具，应改用 search_and_play_music。"
            "调用前可先自然回应一句（如「我试试」「好呀」）。"
            "若工具返回成功，说明语音条已发出，无需再补充任何文字；若返回失败，转述「发不出去」即可。"
            "with_instrumental 参数控制是否混入伴奏：用户只说歌名默认纯人声；"
            "当用户明确要求带伴奏、加上伴奏、有伴奏、跟着伴奏唱时传 true。"
        ),
        activation_type=ActivationType.ALWAYS,
        # 翻唱链路（下载 120s + sidecar 分离转换 600s + 发送）远超宿主默认 60s
        # 工具 RPC 超时；超时会让 bot 报"调用失败"而语音随后照发
        timeout_ms=900_000,
        parameters=[
            ToolParameterInfo(name="query", param_type=ToolParamType.STRING, description="歌曲名或关键词", required=True),
            ToolParameterInfo(name="with_instrumental", param_type=ToolParamType.BOOLEAN, description="是否混入伴奏（默认 false）", required=False),
        ],
    )
    async def handle_cover_tool(self, query: str = "", with_instrumental: bool = False, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if not query.strip():
            return {"content": "请提供歌曲名"}
        sid = self._find_stream_id(stream_id, kwargs)
        try:
            model = self._resolve_model("")
            song, audio, reused = await self._run_cover_dedup(query, model, sid, with_instrumental=with_instrumental)
            if reused:
                # 相同请求已在执行/发送，本调用不重复发送语音
                return {"content": "", "stop_after_execution": True}
            ok = await self._send_voice(audio, sid)
            if ok:
                # 语音条已发出，本工具不再输出文本，避免 MaiBot 额外说"我不会唱"
                return {"content": "", "stop_after_execution": True}
            return {"content": "翻唱完成但语音发不出去"}
        except Exception as exc:
            self.ctx.logger.exception("翻唱工具失败: %s", query)
            return {"content": f"翻唱失败：{exc}"}

    @Tool(
        "speak_voice",
        description=(
            "用克隆音色说话。当用户要求语音回复、发送了语音消息、或适合语音回复时调用。"
            "本工具会用基础 TTS 合成原声，再用克隆音色替换，发送语音条。"
        ),
        activation_type=ActivationType.ALWAYS,
        timeout_ms=600_000,
        parameters=[
            ToolParameterInfo(name="text", param_type=ToolParamType.STRING, description="要说的文本", required=True),
        ],
    )
    async def handle_speak_tool(self, text: str = "", stream_id: str = "", **kwargs: Any) -> dict[str, str]:
        if not text.strip():
            return {"content": "请提供文本"}
        sid = self._find_stream_id(stream_id, kwargs)
        try:
            model = self._resolve_model("")
            audio = await self._run_speak(text, model, sid)
            ok = await self._send_voice(audio, sid)
            return {"content": "已用克隆音色回复"} if ok else {"content": "语音合成完成但发送失败"}
        except Exception as exc:
            self.ctx.logger.exception("说话工具失败")
            return {"content": f"说话失败：{exc}"}

    # ===== 编排调用 =====

    async def _run_cover_dedup(
        self, query: str, sid: str, stream_id: str, *, with_instrumental: bool = False
    ) -> tuple[SongInfo, bytes, bool]:
        """执行翻唱管线；同一会话同一首歌的并发/短时重复请求只跑一次、只发一条语音。

        Returns:
            (歌曲信息, 音频, 是否复用既有结果)。复用时调用方**不得**再发送语音。
        """
        key = (stream_id, query.strip().lower(), sid, bool(with_instrumental))
        now = time.time()
        # 清理超过去重窗口的已完成条目
        for stale in [
            k
            for k, v in self._cover_runs.items()
            if v["task"].done() and now - v.get("done_at", 0.0) > _COVER_DEDUP_WINDOW_S
        ]:
            self._cover_runs.pop(stale, None)

        entry = self._cover_runs.get(key)
        if entry is not None and not entry["task"].done():
            self.ctx.logger.info("相同翻唱请求执行中，等待既有任务（不重复执行/发送）: %s", query)
            result = await asyncio.shield(entry["task"])
            return result[0], result[1], True
        if entry is not None and entry.get("done_at") and now - entry["done_at"] < _COVER_DEDUP_WINDOW_S:
            self.ctx.logger.info(
                "相同翻唱 %.0f 秒前已完成并发送，跳过重复执行: %s", now - entry["done_at"], query
            )
            return entry["result"][0], entry["result"][1], True

        task = asyncio.create_task(self._run_cover(query, sid, stream_id, with_instrumental=with_instrumental))
        entry = {"task": task, "done_at": 0.0, "result": None}
        self._cover_runs[key] = entry
        try:
            result = await task
        except BaseException:
            self._cover_runs.pop(key, None)
            raise
        entry["result"] = result
        entry["done_at"] = time.time()
        return result[0], result[1], False

    async def _run_cover(self, query: str, sid: str, stream_id: str, *, with_instrumental: bool = False) -> tuple[SongInfo, bytes]:
        if self._pipeline is None:
            raise RuntimeError("插件未初始化完成")
        await self._ensure_sidecar_ready()
        cfg = self.config
        convert_kwargs = self._convert_kwargs(sid)
        _, manual_key = self._resolve_f0_up_key(sid)
        rvc_cfg = cfg.rvc
        # 未配置手动映射且开启自动变调时，交给 sidecar 按示例音频音高计算
        if not manual_key and rvc_cfg.auto_key and rvc_cfg.sample_audio.strip():
            convert_kwargs["auto_key"] = True
            convert_kwargs["sample_audio"] = rvc_cfg.sample_audio.strip()
            convert_kwargs["auto_key_offset"] = rvc_cfg.auto_key_offset
            convert_kwargs["auto_key_max"] = rvc_cfg.auto_key_max
        platform = self._resolve_platform("")
        return await self._pipeline.cover_song(
            query,
            sid,
            platform=platform,
            search_limit=cfg.music.search_limit,
            uvr_model=rvc_cfg.uvr_model,
            uvr_agg=rvc_cfg.uvr_agg,
            uvr_weights_dir=rvc_cfg.uvr_weights_dir,
            with_instrumental=with_instrumental,
            convert_kwargs=convert_kwargs,
        )

    async def _run_speak(self, text: str, sid: str, stream_id: str) -> bytes:
        if self._pipeline is None:
            raise RuntimeError("插件未初始化完成")
        await self._ensure_sidecar_ready()
        cfg = self.config.mimo
        reference_b64 = ""
        if cfg.voice_mode == "clone":
            ref_path = cfg.reference_audio.strip()
            if not ref_path:
                raise RuntimeError("clone 模式需配置 mimo.reference_audio 参考音频路径")
            if not Path(ref_path).exists():
                raise RuntimeError(f"参考音频不存在: {ref_path}")
            reference_b64 = base64.b64encode(Path(ref_path).read_bytes()).decode("ascii")
        return await self._pipeline.speak(
            text,
            sid,
            voice_id=cfg.preset_voice,
            reference_audio_base64=reference_b64,
            convert=cfg.rvc_after_tts,
            convert_kwargs=self._convert_kwargs(sid),
        )


def create_plugin() -> SingPlugin:
    return SingPlugin()
