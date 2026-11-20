"""정적 파일 제공 — HTML과 같은 빌드의 JS·CSS를 불러오고, 로드 순서를 지킨다."""
import asyncio, re
import httpx
from app import db
db.get_connection()
import app.main as m

async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://test") as c:
        index = await c.get("/", headers={"If-None-Match": '"old-index"'})
        assert index.status_code == 200 and index.headers["cache-control"] == "no-store"
        assert "etag" not in index.headers
        html = index.text
        alternate_index = await c.get("/index.html")
        assert alternate_index.text == html and alternate_index.headers["cache-control"] == "no-store"
        srcs = re.findall(r'<script src="(/[^"]+)"></script>', html)
        assert len(srcs) >= 10 and srcs == sorted(srcs), srcs
        assert srcs[0].endswith("00-core.js") and srcs[-1].endswith("99-main.js")
        stylesheet = re.search(r'<link rel="stylesheet" href="(/ui/([0-9a-f]+)/style\.css)"', html)
        assert stylesheet, "CSS 주소에도 빌드 버전이 있어야 한다"
        css_url, revision = stylesheet.groups()
        prefix = f"/ui/{revision}"
        for src in srcs:
            assert src.startswith(f"{prefix}/js/"), src
            response = await c.get(src)
            assert response.status_code == 200 and len(response.text) > 50, src
            assert response.headers["cache-control"] == "no-cache", src
        for url, marker in ((css_url, b'.library-grid[data-view="list"]'),
                            (f"{prefix}/js/56-library.js", b"setLibraryView")):
            response = await c.get(url)
            legacy_response = await c.get(url.removeprefix(prefix))
            assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
            assert marker in response.content and response.content == legacy_response.content
            assert legacy_response.headers["cache-control"] == "no-cache"
        wrong_revision = ("1" if revision.startswith("0") else "0") + revision[1:]
        for suffix in ("style.css", "js/56-library.js"):
            assert (await c.get(f"/ui/{wrong_revision}/{suffix}")).status_code == 404
        assert (await c.get("/app.js")).status_code == 404
    print("정적 파일 OK:", len(srcs), "개 스크립트")
asyncio.run(main())
print("\n전부 통과")
