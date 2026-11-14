const { makeApp, idle, ok, done } = require("./harness");

(async () => {
  const saved = [];
  const routes = {
    "/api/workflow/status": { progress: { state: "running", title: "현재 작업" }, subscriptions: [] },
    "/api/workflow/search": { results: [
      { platform: "naver", title_id: "123", title: "악당들의 후원자가 되었다", thumbnail_url: "https://image-comic.pstatic.net/cover.jpg", subscription: null },
      { platform: "kakao", title_id: "456", title: "악당 얼굴의 B급 모험가 주인공과 그 소꿉친구들의 아빠가 되다", thumbnail_url: "https://page-images.kakaoentcdn.com/cover.jpg", subscription: null },
    ] },
    "/api/workflow/preview": { available_count: 1, episodes: [
      { episode_no: 1, subtitle: "미리 볼 회차", available: true, owned: false },
      { episode_no: 2, subtitle: "잠긴 회차", available: false, owned: false },
    ] },
    "/api/workflow/subscribe": (opts) => { saved.push(JSON.parse(opts.body)); return { execution: "scheduled" }; },
  };
  const { w, calls, run } = makeApp(routes); await idle();
  const $ = (id) => w.document.getElementById(id);
  $("workflow-search-query").value = "악당들의";
  $("workflow-search-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true })); await idle();
  const cards = $("workflow-search-results").querySelectorAll("article");
  ok(cards.length === 2 && cards[0].querySelector("img").src === "https://image-comic.pstatic.net/cover.jpg", "네이버 통합 검색 카드가 제공된 표지를 렌더링");
  ok(cards[1].querySelector("img").getAttribute("src") === "/api/kakao-thumbnail/456", "카카오 표지는 기존 서버 표지 프록시 사용");
  ok(cards[1].querySelector(".workflow-result-title").textContent.includes("모험가") && cards[1].querySelector(".webtoon-card-meta").textContent === "카카오페이지", "긴 제목과 플랫폼은 독립 요소에 렌더링");
  for (const [index, url] of [[0, "https://comic.naver.com/webtoon/list?titleId=123"], [1, "https://page.kakao.com/content/456"]]) {
    const title = cards[index].querySelector(".workflow-result-title");
    ok(title.tagName === "A" && title.href === url && title.target === "_blank" && title.relList.contains("noopener"), `${index === 0 ? "네이버" : "카카오"} 제목은 공식 작품 페이지를 새 탭으로 여는 링크`);
    title.addEventListener("click", (event) => event.preventDefault(), { once: true });
    title.click(); await idle();
    ok(!$("workflow-preview-dialog").open && !calls.some((call) => call.url === "/api/workflow/preview") && saved.length === 0, "제목 클릭은 회차 미리보기·구독·다운로드를 실행하지 않음");
    ok(cards[index].querySelector(".workflow-read-button") && [...cards[index].querySelectorAll("button")].some((button) => button.textContent === "회차 미리보기"), "읽기와 회차 미리보기는 별도 버튼으로 유지");
  }
  [...cards[0].querySelectorAll("button")].find((button) => button.textContent === "회차 미리보기").click(); await idle();
  ok($("workflow-preview-dialog").open && $("workflow-preview-title").textContent.includes("후원자") && JSON.parse(calls.find((call) => call.url === "/api/workflow/preview").body).platform === "naver", "별도 회차 미리보기 버튼은 네이버 회차 목록을 앱 안에서 열기");
  $("workflow-preview-close").click();
  cards[1].querySelector(".workflow-preview-cover").click(); await idle();
  ok($("workflow-preview-dialog").open && $("workflow-preview-title").textContent.includes("모험가"), "표지를 누르면 현재 페이지에서 회차 미리보기 열림");
  ok($("workflow-preview-episodes").children.length === 2 && $("workflow-preview-episodes").textContent.includes("다운로드 가능") && $("workflow-preview-episodes").textContent.includes("현재 받을 수 없음"), "미리보기에서 무료·잠긴 회차를 구분");
  ok(!calls.some((call) => call.url.includes("/api/kakao-manual/analyze")) && saved.length === 0, "회차 미리보기는 다른 탭 이동이나 구독·다운로드를 실행하지 않음");
  $("workflow-preview-subscribe").click(); await idle();
  ok(!$("workflow-preview-dialog").open && $("workflow-subscribe-dialog").open, "미리보기에서 구독 범위 선택으로 연결");
  $("workflow-subscribe-submit").click(); await idle();
  ok(saved.length === 1 && saved[0].start_now === false && saved[0].scope.mode === "latest", "다운로드 중에도 구독만 저장 버튼은 전체 회차나 즉시 실행을 요청하지 않음");
  run('renderWorkflowStatus({queue:[{platform:"naver",title_id:"122",title:"첫 대기 작품",state:"queued"},{platform:"naver",title_id:"123",title:"대기 작품",state:"queued"}],subscriptions:[{title_id:"123",title:"대기 작품",platform:"naver",status:"queued",queue_position:2,scope:{mode:"selected",episode_nos:[1]}}]});');
  ok($("workflow-subscriptions").textContent.includes("다운로드 대기") && $("workflow-subscriptions").textContent.includes("대기열 2번째"), "진행 현황에서 대기 작품과 순서 표시");
  w.close(); done();
})().catch((error) => { console.error(error); process.exit(1); });
