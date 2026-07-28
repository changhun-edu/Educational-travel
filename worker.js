/**
 * 나이스 학사일정 중계 (Cloudflare Workers)
 *
 * 인증키를 서버에만 두고, 학교 하나의 학사일정을 앱이 쓰는 형태로 돌려준다.
 * 학부모 브라우저에는 키가 내려가지 않는다.
 *
 * 배포
 *   1. dash.cloudflare.com → Workers & Pages → Create → Worker
 *   2. 이 파일 내용을 붙여넣고 Deploy
 *   3. Settings → Variables and Secrets → Add
 *        이름 NEIS_KEY,  값 발급받은 인증키,  종류 Secret
 *   4. Settings → Variables → Add (일반 변수)
 *        이름 ALLOW_ORIGIN,  값 https://우리학교주소   (모두 허용은 *)
 *   5. 배포된 주소를 앱의 PROXY 값에 넣는다
 *
 * 호출
 *   GET /?sc=F10_7402220&sy=2026
 *   → {"sc":"F10_7402220","sy":2026,"v":[["2026-07-27","2026-08-20"]],"c":["2026-05-04"],"n":190}
 */

const CACHE_SECONDS = 21600;   // 6시간. 학사일정은 하루 한 번 적재된다.

export default {
  async fetch(request, env, ctx) {
    const origin = env.ALLOW_ORIGIN || "*";
    const cors = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Max-Age": "86400",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "GET") return json({ error: "method" }, 405, cors);

    const url = new URL(request.url);
    const sc = url.searchParams.get("sc") || "";
    const sy = url.searchParams.get("sy") || "";

    // 넘겨받는 값은 이 두 개뿐이고, 형식이 맞지 않으면 나이스를 부르지 않는다.
    const m = /^([A-Z]\d{2})_(\d{4,12})$/.exec(sc);
    if (!m || !/^20\d{2}$/.test(sy)) return json({ error: "bad request" }, 400, cors);
    if (!env.NEIS_KEY) return json({ error: "key not configured" }, 500, cors);

    const cache = caches.default;
    const cacheKey = new Request(url.origin + url.pathname + "?sc=" + sc + "&sy=" + sy);
    const hit = await cache.match(cacheKey);
    if (hit) return withCors(hit, cors);

    const api = new URL("https://open.neis.go.kr/hub/SchoolSchedule");
    api.searchParams.set("KEY", env.NEIS_KEY);
    api.searchParams.set("Type", "json");
    api.searchParams.set("pIndex", "1");
    api.searchParams.set("pSize", "1000");
    api.searchParams.set("ATPT_OFCDC_SC_CODE", m[1]);
    api.searchParams.set("SD_SCHUL_CODE", m[2]);
    api.searchParams.set("AA_FROM_YMD", sy + "0301");
    api.searchParams.set("AA_TO_YMD", String(+sy + 1) + "0228");

    let body;
    try {
      const r = await fetch(api.toString(), { cf: { cacheTtl: 3600 } });
      body = await r.json();
    } catch (e) {
      return json({ error: "upstream" }, 502, cors);
    }

    if (!body.SchoolSchedule) {
      const code = (body.RESULT || {}).CODE || "?";
      // 학사일정 미등록도 정상 응답으로 돌려준다. 앱이 정적 데이터를 유지하면 된다.
      return json({ sc, sy: +sy, code, v: [], c: [], n: 0 }, 200, cors);
    }

    const rows = body.SchoolSchedule[1].row || [];
    const out = normalize(rows);
    const res = json({ sc, sy: +sy, code: "INFO-000", ...out }, 200, cors, CACHE_SECONDS);
    ctx.waitUntil(cache.put(cacheKey, res.clone()));
    return res;
  },
};

/* 평일 휴업일을 뽑아 방학 구간과 재량휴업일로 나눈다. 수집기와 같은 규칙. */
function normalize(rows) {
  const off = new Set();
  for (const r of rows) {
    const nm = (r.SBTR_DD_SC_NM || "").trim();
    const y = r.AA_YMD || "";
    if (y.length !== 8) continue;
    if (nm.includes("공휴일")) continue;
    const byName = !nm && /방학|휴업|재량/.test(r.EVENT_NM || "");
    if (!nm.includes("휴업") && !byName) continue;
    const d = new Date(+y.slice(0, 4), +y.slice(4, 6) - 1, +y.slice(6, 8));
    const w = d.getDay();
    if (w === 0 || w === 6) continue;
    off.add(iso(d));
  }

  const dates = [...off].sort().map((s) => new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)));
  const runs = [];
  for (const d of dates) {
    const last = runs[runs.length - 1];
    if (last) {
      let p = add(last[last.length - 1], 1), ok = true;
      while (p < d) {
        const w = p.getDay();
        if (w !== 0 && w !== 6) { ok = false; break; }
        p = add(p, 1);
      }
      if (ok) { last.push(d); continue; }
    }
    runs.push([d]);
  }

  const v = [], c = [];
  for (const run of runs) {
    if (run.length >= 5) v.push([iso(run[0]), iso(run[run.length - 1])]);
    else for (const d of run) c.push(iso(d));
  }
  return { v, c, n: off.size };
}

const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
const add = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);

function json(obj, status, cors, maxAge) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": maxAge ? `public, max-age=${maxAge}` : "no-store",
      ...cors,
    },
  });
}
function withCors(res, cors) {
  const r = new Response(res.body, res);
  for (const k in cors) r.headers.set(k, cors[k]);
  return r;
}
