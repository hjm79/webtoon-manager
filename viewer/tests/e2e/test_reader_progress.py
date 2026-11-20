import httpx
from playwright.sync_api import expect

from conftest import make_chapter_zip


def test_reader_jump_then_scroll_and_idle_tab_close(page, live_server, library):
    for episode in range(1, 4):
        make_chapter_zip(str(library / "naver" / "Reader regression" / f"{episode:03}.zip"),
                         page_count=3, width=720, height=1300)
    assert httpx.post(f"{live_server}/api/rescan").status_code == 200
    series_id = httpx.get(f"{live_server}/api/series").json()[0]["id"]
    series_api = f"{live_server}/api/series/{series_id}"
    chapters = httpx.get(f"{series_api}/chapters").json()["chapters"]
    first, current, following = (chapter["id"] for chapter in chapters)
    reader = f"{live_server}/reader.html?series={series_id}&chapter="

    idle = page.context.new_page()
    idle.goto(reader + first)
    expect(idle.locator('img[data-image-state="loaded"]').first).to_be_visible()

    page.add_init_script('Object.defineProperty(window, "localStorage", { get() { throw new DOMException("denied", "SecurityError") } })')
    page.goto(reader + current)
    expect(page.locator('img[data-image-state="loaded"]').first).to_be_visible()
    with page.expect_response(lambda response: response.url == f"{series_api}/progress") as written:
        page.locator("#chapterBottom").click()
    assert written.value.request.post_data_json["completed"] is False
    assert written.value.request.post_data_json["reading_from_chapter_id"] == current
    assert written.value.request.post_data_json["read_chapter_ids"] == []
    assert httpx.get(f"{series_api}/continue").json()["page_index"] == 2

    stale_requests = []
    idle.on("request", lambda request: stale_requests.append(request) if request.url.endswith("/progress") else None)
    idle.close(run_before_unload=True)
    assert stale_requests == []
    assert httpx.get(f"{series_api}/continue").json()["chapter_id"] == current
    assert not any(chapter["has_read"] for chapter in httpx.get(f"{series_api}/chapters").json()["chapters"])

    page.bring_to_front()
    page.locator("#chapterTop").click()
    page.wait_for_function("window.scrollY < 150")
    final_image = page.locator(f'img[data-chapter="{current}"][data-page="2"]')
    expect(final_image).to_have_attribute("data-image-state", "loaded")
    page.mouse.move(600, 350)
    for _ in range(15):
        page.mouse.wheel(0, 310)
        page.wait_for_timeout(65)
        if page.evaluate("id => completedChapters.has(id)", arg=current):
            break
    page.wait_for_function("id => persistedCompletedChapters.has(id)", arg=current, timeout=5000)
    assert [item["has_read"] for item in httpx.get(f"{series_api}/chapters").json()["chapters"]] == [False, True, False]
    # If continuous reading entered the next chapter while finishing, its saved cursor is still there.
    assert httpx.get(f"{series_api}/continue").json()["chapter_id"] == following


def test_reader_completes_after_resuming_in_a_new_browser_context(browser, live_server, library):
    make_chapter_zip(str(library / "naver" / "Cross-session reading" / "001.zip"),
                     page_count=3, width=720, height=1300)
    assert httpx.post(f"{live_server}/api/rescan").status_code == 200
    series_id = httpx.get(f"{live_server}/api/series").json()[0]["id"]
    series_url = f"{live_server}/api/series/{series_id}"
    chapter_id = httpx.get(f"{series_url}/chapters").json()["chapters"][0]["id"]
    manifest_url = f"{live_server}/api/chapters/{chapter_id}/pages?start_reading=true"
    reader_url = f"{live_server}/reader.html?series={series_id}"

    first = browser.new_page(viewport={"width": 1280, "height": 720})
    try:
        first.goto(reader_url)
        expect(first.locator('img[data-image-state="loaded"]').first).to_be_visible()
        first.mouse.move(600, 350)
        for _ in range(6):
            previous = first.evaluate("window.scrollY")
            first.mouse.wheel(0, 270)
            first.wait_for_function("previous => window.scrollY > previous", arg=previous)
        assert first.evaluate("saveProgressNow()")
        saved = httpx.get(manifest_url).json()["reading_coverage"]
        assert saved["page_index"] >= 1
        assert not httpx.get(f"{series_url}/chapters").json()["chapters"][0]["has_read"]
    finally:
        first.context.close()

    resumed = browser.new_page(viewport={"width": 1280, "height": 720})
    try:
        with resumed.expect_response(lambda response: "/pages?dimensions=1" in response.url) as manifest:
            resumed.goto(reader_url)
        assert manifest.value.json()["reading_coverage"] == saved
        expect(resumed.locator('#pages:not(.positioning)')).to_be_visible()
        resumed.wait_for_function("window.scrollY > 900")
        resumed.mouse.move(600, 350)
        for _ in range(15):
            resumed.mouse.wheel(0, 260)
            resumed.wait_for_timeout(60)
            if resumed.evaluate("id => completedChapters.has(id)", arg=chapter_id):
                break
        resumed.wait_for_function("id => persistedCompletedChapters.has(id)", arg=chapter_id, timeout=5000)
        assert httpx.get(f"{series_url}/chapters").json()["chapters"][0]["has_read"]
        assert httpx.get(manifest_url).json()["reading_coverage"] is None
    finally:
        resumed.context.close()
