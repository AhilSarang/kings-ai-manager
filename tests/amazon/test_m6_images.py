"""P3 M6: Drive links → checks → JPEG → public hosting → Amazon image attributes."""

import io

import pytest
from PIL import Image
from sqlalchemy import select

from app.amazon import images as img
from app.amazon.drive import DriveError, FakeDriveClient, parse_links
from app.db.models import AmazonImage, AmazonListing, File
from app.services.media import FakeMediaStore
from tests.amazon.helpers import US, connect, expect_listings, listing_item


def png(width=1600, height=1600, colour=(255, 255, 255), mode="RGB") -> bytes:
    image = Image.new(mode, (width, height), colour if mode != "RGBA" else colour + (255,))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def jpeg(width=1600, height=1600, colour=(255, 255, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="JPEG")
    return out.getvalue()


def gif(animated=True) -> bytes:
    frames = [Image.new("RGB", (1200, 1200), (255, 255, 255)), Image.new("RGB", (1200, 1200), (250, 250, 250))]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=animated, append_images=frames[1:] if animated else [])
    return out.getvalue()


@pytest.fixture
def account(db, owner):
    return connect(db)


@pytest.fixture
def listing(db, account):
    expect_listings([listing_item()])
    from app.amazon import listings as listings_svc

    listings_svc.sync_all(db)
    return db.execute(select(AmazonListing)).scalar_one()


FOLDER = "https://drive.google.com/drive/folders/FOLDER1"
FILE = "https://drive.google.com/file/d/FILE1/view?usp=sharing"


# ---------- links ----------
def test_parse_every_drive_link_shape():
    text = ("main https://drive.google.com/file/d/AAA111/view "
            "folder https://drive.google.com/drive/u/0/folders/BBB222 "
            "open https://drive.google.com/open?id=CCC333 "
            "sheet https://docs.google.com/spreadsheets/d/DDD444/edit")
    links = parse_links(text)
    assert links["files"] == ["AAA111", "CCC333"] and links["folders"] == ["BBB222"]
    assert links["documents"] == ["DDD444"]
    assert parse_links("no links here") == {"files": [], "folders": [], "documents": []}


# ---------- checks ----------
def test_good_image_passes_every_check():
    checks = img.check(jpeg(), "main.jpg")
    assert checks["errors"] == [] and checks["format"] == "JPEG"
    assert checks["width"] == 1600 and checks["white_background"] is True


def test_small_images_warn_then_block():
    assert any("no zoom" in w for w in img.check(jpeg(800, 800), "small.jpg")["warnings"])
    assert any("at least 500px" in e for e in img.check(jpeg(400, 400), "tiny.jpg")["errors"])


def test_animated_and_oversized_are_rejected():
    assert any("animated" in e for e in img.check(gif(), "spin.gif")["errors"])
    assert img.check(gif(animated=False), "still.gif")["errors"] == []


def test_non_white_background_is_flagged_not_blocked():
    checks = img.check(jpeg(colour=(30, 30, 30)), "dark.jpg")
    assert checks["errors"] == [] and checks["white_background"] is False


def test_unreadable_file_is_a_clear_error():
    with pytest.raises(img.ImageError, match="isn't an image I can read"):
        img.check(b"not an image", "notes.txt")


# ---------- conversion ----------
def test_png_with_transparency_becomes_jpeg_on_white():
    data = png(mode="RGBA", colour=(0, 0, 0))
    converted = img.to_jpeg(Image.open(io.BytesIO(data)))
    out = Image.open(io.BytesIO(converted))
    assert out.format == "JPEG" and out.mode == "RGB"


# ---------- import ----------
def test_folder_import_orders_slots_and_stores_files(db, listing):
    FakeDriveClient.add_file("F1", "01-main.jpg", jpeg(), folder="FOLDER1")
    FakeDriveClient.add_file("F2", "02-side.png", png(), mime="image/png", folder="FOLDER1")
    FakeDriveClient.add_file("F3", "notes.txt", b"hello", mime="text/plain", folder="FOLDER1")
    rows = img.import_images(db, text=f"images: {FOLDER}", listing=listing)
    assert [r.slot for r in rows] == ["main_product_image_locator", "other_product_image_locator_1"]
    assert rows[0].status == "checked" and rows[0].file_id is not None
    assert rows[1].checks["converted_to_jpeg"] is True
    stored = db.get(File, rows[1].file_id)
    assert stored.mime_type == "image/jpeg"


def test_single_file_link_and_swatch(db, listing):
    FakeDriveClient.add_file("FILE1", "swatch.jpg", jpeg())
    rows = img.import_images(db, text=FILE, listing=listing, swatch_ids=("FILE1",))
    assert rows[0].slot == "swatch_product_image_locator"


def test_rejected_image_does_not_consume_a_slot(db, listing):
    FakeDriveClient.add_file("F1", "tiny.jpg", jpeg(300, 300), folder="FOLDER1")
    FakeDriveClient.add_file("F2", "good.jpg", jpeg(), folder="FOLDER1")
    rows = img.import_images(db, text=FOLDER, listing=listing)
    assert rows[0].status == "rejected" and rows[0].file_id is None
    assert rows[1].slot == "main_product_image_locator"


def test_identical_images_are_not_stored_twice(db, listing):
    same = jpeg()
    FakeDriveClient.add_file("F1", "a.jpg", same)
    first = img.import_images(db, text="https://drive.google.com/file/d/F1/view", listing=listing)
    img.host(db, first)
    FakeDriveClient.add_file("F2", "copy-of-a.jpg", same)
    second = img.import_images(db, text="https://drive.google.com/file/d/F2/view", listing=listing)
    assert second[0].id == first[0].id
    assert db.scalar(select(AmazonImage).where(AmazonImage.sha256 == first[0].sha256)) is not None
    assert len(db.execute(select(AmazonImage)).scalars().all()) == 1


def test_no_links_means_no_work(db, listing):
    assert img.import_images(db, text="please update the title", listing=listing) == []


def test_drive_permission_error_explains_the_fix(db, listing):
    FakeDriveClient.fail_with = DriveError("Google hasn't given us Drive access yet. Re-run the Google "
                                           "authorisation to add Drive, then try again.")
    with pytest.raises(DriveError, match="Re-run the Google authorisation"):
        img.import_images(db, text=FILE, listing=listing)


def test_more_than_nine_images_is_refused(db, listing):
    for i in range(10):
        FakeDriveClient.add_file(f"F{i}", f"{i:02d}.jpg", jpeg(colour=(255, 255, 255 - i)), folder="FOLDER1")
    with pytest.raises(img.ImageError, match="main image plus 8 others"):
        img.import_images(db, text=FOLDER, listing=listing)


# ---------- hosting ----------
def test_hosting_publishes_and_maps_to_amazon_attributes(db, listing):
    FakeDriveClient.add_file("F1", "main.jpg", jpeg(), folder="FOLDER1")
    FakeDriveClient.add_file("F2", "side.jpg", jpeg(colour=(254, 254, 254)), folder="FOLDER1")
    rows = img.import_images(db, text=FOLDER, listing=listing)
    img.host(db, rows)
    assert all(r.status == "hosted" and r.public_url.startswith("https://img.example.test/") for r in rows)
    assert len(FakeMediaStore.objects) == 2
    key = rows[0].public_url.split("/")[-1]
    assert key.endswith(".jpg") and len(key) > 30, "unguessable path"

    attributes = img.attributes_for(rows, US)
    assert attributes["main_product_image_locator"][0]["media_location"] == rows[0].public_url
    assert attributes["other_product_image_locator_1"][0]["marketplace_id"] == US


def test_rejected_images_are_never_published(db, listing):
    FakeDriveClient.add_file("F1", "tiny.jpg", jpeg(200, 200), folder="FOLDER1")
    rows = img.import_images(db, text=FOLDER, listing=listing)
    img.host(db, rows)
    assert rows[0].public_url is None and img.attributes_for(rows, US) == {}


def test_hosting_is_idempotent(db, listing):
    FakeDriveClient.add_file("F1", "main.jpg", jpeg())
    rows = img.import_images(db, text="https://drive.google.com/file/d/F1/view", listing=listing)
    url = img.host(db, rows)[0].public_url
    assert img.host(db, rows)[0].public_url == url and len(FakeMediaStore.objects) == 1


# ---------- summary for WhatsApp ----------
def test_summary_reads_like_a_person_wrote_it(db, listing):
    FakeDriveClient.add_file("F1", "main.jpg", jpeg(900, 900, colour=(20, 20, 20)), folder="FOLDER1")
    FakeDriveClient.add_file("F2", "tiny.jpg", jpeg(200, 200), folder="FOLDER1")
    rows = img.import_images(db, text=FOLDER, listing=listing)
    text = img.summary(rows)
    assert "main image: 900×900px, ready" in text
    assert "⚠️" in text and "no zoom" in text
    assert "⛔" in text and "at least 500px" in text
    assert "doesn't look like it's on a pure white background" in text
    assert "can take a few hours to show on Amazon" in text
    assert img.summary([]) == "No images found in that message."
