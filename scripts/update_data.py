#!/usr/bin/env python3
"""Daily data refresh for the MRT transfer page (run by GitHub Actions).

Writes into data/:
  tymc.json      Taoyuan Metro official timetable (www.tymetro.com.tw), train type of every departure
  holidays.json  Government office calendar (行政院人事行政總處, via data.gov.tw dataset 14718)
  tdx/*.json, tt/*.json, fl/*.json   TDX static data, only when TDX_CLIENT_ID / TDX_CLIENT_SECRET are set
  meta.json      when each part was refreshed
Each part is independent: a failure keeps the previous file.
"""
import csv, http.cookiejar, io, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
OUT = os.path.join(os.path.dirname(__file__), "..", "data")
UA = {"User-Agent": "Mozilla/5.0 (compatible; daily-timetable-refresh)"}


def get(url, headers=None, data=None, tries=4, opener=None):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
            with (opener.open(req, timeout=60) if opener else urllib.request.urlopen(req, timeout=60)) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            print(f"  retry {k + 1} {url[:120]}: {e}", file=sys.stderr)
            if code in (400, 401, 403, 404):
                raise
            # TDX answers 429 even at about one call a second, so back off much longer for it
            time.sleep(15 * (k + 1) if code == 429 else 3 * (k + 1))
    raise RuntimeError("failed: " + url)


def write(name, obj):
    path = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def read(name, default):
    try:
        with open(os.path.join(OUT, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


# ---------- Taoyuan Metro official timetable ----------
TY_STATIONS = ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10", "A11", "A12", "A13",
               "A14a", "A15", "A16", "A17", "A18", "A19", "A20", "A21", "A22"]
TY_BASE = "https://www.tymetro.com.tw/tymetro-new/tw/_pages/travel-guide/"
TY_DAYS = 3  # today and the next two days, so one missed run does not leave a gap
CELL = re.compile(r'>(\d{2})點</div>\s*<span class="[^"]*\bdes(\d{2})\b[^"]*">\s*<i>(\d{2})</i>')


def parse_ty(html):
    html = html.split('class="nav timetable_list"')[0]  # the page repeats the tables for small screens
    out = {}
    for part in re.split(r"<table\b", html)[1:]:
        m = re.search(r'summary="桃園捷運列車時刻表:([^"]*)"', part)
        if not m or m.group(1).strip() in ("", "-"):
            continue
        head = m.group(1).strip()
        trains = [[f"{h}:{mi}", t] for h, t, mi in CELL.findall(part)]
        if trains:
            out[head] = sorted(trains)
    return out


def update_tymc():
    old = read("tymc.json", {"days": {}}).get("days", {})
    today = datetime.now(TW).date()
    days = {}
    for i in range(TY_DAYS):
        ds = (today + timedelta(days=i)).isoformat()
        # the site keeps the chosen date in a session cookie (set by POST station-timetable-date.php)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        get(TY_BASE + "timetable-A1", opener=opener)
        day = {}
        for sid in TY_STATIONS:
            # the chosen date does not stick for long, so set it again before every station page
            get(TY_BASE + "station-timetable-date.php", {"Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest"},
                urllib.parse.urlencode({"date": ds}).encode(), opener=opener)
            html = get(TY_BASE + "timetable-" + sid, opener=opener).decode("utf-8", "replace")
            shown = re.search(r'id="timetable_date"[^>]*value="([\d-]+)"', html)
            if not shown or shown.group(1) != ds:
                print(f"  {sid}: page shows {shown and shown.group(1)} instead of {ds}, skipped", file=sys.stderr)
                continue
            tables = parse_ty(html)
            if tables:
                day[sid] = tables
            time.sleep(0.8)
        n = sum(len(v) for t in day.values() for v in t.values())
        print(f"tymetro {ds}: {len(day)} stations, {n} departures")
        if len(day) >= 20 and n > 1000:
            days[ds] = day
    # keep earlier files' days that are still useful (yesterday onward) and were not refreshed now
    keep_from = (today - timedelta(days=1)).isoformat()
    for ds, day in old.items():
        if ds >= keep_from and ds not in days:
            days[ds] = day
    if not any(ds >= today.isoformat() for ds in days):
        raise RuntimeError("tymetro: nothing parsed")
    write("tymc.json", {"source": "桃園捷運公司官網時刻表", "url": TY_BASE + "timetable.php",
                        "types": {"01": "直達車", "02": "尖峰增停直達車", "03": "普通車", "04": "增開往機場普通車", "05": "尖峰跳站普通車"},
                        "days": dict(sorted(days.items()))})


# ---------- government office calendar ----------
def update_holidays():
    meta = json.loads(get("https://data.gov.tw/api/v2/rest/dataset/14718"))
    year = datetime.now(TW).year
    want = {f"{year - 1911}年": year, f"{year + 1 - 1911}年": year + 1}
    out = {}
    for res in meta["result"]["distribution"]:
        desc = res.get("resourceDescription", "")
        if res.get("resourceFormat") != "CSV" or "google" in desc.lower() or not any(desc.startswith(k) for k in want):
            continue
        raw = get(res["resourceDownloadUrl"])
        for enc in ("utf-8-sig", "big5", "cp950"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        for row in csv.DictReader(io.StringIO(text)):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            d, off = row.get("西元日期", ""), row.get("是否放假", "")
            if re.fullmatch(r"\d{8}", d) and off in ("0", "2"):
                out[d] = {"off": off == "2", "note": row.get("備註", "")}
    if len(out) < 300:
        raise RuntimeError(f"holidays: only {len(out)} days parsed")
    print(f"holidays: {len(out)} days")
    write("holidays.json", {"source": "行政院人事行政總處 政府行政機關辦公日曆表（data.gov.tw 14718）", "days": out})


# ---------- TDX ----------
TDX = "https://tdx.transportdata.tw"


def tdx_token():
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": os.environ["TDX_CLIENT_ID"],
                                   "client_secret": os.environ["TDX_CLIENT_SECRET"]}).encode()
    r = get(TDX + "/auth/realms/TDXConnect/protocol/openid-connect/token", {"Content-Type": "application/x-www-form-urlencoded"}, body)
    return json.loads(r)["access_token"]


def update_tdx():
    tok = tdx_token()

    def api(path):
        time.sleep(3)
        return json.loads(get(TDX + "/api/basic" + path + ("&" if "?" in path else "?") + "$format=JSON",
                              {"Authorization": "Bearer " + tok, "Accept": "application/json"}, tries=5))

    def save_timetable(op, rows):
        by = {}
        for r in rows:
            by.setdefault(r.get("StationID"), []).append(r)
        for sid, rs in by.items():
            if sid:
                write(f"tt/{sid}.json", rs)
        print(f"tdx timetable {op}: {len(by)} stations")

    ops = ["TRTC", "NTMC", "TYMC"]
    jobs = []
    for name in ("StationOfRoute", "LineTransfer", "S2STravelTime", "Frequency"):
        for op in ops:
            jobs.append((f"{name}/{op}", f"/v2/Rail/Metro/{name}/{op}", lambda d, n=name, o=op: write(f"tdx/{n}-{o}.json", d)))
    jobs.append(("Line/NTMC", "/v2/Rail/Metro/Line/NTMC", lambda d: write("tdx/Line-NTMC.json", d)))
    for op in ops:
        jobs.append((f"FirstLast/{op}", f"/v2/Rail/Metro/FirstLastTimetable/{op}", lambda d, o=op: write(f"fl/{o}.json", d)))
        jobs.append((f"StationTimeTable/{op}", f"/v2/Rail/Metro/StationTimeTable/{op}", lambda d, o=op: save_timetable(o, d)))

    # one pass, then a second pass for whatever TDX refused; a failed file keeps the previous day's copy
    for attempt in range(2):
        failed = []
        for label, path, done in jobs:
            try:
                done(api(path))
            except Exception as e:  # noqa: BLE001
                print(f"tdx {label}: {e}", file=sys.stderr)
                failed.append((label, path, done))
        if not failed:
            break
        jobs = failed
        if attempt == 0:
            print(f"tdx: {len(failed)} failed, retrying after a pause", file=sys.stderr)
            time.sleep(90)
    else:
        print("tdx still failed: " + ", ".join(j[0] for j in jobs), file=sys.stderr)


def main():
    meta = read("meta.json", {})
    now = datetime.now(TW).isoformat(timespec="seconds")
    jobs = [("tymc", update_tymc), ("holidays", update_holidays)]
    if os.environ.get("TDX_CLIENT_ID") and os.environ.get("TDX_CLIENT_SECRET"):
        jobs.append(("tdx", update_tdx))
    else:
        print("TDX secrets not set; skipping TDX part")
    for key, fn in jobs:
        try:
            fn()
            meta[key] = now
        except Exception as e:  # noqa: BLE001
            print(f"{key} FAILED: {e}", file=sys.stderr)
    write("meta.json", meta)


if __name__ == "__main__":
    main()
