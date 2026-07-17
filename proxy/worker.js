// AutoMech crawl relay — Cloudflare Worker.
// Реле для форум-краула: гоняет GET через ЧИСТЫЙ Cloudflare-edge egress с
// браузерными заголовками. Обходит IP-репутационные и базовые бот-фильтры
// Cloudflare-форумов. НЕ решает активный JS-челлендж (это только настоящий
// браузер) — но у многих форумов блок именно по IP/UA, и реле его снимает.
// Бонус: единый чистый egress для ВСЕХ источников (не только заблокированных).
//
// Вызов:  GET https://<worker>.<sub>.workers.dev/?url=<ENCODED_TARGET>&k=<SECRET>
// Секрет: Settings -> Variables and Secrets -> PROXY_SECRET
//         (то же значение кладём в CRAWL_PROXY_KEY у краулера).

const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  "Accept":
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif," +
    "image/webp,image/apng,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
  "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not;A=Brand";v="24"',
  "sec-ch-ua-mobile": "?0",
  "sec-ch-ua-platform": '"Windows"',
  "Sec-Fetch-Dest": "document",
  "Sec-Fetch-Mode": "navigate",
  "Sec-Fetch-Site": "none",
  "Sec-Fetch-User": "?1",
  "Upgrade-Insecure-Requests": "1",
};

export default {
  async fetch(request, env) {
    const u = new URL(request.url);
    const target = u.searchParams.get("url");
    if (!target)
      return new Response("usage: /?url=<encoded>&k=<secret>", { status: 400 });

    // защита от «открытого прокси»: без верного ключа — отказ
    if (env.PROXY_SECRET && u.searchParams.get("k") !== env.PROXY_SECRET)
      return new Response("forbidden", { status: 403 });

    let tgt;
    try {
      tgt = new URL(target);
    } catch {
      return new Response("bad url", { status: 400 });
    }
    if (tgt.protocol !== "https:" && tgt.protocol !== "http:")
      return new Response("bad scheme", { status: 400 });

    const headers = { ...BROWSER_HEADERS, Referer: tgt.origin + "/" };
    let resp;
    try {
      resp = await fetch(tgt.toString(), {
        method: "GET",
        headers,
        redirect: "follow",
        cf: { cacheTtl: 0, cacheEverything: false },
      });
    } catch (e) {
      return new Response("relay fetch error: " + e, { status: 502 });
    }

    // тело источника как есть + его статус (краулер ждёт 200 и HTML)
    const out = new Response(resp.body, { status: resp.status });
    out.headers.set(
      "content-type",
      resp.headers.get("content-type") || "text/html; charset=utf-8"
    );
    out.headers.set("x-relay-status", String(resp.status));
    return out;
  },
};
