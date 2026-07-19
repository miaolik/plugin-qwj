"""QQ 扫码登录（纯 HTTP，无需无头浏览器）。

流程：ptqrshow 拿二维码图片并写入 qrsig cookie → 用 hash33(qrsig) 算 ptqrtoken →
轮询 ptqrlogin，用户用手机 QQ 扫码确认后返回 check_sig 跳转地址 →
访问该地址由服务端 Set-Cookie 写入 skey / uin / p_skey 等，拼成 Cookie 串返回。

这样即可自动提取登录后的 CK（含 skey），供群文件清理使用；相比无头浏览器更轻量、
更稳定，服务器上也能直接跑。
"""
import time
import urllib.parse

import aiohttp

# 群 qun.qq.com 官方扫码登录参数（与 https://qun.qq.com 登录页内嵌二维码一致）：
# 登录后写入 .qq.com 域的 skey/uin，pan.qun 群文件接口用 g_tk(skey) 作 bkn 即可访问。
APPID = 715030901
DAID = 73
S_URL = "https://qun.qq.com"

_PTUICB_RE = None


def hash33(s: str) -> int:
    """ptqrtoken / bkn 通用的 hash33 算法。"""
    e = 0
    for c in s:
        e += (e << 5) + ord(c)
    return e & 0x7fffffff


def _extract_qrsig(jar: aiohttp.CookieJar) -> str:
    for cookie in jar:
        if cookie.key == "qrsig":
            return cookie.value
    return ""


class QRSession:
    """一次扫码登录会话；持有 aiohttp session 与 qrsig，跨轮询复用同一 cookie jar。"""

    def __init__(self):
        self.jar = aiohttp.CookieJar(unsafe=True)
        self.session = aiohttp.ClientSession(cookie_jar=self.jar)
        self.qrsig = ""
        self.ptqrtoken = 0
        self.created = time.time()

    async def close(self):
        try:
            await self.session.close()
        except Exception:
            pass

    async def fetch_qr(self) -> bytes:
        """获取二维码 PNG，同时记录 qrsig / ptqrtoken。"""
        t = str(time.time())
        url = (
            f"https://ssl.ptlogin2.qq.com/ptqrshow?appid={APPID}&e=2&l=M&s=3&d=72"
            f"&v=4&t={t}&daid={DAID}&pt_3rd_aid=0"
        )
        async with self.session.get(url, headers={"Referer": "https://xui.ptlogin2.qq.com/"}) as resp:
            png = await resp.read()
        self.qrsig = _extract_qrsig(self.jar)
        if not self.qrsig:
            raise RuntimeError("获取二维码失败：未拿到 qrsig")
        self.ptqrtoken = hash33(self.qrsig)
        return png

    async def poll(self) -> dict:
        """轮询一次登录状态。

        返回 {"status": "waiting"|"scanned"|"success"|"expired"|"error",
              "message": str, "cookie": str（仅 success）}。
        """
        url = (
            f"https://ssl.ptlogin2.qq.com/ptqrlogin?u1={urllib.parse.quote(S_URL)}"
            f"&ptqrtoken={self.ptqrtoken}&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052"
            f"&action=0-0-{int(time.time() * 1000)}&js_ver=25010716&js_type=1"
            f"&login_sig=&pt_uistyle=40&aid={APPID}&daid={DAID}&"
        )
        try:
            async with self.session.get(url, headers={"Referer": "https://xui.ptlogin2.qq.com/"}) as resp:
                body = await resp.text()
        except Exception as exc:
            return {"status": "error", "message": f"轮询异常: {exc}"}

        parts = [p.strip().strip("'") for p in body[body.find("(") + 1:body.rfind(")")].split(",")]
        code = parts[0] if parts else ""
        if code == "0":
            check_url = parts[2] if len(parts) > 2 else ""
            cookie = await self._finish(check_url)
            if not cookie or "skey=" not in cookie:
                return {"status": "error", "message": "登录成功但未取到 skey，请重试"}
            return {"status": "success", "message": "登录成功", "cookie": cookie}
        if code == "65":
            return {"status": "expired", "message": "二维码已失效，请重新登录"}
        if code == "67":
            return {"status": "scanned", "message": "已扫码，请在手机上确认登录"}
        if code == "66":
            return {"status": "waiting", "message": "二维码未失效，等待扫码"}
        return {"status": "waiting", "message": body[:80]}

    async def _finish(self, check_url: str) -> str:
        """访问 check_sig 跳转地址，让服务端写入 skey/uin 等，返回 Cookie 串。"""
        if check_url:
            try:
                async with self.session.get(
                    check_url, headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                    allow_redirects=True,
                ):
                    pass
            except Exception:
                pass
        pairs = []
        for cookie in self.jar:
            if cookie.key in ("qrsig",):
                continue
            pairs.append(f"{cookie.key}={cookie.value}")
        return "; ".join(pairs)
