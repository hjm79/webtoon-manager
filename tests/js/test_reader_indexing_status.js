const { makeApp, idle, ok, done } = require("./harness");
const { naver, kakao, subscribed } = require("./factories");
(async () => {
  const states = new Map([
    ["naver:같은 제목", { status: "indexing", downloaded_count: 2, url: null, message: "받은 회차 2개를 서재에 반영하고 있습니다. 잠시 후 다시 눌러주세요." }],
    ["kakao:같은 제목", { status: "ready", url: "/viewer/reader.html?series=kakao" }],
    ["naver:빈 작품", { status: "not_downloaded", downloaded_count: 0, url: null }],
    ["kakao:반영 중인 카카오", { status: "indexing", downloaded_count: 1, url: null }],
    ["naver:파일 보유 응답", { status: "not_downloaded", downloaded_count: 3, url: null }],
  ]);
  const { w, run, calls } = makeApp({
    "/api/naver-list": [subscribed(naver)(1, "같은 제목"), subscribed(naver)(2, "빈 작품"), subscribed(naver)(3, "파일 보유 응답")],
    "/api/kakao-list": { items: [subscribed(kakao)(71000001, "같은 제목"), subscribed(kakao)(71000002, "반영 중인 카카오")], version: "v1", refreshing: false },
    "/api/settings/kakao-webtoons-enabled": { enabled: true },
    "/api/settings/webtoon-server": { webtoon_server_url: "http://viewer" },
    "/api/reader/status": (_opts, url) => {
      const query = new URL(url, "http://x").searchParams;
      return states.get(`${query.get("platform")}:${query.get("title")}`) || { status: "not_downloaded", downloaded_count: 0 };
    },
  });
  await idle();
  run("kakaoWebtoonsEnabled = true; window.__list = loadNaverList(false);"); await w.__list; await idle();
  const $ = (id) => w.document.getElementById(id);
  const read = (title, platform) => Array.from($("naver-list-grid").querySelectorAll("[data-viewer-check-title]")).find((button) => button.dataset.viewerCheckTitle === title && button.dataset.viewerCheckPlatform === platform);
  const pending = read("같은 제목", "naver"), sameKakao = read("같은 제목", "kakao"), zero = read("빈 작품", "naver"), pendingKakao = read("반영 중인 카카오", "kakao");
  ok(pending.textContent === "읽기 · 서재 반영 대기" && pending.title.includes("받은 회차가 있으며 뷰어가 갱신되면 읽을 수 있습니다") && pending.title.includes("2개"), "파일이 있는 네이버 작품은 미다운로드가 아닌 서재 반영 대기와 받은 수를 안내");
  ok(pendingKakao.textContent === "읽기 · 서재 반영 대기" && !pendingKakao.disabled && !pending.disabled, "카카오도 같은 반영 대기 안내를 사용하고 읽기 재시도를 막지 않음");
  ok(zero.textContent === "읽기 · 아직 받은 회차 없음" && sameKakao.textContent === "읽기", "실제 0개와 준비 완료를 구분하고 같은 제목의 플랫폼 상태를 분리");
  ok(read("파일 보유 응답", "naver").dataset.readerStatus === "indexing", "미다운로드 응답이어도 받은 회차 수가 양수이면 반영 대기로 처리");

  pending.click(); await idle();
  ok($("workflow-reader-dialog").open && $("workflow-reader-status").textContent.includes("뷰어가 갱신되면 읽을 수 있습니다") && !$("workflow-reader-frame").getAttribute("src"), "반영 대기 작품의 읽기는 안내를 열고 빈 리더 iframe을 열지 않음");
  run("closeEmbeddedReader(true);");
  states.set("naver:같은 제목", { status: "ready", url: "/viewer/reader.html?series=naver" });
  run('window.__duplicate = makeWorkflowReadButton("같은 제목", "naver"); document.getElementById("workflow-subscriptions").appendChild(window.__duplicate);');
  pending.click(); await idle();
  ok(pending.textContent === "읽기" && w.__duplicate.textContent === "읽기" && pending.dataset.readerStatus === "ready" && sameKakao.dataset.readerStatus === "ready", "읽기 재시도는 새 API 결과로 같은 작품의 모든 버튼을 갱신하고 플랫폼을 혼동하지 않음");
  ok($("workflow-reader-frame").getAttribute("src")?.includes("series=naver"), "서재 반영 완료 후 실제 읽기 경로로 진입");
  run("closeEmbeddedReader(true);");

  states.set("naver:같은 제목", { status: "indexing", downloaded_count: 2 });
  run("window.__probe = pruneMissingViewerIcons();"); await w.__probe; await idle();
  states.set("naver:같은 제목", { status: "ready", url: "/viewer/reader.html?series=naver" });
  states.set("kakao:반영 중인 카카오", { status: "ready", url: "/viewer/reader.html?series=pending-kakao" });
  const probesBefore = calls.filter((call) => call.url.startsWith("/api/reader/status"));
  run("renderNaverList();"); await idle();
  const refreshedProbes = calls.filter((call) => call.url.startsWith("/api/reader/status")).slice(probesBefore.length);
  ok(read("같은 제목", "naver") === pending && read("반영 중인 카카오", "kakao") === pendingKakao && pending.textContent === "읽기" && pendingKakao.textContent === "읽기", "카드를 다시 만들지 않는 목록 갱신도 네이버·카카오의 반영 대기 상태를 재조회해 복구");
  ok(refreshedProbes.length > 0 && refreshedProbes.every((call) => new URL(call.url, "http://x").searchParams.get("title") !== "빈 작품"), "변경 없는 카드 갱신에서는 반영 대기 작품만 재조회");

  states.set("naver:보관 작품", { status: "indexing", downloaded_count: 1 });
  run('subscriptionCache.unsubscribed = [{platform:"naver",title_id:"20",title:"보관 작품",weekdays:["mon","tue","wed","thu","fri","sat","sun"],status:"unsubscribed",ever_subscribed:true}]; renderSubscriptionTab("unsubscribed");'); await idle();
  const archived = $("unsubscribed-list").querySelector("[data-viewer-check-title]");
  ok(archived.dataset.readerStatus === "indexing", "구독 해제 목록의 받은 파일도 서재 반영 대기로 표시");
  states.set("naver:보관 작품", { status: "ready", url: "/viewer/reader.html?series=archived" });
  run('renderSubscriptionTab("unsubscribed");'); await idle();
  ok($("unsubscribed-list").querySelector("[data-viewer-check-title]") === archived && archived.textContent === "읽기", "구독 해제 목록도 같은 카드의 반영 대기 상태를 다시 확인");

  run('renderWorkflowStatus({progress:{state:"deferred",title:"중단 작품",completed:1,total:3,error:"구독 상태가 해제로 바뀌어 다음 회차를 받지 않았습니다."},subscriptions:[{platform:"naver",title_id:"50",title:"중단 작품",status:"deferred",downloaded_count:1,scope:{mode:"all"}}]});');
  ok($("workflow-state").textContent === "구독 해제로 중단" && $("workflow-progress-detail").textContent.includes("다음 회차를 받지 않았습니다"), "구독 해제로 보류된 다운로드는 완료로 표시하지 않고 정확한 중단 사유를 표시");
  ok($("workflow-subscriptions").textContent.includes("중단 작품 · 구독 해제로 중단") && !$("workflow-subscriptions").textContent.includes("중단 작품 · 완료"), "작품별 현황도 중단과 성공을 구분");
  ok(!calls.some((call) => call.method !== "GET"), "읽기 상태 확인과 목록 갱신은 구독·파일·설정을 변경하는 요청을 보내지 않음");
  done();
})();
