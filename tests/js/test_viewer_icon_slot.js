const { makeApp, idle, ok, done } = require("./harness");
const { naver, kakao, subscribed } = require("./factories");
(async () => {
  const routes = {
    "/api/naver-list": [subscribed(naver)(1, "준비된 작품"), subscribed(naver)(2, "아직 없는 작품")],
    "/api/kakao-list": { items: [subscribed(kakao)(71000001, "미설정 작품")], version: "v1", refreshing: false },
    "/api/settings/kakao-webtoons-enabled": { enabled: true }, "/api/settings/webtoon-server": { webtoon_server_url: "http://viewer" },
    "/api/reader/status": (opts, url) => {
      const title = new URL(url, "http://x").searchParams.get("title");
      return title === "준비된 작품" ? { status: "ready", url: "http://viewer/reader.html?series=1" }
        : { status: title === "미설정 작품" ? "not_configured" : "not_downloaded", url: null };
    },
  };
  const { w, run } = makeApp(routes); await idle();
  run("kakaoWebtoonsEnabled = true; window.__p = loadNaverList(false);"); await w.__p; await idle();
  const card = (title) => [...w.document.querySelectorAll("#naver-list-grid .webtoon-card")].find((c) => c.querySelector(".webtoon-card-title").textContent === title);
  const reader = (title) => card(title).querySelector("[data-viewer-check-title]");
  ok(reader("준비된 작품").textContent === "읽기", "준비된 작품에 텍스트 읽기 버튼");
  ok(reader("아직 없는 작품").textContent.includes("아직 받은 회차 없음"), "미다운로드 이유를 버튼에 표시");
  ok(reader("미설정 작품").textContent.includes("뷰어 미설정"), "미설정 이유를 버튼에 표시");
  for (const title of ["준비된 작품", "아직 없는 작품", "미설정 작품"]) {
    const button = reader(title);
    ok(button.style.visibility !== "hidden" && button.style.display !== "none" && !button.hasAttribute("aria-hidden") && !button.disabled, `${title}: 읽기 진입을 숨기지 않음`);
    const actions = card(title).querySelector(".webtoon-card-actions");
    ok(actions.children.length === 3 && actions.lastElementChild.querySelector("[data-analyze]"), `${title}: 읽기·분석을 같은 마지막 줄에 배치`);
  }
  done();
})();
