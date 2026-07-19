"""群文件扫码登录网页：后端实时出码、过期自动刷新、扫码成功自动提取并保存 CK。

流程：主人发「登录」→ 机器人回带令牌的登录链接 → 主人在浏览器打开本页 →
本页向后端要一张实时二维码（快过期会自动换新，不再受转发/显示延迟影响）→
手机 QQ 扫码确认 → 后端轮询到成功即自动提取 qun 域 CK 并存到该主人名下。
"""
import base64
import secrets
import time

from aiohttp import web

from core.plugin.web_pages import register_route

from . import store
from .qr_login import QRSession

# sid -> {"qr": QRSession, "uid": str, "ts": float}
_SESSIONS: dict = {}
_SESSION_TTL = 180


def _prune():
    now = time.time()
    for sid in [s for s, v in _SESSIONS.items() if now - v["ts"] > _SESSION_TTL]:
        v = _SESSIONS.pop(sid, None)
        if v:
            import asyncio
            asyncio.ensure_future(v["qr"].close())


_PAGE = """<!doctype html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>群文件登录</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(135deg,#5865f2,#7289da);min-height:100vh;display:flex;
align-items:center;justify-content:center}
.card{background:#fff;border-radius:16px;padding:28px 24px;width:320px;text-align:center;
box-shadow:0 12px 40px rgba(0,0,0,.2)}
h1{font-size:18px;margin:0 0 4px;color:#2c2f36}
p{font-size:13px;color:#6b7280;margin:6px 0}
.qrbox{width:220px;height:220px;margin:16px auto;border:1px solid #eee;border-radius:12px;
display:flex;align-items:center;justify-content:center;overflow:hidden}
.qrbox img{width:100%;height:100%}
.status{font-size:14px;font-weight:600;margin-top:10px;min-height:20px}
.ok{color:#16a34a}.warn{color:#d97706}.err{color:#dc2626}.wait{color:#2563eb}
button{margin-top:14px;background:#5865f2;color:#fff;border:0;border-radius:8px;
padding:9px 18px;font-size:14px;cursor:pointer}
</style></head><body>
<div class="card">
<h1>QQ 群文件登录</h1>
<p>请用<b>群主/管理员</b>的手机 QQ「扫一扫」摄像头扫描下方二维码</p>
<div class="qrbox"><img id="qr" alt="二维码加载中…"></div>
<div class="status wait" id="st">正在获取二维码…</div>
<button id="refresh" style="display:none">重新获取二维码</button>
</div>
<script>
const TOKEN=new URLSearchParams(location.search).get("token")||"";
let sid="",timer=null;
const $=id=>document.getElementById(id);
function setStatus(t,c){const el=$("st");el.textContent=t;el.className="status "+(c||"wait");}
async function newQR(){
  clearInterval(timer);
  setStatus("正在获取二维码…","wait");$("refresh").style.display="none";
  try{
    const r=await fetch("/api/ext/qwj/qr/new?token="+encodeURIComponent(TOKEN));
    const d=await r.json();
    if(!d.success){setStatus(d.message||"获取失败","err");$("refresh").style.display="";return;}
    sid=d.sid;$("qr").src=d.img;setStatus("等待扫码…","wait");
    timer=setInterval(poll,2000);
  }catch(e){setStatus("网络错误，请重试","err");$("refresh").style.display="";}
}
async function poll(){
  try{
    const r=await fetch("/api/ext/qwj/qr/poll?token="+encodeURIComponent(TOKEN)+"&sid="+encodeURIComponent(sid));
    const d=await r.json();
    if(d.status==="success"){clearInterval(timer);setStatus("✅ 登录成功！CK 已自动保存，回到 QQ 发送「清空群文件 群号」即可","ok");$("qr").style.opacity=.25;}
    else if(d.status==="scanned"){setStatus("📲 已扫码，请在手机上点「确认登录」","warn");}
    else if(d.status==="expired"){clearInterval(timer);setStatus("二维码已过期，正在自动刷新…","warn");newQR();}
    else if(d.status==="error"){clearInterval(timer);setStatus("❌ "+(d.message||"登录失败"),"err");$("refresh").style.display="";}
    else{setStatus("等待扫码…","wait");}
  }catch(e){/* 忽略单次轮询错误 */}
}
$("refresh").onclick=newQR;
if(!TOKEN){setStatus("链接无效：缺少令牌，请回 QQ 重新发送「登录」","err");}
else{newQR();}
</script></body></html>"""


@register_route("GET", "/api/ext/qwj/login", auth=False)
async def page_login(request):
    token = request.query.get("token", "")
    uid = store.resolve_login_token(token)
    if not uid:
        return web.Response(
            text="<h3 style='font-family:sans-serif'>链接已失效，请回 QQ 重新发送「登录」获取新链接。</h3>",
            content_type="text/html", charset="utf-8", status=403,
        )
    return web.Response(text=_PAGE, content_type="text/html", charset="utf-8")


@register_route("GET", "/api/ext/qwj/qr/new", auth=False)
async def api_qr_new(request):
    _prune()
    token = request.query.get("token", "")
    uid = store.resolve_login_token(token)
    if not uid:
        return web.json_response({"success": False, "message": "令牌无效或已过期"})
    qr = QRSession()
    try:
        png = await qr.fetch_qr()
    except Exception as exc:
        await qr.close()
        store.log(f"web 出码失败: {exc}")
        return web.json_response({"success": False, "message": "获取二维码失败，请重试"})
    sid = secrets.token_urlsafe(12)
    _SESSIONS[sid] = {"qr": qr, "uid": str(uid), "ts": time.time()}
    img = "data:image/png;base64," + base64.b64encode(png).decode()
    return web.json_response({"success": True, "sid": sid, "img": img})


@register_route("GET", "/api/ext/qwj/qr/poll", auth=False)
async def api_qr_poll(request):
    token = request.query.get("token", "")
    sid = request.query.get("sid", "")
    uid = store.resolve_login_token(token)
    if not uid:
        return web.json_response({"status": "error", "message": "令牌无效或已过期"})
    sess = _SESSIONS.get(sid)
    if not sess:
        return web.json_response({"status": "expired", "message": "会话不存在"})
    res = await sess["qr"].poll()
    status = res.get("status")
    if status == "success":
        store.set_user_cookie(sess["uid"], res["cookie"])
        store.log_user(sess["uid"], "网页扫码登录成功，已保存CK")
        await sess["qr"].close()
        _SESSIONS.pop(sid, None)
        return web.json_response({"status": "success"})
    if status in ("expired", "error"):
        await sess["qr"].close()
        _SESSIONS.pop(sid, None)
    return web.json_response({"status": status, "message": res.get("message", "")})
