#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
나이스 학사일정 수집기

교육청 단위로 학교 목록과 학사일정을 받아, 학부모 앱이 바로 읽는 정적 JSON을 만든다.
인증키는 이 과정에서만 쓰이고 결과물에는 남지 않는다.

  export NEIS_KEY=발급받은키
  python3 collect.py --office F10 --year 2026

만들어지는 것
  dist/schools.json            학교 목록 (검색용)
  dist/cal/F10_7402220.json    학교별 휴업일
  dist/report.txt              품질 분포와 경고

자동 검증
  - 학년도 수업일수가 190일(초등학교 법정 기준)에서 크게 벗어나면 경고.
    방학이 누락되거나 잘못 잡히면 여기서 걸린다.
  - 나이스가 '공휴일'로 표시한 날과 이 스크립트의 공휴일 규칙을 대조.
    여러 학교가 공통으로 표시한 날이 규칙에 없으면 임시공휴일일 가능성이 높다.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta

BASE = "https://open.neis.go.kr/hub/"
KEY = os.environ.get("NEIS_KEY", "")

# 시도교육청 코드. 응답의 교육청명을 그대로 리포트에 찍으므로 틀린 코드는 바로 드러난다.
OFFICES = ["B10", "C10", "D10", "E10", "F10", "G10", "H10", "I10", "J10",
           "K10", "M10", "N10", "P10", "Q10", "R10", "S10", "T10"]
KINDS = ["초등학교", "중학교", "고등학교"]

# ─────────────────────────────────────────────────────────────
# 공휴일 규칙 (학부모 앱과 같은 규칙을 유지할 것)
# ─────────────────────────────────────────────────────────────
FIXED = [
    ("01-01", "신정", False), ("03-01", "삼일절", True), ("05-01", "노동절", True),
    ("05-05", "어린이날", True), ("06-06", "현충일", False), ("07-17", "제헌절", True),
    ("08-15", "광복절", True), ("10-03", "개천절", True), ("10-09", "한글날", True),
    ("12-25", "성탄절", True),
]
LUNAR = {
    2025: ("2025-01-29", "2025-10-06", "2025-05-05"),
    2026: ("2026-02-17", "2026-09-25", "2026-05-24"),
    2027: ("2027-02-07", "2027-09-15", "2027-05-13"),
    2028: ("2028-01-27", "2028-10-03", "2028-05-02"),
    2029: ("2029-02-13", "2029-09-22", "2029-05-20"),
    2030: ("2030-02-03", "2030-09-12", "2030-05-09"),
    2031: ("2031-01-23", "2031-10-01", "2031-05-28"),
}
EXTRA = {"2026-06-03": "선거일"}


def d(s):
    return date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def holidays(year):
    """한 해의 공휴일 {date: 이름}. 연휴와 대체공휴일을 규칙으로 만든다."""
    m, subs = {}, []

    def put(dt, nm):
        m.setdefault(dt, nm)

    for md, nm, sub in FIXED:
        dt = date(year, int(md[:2]), int(md[3:]))
        put(dt, nm)
        if sub:
            subs.append((dt, dt, nm, "wk"))

    if year in LUNAR:
        seol, chuseok, buddha = LUNAR[year]
        for src, nm in ((seol, "설날"), (chuseok, "추석")):
            mid = d(src)
            for o in (-1, 0, 1):
                put(mid + timedelta(days=o), nm)
            subs.append((mid, mid + timedelta(days=1), nm, "fest"))
        put(d(buddha), "부처님오신날")
        subs.append((d(buddha), d(buddha), "부처님오신날", "wk"))

    for k, nm in EXTRA.items():
        if k.startswith(str(year)):
            put(d(k), nm)

    for mid, end, nm, kind in subs:
        need = 0
        if kind == "fest":
            for o in (-1, 0, 1):
                x = mid + timedelta(days=o)
                if x.weekday() == 6 or (x in m and m[x] != nm):
                    need += 1
        else:
            if end.weekday() >= 5 or (end in m and m[end] != nm):
                need += 1
        cur = end + timedelta(days=1)
        while need > 0:
            if cur.weekday() < 5 and cur not in m:
                m[cur] = "대체공휴일"
                need -= 1
            cur += timedelta(days=1)
    return m


# ─────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────
def call(service, **params):
    """서비스 하나를 끝까지 읽어 (rows, total, code) 반환."""
    rows, total, page = [], None, 1
    while page <= 30:
        q = {"Type": "json", "pIndex": page, "pSize": 1000}
        if KEY:
            q["KEY"] = KEY
        q.update({k: v for k, v in params.items() if v not in (None, "")})
        url = BASE + service + "?" + urllib.parse.urlencode(q)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    body = json.loads(r.read().decode("utf-8"))
                break
            except Exception as e:
                if attempt == 2:
                    return rows, total, "NET-%s" % e
                time.sleep(1.5 * (attempt + 1))

        if service not in body:
            return rows, total, (body.get("RESULT") or {}).get("CODE", "?")
        head = body[service][0]["head"]
        if total is None:
            total = head[0]["list_total_count"]
        rows.extend(body[service][1].get("row") or [])
        if len(rows) >= (total or 0):
            break
        page += 1
        time.sleep(0.1)
    return rows, total, "INFO-000"


# ─────────────────────────────────────────────────────────────
# 정규화
# ─────────────────────────────────────────────────────────────
def group_runs(dates):
    """평일 휴업일을 연속 구간으로 묶는다. 사이에 낀 토·일은 이어붙인다."""
    runs = []
    for x in sorted(dates):
        if runs:
            last = runs[-1]
            probe, ok = last[-1] + timedelta(days=1), True
            while probe < x:
                if probe.weekday() < 5:
                    ok = False
                    break
                probe += timedelta(days=1)
            if ok:
                last.append(x)
                continue
        runs.append([x])
    return runs


def normalize(rows):
    """학사일정 행에서 휴업일을 뽑아 방학 구간과 재량휴업일로 나눈다."""
    vocab = Counter()
    off, pub, labels = set(), set(), {}
    for r in rows:
        nm = (r.get("SBTR_DD_SC_NM") or "").strip()
        vocab[nm or "(null)"] += 1
        ymd = r.get("AA_YMD") or ""
        if len(ymd) != 8:
            continue
        dt = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        if "공휴일" in nm:
            pub.add(dt)
            continue
        by_name = (not nm) and any(w in (r.get("EVENT_NM") or "") for w in ("방학", "휴업", "재량"))
        if "휴업" not in nm and not by_name:
            continue
        if dt.weekday() >= 5:          # 주말은 어차피 수업일이 아님
            continue
        off.add(dt)
        labels.setdefault(dt, r.get("EVENT_NM") or nm)

    vac, clo = [], []
    for run in group_runs(off):
        if len(run) >= 5:
            vac.append([run[0].isoformat(), run[-1].isoformat()])
        else:
            clo.extend(x.isoformat() for x in run)
    # 학교가 공휴일을 휴업일로 중복 등록한 경우 제거 (선거일 등)
    clo = [k for k in clo if d(k) not in holidays(int(k[:4]))]

    null_ratio = vocab["(null)"] / sum(vocab.values()) if vocab else 1.0
    return {
        "vac": vac, "clo": sorted(clo), "pub": pub,
        "vocab": dict(vocab), "null_ratio": round(null_ratio, 3),
        "labels": {k.isoformat(): v for k, v in labels.items()},
    }


def school_days(sy, vac, clo):
    """학년도 수업일수. 190일 언저리가 아니면 데이터가 이상한 것."""
    hol = {}
    hol.update(holidays(sy))
    hol.update(holidays(sy + 1))
    vac_r = [(d(a), d(b)) for a, b in vac]
    clo_s = {d(x) for x in clo}
    cur, end, n = date(sy, 3, 1), date(sy + 1, 2, 28), 0
    while cur <= end:
        if cur.weekday() < 5 and cur not in hol and cur not in clo_s \
           and not any(a <= cur <= b for a, b in vac_r):
            n += 1
        cur += timedelta(days=1)
    return n


def grade(norm, days):
    if not norm["vac"] and not norm["clo"]:
        return "none"
    if not norm["vac"] or not (185 <= days <= 200):
        return "partial"
    return "ok"


# ─────────────────────────────────────────────────────────────
def collect_office(office, kinds, year, out_dir):
    """교육청 하나를 처리해 (idx, cal, stats) 반환."""
    frm, to = "%d0301" % year, "%d0228" % (year + 1)
    idx, cal, counts, warns = [], {}, Counter(), []
    pub_seen = Counter()
    office_name = office

    schools = []
    for kind in kinds:
        rows, _, code = call("schoolInfo", ATPT_OFCDC_SC_CODE=office, SCHUL_KND_SC_NM=kind)
        if code != "INFO-000":
            continue
        for r in rows:
            office_name = r.get("ATPT_OFCDC_SC_NM") or office_name
            schools.append((r["SD_SCHUL_CODE"], r["SCHUL_NM"], kind[0]))
        time.sleep(0.1)

    print("  %s (%s) %d개교" % (office, office_name, len(schools)))
    for i, (sd, name, k1) in enumerate(schools, 1):
        sid = "%s_%s" % (office, sd)
        rows, _, code = call("SchoolSchedule", ATPT_OFCDC_SC_CODE=office, SD_SCHUL_CODE=sd,
                             AA_FROM_YMD=frm, AA_TO_YMD=to)
        q, days, norm = "none", 0, {"vac": [], "clo": [], "pub": set()}
        if code == "INFO-000" and rows:
            norm = normalize(rows)
            days = school_days(year, norm["vac"], norm["clo"])
            q = grade(norm, days)
            for p in norm["pub"]:
                pub_seen[p.isoformat()] += 1
        counts[q] += 1
        idx.append([sid, name, k1])
        if q != "none":
            cal[sid] = {"v": norm["vac"], "c": norm["clo"], "q": q, "d": days}
        if q != "ok":
            warns.append("%s %s (%s): %s, 수업일 %d일, 방학 %d개"
                         % (office, name, sid, q, days, len(norm["vac"])))
        if i % 100 == 0:
            print("    %d/%d" % (i, len(schools)))
        time.sleep(0.05)

    os.makedirs(os.path.join(out_dir, "idx"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "cal"), exist_ok=True)
    idx.sort(key=lambda x: x[1])
    with open(os.path.join(out_dir, "idx", office + ".json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out_dir, "cal", office + ".json"), "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, separators=(",", ":"))

    return office_name, len(schools), counts, warns, pub_seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--office", default="all", help="교육청 코드 또는 all")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--kind", default="all", help="초등학교|중학교|고등학교|all")
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()

    if not KEY:
        print("NEIS_KEY 환경변수가 비어 있습니다.", file=sys.stderr)
        return 1

    offices = OFFICES if a.office == "all" else [a.office.upper()]
    kinds = KINDS if a.kind == "all" else [a.kind]
    os.makedirs(a.out, exist_ok=True)

    print("수집 시작 — 교육청 %d곳 / %s / %d학년도\n" % (len(offices), ", ".join(kinds), a.year))
    started = time.time()

    index, total, all_counts, all_warns = [], 0, Counter(), []
    pub_all = Counter()
    for office in offices:
        name, n, counts, warns, pub = collect_office(office, kinds, a.year, a.out)
        if not n:
            print("  %s 건너뜀 (학교 없음)" % office)
            continue
        index.append({"code": office, "name": name, "count": n,
                      "ok": counts["ok"], "partial": counts["partial"], "none": counts["none"]})
        total += n
        all_counts.update(counts)
        all_warns.extend(warns)
        pub_all.update(pub)

    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"sy": a.year, "fetched": date.today().isoformat(),
                   "offices": index}, f, ensure_ascii=False, indent=1)

    rule = set()
    for y in (a.year, a.year + 1):
        rule |= {k.isoformat() for k in holidays(y)}
    suspects = [(k, n) for k, n in sorted(pub_all.items())
                if k not in rule and n >= max(20, total // 20)]

    L = ["수집 결과 — %d학년도 / %s" % (a.year, ", ".join(kinds)),
         "생성 %s / 교육청 %d곳 / 학교 %d개교 / %d분 소요"
         % (date.today().isoformat(), len(index), total, int((time.time() - started) / 60)),
         "", "전체 품질 분포"]
    for k in ("ok", "partial", "none"):
        L.append("  %-8s %6d개교  %3d%%" % (k, all_counts[k], all_counts[k] * 100 // max(1, total)))
    L += ["", "교육청별"]
    for o in index:
        L.append("  %-4s %-14s %5d개교   ok %3d%%" % (o["code"], o["name"], o["count"],
                                                     o["ok"] * 100 // max(1, o["count"])))
    if suspects:
        L += ["", "공휴일 규칙에 없는데 전국의 여러 학교가 표시한 날 (임시공휴일 의심)"]
        for k, n in suspects:
            L.append("  %s  %d개교" % (k, n))
    if all_warns:
        L += ["", "확인이 필요한 학교 (%d곳)" % len(all_warns)]
        L += ["  " + w for w in all_warns[:200]]
        if len(all_warns) > 200:
            L.append("  … 외 %d곳" % (len(all_warns) - 200))

    report = "\n".join(L)
    with open(os.path.join(a.out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
