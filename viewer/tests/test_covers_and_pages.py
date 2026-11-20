"""
커버 생성/캐싱, 회차 페이지 서빙 관련 회귀 테스트.
"""

import base64
import time
import zipfile

from conftest import make_chapter_zip


def test_cover_is_generated_from_first_chapter_first_page(client, library):
    """GIVEN cover.jpg가 따로 없는 시리즈가 있을 때"""
    make_chapter_zip(str(library / "naver" / "테스트웹툰" / "001 1화.zip"))
    client.post("/api/rescan")
    sid = client.get("/api/series").json()[0]["id"]

    """WHEN 커버를 요청하면"""
    r = client.get(f"/api/series/{sid}/cover")

    """THEN 1화 첫 페이지로 만든 이미지가 응답된다"""
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_cover_is_precomputed_right_after_rescan(client, library):
    """GIVEN 새로 스캔된 시리즈가 있을 때"""
    make_chapter_zip(str(library / "naver" / "테스트웹툰" / "001 1화.zip"))
    client.post("/api/rescan")

    """WHEN 스캔 직후(사전생성이 끝날 시간을 잠깐 기다린 뒤) 서버 내부 캐시를 보면"""
    time.sleep(0.3)
    from app import covers
    sid = client.get("/api/series").json()[0]["id"]

    """THEN 사용자가 요청하기 전에 이미 캐시에 들어가 있다(첫 로딩 지연 방지)"""
    assert sid in covers._cache


def test_chapter_page_count_matches_actual_image_count(client, library):
    """GIVEN 5페이지짜리 회차가 있을 때"""
    make_chapter_zip(str(library / "naver" / "테스트웹툰" / "001 1화.zip"), page_count=5)
    client.post("/api/rescan")
    sid = client.get("/api/series").json()[0]["id"]
    chapter_id = client.get(f"/api/series/{sid}/chapters").json()["chapters"][0]["id"]

    """WHEN 페이지 수를 조회하면"""
    r = client.get(f"/api/chapters/{chapter_id}/pages")

    """THEN 정확히 5로 나온다"""
    assert r.json()["page_count"] == 5


def test_individual_page_is_served_as_image(client, library):
    """GIVEN 회차가 있을 때"""
    make_chapter_zip(str(library / "naver" / "테스트웹툰" / "001 1화.zip"), page_count=3)
    client.post("/api/rescan")
    sid = client.get("/api/series").json()[0]["id"]
    chapter_id = client.get(f"/api/series/{sid}/chapters").json()["chapters"][0]["id"]

    """WHEN 두 번째 페이지를 요청하면"""
    r = client.get(f"/api/chapters/{chapter_id}/pages/1")

    """THEN 이미지가 정상적으로 응답된다"""
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_avif_page_is_indexed_and_preserved_when_extracted(client, library):
    image = base64.b64decode(
        "AAAAIGZ0eXBhdmlmAAAAAGF2aWZtaWYxbWlhZk1BMUIAAAD5bWV0YQAAAAAAAAAvaGRscgAAAAAAAAAAcGljdAAAAAAAAAAAAAAAAFBpY3R1cmVIYW5kbGVyAAAAAA5waXRtAAAAAAAB"
        "AAAAHmlsb2MAAAAARAAAAQABAAAAAQAAASEAAAAiAAAAKGlpbmYAAAAAAAEAAAAaaW5mZQIAAAAAAQAAYXYwMUNvbG9yAAAAAGppcHJwAAAAS2lwY28AAAAUaXNwZQAAAAAAAAAIAAAACAAA"
        "ABBwaXhpAAAAAAMICAgAAAAMYXYxQ4EADAAAAAATY29scm5jbHgAAgACAAIAAAAAF2lwbWEAAAAAAAAAAQABBAECgwQAAAAqbWRhdAoJAAAAARfjV8gCMhUQAJeAEECCAAAQAPrtzQWq78Jh3VA="
    )
    series = library / "kakao" / "AVIF"
    series.mkdir(parents=True)
    with zipfile.ZipFile(series / "001.zip", "w") as archive:
        archive.writestr("001.avif", image)
    assert client.post("/api/rescan").status_code == 200
    sid = client.get("/api/series").json()[0]["id"]
    chapter = client.get(f"/api/series/{sid}/chapters").json()["chapters"][0]["id"]
    assert client.get(f"/api/chapters/{chapter}/pages?dimensions=1").json()["page_count"] == 1
    response = client.get(f"/api/chapters/{chapter}/pages/0")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/avif"
    assert response.content == image


def test_cover_and_page_responses_revalidate_but_series_list_is_not_cached(client, library):
    """GIVEN 시리즈가 있을 때"""
    make_chapter_zip(str(library / "naver" / "테스트웹툰" / "001 1화.zip"))
    client.post("/api/rescan")
    sid = client.get("/api/series").json()[0]["id"]

    """WHEN 커버, 회차 이미지와 목록 API의 캐시 헤더를 비교하면"""
    chapter_id = client.get(f"/api/series/{sid}/chapters").json()["chapters"][0]["id"]
    cover_resp = client.get(f"/api/series/{sid}/cover")
    page_resp = client.get(f"/api/chapters/{chapter_id}/pages/0")
    list_resp = client.get("/api/series")

    assert cover_resp.headers["cache-control"] == "private, no-cache"
    assert page_resp.headers["cache-control"] == "private, no-cache"
    assert list_resp.headers["cache-control"] == "no-store"


def test_unchanged_page_returns_304_without_decoding_and_replaced_zip_has_new_tag(client, library, monkeypatch):
    import os
    from app import services

    archive = library / "naver" / "Conditional page" / "001.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as book:
        book.writestr("0001.jpg", b"first image")
    assert client.post("/api/rescan").status_code == 200
    series_id = client.get("/api/series").json()[0]["id"]
    chapter_id = client.get(f"/api/series/{series_id}/chapters").json()["chapters"][0]["id"]
    page = f"/api/chapters/{chapter_id}/pages/0"
    reads = []
    actual_read = services.read_chapter_page_bytes

    def count_read(path, index):
        reads.append(index)
        return actual_read(path, index)

    monkeypatch.setattr(services, "read_chapter_page_bytes", count_read)
    first = client.get(page)
    assert first.content == b"first image"
    tag = first.headers["etag"]
    cached = client.get(page, headers={"If-None-Match": f'"other", W/{tag}'})
    assert cached.status_code == 304 and cached.content == b""
    assert cached.headers["cache-control"] == "private, no-cache"
    assert cached.headers["etag"] == tag
    assert reads == [0]
    assert client.get(page[:-1] + "9", headers={"If-None-Match": "*"}).status_code == 404

    before = archive.stat()
    replacement = archive.with_name("replacement.tmp")
    with zipfile.ZipFile(replacement, "w") as book:
        book.writestr("0002.jpg", b"other image")
    assert replacement.stat().st_size == before.st_size
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(archive)
    changed = client.get(page, headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.content == b"other image"
    assert changed.headers["etag"] != tag
    assert reads == [0, 0]
