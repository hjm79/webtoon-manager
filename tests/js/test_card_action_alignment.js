const { makeApp, idle, ok, done } = require("./harness");
const { naver, kakao } = require("./factories");
(async () => {
  const items = [naver(1, "네이버 구독", { status: "active" }), naver(2, "네이버 미등록"), kakao(71000001, "카카오 구독", { status: "active" }), kakao(71000002, "카카오 미등록")];
  for (const status of ["ready", "not_downloaded", "not_configured", "error"]) {
    const routes = {
      "/api/naver-list": items.slice(0, 2), "/api/kakao-list": { items: items.slice(2), version: "v1", refreshing: false },
      "/api/settings/kakao-webtoons-enabled": { enabled: true }, "/api/settings/webtoon-server": { webtoon_server_url: status === "not_configured" ? "" : "http://viewer" },
      "/api/reader/status": { status, url: status === "ready" ? "http://viewer/r" : null },
    };
    const { w, run } = makeApp(routes); await idle(); run("kakaoWebtoonsEnabled = true; window.__p = loadNaverList(false);"); await w.__p; await idle();
    const cards = [...w.document.querySelectorAll("#naver-list-grid > .webtoon-card")];
    ok(cards.length === 4 && cards.every((c) => c.querySelector(".webtoon-card-actions").children.length === 3), `${status}: 모든 구독·미등록 카드가 같은 3줄`);
    ok(cards.every((c) => { const row = c.querySelector(".card-viewer-row"); return row && row.querySelector("[data-viewer-check-title]") && row.querySelector("[data-analyze]"); }), `${status}: 모든 카드에 읽기와 분석을 함께 표시`);
    const filter = w.document.getElementById("naver-list-filter-status"); filter.value = "not-active"; filter.dispatchEvent(new w.Event("change")); await idle();
    ok([...w.document.querySelectorAll("#naver-list-grid > .webtoon-card")].every((c) => c.querySelector(".card-viewer-row")), `${status}: 필터를 바꿔도 읽기 줄 유지`);
    w.close();
  }
  done();
})();
