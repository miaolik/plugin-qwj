"""
群文件管理插件 - 每批20个文件，重算bkn，高并发无延迟重试
指令（白名单管理员）：登录、刷新、登录网页、设置登录地址 <url>、群文件登录 <cookie>、清空群文件 <群号>
"""
import re
import json
import time
import asyncio
import aiohttp
from core.plugin.decorators import handler

from .qr_login import QRSession
from .store import (
    log, log_user, ensure_admin_dirs,
    get_user_cookie, set_user_cookie, get_skey, calc_bkn,
    get_base_url, set_base_url, create_login_token,
    is_admin, add_admin, remove_admin, get_admins,
)


async def _require_admin(event) -> bool:
    """仅白名单管理员可操作；非管理员静默忽略，避免打扰普通用户。"""
    return is_admin(event.user_id)


_AT_RE = re.compile(r'<@!?([A-Za-z0-9]+)>')


def _extract_target_ids(event, arg: str) -> list:
    """从被艾特对象(event.mentions)及文本参数中提取目标用户 ID。

    支持：添加管理员 @用户 / 添加管理员 <ID> / 添加管理员 @用户A @用户B。
    （event.content 会剔除 <@id> 标签，真实 ID 从 mentions 取；手动填写时从文本取。）
    """
    ids = []
    for m in (event.mentions or []):
        if not isinstance(m, dict):
            continue
        if m.get('is_you') or m.get('bot') or m.get('scope') == 'all':
            continue
        mid = m.get('id')
        if mid:
            ids.append(str(mid))
    if arg:
        ids.extend(_AT_RE.findall(arg))
        for tok in _AT_RE.sub(' ', arg).split():
            ids.append(tok)
    seen = set()
    out = []
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


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
# 单次会话内自动刷新的最多次数（受 QQ 被动回复条数限制，控制在安全范围）
_MAX_AUTO_REFRESH = 2
# 会话轮询总时长（秒），保持在 QQ 被动消息 5 分钟窗口内
_LOGIN_DEADLINE = 240


async def _qr_login_flow(event):
    """在当前会话里直接发二维码图片：过期自动换新，扫码成功后原地提示登录成功。"""
    qr = QRSession()
    try:
        try:
            png = await qr.fetch_qr()
        except Exception as e:
            log(f"获取二维码失败: {e}")
            await event.reply("❌ 获取二维码失败，请稍后重试～")
            return
        await event.reply_image(
            png, "📱 请用群主/管理员手机 QQ「扫一扫」扫描此码并确认；过期会自动换新，也可发「刷新」。"
        )

        refreshes = 0
        last_status = ""
        deadline = time.time() + _LOGIN_DEADLINE
        while time.time() < deadline:
            await asyncio.sleep(2)
            res = await qr.poll()
            status = res.get("status")
            if status == "success":
                set_user_cookie(event.user_id, res["cookie"])
                log_user(event.user_id, "扫码登录成功，已保存CK")
                await event.reply("✅ 登录成功，CK 已自动提取并保存！现在发「清空群文件 群号」即可～")
                return
            if status == "scanned" and last_status != "scanned":
                await event.reply("📲 已扫码，请在手机上点「确认登录」～")
            if status == "expired":
                if refreshes >= _MAX_AUTO_REFRESH:
                    await event.reply("⌛ 二维码多次过期，请重新发「登录」或「刷新」～")
                    return
                refreshes += 1
                try:
                    png = await qr.fetch_qr()
                except Exception as e:
                    log(f"刷新二维码失败: {e}")
                    await event.reply("❌ 刷新二维码失败，请重新发「登录」～")
                    return
                await event.reply_image(png, f"🔄 二维码已自动刷新（第 {refreshes} 次），请尽快扫描～")
                last_status = ""
                continue
            if status == "error":
                log(f"扫码登录错误: {res.get('message')}")
                await event.reply(f"❌ 登录失败：{res.get('message')}")
                return
            last_status = status
        await event.reply("⌛ 登录超时，请重新发「登录」～")
    finally:
        await qr.close()


@handler(r'^(登录群文件|登录|刷新|刷新二维码)$', name='登录群文件', desc='聊天内出二维码扫码登录并自动提取CK')
async def cmd_login(event, match):
    if not await _require_admin(event):
        return
    await _qr_login_flow(event)

@handler(r'^(登录网页|网页登录)$', name='登录网页', desc='获取浏览器扫码登录链接（图片扫不了时用）')
async def cmd_login_web(event, match):
    """下发后端登录网页链接：适合聊天里图片扫不了的 QQ，网页内实时出码、过期自动刷新。"""
    if not await _require_admin(event):
        return
    base = get_base_url()
    if not base:
        await event.reply(
            "⚠ 还没配置登录页地址。请先发送：\n"
            "设置登录地址 https://你的面板域名:5200\n"
            "（填写机器人 Web 面板的外网可访问地址，然后再发「登录网页」）"
        )
        return
    token = create_login_token(event.user_id)
    url = f"{base}/api/ext/qwj/login?token={token}"
    log_user(event.user_id, "生成登录链接")
    content = (
        "🔑 点下方按钮打开登录页（或复制链接到浏览器），用**群主/管理员**手机 QQ 扫码：\n"
        f"`{url}`\n\n二维码在网页里实时生成、过期会自动刷新；扫码确认后 CK 自动保存，"
        "随后回来发「清空群文件 群号」即可。链接 15 分钟内有效。"
    )
    await event.reply(
        content,
        buttons=[[{"text": "🔑 打开登录页", "link": url}]],
        msg_type=2,
    )

@handler(r'^设置登录地址\s+(\S+)$', name='设置登录地址', desc='配置登录页外网地址')
async def cmd_set_base(event, match):
    if not await _require_admin(event):
        return
    url = match.group(1).strip()
    if not url.startswith(("http://", "https://")):
        await event.reply("❌ 地址需以 http:// 或 https:// 开头～")
        return
    set_base_url(url)
    log_user(event.user_id, "设置登录地址")
    await event.reply(f"✅ 已保存登录页地址：\n`{url.rstrip('/')}`\n现在可发送「登录」获取登录链接～", msg_type=2)

@handler(r'^群文件登录\s+(.+)', name='群文件登录', desc='保存当前管理员的群文件Cookie')
async def cmd_save_cookie(event, match):
    if not await _require_admin(event):
        return
    cookie_str = match.group(1).strip()
    if 'skey=' not in cookie_str:
        await event.reply("❌ Cookie 无效，缺少 skey 字段～")
        return
    set_user_cookie(event.user_id, cookie_str)
    log_user(event.user_id, "更新Cookie")
    await event.reply("✅ Cookie 已保存，31天内有效～")

@handler(r'^(添加管理员|新增管理员)(?:\s+(.*))?$', name='添加管理员', desc='把用户(可艾特)加入群文件管理员白名单')
async def cmd_add_admin(event, match):
    if not await _require_admin(event):
        return
    targets = _extract_target_ids(event, (match.group(2) or "").strip())
    if not targets:
        await event.reply("用法：添加管理员 @用户（或 添加管理员 <用户ID>）～")
        return
    added, existed = [], []
    for t in targets:
        (added if add_admin(t) else existed).append(t)
    log_user(event.user_id, f"添加管理员 added={added} existed={existed}")
    lines = []
    if added:
        lines.append("✅ 已添加管理员：\n" + "\n".join(f"`{a}`" for a in added))
    if existed:
        lines.append("ℹ️ 已在名单中：\n" + "\n".join(f"`{a}`" for a in existed))
    await event.reply("\n".join(lines), msg_type=2)

@handler(r'^删除管理员(?:\s+(.*))?$', name='删除管理员', desc='把用户(可艾特)移出群文件管理员白名单')
async def cmd_del_admin(event, match):
    if not await _require_admin(event):
        return
    targets = _extract_target_ids(event, (match.group(1) or "").strip())
    if not targets:
        await event.reply("用法：删除管理员 @用户（或 删除管理员 <用户ID>）～")
        return
    removed, missing = [], []
    for t in targets:
        (removed if remove_admin(t) else missing).append(t)
    log_user(event.user_id, f"删除管理员 removed={removed} missing={missing}")
    lines = []
    if removed:
        lines.append("✅ 已移除管理员：\n" + "\n".join(f"`{a}`" for a in removed))
    if missing:
        lines.append("ℹ️ 不在名单中：\n" + "\n".join(f"`{a}`" for a in missing))
    await event.reply("\n".join(lines), msg_type=2)

@handler(r'^管理员列表$', name='管理员列表', desc='查看群文件管理员白名单')
async def cmd_list_admin(event, match):
    if not await _require_admin(event):
        return
    admins = get_admins()
    body = "\n".join(f"`{a}`" for a in admins) if admins else "（空）"
    await event.reply(f"👮 群文件管理员白名单（{len(admins)}）：\n{body}", msg_type=2)

@handler(r'^清空群文件\s+(\d+)$', name='清空群文件', desc='清空指定群的所有文件（管理员用）')
async def cmd_clear_files(event, match):
    if not await _require_admin(event):
        return
    group_id = match.group(1)
    user_id = event.user_id
    log_user(user_id, f"请求清空群 {group_id}")

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
        log_user(user_id, f"清空群 {group_id} 完成，成功 {success_count}/{len(batches)} 批，共 {total} 个文件")

    await event.reply(f"✅ 清理完成，群 {group_id} 共处理 {total} 个文件～")

ensure_admin_dirs()
log("群文件管理插件已加载")