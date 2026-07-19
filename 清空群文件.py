"""
群文件管理插件 - 每批20个文件，重算bkn，高并发无延迟重试
指令（主人全局）：登录群文件、群文件登录cookie、清空群文件 [群号]
"""
import os
import re
import json
import time
import asyncio
import aiohttp
from datetime import datetime
from core.plugin.decorators import handler

from .qr_login import QRSession

# ---------- 配置 ----------
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(PLUGIN_DIR, "json")
COOKIE_FILE = os.path.join(STATE_DIR, "pancookie.json")
LOG_FILE = os.path.join(PLUGIN_DIR, "mm.txt")

def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

# ---------- Cookie 管理 ----------
def load_cookies():
    if not os.path.exists(COOKIE_FILE):
        return {}
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        valid = {}
        for uid, info in data.items():
            if isinstance(info, dict) and info.get("expire", 0) > now:
                valid[uid] = info
        if len(valid) != len(data):
            save_cookies(valid)
        return valid
    except:
        return {}

def save_cookies(cookies: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f)

def get_user_cookie(user_id: str) -> str | None:
    cookies = load_cookies()
    info = cookies.get(user_id)
    if info:
        return info.get("cookie")
    return None

def set_user_cookie(user_id: str, cookie_str: str):
    cookies = load_cookies()
    cookies[user_id] = {
        "cookie": cookie_str,
        "expire": time.time() + 31 * 24 * 3600
    }
    save_cookies(cookies)

# ---------- 工具 ----------
def get_skey(cookie_str: str) -> str | None:
    match = re.search(r'skey=([^;]+)', cookie_str)
    return match.group(1) if match else None

def calc_bkn(skey: str) -> int:
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7fffffff

async def safe_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except:
        return None

# ---------- API ----------
async def fetch_file_list(session, group_id, bkn, cookie_str, start, cnt=50):
    url = (
        f"https://pan.qun.qq.com/cgi-bin/group_file/get_file_list"
        f"?gc={group_id}&bkn={bkn}&folder_id=/&start_index={start}&cnt={cnt}"
        f"&filter_code=0&show_onlinedoc_folder=1&src=qpan"
    )
    headers = {"Cookie": cookie_str}
    log(f"请求文件列表: start={start}, cnt={cnt}")
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                log(f"HTTP {resp.status}")
                return None
            text = await resp.text()
            data = await safe_json(text)
            if not data:
                log(f"非JSON响应: {text[:200]}")
                return None
            ec = data.get('ec')
            if ec != 0:
                log(f"API错误 ec={ec} em={data.get('em', '')}")
                return None
            total = data.get('total_cnt', 0)
            files = data.get('file_list', [])
            log(f"获取成功: ec=0, total={total}, 本页={len(files)}")
            return data
    except Exception as e:
        log(f"请求异常: {e}")
        return None

async def get_all_files(session, group_id, bkn, cookie_str) -> list[dict] | None:
    first = await fetch_file_list(session, group_id, bkn, cookie_str, 0, 50)
    if not first:
        return None
    total = first.get('total_cnt', 0)
    all_files = first.get('file_list', [])
    if total <= 50:
        return all_files
    tasks = []
    for i in range(1, (total + 49) // 50):
        tasks.append(fetch_file_list(session, group_id, bkn, cookie_str, i * 50, 50))
    results = await asyncio.gather(*tasks)
    for res in results:
        if res and res.get('ec') == 0:
            all_files.extend(res.get('file_list', []))
    return all_files

async def delete_batch(session, group_id, cookie_str, batch_files, max_retries=3):
    """删除一批文件，每次请求前重新计算 bkn，失败自动重试（无睡眠）"""
    delete_list = []
    for f in batch_files:
        delete_list.append({
            'gc': int(group_id),
            'app_id': 4,
            'bus_id': f['bus_id'],
            'file_id': f['id'],
            'parent_folder_id': f.get('parent_id', '/')
        })

    headers = {"Cookie": cookie_str}

    for attempt in range(1, max_retries + 1):
        # 重新计算 bkn
        skey = get_skey(cookie_str)
        if not skey:
            log("Cookie 中缺少 skey，无法计算 bkn")
            return False
        bkn = calc_bkn(skey)

        form_data = {
            'gc': group_id,
            'bkn': str(bkn),
            'file_list': json.dumps({'file_list': delete_list})
        }

        try:
            async with session.post(
                'https://pan.qun.qq.com/cgi-bin/group_file/delete_file',
                data=form_data,
                headers=headers
            ) as resp:
                text = await resp.text()
                if resp.status == 200:
                    data = await safe_json(text)
                    if data and data.get('ec') == 0:
                        log(f"✅ 删除成功，批次大小: {len(batch_files)}")
                        return True
                    else:
                        log(f"⚠️ 删除API返回错误 ec={data.get('ec') if data else '?'}, 尝试 {attempt}/{max_retries}")
                elif resp.status == 500:
                    log(f"⚠️ 服务器500错误，尝试 {attempt}/{max_retries}")
                else:
                    log(f"⚠️ 删除请求返回 HTTP {resp.status}, 尝试 {attempt}/{max_retries}")
        except Exception as e:
            log(f"⚠️ 删除请求异常: {e}, 尝试 {attempt}/{max_retries}")

    log(f"❌ 删除失败，已重试 {max_retries} 次，批次大小: {len(batch_files)}")
    return False

# ---------- 指令 ----------
@handler(r'^(登录群文件|登录)$', name='登录群文件', desc='扫码登录群文件并自动提取CK', owner_only=True)
async def cmd_login(event, match):
    """扫码登录：发送二维码 → 用户手机 QQ 扫码确认 → 自动提取 CK 并保存。"""
    qr = QRSession()
    try:
        try:
            png = await qr.fetch_qr()
        except Exception as e:
            log(f"获取二维码失败: {e}")
            await event.reply("❌ 获取二维码失败，请稍后重试～")
            return

        await event.reply_image(png, "📱 请用手机 QQ 扫码并确认登录（约2分钟内有效）")

        last_status = ""
        deadline = time.time() + 110
        while time.time() < deadline:
            await asyncio.sleep(2)
            res = await qr.poll()
            status = res.get("status")
            if status == "success":
                set_user_cookie(event.user_id, res["cookie"])
                log(f"用户 {event.user_id} 扫码登录成功，已保存CK")
                await event.reply("✅ 登录成功，CK 已自动提取并保存（31天内有效）～\n现在可发送：清空群文件 群号")
                return
            if status == "scanned" and last_status != "scanned":
                await event.reply("📲 已扫码，请在手机上点击「确认登录」～")
            if status == "expired":
                await event.reply("⌛ 二维码已失效，请重新发送 登录～")
                return
            if status == "error":
                log(f"扫码登录错误: {res.get('message')}")
                await event.reply(f"❌ 登录失败：{res.get('message')}")
                return
            last_status = status
        await event.reply("⌛ 登录超时，请重新发送 登录～")
    finally:
        await qr.close()

@handler(r'^群文件登录\s+(.+)', name='群文件登录', desc='保存当前主人的群文件Cookie', owner_only=True)
async def cmd_save_cookie(event, match):
    cookie_str = match.group(1).strip()
    if 'skey=' not in cookie_str:
        await event.reply("❌ Cookie 无效，缺少 skey 字段～")
        return
    set_user_cookie(event.user_id, cookie_str)
    log(f"用户 {event.user_id} 更新Cookie")
    await event.reply("✅ Cookie 已保存，31天内有效～")

@handler(r'^清空群文件\s+(\d+)$', name='清空群文件', desc='清空指定群的所有文件（主人用）', owner_only=True)
async def cmd_clear_files(event, match):
    group_id = match.group(1)
    user_id = event.user_id
    log(f"用户 {user_id} 请求清空群 {group_id}")

    cookie_str = get_user_cookie(user_id)
    if not cookie_str:
        await event.reply("❌ 你还没有登录群文件，请先使用 登录群文件 指令～")
        return

    skey = get_skey(cookie_str)
    if not skey:
        await event.reply("❌ Cookie 中缺少 skey，请重新登录～")
        return

    bkn = calc_bkn(skey)
    log(f"初始 bkn={bkn}")

    await event.reply("⏳ 正在清理中，请稍后...")

    async with aiohttp.ClientSession() as session:
        all_files = await get_all_files(session, group_id, bkn, cookie_str)
        if all_files is None:
            await event.reply("❌ Cookie 已失效，请重新登录群文件～")
            return
        if not all_files:
            await event.reply("✅ 清理完成（群文件原本就是空的）～")
            return

        total = len(all_files)
        batch_size = 20  # 每批20个文件，更稳定
        batches = [all_files[i:i+batch_size] for i in range(0, total, batch_size)]
        log(f"共 {total} 个文件，分 {len(batches)} 批删除（每批 {batch_size} 个）")

        tasks = [delete_batch(session, group_id, cookie_str, batch) for batch in batches]
        results = await asyncio.gather(*tasks)
        success_count = sum(1 for r in results if r)
        log(f"删除完成，成功 {success_count}/{len(batches)} 批")

    await event.reply(f"✅ 清理完成，群 {group_id} 共处理 {total} 个文件～")

log("群文件管理插件已加载")