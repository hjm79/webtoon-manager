// 웹툰 전체목록 카드의 "분석" 버튼 — 누르면 수동 다운로드 탭으로 이동해 그 작품의 분석 화면을 연다(네이버: titleId, 카카오: 작품 번호).
// 자리: 뷰어 버튼이 **보이는** 카드는 뷰어 아이콘 옆(같은 줄), 뷰어 버튼이 없거나 숨겨진 카드는 마지막 텍스트 버튼(목록제외/제외됨으로) 아래에 같은 크기의 버튼으로.
// 그래서 전체목록의 모든 카드는 항상 3줄이다(구독/구독해제, 목록제외/제외됨으로, 뷰어 줄 또는 분석 줄). 다른 탭(제외됨/구독해제)에는 분석 버튼이 없다.
const { makeApp, idle, ok, done } = require("./harness");
const { naver, kakao, subscribed } = require("./factories");
const SUB = { status: "active", ever_subscribed: true };
(async () => {
  const analyzed = [];
  async function render({ serverUrl = "http://viewer", items, inViewer = () => true }) {
    const routes = {
      "/api/naver-list": () => items.filter((i) => i.platform !== "kakao"),
      "/api/kakao-list": () => ({ items: items.filter((i) => i.platform === "kakao"), version: "v1", refreshing: false, refreshed_at: "2026-09-30T01:00:00+00:00" }),
      "/api/settings/kakao-webtoons-enabled": { enabled: true }, "/api/settings/webtoon-server": { webtoon_server_url: serverUrl }, "/api/webtoons": () => [], "/api/kakao-webtoons": () => [],
      "/api/reader/status": (opts, url) => ({ status: !serverUrl ? "not_configured" : inViewer(decodeURIComponent((url.match(/title=([^&]+)/) || [])[1] || "")) ? "ready" : "not_downloaded", url: "http://viewer/x" }),
      "/api/manual-download/analyze": (opts, url) => { analyzed.push(["naver", new URL(url, "http://x").searchParams.get("title_id")]); return { title_id: "1", title: "분석한 작품", webtoon_type: "webtoon", is_finished: false, episodes: [], subscribed: false, thumbnail_url: "" }; },
      "/api/kakao-manual/analyze": (opts, url) => { analyzed.push(["kakao", new URL(url, "http://x").searchParams.get("series_id")]); return { series_id: 1, title: "분석한 카카오", episodes: [], tickets: {}, subscribed: false, stats: {}, plan: {} }; },
    };
    const app = makeApp(routes); await idle(); app.run(`kakaoWebtoonsEnabled = true; webtoonServerConfigured = ${serverUrl ? "true" : "false"};`);
    app.run("window.__p = loadNaverList(false);"); await app.w.__p; await idle();
    const card = (title) => [...app.w.document.querySelectorAll("#naver-list-grid > .webtoon-card")].find((c) => c.querySelector(".webtoon-card-title").textContent === title);
    const actions = (title) => card(title).querySelector(".webtoon-card-actions");
    const lines = (title) => [...actions(title).children].filter((el) => el.style.display !== "none");        // 화면에서 줄을 차지하는 것
    const analyzeBtn = (title) => actions(title).querySelector("[data-analyze]");
    return { ...app, card, actions, lines, analyzeBtn };
  }
  const ITEMS = [naver(1, "네이버 구독 중", SUB), naver(2, "네이버 미구독"), kakao(71000001, "카카오 구독 중", { ...SUB, platform: "kakao" }), kakao(71000002, "카카오 미구독", { platform: "kakao" })];
  const T = ["네이버 구독 중", "네이버 미구독", "카카오 구독 중", "카카오 미구독"];

  // Scenario 1: 뷰어 버튼이 보이는 구독 중 카드 — 분석 버튼이 뷰어 아이콘 바로 옆(같은 줄)에 작게. 줄 수는 3줄(텍스트 2 + 뷰어/분석 줄 1)
  let ctx = await render({ items: ITEMS });
  for (const t of ["네이버 구독 중", "카카오 구독 중"]) {
    const row = ctx.lines(t)[2], icon = ctx.actions(t).querySelector("[data-viewer-check-title]"), btn = ctx.analyzeBtn(t);
    ok(ctx.lines(t).length === 3 && row.contains(icon) && row.contains(btn) && icon.nextElementSibling === btn, `${t}: 뷰어 아이콘 옆에 분석 버튼, 3줄 (${ctx.lines(t).length}줄)`);
    ok(btn.textContent.trim() === "분석" && btn.classList.contains("analyze-inline"), `${t}: 작은(inline) 분석 버튼`);
  }
  // 구독 여부나 준비 상태와 관계없이 읽기·분석을 같은 마지막 줄에 남긴다.
  for (const t of ["네이버 미구독", "카카오 미구독"]) {
    const row = ctx.lines(t)[2], btn = ctx.analyzeBtn(t);
    ok(ctx.lines(t).length === 3 && row.contains(btn) && row.querySelector("[data-viewer-check-title]"), `${t}: 읽기와 분석 모두 표시`);
  }
  for (const options of [{ inViewer: () => false }, { serverUrl: "" }]) {
    ctx = await render({ items: ITEMS, ...options });
    ok(T.every((t) => ctx.lines(t).length === 3 && ctx.lines(t)[2].contains(ctx.analyzeBtn(t))), "미다운로드·미설정에도 분석 위치 유지");
  }

  // Scenario 5: 분석 버튼은 전체목록 카드에만 — 제외됨/구독해제 카드에는 없다
  ctx.run("subscriptionCache.excluded = [" + JSON.stringify(naver(9, "제외된 작품", { status: "excluded" })) + "]; renderSubscriptionTab('excluded');"); await idle();
  ok(!ctx.w.document.querySelector("#excluded-list [data-analyze]") && ctx.w.document.querySelectorAll("#excluded-list .webtoon-card").length === 1, "제외됨 카드에는 분석 버튼이 없다");

  // Scenario 6: CSS — jsdom은 레이아웃을 계산하지 않으므로 규칙이 실제로 들어 있는지 스타일 파일을 직접 본다(예전에 이 규칙이 파일에 안 들어갔는데 테스트가 통과한 적이 있다).
  // 뷰어 아이콘 + 분석 버튼은 한 줄(flex)이고, 분석 버튼이 아이콘을 뺀 나머지 폭을 채워서 줄 전체가 위의 버튼들과 같은 폭이다. 아이콘은 가운데 정렬(align-self)이 아니라 줄 안에서 정렬한다.
  const css = require("fs").readFileSync(require("path").join(__dirname, "../../app/static/style.css"), "utf8");
  const block = (selector) => { const m = css.match(new RegExp(selector.replace(/[.]/g, "\\.") + "\\s*\\{([^}]*)\\}")); return m ? m[1] : ""; };
  ok(/display:\s*flex/.test(block(".card-viewer-row")) && /align-items:\s*center/.test(block(".card-viewer-row")), "뷰어 줄은 flex 한 줄: " + block(".card-viewer-row").trim());
  ok(/flex:\s*1\s+1\s+auto/.test(block(".webtoon-card-actions .analyze-inline")), "분석 버튼은 남은 폭을 채운다: " + block(".webtoon-card-actions .analyze-inline").trim());
  ok(/align-self:\s*auto/.test(block(".card-viewer-row .icon-btn")), "줄 안의 아이콘은 가운데 정렬 해제: " + block(".card-viewer-row .icon-btn").trim());
  done();
})();
