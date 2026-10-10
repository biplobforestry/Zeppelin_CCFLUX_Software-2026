"""MicaSense archives must be opened once per band, not once per read.

A capture is one ZIP holding a band per file. Metadata extraction and image
verification each opened the archive from scratch, so a six-band capture paid
twelve central-directory reads. On the campaign disk that measured 1.2 s per
band — 64 minutes for 3,216 bands, which is why the camera pass never finished.
"""

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from PIL import Image

from core.detector import InputCandidate
from core.resource_manager import CameraBatchPolicy, ResourceLimits
from instruments.micasense.adapter import (
    ArchiveImage,
    MicaSenseLevel1Adapter,
    _all_images,
    release_archive_handle,
)


def _capture_archive(path: Path, capture: str, bands: int = 6) -> Path:
    """Write one MicaSense-shaped capture archive with a TIFF per band."""
    with zipfile.ZipFile(path, "w") as bundle:
        for band in range(1, bands + 1):
            buffer = io.BytesIO()
            Image.new("L", (8, 8), color=band * 10).save(buffer, format="TIFF")
            bundle.writestr(f"{capture}_{band}.tif", buffer.getvalue())
    return path


@pytest.fixture()
def adapter(tmp_path):
    made = MicaSenseLevel1Adapter(
        output_root=tmp_path / "out",
        flight_name="Flight_2707",
        resource_limits=ResourceLimits(2, 4 * 1024**3),
        batch_policy=CameraBatchPolicy(
            maximum_batch_files=32, maximum_thumbnail_count=4
        ),
    )
    yield made
    release_archive_handle()


def test_bands_of_one_capture_reuse_a_single_archive_handle(adapter, tmp_path, monkeypatch):
    archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000")
    images = _all_images((archive,))
    assert len(images) == 6

    opens: list[Path] = []
    original = zipfile.ZipFile

    def counting(file, *args, **kwargs):
        opens.append(Path(str(file)))
        return original(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", counting)
    release_archive_handle()
    for image in images:
        adapter._inspect_one(image)

    # One open for six bands, not one per read and certainly not two.
    assert len(opens) == 1, f"opened the archive {len(opens)} times for 6 bands"


def test_switching_archive_closes_the_previous_handle(adapter, tmp_path):
    first = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=2)
    second = _capture_archive(tmp_path / "IMG_0001.zip", "IMG_0001", bands=2)
    release_archive_handle()

    for image in _all_images((first, second)):
        record = adapter._inspect_one(image)
        assert not record["corrupt"]


def test_inspection_still_reports_bands_and_integrity(adapter, tmp_path):
    archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000")
    release_archive_handle()

    records = [adapter._inspect_one(image) for image in _all_images((archive,))]

    assert sorted(record["band_number"] for record in records) == [1, 2, 3, 4, 5, 6]
    assert not any(record["corrupt"] for record in records)
    assert all(record["size_bytes"] > 0 for record in records)


def test_a_corrupt_member_is_reported_not_raised(adapter, tmp_path):
    archive = tmp_path / "IMG_0002.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("IMG_0002_1.tif", b"not an image")
    release_archive_handle()

    record = adapter._inspect_one(
        ArchiveImage(archive, "IMG_0002_1.tif", size_bytes=12)
    )

    assert record["corrupt"] is True


def test_unset_camera_clock_does_not_exclude_images(adapter, tmp_path):
    """Epoch timestamps say nothing about when an image was taken.

    The MicaSense delivered for Flight_2707 was recorded with the camera clock
    unset, so every band is dated 1970. Filtering on that would discard the
    whole delivery; the images are kept and the operator is told why.
    """
    archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=2)
    release_archive_handle()
    loaded = adapter.load(InputCandidate("micasense", (archive,), 1.0, "fixture"))

    result = adapter.process_quicklook(
        loaded,
        {
            "analysis_start": datetime(2026, 7, 27, 7, 30, tzinfo=timezone.utc),
            "analysis_end": datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc),
        },
    )

    # Both bands survive a filter they cannot possibly satisfy, and the reason
    # is stated rather than the delivery silently vanishing.
    assert result.file_count == 2
    assert any(
        "no usable acquisition time" in warning for warning in result.warnings
    )


def test_pre_campaign_timestamps_are_dropped_from_the_calculation(adapter):
    """A frame dated before the campaign was dated by an unlocked GPS.

    Its stamp says nothing about when the image was taken, so it cannot bound
    the coverage, join a capture or contribute a trigger interval. It is counted
    and reported instead of being used.
    """
    import inspect

    from instruments.micasense.adapter import MINIMUM_CAPTURE_YEAR

    source = inspect.getsource(adapter.process_quicklook)
    assert MINIMUM_CAPTURE_YEAR == 2025
    assert "stamp.year < MINIMUM_CAPTURE_YEAR" in source
    assert "GPS had not" in source or "GPS had not locked" in source


def test_the_time_range_pass_reads_one_band_per_capture(adapter, tmp_path):
    """It decompressed all six, including the 10 MB panchromatic one.

    On Flight_CCT0803 that was 14,226 decompressions for 2,371 captures, and it
    is where MicaSense spent most of a 55-minute run. All six bands are written
    by one trigger and carry one acquisition time, so the other five tell the
    time range nothing new - which is what ArchiveImage.read_metadata already
    marks and this pass ignored.
    """
    archives = [
        _capture_archive(tmp_path / f"IMG_{index:04d}.zip", f"IMG_{index:04d}")
        for index in range(3)
    ]
    release_archive_handle()

    decoded: list[str] = []
    original = adapter._metadata

    def counting(source, payload=None):
        decoded.append(getattr(source, "member", str(source)))
        return original(source, payload)

    adapter._metadata = counting
    adapter.extract_time_range(InputCandidate("micasense", tuple(archives), 1.0, "t"))
    release_archive_handle()

    # Three captures of six bands each: three decodes, not eighteen.
    assert len(decoded) == 3, decoded
    assert not [name for name in decoded if name.endswith("_6.tif")]


class TestCapturesAreReadAlongsideEachOther:
    """Flight_CC0806's 59 978 images were read one batch of 32 at a time.

    Each batch was a barrier: the pool was built, its files read, and every
    worker then waited for the slowest before the next batch could start, 1 865
    times over. One pool for the whole delivery measured 27% faster warm and no
    slower cold, where the disk sets the pace regardless.

    The unit of work is the archive, not the file. _open_archive keeps one
    handle per thread and drops it as soon as that thread is handed a member of
    a different archive, so scattering a capture's six bands over six workers
    would make every band pay its own central-directory read.
    """

    SOURCE = (
        Path(__file__).resolve().parents[1]
        / "instruments" / "micasense" / "adapter.py"
    ).read_text(encoding="utf-8")

    def _pass(self) -> str:
        start = self.SOURCE.index("    def process_quicklook(")
        end = self.SOURCE.index("self._share_capture_metadata()", start)
        return self.SOURCE[start:end]

    def test_the_batch_barrier_is_gone(self):
        body = self._pass()
        assert "iter_batches" not in body
        assert "iter_batches" not in self.SOURCE, "the import outlived its use"

    def test_one_pool_covers_the_delivery(self):
        body = self._pass()
        assert body.count("ThreadPoolExecutor(") == 1
        assert "ccflux-micasense-meta" in body

    def test_the_reader_count_is_declared_and_bounded(self):
        from instruments.micasense.adapter import MICASENSE_METADATA_READERS

        assert 1 <= MICASENSE_METADATA_READERS <= 32

    def test_a_captures_bands_are_one_unit_of_work(self, tmp_path):
        from instruments.micasense.adapter import _reading_groups

        first = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=6)
        second = _capture_archive(tmp_path / "IMG_0001.zip", "IMG_0001", bands=6)
        images = _all_images((first, second))

        groups = _reading_groups(images)

        assert [len(group) for group in groups] == [6, 6]
        for group in groups:
            archives = {images[index].archive for index in group}
            assert len(archives) == 1

    def test_loose_images_parallelise_one_by_one(self, tmp_path):
        """A plain file has no handle to reuse, so nothing is gained by pairing
        it with its neighbour and a worker would sit idle behind it."""
        from instruments.micasense.adapter import _reading_groups

        loose = [tmp_path / f"IMG_{index:04d}_1.tif" for index in range(4)]

        assert _reading_groups(loose) == [[0], [1], [2], [3]]

    def test_no_archive_is_left_open_by_a_worker(self, adapter, tmp_path):
        """The handle lives in thread-local storage, so a worker that keeps one
        keeps it until the thread dies - and on Windows an archive still open is
        an archive the next stage cannot move or delete."""
        archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=3)
        images = _all_images((archive,))
        release_archive_handle()

        closed: list[bool] = []
        original = adapter._inspect_one

        def watching(item):
            record = original(item)
            closed.append(True)
            return record

        adapter._inspect_one = watching
        adapter._read_group(images, list(range(len(images))))

        from instruments.micasense.adapter import _ARCHIVE_HANDLES

        assert len(closed) == 3
        assert getattr(_ARCHIVE_HANDLES, "handle", None) is None

    def test_a_group_gives_its_handle_back_even_when_a_band_fails(
        self, adapter, tmp_path
    ):
        archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=2)
        images = _all_images((archive,))
        release_archive_handle()
        adapter._inspect_one = lambda item: (_ for _ in ()).throw(RuntimeError("x"))

        with pytest.raises(RuntimeError):
            adapter._read_group(images, [0, 1])

        from instruments.micasense.adapter import _ARCHIVE_HANDLES

        assert getattr(_ARCHIVE_HANDLES, "handle", None) is None

    def test_records_stay_in_delivery_order(self, adapter, tmp_path):
        """Bands finish in whatever order the disk returns them, and the capture
        counts join on position."""
        for index in range(4):
            _capture_archive(tmp_path / f"IMG_{index:04d}.zip", f"IMG_{index:04d}")
        release_archive_handle()
        candidate = InputCandidate(
            "micasense", tuple(sorted(tmp_path.glob("*.zip"))), 1.0, "fixture"
        )

        adapter.process_quicklook(adapter.load(candidate), {})

        read = [record["capture_id"] for record in adapter._records]
        assert read == sorted(read), read

    def test_cancelling_does_not_wait_for_the_queue(self):
        """Leaving the pool otherwise blocks on every queued capture."""
        body = self._pass()
        assert "future.cancel()" in body
        assert "self._check_cancelled()" in body


class TestTheTableSurvivesRaggedRows:
    """Sharpness and the intensity percentiles are measured on a sample of the
    captures, so the rows are not all the same shape. The header was taken from
    rows[0], which has none of them, and DictWriter raised on the first sampled
    capture: "dict contains fields not in fieldnames: 'sharpness', 'p50',
    'dark_fraction', 'p01', 'p99', 'saturated_fraction'". Flight_CC0808 failed
    on that after 102 minutes, with every number already computed.
    """

    @staticmethod
    def _rows():
        return [
            {"capture_id": "IMG_0000", "band_number": 1, "size_bytes": 1200},
            {"capture_id": "IMG_0001", "band_number": 1, "size_bytes": 1300,
             "sharpness": 0.42, "p01": 120, "p50": 800, "p99": 4000,
             "dark_fraction": 0.01, "saturated_fraction": 0.0},
            {"capture_id": "IMG_0002", "band_number": 1, "size_bytes": 1250},
        ]

    def test_a_sampled_column_does_not_fail_the_delivery(self, tmp_path):
        from instruments.micasense.adapter import _write_csv

        target = tmp_path / "captures.csv"
        _write_csv(target, self._rows())  # must not raise

        assert target.is_file()

    def test_every_column_any_row_carries_is_in_the_header(self, tmp_path):
        import csv

        from instruments.micasense.adapter import _write_csv

        target = tmp_path / "captures.csv"
        _write_csv(target, self._rows())

        header = target.read_text(encoding="utf-8").splitlines()[0]
        for column in ("sharpness", "p01", "p50", "p99",
                       "dark_fraction", "saturated_fraction"):
            assert column in header, column
        # The common columns stay at the front, where a reader looks for them.
        assert header.startswith("capture_id,band_number,size_bytes")

    def test_a_row_without_a_sampled_column_is_blank_not_dropped(self, tmp_path):
        import csv

        from instruments.micasense.adapter import _write_csv

        target = tmp_path / "captures.csv"
        _write_csv(target, self._rows())

        read = list(csv.DictReader(
            target.read_text(encoding="utf-8").splitlines()
        ))
        assert len(read) == 3
        assert read[1]["sharpness"] == "0.42"
        assert read[0]["sharpness"] == ""

    def test_a_delivery_with_no_rows_still_writes(self, tmp_path):
        from instruments.micasense.adapter import _write_csv

        target = tmp_path / "empty.csv"
        _write_csv(target, [])

        assert target.is_file()


class TestSharpnessSamplingCannotFailTheFlight:
    """Flight_CC0809 died at 76% after 97 minutes, "Failed during Sampling
    panchromatic sharpness and saturation". The function's own docstring says a
    capture whose band 6 cannot be read is left without the fields rather than
    failing the flight; the named exception list did not hold that promise,
    because decoding a 10 MB panchromatic TIFF can raise things Pillow and
    numpy do not document.
    """

    @staticmethod
    def _capture(path: Path, number: int, bands: int = 6):
        _capture_archive(path, path.stem, bands=bands)
        return {"image_number": number, "archive": str(path)}

    def test_every_sampled_capture_is_read(self, tmp_path):
        from instruments.micasense.adapter import (
            _sample_panchromatic_quality, release_archive_handle,
        )

        captures = [
            self._capture(tmp_path / f"IMG_{index:04d}.zip", index)
            for index in range(5)
        ]
        release_archive_handle()

        assert _sample_panchromatic_quality(captures, step=1) == 5
        assert "sharpness" in captures[0]

    def test_an_undecodable_band_costs_only_its_own_capture(self, tmp_path):
        import zipfile

        from instruments.micasense.adapter import (
            _sample_panchromatic_quality, release_archive_handle,
        )

        broken = tmp_path / "IMG_9999.zip"
        with zipfile.ZipFile(broken, "w") as bundle:
            bundle.writestr("IMG_9999_6.tif", b"not an image at all")
        good = self._capture(tmp_path / "IMG_0001.zip", 1)
        release_archive_handle()

        sampled = _sample_panchromatic_quality(
            [{"image_number": 0, "archive": str(broken)}, good], step=1
        )

        assert sampled == 1, "the good capture must still be sampled"

    def test_anything_the_decoder_raises_is_survived(self, tmp_path, monkeypatch):
        """Named exceptions were the bug: the guard has to be the contract."""
        from instruments.micasense import adapter

        captures = [
            self._capture(tmp_path / f"IMG_{index:04d}.zip", index)
            for index in range(3)
        ]
        adapter.release_archive_handle()

        class Unnamed(Exception):
            pass

        def explode(_data):
            raise Unnamed("something numpy did not document")

        monkeypatch.setattr(adapter, "_sharpness_and_exposure", explode)

        assert adapter._sample_panchromatic_quality(captures, step=1) == 0

    def test_the_shared_handle_is_not_closed(self, tmp_path):
        """_open_archive caches per thread; closing it leaves the cache holding
        a closed ZipFile, and any layout that samples one archive twice then
        loses those captures silently."""
        import inspect

        from instruments.micasense.adapter import _sample_panchromatic_quality

        source = inspect.getsource(_sample_panchromatic_quality)
        assert "with _open_archive(" not in source
        assert "bundle = _open_archive(" in source


def test_a_loose_tiff_is_still_read(adapter, tmp_path):
    """The skip keys on ArchiveImage.read_metadata; a plain file has no flag."""
    import io as _io

    from PIL import Image as _Image

    path = tmp_path / "IMG_0000_1.tif"
    buffer = _io.BytesIO()
    _Image.new("L", (8, 8), color=5).save(buffer, format="TIFF")
    path.write_bytes(buffer.getvalue())

    decoded: list[str] = []
    original = adapter._metadata
    adapter._metadata = lambda s, p=None: (decoded.append(str(s)), original(s, p))[1]
    adapter.extract_time_range(InputCandidate("micasense", (path,), 1.0, "t"))
    assert len(decoded) == 1
