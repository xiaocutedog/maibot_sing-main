"""音乐搜索客户端 — 内嵌网易云音乐与 QQ 音乐搜索/取链/登录。

精简自 maibot-music 的 MusicSearchClient，只保留翻唱流程需要的
「搜索 → 获取可播放音频 URL」能力，去掉音乐卡片解析等无关逻辑，
并补充网易云账号密码登录与 QQ 扫码登录。

依赖: httpx + cryptography（网易云 eapi/weapi 加密）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = __import__("logging").getLogger("maibot-sing.music")

_REQUEST_TIMEOUT = 10

# 网易云音乐 eapi 加密密钥（16 字节 AES-128-ECB）
_EAPI_KEY = b"e82ckenh8dichen8"

# 网易云客户端 api 加密（interface3）相关
_INTERFACE3_DOMAIN = "https://interface3.music.163.com"
_NETEASE_ID_XOR_KEY = "3go8&$8*3*3h0k(2)2"
_API_IPHONE_UA = "NeteaseMusic 9.0.90/5038 (iPhone; iOS 16.2; zh_CN)"

# 真实设备 ID 池（来自社区项目，用于匿名注册降低风控概率）
_NETEASE_DEVICE_POOL = [
        "85530C2D7F213CEB2D6BABCEE91C19CDC1ABE562C6B92B2C3F0C",
        "606E155DC61301B1C6D4D35D7B7B60F8B7959BE3C35C18124F85",
        "8502932B4355A674F0038D2489919507C186E8BC39156710106C",
        "6AF79C72C653FDBFA917D44B004BD315C470780029F06450583F",
        "980808E8744653D2DF6CACE7B89B2D8BA9CCBA6A48020F4BC15D",
        "D3AE2BD7B7C2F7FF617DDD80D6EB551191585EE788FCB63D7C96",
        "2D38B34D961BF9F8B1D4B8A53A7491D849276B2C13EA18B9CD19",
        "A0760988FF3358DE0A45FCDE522F2BBF71CB2FC89C5BF008750D",
        "6284962FC18AC0E9C69CA2D1B2F42ABF299FE8331DC113F392B9",
        "E22346CDD1A471D5823688D4E6C1D7B1CF86A7B9310410A3C139",
        "1B9827C26304A65B79AFDAC0F4C11619D6677B98D70C4082F44F",
        "711CA28B9221062F86A9CBD7BA3FAD2FFBCE10A0957F0B772FE2",
        "ADF482F206C562274FF474744CCADEC4E759DB30C4675646C1BE",
        "FCD1682B17A3D845FCCC4DB56431E0057C670C0FEAF5C1A24E22",
        "A55889CA497A5C79A9D8B0FE3073456520D59D20938C237168FC",
        "09D9DF1E66415F8C478D07E5D7E94C7B9DC4CCC77EBFCDAFBD27",
        "3F72D6E4C73BAB17B3FE88197E87923C49BA6C784C772F53E432",
        "B0189361A2795FDAA2007FAA73214156BF6B7DC96317D13E35D4",
        "86B29A3C5083156D1A822B5966E45E44EC88EB3A8736A3F644E2",
        "44D332482CCA62AF8F2F08EA6E5DB6941AE6B0932B2E38E9A0E8",
        "67920C4CC1A87E9BD0008B384EF292D246F763F67B0CB5E0A7A6",
        "7F11B27A415D8A672AA9BC04A341DD725E22D7A709AD8CFDE19D",
        "A1FCDA6C9B4A15E538D99E42022AC87454AE515179899B6A6526",
        "6446586989DC0A8AD321B7F87132752EA2F61323BC4930AD7519",
    ]

# 网易云音乐 weapi 加密常量（网页端登录等接口使用）
_WEAPI_AES_KEY = b"0CoJUm6Qyw8W8jud"  # 第一层 AES-128-CBC 固定密钥
_WEAPI_AES_IV = b"0102030405060708"
_WEAPI_RSA_N = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f5"
    "6135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047"
    "b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
_WEAPI_RSA_E = 0x10001
_WEAPI_RAND_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_NETEASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}

_EAPI_HEADERS = {
    "User-Agent": "NeteaseMusic/9.1.65.240916182646(9001065);Dalvik/2.1.0 (Linux; U; Android 14)",
    "Referer": "/api/song/enhance/player/url",
}

_QQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://y.qq.com/",
}


@dataclass
class SongInfo:
    """歌曲信息。"""

    song_id: str
    name: str
    artists: str
    album: str
    platform: str  # "163" 或 "qq"
    media_id: str = ""  # QQ 音乐的 strMediaMid

    def display(self) -> str:
        parts = [self.name]
        if self.artists:
            parts.append(f"- {self.artists}")
        return " ".join(parts)


class MusicSearchError(RuntimeError):
    """音乐平台请求或响应异常。"""


def _aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB 加密，PKCS7 填充。"""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    return encryptor.update(padded) + encryptor.finalize()


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC 加密，PKCS7 填充。"""
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    return encryptor.update(padded) + encryptor.finalize()


def _weapi_encrypt(params: dict[str, Any]) -> dict[str, str]:
    """网易云音乐 weapi 参数加密，返回 {"params", "encSecKey"} 表单字段。

    两层 AES-128-CBC（固定密钥 + 随机 16 字符密钥，IV 固定），再对
    反序后的随机密钥做无填充教科书式 RSA，得到定长 256 位十六进制 encSecKey。
    """
    text = json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    first = base64.b64encode(_aes_cbc_encrypt(_WEAPI_AES_KEY, _WEAPI_AES_IV, text)).decode("ascii")
    sec_key = "".join(secrets.choice(_WEAPI_RAND_CHARSET) for _ in range(16))
    params_enc = base64.b64encode(_aes_cbc_encrypt(sec_key.encode(), _WEAPI_AES_IV, first.encode("ascii"))).decode("ascii")
    secret_int = int(sec_key[::-1].encode("utf-8").hex(), 16)
    enc_sec_key = format(pow(secret_int, _WEAPI_RSA_E, int(_WEAPI_RSA_N, 16)), "0256x")
    return {"params": params_enc, "encSecKey": enc_sec_key}


def _hash33(s: str, h: int = 0) -> int:
    """QQ 登录使用的 hash33 算法（ptqrtoken / g_tk）。"""
    for c in s:
        h = (h << 5) + h + ord(c)
    return 2147483647 & h


def _qr_png(text: str) -> bytes:
    """将文本渲染为二维码 PNG 字节（segno 纯 Python 实现，无额外图像依赖）。"""
    import io

    try:
        import segno
    except ImportError as exc:
        raise MusicSearchError("缺少二维码生成库 segno，请在 MaiBot 环境执行: pip install segno") from exc
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=10, border=2)
    return buf.getvalue()


def _eapi_encrypt(url: str, params: dict[str, Any]) -> str:
    """网易云音乐 eapi 加密参数。"""
    data_text = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    sign_src = f"nobody{url}use{data_text}md5forencrypt"
    md5_hash = hashlib.md5(sign_src.encode()).hexdigest()
    sign_text = f"{url}-36cd479b6b5-{data_text}-36cd479b6b5-{md5_hash}"
    return _aes_ecb_encrypt(_EAPI_KEY, sign_text.encode()).hex().upper()


class MusicSearchClient:
    """音乐搜索客户端，支持网易云（默认，无需登录）与 QQ 音乐（需登录）。

    Args:
        netease_cookie: 网易云登录态 {"MUSIC_U": ..., "__csrf": ...}，可选，用于高音质。
        qq_cookie: QQ 音乐登录态 {"uin": ..., "qqmusic_key": ...}，可选。
    """

    def __init__(
        self,
        netease_cookie: dict[str, str] | None = None,
        qq_cookie: dict[str, str] | None = None,
    ) -> None:
        self._netease_cookie = netease_cookie or {}
        self._qq_cookie = {k: v.strip() for k, v in (qq_cookie or {}).items()}
        self._qq_tme_login_type = self._qq_cookie.pop("tme_login_type", "")

        netease_cookies = httpx.Cookies()
        if self._netease_cookie.get("MUSIC_U"):
            netease_cookies.set("MUSIC_U", self._netease_cookie["MUSIC_U"], domain=".music.163.com", path="/")
        if self._netease_cookie.get("__csrf"):
            netease_cookies.set("__csrf", self._netease_cookie["__csrf"], domain=".music.163.com", path="/")

        qq_cookies = {}
        if self._qq_cookie.get("uin"):
            qq_cookies["uin"] = self._qq_cookie["uin"]
        if self._qq_cookie.get("qqmusic_key"):
            qq_cookies["qqmusic_key"] = self._qq_cookie["qqmusic_key"]

        self._netease_client = httpx.AsyncClient(
            headers=_NETEASE_HEADERS,
            cookies=netease_cookies,
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        self._qq_client = httpx.AsyncClient(
            headers=_QQ_HEADERS,
            cookies=qq_cookies,
            timeout=_REQUEST_TIMEOUT,
        )
        # 网易云客户端 api 请求所需（interface3 明文接口 + 手机客户端 cookie）
        self._device_id = secrets.choice(_NETEASE_DEVICE_POOL)
        self._anon_token = ""

    async def close(self) -> None:
        for client in (self._netease_client, self._qq_client):
            try:
                await client.aclose()
            except Exception:
                pass

    # ===== 登录 =====

    def apply_netease_cookies(self, cookies: dict[str, str]) -> None:
        """替换网易云登录态 cookie。"""
        self._netease_cookie = {k: v for k, v in cookies.items() if v}
        jar = httpx.Cookies()
        if self._netease_cookie.get("MUSIC_U"):
            jar.set("MUSIC_U", self._netease_cookie["MUSIC_U"], domain=".music.163.com", path="/")
        if self._netease_cookie.get("__csrf"):
            jar.set("__csrf", self._netease_cookie["__csrf"], domain=".music.163.com", path="/")
        self._netease_client.cookies = jar

    def apply_qq_cookies(self, cookies: dict[str, str]) -> None:
        """替换 QQ 音乐登录态 cookie（uin / qqmusic_key，可带 tme_login_type）。"""
        cookies = dict(cookies)
        self._qq_tme_login_type = str(cookies.pop("tme_login_type", "") or "")
        self._qq_cookie = {k: str(v).strip() for k, v in cookies.items() if v}
        jar = httpx.Cookies()
        if self._qq_cookie.get("uin"):
            jar.set("uin", self._qq_cookie["uin"])
        if self._qq_cookie.get("qqmusic_key"):
            jar.set("qqmusic_key", self._qq_cookie["qqmusic_key"])
        self._qq_client.cookies = jar

    def get_netease_cookies(self) -> dict[str, str]:
        return dict(self._netease_cookie)

    def set_netease_device(self, device_id: str, anon_token: str = "") -> None:
        """恢复持久化的网易云设备 ID 与匿名 token（避免重复注册被限频）。"""
        if device_id:
            self._device_id = str(device_id)
        if anon_token:
            self._anon_token = str(anon_token)

    def get_netease_device(self) -> dict[str, str]:
        return {"device_id": self._device_id, "anon_token": self._anon_token}

    def get_qq_cookies(self) -> dict[str, str]:
        cookies = dict(self._qq_cookie)
        if self._qq_tme_login_type:
            cookies["tme_login_type"] = self._qq_tme_login_type
        return cookies

    async def login_netease(self, account: str, password: str, countrycode: str = "86") -> dict[str, str]:
        """网易云账号密码登录（手机号或邮箱），成功返回并应用登录 cookie。

        密码按网易云要求先做 MD5；登录成功后 MUSIC_U/__csrf 会写入会话 cookie。
        """
        account = account.strip()
        password = password.strip()
        if not account or not password:
            raise MusicSearchError("网易云登录需要账号和密码")

        md5_pwd = hashlib.md5(password.encode("utf-8")).hexdigest()
        if "@" in account:
            path = "/weapi/login"
            payload: dict[str, Any] = {"username": account, "password": md5_pwd, "rememberLogin": "true"}
        else:
            path = "/weapi/login/cellphone"
            payload = {
                "phone": account,
                "countrycode": countrycode.strip() or "86",
                "password": md5_pwd,
                "rememberLogin": "true",
                "checkToken": "",
            }
        try:
            resp = await self._netease_client.post(
                f"https://music.163.com{path}",
                data=_weapi_encrypt(payload),
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise MusicSearchError(f"网易云登录请求失败: {exc}") from exc

        code = data.get("code")
        if code != 200:
            msg = str(data.get("message") or data.get("msg") or "").strip()
            raise MusicSearchError(f"网易云登录失败: code={code!r} {msg}".strip())

        # 登录成功后服务端通过 Set-Cookie 下发 MUSIC_U/__csrf
        music_u = self._netease_client.cookies.get("MUSIC_U") or ""
        csrf = self._netease_client.cookies.get("__csrf") or ""
        if not music_u:
            for cookie in data.get("cookies", []) if isinstance(data.get("cookies"), list) else []:
                if isinstance(cookie, dict) and cookie.get("name") == "MUSIC_U":
                    music_u = str(cookie.get("value") or "")
                    break
        if not music_u:
            raise MusicSearchError("网易云登录响应未包含 MUSIC_U 登录态")

        self.apply_netease_cookies({"MUSIC_U": music_u, "__csrf": csrf})
        return self.get_netease_cookies()

    async def _ensure_anon_token(self) -> None:
        """获取网易云匿名访客 token（MUSIC_A），失败不阻塞扫码。"""
        if self._anon_token:
            return
        encoded = base64.b64encode(
            f"{self._device_id} {self._cloudmusic_encode_id(self._device_id)}".encode("utf-8")
        ).decode("ascii")
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                resp = await self._netease_client.post(
                    f"{_INTERFACE3_DOMAIN}/api/register/anonimous",
                    data={"username": encoded},
                    headers=self._api_request_headers(),
                )
                self._anon_token = resp.cookies.get("MUSIC_A") or ""
                if self._anon_token:
                    logger.info("已获取网易云匿名 token")
                    return
            except Exception as exc:
                last_exc = exc
            await asyncio.sleep(1)
        logger.warning("获取网易云匿名 token 失败（仍会尝试扫码）: %s", last_exc)

    @staticmethod
    def _cloudmusic_encode_id(device_id: str) -> str:
        """网易云设备 ID 混淆编码：XOR 固定密钥后取 MD5 再 base64。"""
        xored = "".join(
            chr(ord(c) ^ ord(_NETEASE_ID_XOR_KEY[i % len(_NETEASE_ID_XOR_KEY)]))
            for i, c in enumerate(device_id)
        )
        digest = hashlib.md5(xored.encode("utf-8")).digest()
        return base64.b64encode(digest).decode("ascii")

    def _api_request_headers(self) -> dict[str, str]:
        """网易云客户端 api（interface3）请求头：iphone UA + 客户端 cookie。"""
        fields = {
            "osver": "16.2",
            "deviceId": self._device_id,
            "os": "iphone",
            "appver": "9.0.90",
            "versioncode": "140",
            "buildver": str(int(time.time())),
            "resolution": "1170x2532",
            "channel": "",
            "requestId": f"{int(time.time() * 1000)}_{secrets.randbelow(10000):04d}",
        }
        csrf = self._netease_cookie.get("__csrf", "")
        if csrf:
            fields["__csrf"] = csrf
        if self._anon_token:
            fields["MUSIC_A"] = self._anon_token
        music_u = self._netease_cookie.get("MUSIC_U", "")
        if music_u:
            fields["MUSIC_U"] = music_u
        cookie = "; ".join(f"{k}={v}" for k, v in fields.items())
        return {"User-Agent": _API_IPHONE_UA, "Cookie": cookie}

    async def netease_qrcode_start(self) -> tuple[bytes, str]:
        """发起网易云扫码登录，返回 (二维码 PNG 字节, unikey)。"""
        await self._ensure_anon_token()
        try:
            resp = await self._netease_client.post(
                f"{_INTERFACE3_DOMAIN}/api/login/qrcode/unikey",
                data={"type": 3},
                headers=self._api_request_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise MusicSearchError(f"获取网易云登录二维码失败: {exc}") from exc
        unikey = str(data.get("unikey") or "")
        if data.get("code") != 200 or not unikey:
            raise MusicSearchError(f"获取网易云登录二维码失败: code={data.get('code')!r}")
        # 二维码内容为登录页 URL（App 扫码后在该页确认授权），本地渲染 PNG
        qr_url = f"https://music.163.com/login?codekey={unikey}"
        return _qr_png(qr_url), unikey

    async def netease_qrcode_poll(self, unikey: str) -> str:
        """查询网易云扫码状态，返回 waiting / scanned / expired / success。

        success 时登录 cookie（MUSIC_U/__csrf 经 Set-Cookie 下发）已写入会话并应用。
        """
        try:
            resp = await self._netease_client.post(
                f"{_INTERFACE3_DOMAIN}/api/login/qrcode/client/login",
                data={"key": unikey, "type": 3},
                headers=self._api_request_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise MusicSearchError(f"查询扫码状态失败: {exc}") from exc

        code = data.get("code")
        if code == 801:
            return "waiting"
        if code == 802:
            return "scanned"
        if code == 800:
            return "expired"
        if code == 803:
            # 授权成功：MUSIC_U 经 Set-Cookie 下发；部分响应也会在 body 里带 cookie 串
            cookie_src = str(data.get("cookie") or "")
            music_u = (resp.cookies.get("MUSIC_U") or self._netease_client.cookies.get("MUSIC_U") or "")
            if not music_u:
                match = re.search(r"MUSIC_U=([^;]+)", cookie_src)
                music_u = match.group(1) if match else ""
            if not music_u:
                raise MusicSearchError("扫码确认成功但响应未包含登录态")
            csrf_match = re.search(r"__csrf=([^;]+)", cookie_src)
            csrf = (resp.cookies.get("__csrf") or self._netease_client.cookies.get("__csrf")
                    or (csrf_match.group(1) if csrf_match else ""))
            self.apply_netease_cookies({"MUSIC_U": music_u, "__csrf": csrf})
            return "success"
        return "waiting"

    async def get_netease_profile(self) -> dict[str, str]:
        """校验网易云登录态，返回账号昵称（不含账号 ID）。"""
        csrf = self._netease_cookie.get("__csrf", "")
        try:
            resp = await self._netease_client.get(
                "https://music.163.com/api/nuser/account/get",
                params={"csrf_token": csrf} if csrf else None,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise MusicSearchError(f"网易云登录态查询请求失败: {exc}") from exc
        # 未登录时该接口也返回 code=200，但 profile 为空
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        nickname = str(profile.get("nickname") or "").strip()
        if data.get("code") != 200 or not nickname:
            raise MusicSearchError("网易云未登录或登录态已失效")
        return {"nickname": nickname}

    async def get_qq_profile(self) -> dict[str, str]:
        """校验 QQ 音乐登录态，返回账号昵称（不含账号 ID）。"""
        uin = self._qq_cookie.get("uin", "").strip()
        qqmusic_key = self._qq_cookie.get("qqmusic_key", "").strip()
        if not uin or not qqmusic_key:
            raise MusicSearchError("QQ 音乐未登录（缺少 uin 或 qqmusic_key）")
        try:
            resp = await self._qq_client.get(
                "https://c6.y.qq.com/rsc/fcgi-bin/fcg_get_profile_homepage.fcg",
                params={
                    "g_tk": str(_hash33(qqmusic_key, 5381)),
                    "format": "json",
                    "inCharset": "utf-8",
                    "outCharset": "utf-8",
                    "notice": "0",
                    "cid": "205360838",
                    "needNewCode": "0",
                    "loginUin": uin,
                    "hostUin": "0",
                    "userid": uin,
                    "reqfrom": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise MusicSearchError(f"QQ 登录态查询请求失败: {exc}") from exc
        if data.get("code") != 0:
            raise MusicSearchError(f"QQ 音乐登录态已失效: code={data.get('code')!r}")
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        nickname = ""
        for key in ("nick", "nickname", "name"):
            nickname = str(payload.get(key) or "").strip()
            if nickname:
                break
        return {"nickname": nickname}

    async def qq_qrcode_start(self) -> tuple[bytes, str]:
        """发起 QQ 扫码登录，返回 (二维码 PNG 字节, qrsig)。"""
        try:
            resp = await self._qq_client.get(
                "https://ssl.ptlogin2.qq.com/ptqrshow",
                params={
                    "appid": "716027609",
                    "e": "2",
                    "l": "M",
                    "s": "3",
                    "d": "72",
                    "v": "4",
                    "t": str(secrets.randbelow(10**8)),
                    "daid": "383",
                    "pt_3rd_aid": "100497308",
                },
                headers={"User-Agent": _QQ_HEADERS["User-Agent"], "Referer": "https://xui.ptlogin2.qq.com/"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MusicSearchError(f"获取 QQ 登录二维码失败: {exc}") from exc
        qrsig = resp.cookies.get("qrsig") or ""
        if not qrsig:
            raise MusicSearchError("获取 QQ 登录二维码失败: 响应缺少 qrsig")
        return resp.content, qrsig

    async def qq_qrcode_poll(self, qrsig: str) -> tuple[str, str]:
        """查询扫码状态，返回 (状态, 附加信息)。

        状态: ``waiting``(未扫码) / ``scanned``(已扫码待确认) / ``expired``(已失效) /
        ``success``(附加信息为 ``uin|ptsigx``)。
        """
        try:
            resp = await self._qq_client.get(
                "https://ssl.ptlogin2.qq.com/ptqrlogin",
                params={
                    "u1": "https://graph.qq.com/oauth2.0/login_jump",
                    "ptqrtoken": str(_hash33(qrsig)),
                    "ptredirect": "0",
                    "h": "1",
                    "t": "1",
                    "g": "1",
                    "from_ui": "1",
                    "ptlang": "2052",
                    "action": f"0-0-{int(time.time() * 1000)}",
                    "js_ver": "20102616",
                    "js_type": "1",
                    "pt_uistyle": "40",
                    "aid": "716027609",
                    "daid": "383",
                    "pt_3rd_aid": "100497308",
                    "has_onekey": "1",
                },
                headers={"User-Agent": _QQ_HEADERS["User-Agent"], "Referer": "https://xui.ptlogin2.qq.com/"},
                cookies={"qrsig": qrsig},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MusicSearchError(f"查询扫码状态失败: {exc}") from exc

        match = re.search(r"ptuiCB\((.*)\)", resp.text or "")
        if not match:
            raise MusicSearchError("解析扫码状态失败: 响应格式异常")
        args = re.findall(r"'((?:\\.|[^'])*)'", match.group(1))
        if not args:
            raise MusicSearchError("解析扫码状态失败: 状态参数为空")
        code = args[0]
        if code == "67":
            return "scanned", ""
        if code == "65":
            return "expired", ""
        if code == "0":
            check_url = args[2] if len(args) > 2 else ""
            # uin 为纯数字，ptsigx 为无 & 的 token；参数顺序不定，按边界匹配
            uin_match = re.search(r"[?&]uin=(\d+)", check_url)
            sigx_match = re.search(r"[?&]ptsigx=([^&]+)", check_url)
            if not uin_match or not sigx_match:
                raise MusicSearchError("扫码成功但解析登录凭据失败")
            return "success", f"{uin_match.group(1)}|{sigx_match.group(1)}"
        # 66=未扫码，其余未知状态码均按等待处理
        return "waiting", ""

    async def qq_qrcode_finish(self, uin: str, sigx: str) -> dict[str, str]:
        """扫码确认后换取 QQ 音乐登录 cookie（uin / qqmusic_key），并应用到会话。"""
        headers = {"User-Agent": _QQ_HEADERS["User-Agent"], "Referer": "https://xui.ptlogin2.qq.com/"}
        # 1. check_sig：建立 QQ 互联登录态，响应 cookie 里有 p_skey
        try:
            check_resp = await self._qq_client.get(
                "https://ssl.ptlogin2.graph.qq.com/check_sig",
                params={
                    "uin": uin,
                    "pttype": "1",
                    "service": "ptqrlogin",
                    "nodirect": "0",
                    "ptsigx": sigx,
                    "s_url": "https://graph.qq.com/oauth2.0/login_jump",
                    "ptlang": "2052",
                    "ptredirect": "100",
                    "aid": "716027609",
                    "daid": "383",
                    "j_later": "0",
                    "low_login_hour": "0",
                    "regmaster": "0",
                    "pt_login_type": "3",
                    "pt_aid": "0",
                    "pt_aaid": "16",
                    "pt_light": "0",
                    "pt_3rd_aid": "100497308",
                },
                headers=headers,
                follow_redirects=False,
            )
            p_skey = check_resp.cookies.get("p_skey") or ""
        except httpx.HTTPError as exc:
            raise MusicSearchError(f"QQ 登录凭据校验失败: {exc}") from exc
        if not p_skey:
            raise MusicSearchError("QQ 登录凭据校验失败: 未取得 p_skey")

        # 2. QQ 互联 OAuth 授权，换取 code
        try:
            auth_resp = await self._qq_client.post(
                "https://graph.qq.com/oauth2.0/authorize",
                data={
                    "response_type": "code",
                    "client_id": "100497308",
                    "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/",
                    "scope": "get_user_info,get_app_friends",
                    "state": "state",
                    "switch": "",
                    "from_ptlogin": "1",
                    "src": "1",
                    "update_auth": "1",
                    "openapi": "1010_1030",
                    "g_tk": str(_hash33(p_skey, 5381)),
                    "auth_time": str(int(time.time() * 1000)),
                    "ui": str(uuid.uuid4()),
                },
                headers=headers,
                cookies=check_resp.cookies,
                follow_redirects=False,
            )
            location = auth_resp.headers.get("location", "")
        except httpx.HTTPError as exc:
            raise MusicSearchError(f"QQ 互联授权请求失败: {exc}") from exc
        code_match = re.search(r"[?&]code=([^&]+)", location)
        if not code_match:
            raise MusicSearchError(f"QQ 互联授权失败: {location[:120] or '未返回跳转'}")

        # 3. code 换 QQ 音乐凭据（musicid=uin, musickey=qqmusic_key）
        req_data = {
            "QQConnectLogin.LoginServer": {"method": "QQLogin", "param": {"code": code_match.group(1)}},
            "comm": {"tmeLoginType": 2, "format": "json", "ct": 19, "cv": 0},
        }
        try:
            resp = await self._qq_client.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=req_data)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise MusicSearchError(f"QQ 音乐登录换票请求失败: {exc}") from exc
        if data.get("code") not in (None, 0):
            raise MusicSearchError(f"QQ 音乐登录换票失败: code={data.get('code')!r}")
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        musicid = str(payload.get("musicid") or data.get("musicid") or "")
        musickey = str(payload.get("musickey") or data.get("musickey") or "")
        if not musicid or not musickey:
            raise MusicSearchError("QQ 音乐登录响应缺少凭据")

        cookies = {"uin": musicid, "qqmusic_key": musickey, "tme_login_type": "2"}
        self.apply_qq_cookies(cookies)
        return cookies

    async def search(self, query: str, platform: str, limit: int = 5) -> list[SongInfo]:
        """搜索歌曲，platform 为 "163" 或 "qq"。"""
        if platform == "qq":
            return await self._search_qq(query, limit)
        return await self._search_netease(query, limit)

    async def _search_netease(self, query: str, limit: int) -> list[SongInfo]:
        try:
            resp = await self._netease_client.get(
                "https://music.163.com/api/search/get/web",
                params={"s": query, "type": "1", "limit": str(limit), "offset": "0"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise MusicSearchError(f"网易云音乐搜索请求失败: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise MusicSearchError("网易云音乐搜索响应不是有效 JSON") from exc

        if data.get("code") != 200:
            raise MusicSearchError(f"网易云音乐搜索业务失败: code={data.get('code')!r}")

        songs = data.get("result", {}).get("songs", [])
        results: list[SongInfo] = []
        for song in songs:
            if not isinstance(song, dict):
                continue
            artists = ", ".join(
                a.get("name", "") for a in song.get("artists", []) if isinstance(a, dict) and a.get("name")
            )
            album = song.get("album", {}).get("name", "") if isinstance(song.get("album"), dict) else ""
            song_id = str(song.get("id", ""))
            name = str(song.get("name", ""))
            if song_id and name:
                results.append(SongInfo(song_id, name, artists, album, "163"))
        return results

    async def _search_qq(self, query: str, limit: int) -> list[SongInfo]:
        uin = self._qq_cookie.get("uin", "").strip()
        qqmusic_key = self._qq_cookie.get("qqmusic_key", "").strip()
        if not uin or not qqmusic_key:
            raise MusicSearchError("QQ音乐搜索需要登录，请在配置中填写 qq.uin 与 qq.qqmusic_key")

        req_data = {
            "req_1": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {"search_type": 0, "query": query, "page_num": 1, "num_per_page": limit},
            },
            "loginUin": uin,
            "comm": self._build_qq_comm(uin, qqmusic_key),
        }
        try:
            resp = await self._qq_client.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=req_data)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise MusicSearchError(f"QQ音乐搜索请求失败: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise MusicSearchError("QQ音乐搜索响应不是有效 JSON") from exc

        req_result = data.get("req_1", {})
        if data.get("code") not in (None, 0) or req_result.get("code") not in (None, 0):
            raise MusicSearchError("QQ音乐搜索业务失败")

        song_list = req_result.get("data", {}).get("body", {}).get("song", {}).get("list", [])
        results: list[SongInfo] = []
        for song in song_list:
            if not isinstance(song, dict):
                continue
            singers = song.get("singer", [])
            artists = ", ".join(s.get("name", "") for s in singers if isinstance(s, dict) and s.get("name"))
            album = song.get("album", {}).get("name", "") if isinstance(song.get("album"), dict) else ""
            song_mid = str(song.get("mid", "") or song.get("songmid", ""))
            name = str(song.get("name", "") or song.get("songname", ""))
            media_mid = str(song.get("file", {}).get("media_mid", "") or song.get("strMediaMid", ""))
            if song_mid and name:
                results.append(SongInfo(song_mid, name, artists, album, "qq", media_mid))
        return results

    def _build_qq_comm(self, uin: str, qqmusic_key: str) -> dict[str, Any]:
        """构造 QQ 音乐 musicu.fcg 请求的 comm 字段（含扫码登录类型）。"""
        comm: dict[str, Any] = {"uin": uin, "format": "json", "ct": 19, "cv": 0, "authst": qqmusic_key}
        if self._qq_tme_login_type:
            try:
                comm["tmeLoginType"] = int(self._qq_tme_login_type)
            except (TypeError, ValueError):
                pass
        return comm

    async def get_song_url(self, song_id: str, platform: str, media_id: str = "") -> str | None:
        """获取歌曲可播放音频 URL，失败返回 None。"""
        if platform == "qq":
            return await self._get_qq_song_url(song_id, media_id)
        return await self._get_netease_song_url(song_id)

    async def _get_netease_song_url(self, song_id: str) -> str | None:
        # 1. eapi 加密接口
        api_path = "/api/song/enhance/player/url"
        params: dict[str, Any] = {"ids": f"[{song_id}]", "br": 999000}
        csrf = self._netease_cookie.get("__csrf", "")
        if csrf:
            params["csrf_token"] = csrf
        try:
            enc = _eapi_encrypt(api_path, params)
            resp = await self._netease_client.post(
                f"https://interface.music.163.com/eapi{api_path}",
                data={"params": enc},
                headers=_EAPI_HEADERS,
                follow_redirects=False,
            )
            resp.raise_for_status()
            data = resp.json()
            url_list = data.get("data", [])
            if url_list and isinstance(url_list, list):
                url = str(url_list[0].get("url", "") or "").strip()
                if url:
                    return url
        except Exception:
            logger.debug("网易云 eapi 取链失败: %s", song_id)

        # 2. 标准 Web 接口
        try:
            resp = await self._netease_client.get(
                "https://music.163.com/api/song/enhance/player/url",
                params={"ids": f"[{song_id}]", "br": "999000"},
            )
            resp.raise_for_status()
            data = resp.json()
            url_list = data.get("data", [])
            if url_list and isinstance(url_list, list):
                url = str(url_list[0].get("url", "") or "").strip()
                if url:
                    return url
        except Exception:
            logger.debug("网易云标准接口取链失败: %s", song_id)

        # 3. 直链重定向兜底
        try:
            resp = await self._netease_client.get(
                f"https://music.163.com/song/media/outer/url?id={song_id}.mp3",
                follow_redirects=True,
            )
            final_url = str(resp.url)
            if final_url and any(ext in final_url for ext in (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac")):
                return final_url
        except Exception:
            logger.debug("网易云直链取链失败: %s", song_id)

        return None

    async def _get_qq_song_url(self, song_mid: str, media_mid: str) -> str | None:
        resource_mid = media_mid or song_mid
        quality_prefixes = [("F000", ".flac"), ("M800", ".mp3"), ("M500", ".mp3"), ("C400", ".m4a")]
        filenames = [f"{prefix}{resource_mid}{ext}" for prefix, ext in quality_prefixes]
        guid = str(secrets.randbelow(9000000000) + 1000000000)
        uin = self._qq_cookie.get("uin", "0")
        qqmusic_key = self._qq_cookie.get("qqmusic_key", "")

        req_data = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": filenames,
                    "guid": guid,
                    "songmid": [song_mid] * len(filenames),
                    "songtype": [0] * len(filenames),
                    "uin": uin,
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "loginUin": uin,
            "comm": self._build_qq_comm(uin, qqmusic_key),
        }
        try:
            resp = await self._qq_client.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=req_data)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("QQ音乐取链失败: %s: %s", song_mid, exc)
            return None

        result_data = data.get("req_0", {}).get("data", {})
        sip = result_data.get("sip", [])
        midurlinfo = result_data.get("midurlinfo", [])
        if not isinstance(sip, list) or not isinstance(midurlinfo, list):
            return None

        info_by_filename = {
            str(info.get("filename", "")): info
            for info in midurlinfo
            if isinstance(info, dict) and info.get("filename")
        }
        ordered = [info_by_filename.get(f) for f in filenames]
        for info in ordered:
            if not info:
                continue
            purl = str(info.get("purl", "") or "").strip()
            if not purl:
                continue
            if purl.startswith(("http://", "https://")):
                return purl
            domain = next((str(s) for s in sip if isinstance(s, str) and s.startswith("https://")), "")
            if not domain:
                domain = next((str(s) for s in sip if isinstance(s, str) and s), "")
            if domain:
                audio_url = urljoin(domain, purl)
                parsed = urlparse(audio_url)
                if parsed.scheme in {"http", "https"} and parsed.netloc:
                    return audio_url
        return None
