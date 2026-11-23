from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from PIL import Image

from exleasingcar_archive.media_previews import (
    MediaPreviewError,
    MediaPreviewSettings,
    generate_photo_previews,
)


def _image_bytes(size=(2400, 1600), *, orientation=None, image_format="JPEG"):
    output = BytesIO()
    image = Image.new("RGB", size, (20, 110, 190))
    save_options = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        save_options["exif"] = exif
    image.save(output, format=image_format, **save_options)
    image.close()
    return output.getvalue()


def test_previews_are_deterministic_and_fit_their_configured_bounds():
    source = _image_bytes()
    first = generate_photo_previews(source)
    second = generate_photo_previews(source)

    assert [preview.spec.variant for preview in first] == ["card", "gallery"]
    assert [preview.sha256 for preview in first] == [preview.sha256 for preview in second]
    assert [preview.body for preview in first] == [preview.body for preview in second]
    assert hashlib.sha256(source).digest() != hashlib.sha256(first[0].body).digest()

    for preview in first:
        assert preview.mime_type == "image/jpeg"
        assert preview.width <= preview.spec.max_width
        assert preview.height <= preview.spec.max_height
        assert preview.width / preview.height == pytest.approx(3 / 2, abs=0.01)


def test_exif_orientation_is_applied_before_preview_dimensions_are_calculated():
    source = _image_bytes((120, 60), orientation=6)
    previews = generate_photo_previews(source)

    assert len(previews) == 2
    assert all(preview.width == 60 for preview in previews)
    assert all(preview.height == 120 for preview in previews)


def test_transparent_png_is_flattened_to_jpeg_without_upscaling():
    source = BytesIO()
    image = Image.new("RGBA", (120, 80), (255, 0, 0, 128))
    image.save(source, format="PNG")
    image.close()

    previews = generate_photo_previews(source.getvalue())

    assert all(preview.width == 120 and preview.height == 80 for preview in previews)
    with Image.open(BytesIO(previews[0].body)) as converted:
        assert converted.mode == "RGB"


def test_unsupported_or_corrupt_images_are_reported_without_raw_pillow_errors():
    with pytest.raises(MediaPreviewError) as unsupported:
        generate_photo_previews(b"not-an-image")
    assert unsupported.value.kind == "unsupported"

    with pytest.raises(MediaPreviewError) as empty:
        generate_photo_previews(b"")
    assert empty.value.kind == "failed"


def test_source_pixel_limit_is_checked_before_image_conversion():
    source = _image_bytes((100, 100))
    settings = MediaPreviewSettings(max_source_pixels=5_000)

    with pytest.raises(MediaPreviewError, match="safety limit") as error:
        generate_photo_previews(source, settings=settings)

    assert error.value.kind == "unsupported"
