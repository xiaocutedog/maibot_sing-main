"""RVC 声音转换 sidecar 服务。

在 RVC 自带的 Python 3.9 运行时下启动，通过标准库 http.server 暴露
人声分离与音色转换接口，供 MaiBot 插件（Python 3.12）跨进程调用。

RVC 推理链依赖 Python 3.9 运行时，而 MaiBot 运行于 Python 3.12，
两者无法共用同一进程，因此以独立 sidecar 进程提供 HTTP 服务。

启动方式::

    runtime/python.exe sidecar/server.py --rvc-root D:/RVC20240604Nvidia --port 7898

端点:
    GET  /health    健康检查，返回 {"status": "ready", "device": "cuda:0"}
    GET  /models    列出可用音色模型（weight_root 顶层 *.pth 文件名）
    POST /separate  人声分离（UVR5），请求体为整曲音频，返回人声 wav
    POST /convert   音色转换（RVC），请求体为原声音频，返回换音色 wav
    POST /cover     翻唱一站式：分离 →（纯人声时裁剪静音）→ 换音色 →（可选混伴奏，
                    混伴奏时人声保留整段以对齐原曲时间轴）
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import librosa
import numpy as np
import soundfile as sf
import torch

# ---- 启动参数解析（先于 RVC 模块导入，避免 Config 解析到未知参数）----
_parser = argparse.ArgumentParser(add_help=True)
_parser.add_argument("--rvc-root", required=True, help="RVC 安装根目录")
_parser.add_argument("--port", type=int, default=7898, help="监听端口")
_args, _unknown = _parser.parse_known_args()

_RVC_ROOT: str = os.path.abspath(_args.rvc_root)
_PORT: int = int(_args.port)

# 切到 RVC 根目录并加入 sys.path，使 infer / configs 等模块可导入
os.chdir(_RVC_ROOT)
sys.path.insert(0, _RVC_ROOT)

# 设置 RVC 依赖的环境变量（使用绝对路径，保证可迁移）
os.environ["weight_root"] = os.path.join(_RVC_ROOT, "assets", "weights")
os.environ["weight_uvr5_root"] = os.path.join(_RVC_ROOT, "assets", "uvr5_weights")
os.environ["index_root"] = os.path.join(_RVC_ROOT, "logs")
os.environ["outside_index_root"] = os.path.join(_RVC_ROOT, "assets", "indices")
os.environ["rmvpe_root"] = os.path.join(_RVC_ROOT, "assets", "rmvpe")
os.environ["TEMP"] = os.path.join(_RVC_ROOT, "TEMP")
os.makedirs(os.environ["TEMP"], exist_ok=True)

# 将 ffmpeg 所在目录加入 PATH（RVC 根目录自带 ffmpeg.exe）
os.environ["PATH"] = _RVC_ROOT + os.pathsep + os.environ.get("PATH", "")

# 清空命令行参数，避免 Config.arg_parse 解析到 sidecar 自己的 --rvc-root
sys.argv = [sys.argv[0]]

# ---- 导入 RVC 推理模块（耗时，进程启动时一次性完成）----
from configs.config import Config  # noqa: E402
from infer.modules.uvr5.modules import uvr  # noqa: E402
from infer.modules.vc.modules import VC  # noqa: E402
from infer.modules.vc.utils import get_index_path_from_model  # noqa: E402

_CONFIG = Config()
_VC = VC(_CONFIG)

# 推理互斥锁：torch 模型非线程安全，convert / separate 串行执行
_LOCK = threading.Lock()
# sidecar 代码版本：修改 server.py 的处理逻辑时递增，
# 插件据此检测端口上是否残留旧代码进程（陈旧进程会被自动终止重启）
SIDECAR_VERSION = "4"
# 已加载的模型 sid，避免重复加载同一模型
_LAST_SID: str | None = None
# 自动变调用的 RMVPE 音高估计模型（懒加载）
_RMVPE = None
# 示例音频基频缓存: path -> (mtime, median_f0)
_SAMPLE_F0_CACHE: dict[str, tuple[float, float]] = {}


def _get_rmvpe():
    """懒加载 RMVPE 音高估计模型（与 RVC 翻唱使用同一模型，保证口径一致）。"""
    global _RMVPE
    if _RMVPE is None:
        from infer.lib.rmvpe import RMVPE

        _RMVPE = RMVPE(
            os.path.join(os.environ["rmvpe_root"], "rmvpe.pt"),
            is_half=_CONFIG.is_half,
            device=_CONFIG.device,
        )
    return _RMVPE


def _estimate_f0_median(samples: "np.ndarray", sr: int) -> float:
    """估计一段音频中有声部分的中位基频（Hz），无声或有效音高过少时返回 0。

    RMVPE 输入为 16kHz 单声道采样，返回逐帧基频（0 表示未检出）。
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if samples.size < sr // 2:
        return 0.0
    if sr != 16000:
        samples = librosa.resample(samples, orig_sr=sr, target_sr=16000)
    with _LOCK:
        f0 = _get_rmvpe().infer_from_audio(samples.astype(np.float32), thred=0.03)
    f0 = np.asarray(f0)
    voiced = f0[f0 > 0]
    if voiced.size < 50:
        return 0.0
    return float(np.median(voiced))


def _sample_audio_f0(path: str) -> float:
    """读取音色示例音频的中位基频，按 (路径, mtime) 缓存。"""
    mtime = os.path.getmtime(path)
    cached = _SAMPLE_F0_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    audio, sr = librosa.load(path, sr=16000, mono=True)
    f0 = _estimate_f0_median(audio, 16000)
    if f0 <= 0:
        raise RuntimeError(f"示例音频中未能检测到有效人声: {path}")
    _SAMPLE_F0_CACHE[path] = (mtime, f0)
    return f0


def list_models() -> list[str]:
    """列出 weight_root 顶层的可用音色模型文件名（含 .pth 扩展名）。"""
    weight_root = os.environ["weight_root"]
    return sorted(name for name in os.listdir(weight_root) if name.endswith(".pth"))


def _write_audio(raw: bytes, workdir: str, filename: str) -> str:
    """将请求体写入临时文件并返回路径。"""
    path = os.path.join(workdir, filename)
    with open(path, "wb") as f:
        f.write(raw)
    return path


def _normalize_peak(audio: np.ndarray, target_peak: float = 0.89) -> np.ndarray:
    """将 int16 音频峰值归一化到目标比例，避免响度过大或削波。

    RVC pipeline 返回的 ``audio_opt`` 已经是 int16（内部已按峰值 0.99 归一化）。
    这里按目标峰值重新缩放，仅当当前峰值高于目标时才压低，绝不放大弱信号。
    """
    audio = np.asarray(audio)
    peak = float(np.abs(audio).max())
    if peak <= 0:
        return audio
    scale = (target_peak * 32767.0) / peak
    if scale >= 1.0:
        return audio
    return (audio.astype(np.float64) * scale).round().astype(np.int16)


def _trim_silence(
    audio: np.ndarray,
    sr: int,
    *,
    threshold_factor: float = 0.01,
    pad_seconds: float = 0.05,
) -> np.ndarray:
    """裁剪音频前后接近静音的片段。

    Args:
        audio: 音频采样数据（1D 单声道或 2D 多声道）。
        sr: 采样率。
        threshold_factor: 判定为静音的幅度阈值，相对于整段峰值比例。
        pad_seconds: 在裁出的有效片段前后各保留的缓冲时长，避免切掉起音瞬态。

    Returns:
        裁剪后的采样数据；整段都低于阈值时原样返回。
    """
    audio = np.asarray(audio)
    if audio.ndim > 1:
        magnitude = np.abs(audio).max(axis=1)
    else:
        magnitude = np.abs(audio)

    peak = float(magnitude.max()) if magnitude.size else 0.0
    if peak <= 0:
        return audio

    threshold = peak * threshold_factor
    active = np.flatnonzero(magnitude > threshold)
    if active.size == 0:
        return audio

    start = int(active[0])
    end = int(active[-1]) + 1
    pad = int(pad_seconds * sr)
    start = max(0, start - pad)
    end = min(audio.shape[0], end + pad)
    if start == 0 and end == audio.shape[0]:
        return audio
    return audio[start:end]


def _mix_instrumental(
    vocal: np.ndarray,
    vocal_sr: int,
    instrumental: np.ndarray,
    ins_sr: int,
    *,
    gain: float = 0.85,
) -> np.ndarray:
    """将换音色后的人声与伴奏混音，返回 int16 单声道采样。

    人声（RVC 输出，int16）与伴奏（UVR 分离，可能为 float）量纲不一致，
    先统一归一化到 float（±1）域再混音。伴奏按 ``gain`` 缩放后与人声叠加，
    叠加后做峰值归一化防止削波。

    注意：必须返回 int16——调用方用 ``sf.write(..., subtype="PCM_16")`` 落盘，
    浮点输入会被 soundfile 按 ±1 满幅再乘 32768 写入，返回 ±32767 范围的
    浮点会导致全部样本溢出钳位成全幅方波（整条音频爆音）。
    """

    def _to_float(a: np.ndarray) -> np.ndarray:
        a = np.asarray(a)
        if a.dtype in (np.int16, np.int32):
            return a.astype(np.float64) / 32768.0
        return a.astype(np.float64)

    vocal = _to_float(vocal)
    instrumental = _to_float(instrumental)

    if instrumental.ndim > 1:
        instrumental = instrumental.mean(axis=1)
    if vocal.ndim > 1:
        vocal = vocal.mean(axis=1)

    # 对齐采样率
    if ins_sr != vocal_sr:
        instrumental = librosa.resample(instrumental, orig_sr=ins_sr, target_sr=vocal_sr)

    # 对齐长度：不足补零，超出截断
    if instrumental.shape[0] < vocal.shape[0]:
        instrumental = np.pad(instrumental, (0, vocal.shape[0] - instrumental.shape[0]))
    else:
        instrumental = instrumental[: vocal.shape[0]]

    mixed = vocal + instrumental * gain
    peak = float(np.abs(mixed).max()) if mixed.size else 0.0
    if peak > 1.0:
        mixed = mixed * (1.0 / peak)
    return (np.clip(mixed, -1.0, 1.0) * 32767.0 * 0.99).round().astype(np.int16)


def _pitch_shift(samples: "np.ndarray", sr: int, n_steps: int) -> "np.ndarray":
    """整体移调 n_steps 个半音，时长保持不变（变调不移速）。

    人声经 RVC 变调后时长不变，伴奏必须用同样"只变音高不变速"的方式跟随同一
    半音数，否则混音跑调；逐声道处理以兼容立体声 stem 与各版本 librosa。
    """
    samples = np.asarray(samples)
    if samples.ndim > 1:
        return np.stack(
            [
                librosa.effects.pitch_shift(np.ascontiguousarray(samples[:, ch]), sr=sr, n_steps=n_steps)
                for ch in range(samples.shape[1])
            ],
            axis=1,
        )
    return librosa.effects.pitch_shift(np.ascontiguousarray(samples), sr=sr, n_steps=n_steps)


def _first_wav(root: str) -> str:
    """返回目录下第一个 .wav 文件的路径，没有则返回空串。"""
    return next(
        (os.path.join(root, name) for name in os.listdir(root) if name.endswith(".wav")),
        "",
    )


def _separate_mdx(
    onnx_path: str,
    inp_path: str,
    vocal_path: str,
    ins_path: str,
    *,
    dim_f: int = 3072,
    dim_t: int = 8,
    n_fft: int = 6144,
    hop: int = 1024,
    compensate: float = 1.035,
    chunks: int = 4,
    margin: int = 44100,
) -> None:
    """用 MDX-Net（onnx）模型做人声分离。

    Kim Vocal 1 等 MDX-Net 模型直接输出人声（primary_stem=Vocals），
    伴奏 = 原曲 − 人声。模型时间维固定为 dim_t=256 帧，需分块推理并重叠相加。
    分块流程忠实复刻 RVC 自带 ``mdxnet.Predictor`` 的 demix 逻辑。
    """
    import onnxruntime as ort

    from infer.modules.uvr5.mdxnet import ConvTDFNetTrim

    # STFT/ISTFT 在 CPU 上做（与 RVC 自带 mdxnet.py 一致），onnx 推理优先 GPU、失败回退 CPU
    cpu = torch.device("cpu")
    net = ConvTDFNetTrim(cpu, "Conv-TDF", "vocals", 11, dim_f, dim_t, n_fft, hop)
    try:
        sess = ort.InferenceSession(
            onnx_path, providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
        )
    except Exception:
        sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])

    mix, rate = librosa.load(inp_path, mono=False, sr=44100)
    if mix.ndim == 1:
        mix = np.asfortranarray([mix, mix])
    samples = mix.shape[-1]

    outer_chunk = chunks * 44100
    if margin > outer_chunk:
        margin = outer_chunk
    trim = n_fft // 2
    # 内层 STFT 窗口与步长：net.chunk_size = hop*(dim_t-1)，步长减去前后各 trim
    inner_chunk = net.chunk_size
    gen_size = inner_chunk - 2 * trim

    # 外层分块，相邻块保留 margin 重叠
    segmented: dict[int, np.ndarray] = {}
    for skip in range(0, samples, outer_chunk):
        s_margin = 0 if skip == 0 else margin
        start = skip - s_margin
        end = min(skip + outer_chunk + margin, samples)
        segmented[skip] = mix[:, start:end].copy()
        if end == samples:
            break

    sources: list[np.ndarray] = []
    last_skip = max(segmented)
    for skip in segmented:
        cmix = segmented[skip]
        n_sample = cmix.shape[1]
        pad = (gen_size - n_sample % gen_size) % gen_size
        mix_p = np.concatenate(
            [np.zeros((2, trim)), cmix, np.zeros((2, pad)), np.zeros((2, trim))], 1
        )
        mix_waves = []
        i = 0
        while i < n_sample + pad:
            mix_waves.append(np.array(mix_p[:, i : i + inner_chunk]))
            i += gen_size
        waves = torch.tensor(np.stack(mix_waves), dtype=torch.float32)

        with torch.no_grad():
            spek = net.stft(waves)
            spec_pred = sess.run(None, {"input": spek.numpy()})[0]
            tar = net.istft(torch.tensor(spec_pred))
        # 去掉前后 trim 后，每窗长 gen_size，拼接后截回本块的 n_sample 个样本
        tar_signal = (
            tar[:, :, trim:-trim]
            .transpose(0, 1)
            .reshape(2, -1)
            .numpy()[:, :n_sample]
        )

        out_start = 0 if skip == 0 else margin
        out_end = None if skip == last_skip else -margin
        sources.append(tar_signal[:, out_start:out_end])

    vocal = np.concatenate(sources, axis=-1) * compensate
    vocal = np.clip(vocal, -1.0, 1.0)
    instrumental = mix - vocal
    # 相减结果可达 ±2，PCM_16 容纳不下；直接写盘会在鼓点等瞬态处被硬削波，
    # 混音后表现为伴奏爆音。这里整体缩放回 ±1 内，只改增益不引入削波失真
    ins_peak = float(np.abs(instrumental).max())
    if ins_peak > 1.0:
        instrumental = instrumental / ins_peak * 0.99

    sf.write(vocal_path, vocal.T, rate, subtype="PCM_16")
    sf.write(ins_path, instrumental.T, rate, subtype="PCM_16")


def _resolve_uvr_model_path(model: str, uvr_weights_dir: str) -> str:
    """解析分离模型完整路径，返回 (路径, 是否 MDX-onnx)。"""
    base = uvr_weights_dir or os.environ.get("weight_uvr5_root", "")
    candidate = os.path.join(base, model)
    if candidate.endswith(".onnx"):
        return candidate
    if not os.path.exists(candidate) and os.path.exists(candidate + ".onnx"):
        return candidate + ".onnx"
    return candidate


def _run_separation(
    model: str,
    inp_root: str,
    vocal_root: str,
    ins_root: str,
    agg: int,
    uvr_weights_dir: str = "",
) -> None:
    """按模型架构分派人声分离：MDX-Net（onnx）走独立推理，VR 走 RVC uvr()。"""
    model_path = _resolve_uvr_model_path(model, uvr_weights_dir)
    if model_path.endswith(".onnx"):
        inp_path = _first_wav(inp_root)
        if not inp_path:
            raise RuntimeError("待分离音频不存在")
        vocal_path = os.path.join(vocal_root, "vocal_song.wav")
        ins_path = os.path.join(ins_root, "instrument_song.wav")
        with _LOCK:
            _separate_mdx(model_path, inp_path, vocal_path, ins_path)
        return

    # VR 架构：临时切换权重目录后走 RVC uvr()
    old = os.environ.get("weight_uvr5_root")
    try:
        with _LOCK:
            if uvr_weights_dir:
                os.environ["weight_uvr5_root"] = uvr_weights_dir
            # uvr 为生成器，需要完整迭代才能完成分离
            for _ in uvr(model, inp_root, vocal_root, [], ins_root, agg, "wav"):
                pass
    finally:
        if old is None:
            os.environ.pop("weight_uvr5_root", None)
        else:
            os.environ["weight_uvr5_root"] = old


class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    def log_message(self, format: str, *args: Any) -> None:
        """静默默认访问日志，避免刷屏。"""

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_audio(self, audio: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {
                "status": "ready",
                "device": _CONFIG.device,
                "version": SIDECAR_VERSION,
                "pid": os.getpid(),
            })
            return
        if parsed.path == "/models":
            self._send_json(200, {"models": list_models()})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/separate":
                self._handle_separate(query)
            elif parsed.path == "/convert":
                self._handle_convert(query)
            elif parsed.path == "/cover":
                self._handle_cover(query)
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 — 统一返回错误，避免进程崩溃
            traceback.print_exc()
            self._send_json(500, {"error": str(exc)})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _handle_separate(self, query: dict[str, list[str]]) -> None:
        model = query.get("model", ["HP2_all_vocals"])[0]
        agg = int(query.get("agg", ["10"])[0])
        uvr_weights_dir = query.get("uvr_weights_dir", [""])[0]
        raw = self._read_body()

        workdir = tempfile.mkdtemp(dir=os.environ["TEMP"])
        inp_root = os.path.join(workdir, "input")
        vocal_root = os.path.join(workdir, "vocal")
        ins_root = os.path.join(workdir, "ins")
        os.makedirs(inp_root)
        os.makedirs(vocal_root)
        os.makedirs(ins_root)
        try:
            _write_audio(raw, inp_root, "song.wav")
            _run_separation(model, inp_root, vocal_root, ins_root, agg, uvr_weights_dir)

            result_path = _first_wav(vocal_root)
            if not result_path:
                raise RuntimeError("人声分离未生成结果文件")

            # 读取分离后的人声，裁剪前后静音片段再回写
            audio_data, sr = sf.read(result_path, dtype="float32", always_2d=False)
            trimmed = _trim_silence(audio_data, sr)
            buf = io.BytesIO()
            sf.write(buf, trimmed, sr, format="wav", subtype="PCM_16")
            audio = buf.getvalue()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        self._send_audio(audio)

    def _convert_raw(
        self,
        query: dict[str, list[str]],
        raw: bytes,
        f0_up_key_override: int | None = None,
    ) -> tuple[np.ndarray, int]:
        """执行 RVC 音色转换，返回 (int16 采样, 采样率)。

        ``f0_up_key_override`` 非空时（自动变调计算结果）优先于 query 里的 f0_up_key。
        """
        sid = query.get("sid", [""])[0]
        spk_id = int(query.get("spk_id", ["0"])[0])
        if f0_up_key_override is not None:
            f0_up_key = int(f0_up_key_override)
        else:
            f0_up_key = int(query.get("f0_up_key", ["0"])[0])
        f0_method = query.get("f0_method", ["rmvpe"])[0]
        index_rate = float(query.get("index_rate", ["0.75"])[0])
        filter_radius = int(query.get("filter_radius", ["3"])[0])
        resample_sr = int(query.get("resample_sr", ["0"])[0])
        rms_mix_rate = float(query.get("rms_mix_rate", ["0.25"])[0])
        protect = float(query.get("protect", ["0.33"])[0])

        if not sid:
            raise RuntimeError("缺少 sid 参数（音色模型文件名）")

        workdir = tempfile.mkdtemp(dir=os.environ["TEMP"])
        inp_path = _write_audio(raw, workdir, "input.wav")
        try:
            with _LOCK:
                global _LAST_SID
                if _LAST_SID != sid:
                    _VC.get_vc(sid)
                    _LAST_SID = sid
                index = get_index_path_from_model(sid)
                info, (tgt_sr, audio_opt) = _VC.vc_single(
                    spk_id,
                    inp_path,
                    f0_up_key,
                    None,
                    f0_method,
                    index,
                    "",
                    index_rate,
                    filter_radius,
                    resample_sr,
                    rms_mix_rate,
                    protect,
                )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if not info.startswith("Success"):
            raise RuntimeError(f"音色转换失败: {info}")

        # RVC pipeline 返回的 audio_opt 已是 int16（内部峰值归一化到 0.99）。
        # 这里只做峰值压低，避免直接按 [-1,1] 二次缩放把 int16 当成浮点压成方波。
        return _normalize_peak(audio_opt), tgt_sr

    def _handle_convert(self, query: dict[str, list[str]]) -> None:
        raw = self._read_body()
        audio_int16, tgt_sr = self._convert_raw(query, raw)
        buf = io.BytesIO()
        sf.write(buf, audio_int16, tgt_sr, format="wav", subtype="PCM_16")
        self._send_audio(buf.getvalue())

    def _handle_cover(self, query: dict[str, list[str]]) -> None:
        """翻唱一站式：分离 →（纯人声时裁剪静音）→ 换音色 →（可选混伴奏）。"""
        sid = query.get("sid", [""])[0]
        uvr_model = query.get("uvr_model", ["HP2_all_vocals"])[0]
        agg = int(query.get("agg", ["10"])[0])
        uvr_weights_dir = query.get("uvr_weights_dir", [""])[0]
        trim = query.get("trim", ["1"])[0] not in ("0", "false", "False", "no")
        with_instrumental = query.get("with_instrumental", ["0"])[0] in (
            "1", "true", "True", "yes",
        )
        auto_key = query.get("auto_key", ["0"])[0] in ("1", "true", "True", "yes")
        sample_audio = query.get("sample_audio", [""])[0]
        try:
            auto_key_offset = int(query.get("auto_key_offset", ["0"])[0])
        except ValueError:
            auto_key_offset = 0
        try:
            auto_key_max = int(query.get("auto_key_max", ["5"])[0])
        except ValueError:
            auto_key_max = 5
        auto_key_max = max(1, auto_key_max)
        raw = self._read_body()

        workdir = tempfile.mkdtemp(dir=os.environ["TEMP"])
        inp_root = os.path.join(workdir, "input")
        vocal_root = os.path.join(workdir, "vocal")
        ins_root = os.path.join(workdir, "ins")
        os.makedirs(inp_root)
        os.makedirs(vocal_root)
        os.makedirs(ins_root)
        try:
            _write_audio(raw, inp_root, "song.wav")
            _run_separation(uvr_model, inp_root, vocal_root, ins_root, agg, uvr_weights_dir)

            vocal_path = _first_wav(vocal_root)
            if not vocal_path:
                raise RuntimeError("人声分离未生成结果文件")

            # 读取分离后的人声。仅纯人声输出时裁剪前后静音；
            # 带伴奏时必须保留整段——伴奏按原曲时间轴混音，
            # 裁剪人声会让它相对伴奏整体前移，造成人声与伴奏错位
            vocal_data, vocal_sr = sf.read(vocal_path, dtype="float32", always_2d=False)
            if trim and not with_instrumental:
                vocal_data = _trim_silence(vocal_data, vocal_sr)

            vocal_buf = io.BytesIO()
            sf.write(vocal_buf, vocal_data, vocal_sr, format="wav", subtype="PCM_16")

            # 自动变调：以示例音频的中位基频为基准，把歌曲人声整体移到该音高上。
            # 纯人声裁剪与否不影响中位基频的估计
            f0_override = None
            if auto_key:
                if not sample_audio or not os.path.exists(sample_audio):
                    raise RuntimeError(f"自动变调需要有效的示例音频: {sample_audio or '未配置'}")
                song_f0 = _estimate_f0_median(vocal_data, vocal_sr)
                if song_f0 <= 0:
                    raise RuntimeError("歌曲人声未能检测到有效音高，无法自动变调")
                sample_f0 = _sample_audio_f0(sample_audio)
                # 变调幅度越大音色越哑，默认钳制在 ±5 半音内（可通过 auto_key_max 调整）
                f0_override = int(
                    max(-auto_key_max, min(auto_key_max, round(12.0 * math.log2(sample_f0 / song_f0)) + auto_key_offset))
                )
                print(
                    f"自动变调: 歌曲人声 {song_f0:.1f}Hz, 示例音频 {sample_f0:.1f}Hz -> f0_up_key={f0_override}"
                    f"（含微调 {auto_key_offset:+d}，上限 ±{auto_key_max}）",
                    flush=True,
                )

            converted, tgt_sr = self._convert_raw(query, vocal_buf.getvalue(), f0_up_key_override=f0_override)

            if not with_instrumental:
                out_buf = io.BytesIO()
                sf.write(out_buf, converted, tgt_sr, format="wav", subtype="PCM_16")
                self._send_audio(out_buf.getvalue())
                return

            ins_path = _first_wav(ins_root)
            if not ins_path:
                raise RuntimeError("伴奏分离未生成结果文件")
            instrumental, ins_sr = sf.read(ins_path, dtype="float32", always_2d=False)
            # 人声变调后（无论来源：自动变调/手动微调/model_keys），伴奏必须跟随
            # 相同的半音数（时长不变），否则人声与伴奏的调性不一致，整首歌跑调
            vocal_shift = f0_override if f0_override is not None else int(query.get("f0_up_key", ["0"])[0])
            if vocal_shift:
                instrumental = _pitch_shift(instrumental, ins_sr, vocal_shift)
            mixed = _mix_instrumental(converted, tgt_sr, instrumental, ins_sr)
            out_buf = io.BytesIO()
            sf.write(out_buf, mixed, tgt_sr, format="wav", subtype="PCM_16")
            self._send_audio(out_buf.getvalue())
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", _PORT), Handler)
    print(f"RVC sidecar listening on 127.0.0.1:{_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
