"""Deterministic, bounded JPEG previews for archived vehicle photos."""

from __future__ import annotations

import hashlib
import json
import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED_INPUT_FORMATS = frozenset({"JPEG", "PNG", "GIF", "WEBP"})
PREVIEW_TRANSFORM_VERSION = "jpeg-v1"


class MediaPreviewError(ValueError):
    """A safe, URL-free image conversion error."""

    def __init__(self, message: str, *, kind: Literal["unsupported", "failed"]) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class MediaPreviewSpec:
    """One named preview size and its deterministic transformation settings."""

    variant: Literal["card", "gallery"]
    max_width: int
    max_height: int
    quality: int
    transform_version: str
    settings_json: str


@dataclass(frozen=True, slots=True)
class MediaPreviewSettings:
    """Safe defaults for the list-card and detail-gallery image derivatives."""

    card_max_width: int = 360
    card_max_height: int = 270
    card_quality: int = 78
    gallery_max_width: int = 1280
    gallery_max_height: int = 960
    gallery_quality: int = 84
    max_source_pixels: int = 40_000_000
    transform_version: str = PREVIEW_TRANSFORM_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("card_max_width", self.card_max_width),
            ("card_max_height", self.card_max_height),
            ("gallery_max_width", self.gallery_max_width),
            ("gallery_max_height", self.gallery_max_height),
            ("max_source_pixels", self.max_source_pixels),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        for name, value in (
            ("card_quality", self.card_quality),
            ("gallery_quality", self.gallery_quality),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not 60 <= value <= 95:
                raise ValueError(f"{name} must be between 60 and 95")
        if not isinstance(self.transform_version, str) or not self.transform_version.strip():
            raise ValueError("transform_version cannot be empty")
        if len(self.transform_version) > 64:
            raise ValueError("transform_version is too long")

    def specs(self) -> tuple[MediaPreviewSpec, MediaPreviewSpec]:
        return (
            self._spec("card", self.card_max_width, self.card_max_height, self.card_quality),
            self._spec(
                "gallery",
                self.gallery_max_width,
                self.gallery_max_height,
                self.gallery_quality,
            ),
        )

    def _spec(
        self,
        variant: Literal["card", "gallery"],
        max_width: int,
        max_height: int,
        quality: int,
    ) -> MediaPreviewSpec:
        settings = {
            "background": "white",
            "format": "JPEG",
            "max_height": max_height,
            "max_width": max_width,
            "quality": quality,
            "resampling": "LANCZOS",
        }
        return MediaPreviewSpec(
            variant=variant,
            max_width=max_width,
            max_height=max_height,
            quality=quality,
            transform_version=self.transform_version,
            settings_json=json.dumps(settings, sort_keys=True, separators=(",", ":")),
        )


@dataclass(frozen=True, slots=True)
class GeneratedMediaPreview:
    """One JPEG derivative with the exact settings used to create it."""

    spec: MediaPreviewSpec
    body: bytes
    sha256: str
    width: int
    height: int
    mime_type: str = "image/jpeg"


def _as_rgb(image: Image.Image) -> Image.Image:
    """Convert an image to RGB, flattening transparency over white."""

    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def generate_photo_previews(
    body: bytes,
    *,
    settings: MediaPreviewSettings | None = None,
) -> tuple[GeneratedMediaPreview, ...]:
    """Create deterministic, bounded previews without changing the source bytes.

    The first frame is used for animated GIFs. AVIF and other formats that the
    installed Pillow build cannot reliably decode are left as original-only
    assets and reported as unsupported.
    """

    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    if not body:
        raise MediaPreviewError("image data is empty", kind="failed")
    selected_settings = settings or MediaPreviewSettings()

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as source:
                source_format = (source.format or "").upper()
                if source_format not in SUPPORTED_INPUT_FORMATS:
                    raise MediaPreviewError(
                        "image format is not supported for preview generation",
                        kind="unsupported",
                    )
                width, height = source.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > selected_settings.max_source_pixels
                ):
                    raise MediaPreviewError(
                        "image dimensions exceed the preview safety limit",
                        kind="unsupported",
                    )

                # ImageOps handles EXIF orientation before dimensions are calculated.
                source.seek(0)
                oriented = ImageOps.exif_transpose(source)
                oriented.load()
                rgb = _as_rgb(oriented)
                if oriented is not source:
                    oriented.close()

                previews: list[GeneratedMediaPreview] = []
                for spec in selected_settings.specs():
                    resized = rgb.copy()
                    resized.thumbnail(
                        (spec.max_width, spec.max_height),
                        resample=Image.Resampling.LANCZOS,
                        reducing_gap=2.0,
                    )
                    output = BytesIO()
                    resized.save(
                        output,
                        format="JPEG",
                        quality=spec.quality,
                        optimize=True,
                        progressive=True,
                        subsampling=2,
                    )
                    preview_body = output.getvalue()
                    previews.append(
                        GeneratedMediaPreview(
                            spec=spec,
                            body=preview_body,
                            sha256=hashlib.sha256(preview_body).hexdigest(),
                            width=resized.width,
                            height=resized.height,
                        )
                    )
                    resized.close()
                rgb.close()

                return tuple(previews)
    except MediaPreviewError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaPreviewError(
            "image dimensions exceed the preview safety limit",
            kind="unsupported",
        ) from exc
    except UnidentifiedImageError as exc:
        raise MediaPreviewError(
            "image format is unsupported or cannot be identified",
            kind="unsupported",
        ) from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise MediaPreviewError("image could not be decoded", kind="failed") from exc
