#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bale-bot.py — دانلودر یوتیوب روی GitHub Actions برای ربات بله
--------------------------------------------------------------
Worker کلادفلر وقتی نمونه‌های cobalt شکست بخورند، با یک PAT این workflow را
dispatch می‌کند؛ این اسکریپت روی رانر گیت‌هاب اجرا می‌شود:
  ۱) ویدیو را با yt-dlp دانلود می‌کند (تا 720p mp4 یا mp3) — با نردبان ۵ کلاینت
  ۲) فایل را به Release کتابخانه (تگ library) می‌چسباند (تا ۲ گیگابایت)
  ۳) لینک دائمی را در بله به همان چت می‌فرستد
متغیرها (workflow ست می‌کند):
  GH_TOKEN، BALE_TOKEN (Secret)، LIB_REPO، VID، MODE، QUALITY، CHAT_ID،
  COOKIES (اختیاری — محتوای cookies.txt برای ویدیوهای بسیار قفل)
"""
import glob
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

BALE_TOKEN = os.environ.get("BALE_TOKEN", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
LIB_REPO = os.environ.get("LIB_REPO", "")
LIB_TAG = os.environ.get("LIB_TAG", "library")
VID = os.environ.get("VID", "")
MODE = os.environ.get("MODE", "video")
QUALITY = os.environ.get("QUALITY", "720")
CHAT_ID = os.environ.get("CHAT_ID", "")
COOKIES = os.environ.get("COOKIES", "")

GH_API = "https://api.github.com"
GH_UPLOAD = "https://uploads.github.com"
MAX_BYTES = 2 * 1024 * 1024 * 1024 - 1024 * 1024   # سقف asset گیت‌هاب با حاشیه


def bale_send(text):
    """پیام به کاربر — کوشا؛ خطای ارسال نباید دانلود سالم را مردود کند"""
    if not BALE_TOKEN or not CHAT_ID:
        return
    try:
        req = urllib.request.Request(
            f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": CHAT_ID, "text": text[:4000]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:  # noqa: BLE001 — گزارش، نه توقف
        print("[bale-bot] send failed:", e, flush=True)


def gh(method, path, body=None):
    """GET/POST/DELETE روی api.github.com — خروجی (status, json|None)"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GH_API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "bale-bot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except Exception:
            return e.code, None


def gh_upload(path, filepath, content_type="application/octet-stream"):
    """آپلود استریمی فایل به uploads.github.com — خروجی (status, json|None)"""
    size = os.path.getsize(filepath)
    f = open(filepath, "rb")
    try:
        req = urllib.request.Request(
            GH_UPLOAD + path,
            data=f,
            method="POST",
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "Content-Type": content_type,
                "Content-Length": str(size),
                "User-Agent": "bale-bot",
            },
        )
        with urllib.request.urlopen(req, timeout=3600) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    finally:
        f.close()


def clean_name(title, ext):
    n = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", str(title or "video")).strip()
    n = re.sub(r"\s+", " ", n)[:80].strip() or "video"
    if not re.search(r"\.[A-Za-z0-9]{2,5}$", n):
        n += ext
    return n


def write_cookies():
    """در صورت وجود COOKIES آن را در cookies.txt می‌نویسد و آرگومان‌های مربوط را می‌دهد"""
    if not COOKIES:
        return []
    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write(COOKIES.lstrip().lstrip("﻿"))  # BOM احتمالی paste را هم پاک کن
    return ["--cookies", "cookies.txt"]


def cookie_notes():
    """اگر حال‌واحوای کوکی مشکوک است، یادداشت‌های تشخیصی فارسی بده — وگرنه []"""
    if not COOKIES:
        return ["کوکی (Secret با نام COOKIES) ست نشده — ویدیوهای قفل از سرور گیت‌هاب "
                "بدون نشست لاگین معمولاً باز نمی‌شوند (گزینهٔ G در README)."]
    if COOKIES.lstrip().startswith("["):
        return ["فرمت فایل کوکی JSON است — در افزونه گزینهٔ Export (فرمت Netscape) را بزن؛ "
                "فایل درست باید با «# Netscape HTTP Cookie File» شروع شود."]
    if "LOGIN_INFO" not in COOKIES:
        return ["کوکی نشست لاگین ندارد (احتمالاً بدون لاگین Export شده) — در مرورگر به یوتیوب "
                "با اکانت بلااستفاده لاگین کن و دوباره Export بگیر."]
    return []


def diagnose_fail(url):
    """پروب تشخیصی -v -F بعد از شکست همهٔ تلاش‌ها: هشدارهای PO Token / EJS / solver و
    وضعیت فرمت‌ها را از خروجی verbose بیرون می‌کشد تا خطای واقعی دیده شود"""
    try:
        p = subprocess.run(["yt-dlp", "-v", "-F", "--no-playlist", *write_cookies(), url],
                           capture_output=True, text=True, timeout=180)
    except Exception:
        return []
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    keys = ("po token", "pot", "ejs", "solver", "jsc", "runtime", "provider",
            "warning", "sign in", "requested format", "available formats")
    return [ln.strip() for ln in out.splitlines() if any(k in ln.lower() for k in keys)][-12:]


def download(vid, mode, quality):
    """yt-dlp با نردبان client ها؛ خروجی (مسیر فایل، عنوان) یا (None، متن خطای کامل برای کاربر)"""
    url = f"https://www.youtube.com/watch?v={vid}"
    cookie_args = write_cookies()

    if mode == "audio":
        fmt_args = ["-x", "--audio-format", "mp3"]
    else:
        q = quality if str(quality).isdigit() else "720"
        # محدودیت [ext=mp4] را برداشتیم: اگر برای کلاینتی فقط webm بدهد نردبان نچسبد؛
        # ظرف نهایی همان mp4 می‌ماند چون --merge-output-format mp4 سر جایش است.
        fmt_args = ["-f", f"bv*[height<={q}]+ba/b[height<={q}]/bv*+ba/b",
                    "--merge-output-format", "mp4"]

    # نردبان کلاینت‌ها — همه نام‌های معتبر نسخهٔ فعلی yt-dlp (اکتبر ۲۰۲۶):
    #   پیش‌فرض اول می‌آید تا انتخاب خود yt-dlp و کوکی اثر کند؛ بعد خانوادهٔ tv
    #   (دوست‌دار IP دیتاسنتر)، بعد visionos و android_vr.
    # «requested format is not available» خطای انتخاب‌گر است و yt-dlp خودش روی آن
    # کلاینت بعدی را امتحان نمی‌کند — پس نردبان را صریح خودمان می‌چرخانیم.
    ladder = [
        ("پیش‌فرض", []),
        ("tv_simply", ["--extractor-args", "youtube:player_client=tv_simply"]),
        ("tv", ["--extractor-args", "youtube:player_client=tv"]),
        ("visionos", ["--extractor-args", "youtube:player_client=visionos"]),
        ("android_vr", ["--extractor-args", "youtube:player_client=android_vr"]),
    ]

    attempts = []
    for i, (name, extra) in enumerate(ladder):
        out = f"out{i}"
        # --concurrent-fragments 4: دانلود ۴ فرگمنت موازی (۲-۴ برابر سریع‌تر)
        # --print "after_move:%(title)s": بعد از اتمام دانلود و move، عنوان رو چاپ می‌کنه
        #   نکته: بدون prefix یعنی after_move، --print imply simulate می‌کرد و دانلود نمی‌شد!
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings",
               "--concurrent-fragments", "4",
               "--print", "after_move:%(title)s",
               "-o", f"{out}.%(ext)s", *fmt_args, *cookie_args, *extra, url]
        print(f"[bale-bot] attempt {i + 1}/{len(ladder)} ({name}):",
              " ".join(cmd[:8]), "...", flush=True)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
        except subprocess.TimeoutExpired:
            attempts.append((name, "timeout پس از ۷ دقیقه"))
            print("[bale-bot]   ↳ timeout", flush=True)
            continue
        if p.returncode == 0:
            files = [f for f in glob.glob(f"{out}.*") if not f.endswith(".part")]
            if files:
                # عنوان از خروجی --print (خط آخر stdout)؛ fallback روی vid
                title_lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
                title = title_lines[-1] if title_lines else vid
                return max(files, key=os.path.getsize), title
            attempts.append((name, "exit 0 ولی فایل پیدا نشد"))
            continue
        err_line = ""
        for ln in reversed((p.stderr or "").strip().splitlines()):
            if ln.strip():
                err_line = ln.strip()
                break
        err_line = re.sub(r"^ERROR:\s*", "", err_line)[:140] or f"exit {p.returncode} بدون پیام"
        attempts.append((name, err_line))
        print(f"[bale-bot]   ↳ {name}: {err_line}", flush=True)

    notes = cookie_notes()
    probe = diagnose_fail(url)
    for ln in probe:
        print("[bale-bot] diagnose:", ln[:200], flush=True)
    lines = ["😕 دانلود روی سرور گیت‌هاب با همهٔ روش‌ها انجام نشد.", "تلاش‌ها:"]
    lines += [f"• {n}: {e}" for n, e in attempts]
    if probe:
        lines += ["", "🔍 تشخیص:"]
        lines += [f"• {ln[:150]}" for ln in probe[-5:]]
    if notes:
        lines += ["", *[f"⚠️ {x}" for x in notes]]
    lines += ["", "معمول‌ترین رفع: کوکی تازهٔ لاگین‌شده (گزینهٔ G در README) و بعد دوباره فرستادن همان لینک."]
    return None, "\n".join(lines)


def get_title(vid):
    """عنوان برای نام فایل — از yt-dlp -J؛ کوشا"""
    try:
        p = subprocess.run(["yt-dlp", "-J", "--no-playlist", "--no-warnings", *write_cookies(),
                            f"https://www.youtube.com/watch?v={vid}"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode == 0:
            return json.loads(p.stdout).get("title") or vid
    except Exception:
        pass
    return vid


def ensure_release():
    st, data = gh("GET", f"/repos/{LIB_REPO}/releases/tags/{LIB_TAG}")
    if st == 200 and data:
        return data["id"]
    if st == 404:
        st2, data2 = gh("POST", f"/repos/{LIB_REPO}/releases",
                        {"tag_name": LIB_TAG, "name": "کتابخانه", "body": "فیلم‌های کتابخانهٔ ربات بله"})
        if st2 in (201, 200) and data2:
            return data2["id"]
        raise RuntimeError(f"release-create-{st2}")
    raise RuntimeError(f"release-get-{st}")


def upload_to_library(rel_id, filepath, filename):
    """اگه فایل هم‌نام از قبل تو کتابخانه هست، همان لینک قبلی برگردانده می‌شود
    (هیچ‌چیز حذف نمی‌شود)؛ وگرنه آپلود می‌شود → صبر تا uploaded → لینک.
    در صورت 422 (تکرار نام که pre-check ندید)، دوباره جست‌وجو می‌کند."""
    def find_existing():
        st, rel = gh("GET", f"/repos/{LIB_REPO}/releases/tags/{LIB_TAG}")
        if st != 200 or not rel:
            return None
        for a in rel.get("assets", []):
            if a.get("name") == filename:
                return a
        return None

    # pre-check: اگه هست، آپلود نزن
    existing = find_existing()
    if existing:
        print("[bale-bot] already in library — returning existing link:", filename, flush=True)
        return existing.get("browser_download_url")

    # آپلود
    st, asset = gh_upload(f"/repos/{LIB_REPO}/releases/{rel_id}/assets?name={urllib.parse.quote(filename)}", filepath)

    # 422 = تکرار نام (گاهی pre-check به‌خاطر cache یا race وقتش نمی‌رسه ببینه)
    if st == 422:
        print(f"[bale-bot] got 422 — re-checking for existing asset '{filename}'", flush=True)
        existing = find_existing()
        if existing:
            print("[bale-bot] found after 422:", existing.get("name"), flush=True)
            return existing.get("browser_download_url")
        # اگه واقعاً پیدا نشد، لیست اسم‌ها رو چاپ کن برای دیباگ
        st2, rel2 = gh("GET", f"/repos/{LIB_REPO}/releases/tags/{LIB_TAG}")
        if st2 == 200 and rel2:
            names = [a.get("name") for a in rel2.get("assets", [])]
            print("[bale-bot] assets currently in library:", names, flush=True)

    if st not in (201, 200) or not asset:
        raise RuntimeError(f"asset-upload-{st}")
    for _ in range(30):  # آپلود گیت‌هاب ابتدا state=started است
        st, a = gh("GET", f"/repos/{LIB_REPO}/releases/assets/{asset['id']}")
        if st == 200 and a and a.get("state") == "uploaded":
            return a.get("browser_download_url")
        time.sleep(3)
    return asset.get("browser_download_url")


def main():
    if not (GH_TOKEN and LIB_REPO and VID and CHAT_ID and BALE_TOKEN):
        print("[bale-bot] missing env", flush=True)
        bale_send("⚙️ تنظیم صف گیت‌هاب ناقص است (Secret یا متغیرها). README-github.txt را ببین.")
        return 1
    bale_send("🎬 دانلود روی سرور گیت‌هاب شروع شد؛ معمولاً چند دقیقه طول می‌کشد…")

    # اگر کوکی مشکوک است، همان اول بگو تا کاربر همزمان با اجرا درستش کند
    notes = cookie_notes()
    if notes:
        bale_send("⚠️ پیش از دانلود: " + notes[0])

    path, info = download(VID, MODE, QUALITY)
    if path is None:
        bale_send(info)
        return 1

    size = os.path.getsize(path)
    if size > MAX_BYTES:
        bale_send(f"😕 فایل بزرگ‌تر از سقف ۲ گیگابایت گیت‌هاب است ({size // (1024 * 1024)}MB).")
        return 1

    ext = ".mp3" if MODE == "audio" else ".mp4"
    # info = عنوان ویدیو که از yt-dlp --print گرفته شده (هیچ فراخوانی اضافه نیست)
    filename = clean_name(info, ext)
    try:
        rel_id = ensure_release()
        link = upload_to_library(rel_id, path, filename)
    except Exception as e:  # noqa: BLE001
        bale_send(f"😕 آپلود به کتابخانه شکست خورد.\n(کد خطا: {e})")
        return 1

    mb = max(1, size // (1024 * 1024))
    bale_send(f"✅ آماده شد:\n📄 {filename} ({mb}MB)\n🔗 {link}\n"
              f"این لینک دائمی است و از این به بعد در /list هم هست.")
    try:
        os.remove(path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
