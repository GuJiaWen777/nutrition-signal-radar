"""Cover rendering for the news line (Phase 4 P4-05, ADR-0014).

Two engines, one fixed brand template:

- Primary: fill the versioned HTML template (``templates/cover.html``, inline
  CSS, fixed brand colors and copy) and screenshot it with the local Edge or
  Chrome in headless mode.  The template file is part of the repository, so
  the "covers are completely uniform" acceptance rule holds — design changes
  are code changes, never per-run improvisation.
- Fallback: the deterministic PIL renderer below, used automatically when no
  local browser can be found or the screenshot fails/produces a wrong size.

The EarlyData logo is optional: until the operator drops the original file at
``var/assets/earlydata-logo.png`` covers render without it and callers record
that fact.  Third-party logos are never fetched.
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COVER_WIDTH = 1800
COVER_HEIGHT = 766
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "cover.html"
_TITLE_COLOR = (23, 59, 102)      # #173B66
_INFO_COLOR = (47, 108, 174)      # #2F6CAE
_BODY_COLOR = (43, 53, 69)        # #2B3545
_MUTED_COLOR = (113, 128, 150)    # #718096
_GRADIENT_TOP = (255, 255, 255)   # #FFFFFF
_GRADIENT_BOTTOM = (220, 234, 248)  # 浅蓝渐变底
_ACCENT_GREEN = (114, 191, 68)    # #72BF44
_MARGIN_X = 96
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)
_BROWSER_CANDIDATES = (
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)


class NewsCoverError(RuntimeError):
    """Raised when the cover cannot be rendered by any engine."""


@dataclass(frozen=True)
class CoverRenderResult:
    path: Path
    engine: str  # html_browser | pil_fallback

    def as_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "engine": self.engine}


def render_news_cover(
    output_path: Path,
    *,
    article_title: str = "",
    date_text: str,
    logo_path: Path | None = None,
) -> CoverRenderResult:
    """Render the fixed-brand 1800×766 cover PNG and return engine + path."""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    browser = find_headless_browser()
    if browser is not None:
        try:
            _render_with_browser(browser, output_path, article_title=article_title, date_text=date_text, logo_path=logo_path)
            with Image.open(output_path) as rendered:
                if rendered.size == (COVER_WIDTH, COVER_HEIGHT):
                    return CoverRenderResult(path=output_path, engine="html_browser")
        except Exception:
            pass  # any browser failure falls through to the PIL renderer
    _render_cover_pil(output_path, article_title=article_title, date_text=date_text, logo_path=logo_path)
    return CoverRenderResult(path=output_path, engine="pil_fallback")


def find_headless_browser() -> Path | None:
    """Locate a local Edge/Chrome for headless screenshots (no download)."""

    override = os.environ.get("RADAR_BROWSER_PATH", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    candidates.extend(Path(path) for path in _BROWSER_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def render_cover_html(
    output_path: Path,
    *,
    article_title: str = "",
    date_text: str,
    logo_path: Path | None = None,
) -> Path:
    """Fill the versioned template and write the standalone cover HTML."""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    has_logo = logo_path is not None and Path(logo_path).is_file()
    # The operator-provided logo already carries the wordmark; only fall back
    # to the text brand line when no logo is configured.
    brand_block = (
        ""
        if has_logo
        else '<div class="brand">E A R L Y D A T A</div>'
    )
    logo_img = f'<img src="{Path(logo_path).resolve().as_uri()}" alt="EarlyData">' if has_logo else ""
    filled = (
        template.replace("{{LOGO_IMG}}", logo_img)
        .replace("{{BRAND_BLOCK}}", brand_block)
        .replace("{{ARTICLE_TITLE}}", html.escape(str(article_title or ""), quote=False))
        .replace("{{DATE_TEXT}}", html.escape(str(date_text or ""), quote=False))
    )
    output_path.write_text(filled, encoding="utf-8")
    return output_path


def _render_with_browser(
    browser: Path,
    output_path: Path,
    *,
    article_title: str,
    date_text: str,
    logo_path: Path | None,
) -> None:
    html_path = output_path.with_suffix(".html")
    render_cover_html(
        html_path, article_title=article_title, date_text=date_text, logo_path=logo_path
    )
    # Edge intermittently exits 0 without writing the screenshot (observed on
    # busy machines); retry with a fresh profile before falling back to PIL.
    last_error = "no attempt"
    for _attempt in range(3):
        profile_dir = tempfile.mkdtemp(prefix="news-cover-")
        command = [
            str(browser),
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--no-first-run",
            "--disable-extensions",
            f"--user-data-dir={profile_dir}",
            f"--screenshot={output_path.resolve()}",
            f"--window-size={COVER_WIDTH},{COVER_HEIGHT}",
            html_path.resolve().as_uri(),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, timeout=120, check=False
            )
            last_error = (
                f"exit={completed.returncode} "
                f"stderr={completed.stderr.decode('utf-8', 'ignore')[:200]}"
            )
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout: {exc}"
        if output_path.is_file():
            break
        shutil.rmtree(profile_dir, ignore_errors=True)
    if not output_path.is_file():
        raise NewsCoverError(f"browser screenshot failed ({last_error})")


# ---------------------------------------------------------------------------
# Deterministic PIL fallback


def _render_cover_pil(
    output_path: Path,
    *,
    article_title: str,
    date_text: str,
    logo_path: Path | None,
) -> None:
    image = _gradient_canvas()
    draw = ImageDraw.Draw(image)
    fonts = _load_fonts()
    y = _draw_logo(image, logo_path)
    _draw_brand_block(draw, fonts, y, article_title, date_text, with_logo=logo_path is not None and Path(logo_path).is_file())
    image.save(Path(output_path), format="PNG")


def _gradient_canvas() -> Image.Image:
    canvas = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), _GRADIENT_TOP)
    top, bottom = _GRADIENT_TOP, _GRADIENT_BOTTOM
    for y in range(COVER_HEIGHT):
        ratio = y / (COVER_HEIGHT - 1)
        row_color = tuple(
            int(top[channel] + (bottom[channel] - top[channel]) * ratio)
            for channel in range(3)
        )
        ImageDraw.Draw(canvas).line([(0, y), (COVER_WIDTH, y)], fill=row_color)
    # A thin brand-green accent line anchors the data-technology feel.
    ImageDraw.Draw(canvas).rectangle(
        [0, COVER_HEIGHT - 10, COVER_WIDTH, COVER_HEIGHT], fill=_ACCENT_GREEN
    )
    return canvas


def _load_fonts() -> dict[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    def load(size: int):
        for candidate in _FONT_CANDIDATES:
            path = Path(candidate)
            if path.is_file():
                try:
                    return ImageFont.truetype(str(path), size)
                except OSError:
                    continue
        return ImageFont.load_default()

    return {
        "brand": load(40),
        "series": load(84),
        "title": load(50),
        "meta": load(34),
    }


def _draw_logo(image: Image.Image, logo_path: Path | None) -> int:
    """Paste the operator-provided logo top-left; return the next free y."""

    if logo_path is None or not Path(logo_path).is_file():
        return _MARGIN_X + 8
    try:
        logo = Image.open(Path(logo_path)).convert("RGBA")
    except OSError:
        return _MARGIN_X + 8
    target_height = 88
    scale = target_height / max(1, logo.height)
    logo = logo.resize(
        (max(1, int(logo.width * scale)), target_height), Image.LANCZOS
    )
    image.paste(logo, (_MARGIN_X, 64), logo)
    return 64 + target_height + 36


def _draw_brand_block(
    draw, fonts, y: int, article_title: str, date_text: str, *, with_logo: bool = False
) -> int:
    if not with_logo:
        draw.text((_MARGIN_X, y), "E A R L Y D A T A", font=fonts["brand"], fill=_INFO_COLOR)
        y += 40 + 28
    draw.text((_MARGIN_X, y), "营养健康行业资讯", font=fonts["series"], fill=_TITLE_COLOR)
    y += 84 + 30

    title_lines = _wrap_title(draw, fonts["title"], article_title, max_lines=2)
    for line in title_lines:
        draw.text((_MARGIN_X, y), line, font=fonts["title"], fill=_BODY_COLOR)
        y += 50 + 18

    meta_line = f"政策 · 品牌 · 展会｜{date_text}"
    draw.text((_MARGIN_X, max(y, COVER_HEIGHT - 110)), meta_line, font=fonts["meta"], fill=_MUTED_COLOR)
    return y


def _wrap_title(draw, font, title: str, *, max_lines: int) -> list[str]:
    """Fit the article title into at most ``max_lines`` with an ellipsis."""

    cleaned = " ".join(str(title or "").split())
    if not cleaned:
        return []
    max_width = COVER_WIDTH - 2 * _MARGIN_X
    lines: list[str] = []
    current = ""
    for char in cleaned:
        candidate = current + char
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
            continue
        lines.append(current)
        if len(lines) == max_lines:
            break
        current = char
    else:
        if current:
            lines.append(current)
    if len(lines) == max_lines and _join(lines) != cleaned:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines


def _join(lines: list[str]) -> str:
    return "".join(line.strip() for line in lines)


__all__ = [
    "COVER_HEIGHT",
    "COVER_WIDTH",
    "CoverRenderResult",
    "NewsCoverError",
    "find_headless_browser",
    "render_cover_html",
    "render_news_cover",
]
