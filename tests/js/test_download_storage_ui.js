const { makeApp, idle, waitFor, ok, done } = require("./harness");
(async () => {
  const storage = (filesystem = "disk-a", available = 40 * 1024 ** 3) => ({ status: "ok", filesystem_id: filesystem, total_bytes: 100 * 1024 ** 3, used_bytes: 60 * 1024 ** 3, available_bytes: available, read_only: false });
  let response = storage(), slow = [];
  const roots = { base: "/download", host_path: "/mnt/download", naver: { path: "/download/naver", effective: "/download/naver", effective_host: "/mnt/download/naver", storage: storage() }, kakao: { path: "", effective: "/download/naver", effective_host: "/mnt/download/naver", storage: storage() } };
  const { w, run, calls } = makeApp({
    "/api/settings/download-roots": roots,
    "/api/settings/download-storage": (opts, url) => {
      const path = new URL(url, "http://test").searchParams.get("path");
      if (path === "slow") return new Promise((resolve) => slow.push(resolve));
      return { storage: path === "other" ? storage("disk-b", 10 * 1024 ** 3) : response };
    },
    "/api/archive/folders": { folders: [{ name: "other", path: "other" }] },
  });
  await idle();
  const $ = (id) => w.document.getElementById(id);
  const button = (id, text) => Array.from($(id).querySelectorAll("button")).find((b) => b.textContent === text);
  run("kakaoWebtoonsEnabled = true; window.__p = loadDownloadRoots();"); await w.__p; await idle();
  ok(/남음 40 GiB \/ 전체 100 GiB/.test($("naver-download-storage").textContent), "실제 파일시스템 여유·전체 공간을 단위와 함께 표시");
  ok($("naver-download-storage").querySelector("progress").value === 60 * 1024 ** 3, "사용량 막대와 접근성 이름을 표시");
  ok(!$("download-storage-shared").classList.contains("hidden") && /공유/.test($("download-storage-shared").textContent), "동일 디스크 공간을 두 폴더가 공유함을 안내");
  const noWrites = () => calls.every((c) => c.method !== "POST");

  response = storage("disk-a", 0);
  $("btn-download-storage-refresh").click(); await idle();
  ok(/남음 0 B/.test($("naver-download-storage").textContent), "남은 용량 0도 정확히 표시");
  ok(noWrites(), "용량 새로고침은 저장 요청을 하지 않음");

  button("naver-download-root-picker", "폴더 선택").click(); await idle();
  button("naver-download-root-picker", "📁 other").click(); await idle();
  button("naver-download-root-picker", "이 폴더 선택").click(); await idle();
  ok(/남음 10 GiB/.test($("naver-download-storage").textContent) && /남음 10 GiB/.test($("kakao-download-storage").textContent), "저장 전 네이버 폴더 변경이 상속 카카오 용량에도 반영됨");
  ok($("kakao-download-root-picker").textContent.includes("/mnt/download/other"), "상속 카카오 경로 표시도 새 draft를 따라감");
  ok(calls.some((c) => c.url.includes("download-storage?") && new URL(c.url, "http://test").searchParams.get("path") === "other"), "draft 조회는 base 상대경로만 전달");
  ok(noWrites(), "폴더 선택만으로 저장값을 바꾸지 않음");

  response = { status: "permission_denied", available_bytes: null };
  $("btn-download-storage-refresh").click(); await idle();
  // 'other' fixture is a known separate volume; return to the saved/default path for error responses.
  button("naver-download-root-picker", "기본으로").click(); await idle();
  ok(/권한/.test($("naver-download-storage").textContent) && !$("naver-download-storage").querySelector("progress"), "권한 실패 시 다른 디스크 수치나 사용 막대를 표시하지 않음");
  response = { status: "not_mounted" };
  $("btn-download-storage-refresh").click(); await idle();
  ok(/연결되지 않았/.test($("naver-download-storage").textContent), "bind 마운트 손실을 명시함");
  response = { ...storage(), read_only: true };
  $("btn-download-storage-refresh").click(); await idle();
  ok(/읽기 전용/.test($("naver-download-storage").textContent), "읽기 전용 상태를 표시하되 설정은 변경하지 않음");

  run("downloadRootDraft.naver = '/download/slow'; renderDownloadRootPicker('naver');");
  await waitFor(() => slow.length === 2, "두 플랫폼의 느린 draft 조회");
  run("downloadRootDraft.naver = '/download/other'; renderDownloadRootPicker('naver');");
  await waitFor(() => /남음 10 GiB/.test($("naver-download-storage").textContent), "최신 draft 용량");
  slow.forEach((resolve) => resolve({ storage: storage("stale-disk", 99 * 1024 ** 3) })); await idle();
  ok(/남음 10 GiB/.test($("naver-download-storage").textContent) && /남음 10 GiB/.test($("kakao-download-storage").textContent), "늦게 도착한 이전 경로 응답이 최신 용량을 덮지 않음");
  ok(noWrites(), "모든 조회·오류·기본 복귀가 원래 설정을 저장하지 않음");
  w.close();

  const external = { base: "/download", host_path: "", naver: { path: "/separate/naver", effective: "/separate/naver", storage: storage("n") }, kakao: { path: "/separate/kakao", effective: "/separate/kakao", storage: storage("k") } };
  const other = makeApp({ "/api/settings/download-roots": external, "/api/settings/download-storage": (opts, url) => ({ storage: storage(new URL(url, "http://test").searchParams.get("kind") === "naver" ? "n" : "k") }) });
  await idle(); other.run("kakaoWebtoonsEnabled = true; loadDownloadRoots();"); await idle();
  const doc = other.w.document;
  ok(doc.getElementById("download-storage-shared").classList.contains("hidden"), "서로 다른 디스크는 같은 공간으로 안내하지 않음");
  Array.from(doc.querySelectorAll("#kakao-download-root-picker button")).find((b) => b.textContent === "네이버와 같게").click(); await idle();
  const query = new URL(other.calls.filter((c) => c.url.includes("download-storage?")).at(-1).url, "http://test").searchParams;
  ok(query.get("kind") === "naver" && !query.has("path"), "base 밖 기존 네이버 경로 상속은 저장된 kind로만 안전하게 조회");
  ok(!doc.getElementById("download-storage-shared").classList.contains("hidden"), "외부 bind 상속 뒤 동일 디스크 안내 갱신");
  other.w.close();
  done();
})();
