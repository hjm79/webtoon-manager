"""Catalog renumbering, archive receipts and collision-safe ZIP publication."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from app import db, kakao_records, repository, subscription_scope
from app.kakao_page_download import models, runner
from app.repository.backup import validate_backup
from fakes import image_bytes


class Provider:
    def __init__(self, ordered_products):
        self.episodes = [models.Episode(product, f"회차 {product}", number, f"회차 {product}", True, True, 1, False)
                         for number, product in enumerate(ordered_products, start=1)]
        self.requests = []

    async def list_episodes(self, series_id):
        return {"title": "카카오 회차 검증"}, self.episodes

    async def viewer_image_urls(self, series_id, product_id):
        self.requests.append(product_id)
        return ["https://example.test/image"]

    async def download_image(self, url):
        return image_bytes("JPEG"), "image/jpeg"

    async def fetch_about(self, series_id):
        return None


def preview(provider):
    return [{"episode_no": episode.number, "product_id": episode.product_id, "available": True, "owned": False}
            for episode in provider.episodes]


async def main():
    db.get_connection()
    series_id = 90123481
    repository.upsert_new_kakao_webtoon(series_id, "카카오 회차 검증")
    repository.set_kakao_downloaded_numbers(series_id, [3])
    original = Provider([101, 102, 103])
    changed = Provider([100, 101, 102, 103, 104])
    subscription_scope.set_choice("kakao", series_id, "selected", preview(original), episode_nos=[2])
    with TemporaryDirectory() as tmp:
        folder = models.series_folder(tmp, "카카오 회차 검증")
        folder.mkdir()
        old = folder / models.episode_zip_name(3, "회차 103")
        with ZipFile(old, "w") as archive:
            archive.comment = models.zip_identity_comment(series_id, 103)
            archive.writestr("001.jpg", image_bytes("JPEG"))
        old_bytes = old.read_bytes()

        new_episode = changed.episodes[2]
        canonical = folder / models.episode_zip_name(3, new_episode.subtitle)
        with ZipFile(canonical, "w") as legacy:
            legacy.writestr("001.jpg", image_bytes("JPEG"))
        legacy_bytes = canonical.read_bytes()

        # The numbered history and another product's ZIP must not hide the selected product.
        plan = runner._plan_for_run(series_id, {"title": folder.name}, changed.episodes, models.scan_existing_files(folder))
        assert [episode.product_id for episode in plan.to_download] == [102]
        assert {row.episode.product_id for row in plan.rows if row.downloaded} == {103}

        copied = await runner.download_episode(changed, series_id, new_episode, folder)
        assert copied is not None and copied != canonical
        assert models.read_zip_identity(copied) == (series_id, 102)
        assert canonical.read_bytes() == legacy_bytes and old.read_bytes() == old_bytes
        assert await runner.download_episode(changed, series_id, new_episode, folder) == copied
        assert len(list(folder.glob("*.zip"))) == 3

        copied.rename(Path(tmp) / "archived.zip")
        canonical.rename(Path(tmp) / "unknown.zip")
        changed_again = Provider([99, 100, 101, 102, 103, 104])
        plan = kakao_records.plan_for_catalog(series_id, changed_again.episodes, models.scan_existing_files(folder), folder.name)
        assert {(row.episode.product_id, row.episode.number) for row in plan.rows if row.downloaded} == {(102, 4), (103, 5)}
        assert kakao_records.product_receipts(series_id) == {102: {3}}

        subscription_scope.set_choice("kakao", series_id, "future", preview(original))
        surviving = Provider([101, 102, 104])
        assert subscription_scope.allowed_kakao_numbers(series_id, surviving.episodes) == {3}
        removed = Provider([105, 106])
        result = await runner.run_download(removed, series_id=series_id, title=None, download_root=tmp, max_episodes=10)
        assert result.error and "구독 범위" in result.error and not removed.requests

        backup = repository.export_all()
        validate_backup(backup)
        receipt = next(row for row in backup["settings"] if row["key"].startswith(kakao_records.PRODUCT_RECEIPTS_PREFIX))
        receipt["value"] = '{"102":[-1]}'
        try:
            validate_backup(backup)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid Kakao product receipt was accepted")


asyncio.run(main())
