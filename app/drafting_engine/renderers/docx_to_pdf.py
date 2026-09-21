"""Canonical DOCX -> PDF conversion.

The legal renderer used to produce the DOCX with python-docx and the PDF with a
completely independent ReportLab layout. Two layout engines cannot be kept in
step, so the PDF was never a faithful representation of the DOCX (different
line wrapping, paragraph heights, page breaks and — for Devanagari — a glyph
based text layer that extracted as private-use garbage).

This module converts the *finalized DOCX* to PDF with LibreOffice in headless
mode, so the PDF is produced by one layout engine from the same document the
advocate edits and signs.

Deployment notes
----------------
* Requires a LibreOffice installation on the host (``soffice``). On Heroku add
  the LibreOffice buildpack and/or the ``Aptfile`` shipped in this repository.
* Runs without a GUI and without a writable home directory: every conversion
  gets a private temporary ``HOME``, font dir and LibreOffice user profile, so
  concurrent bot users cannot collide on a shared profile lock.
* The bundled Unicode Devanagari fonts are staged into the conversion's private
  temporary directory and pinned through ``FONTCONFIG_FILE``, so the conversion
  uses exactly the fonts this repository ships instead of whatever the host
  happens to have installed. Nothing is ever written inside the repository.
  Font choice changes glyph metrics, line breaking and the PDF's ToUnicode
  mapping, so without this pinning the same draft would not lay out the same way
  on two different machines.
* No transliteration, no rasterisation and no image-only pages: the produced
  PDF keeps real selectable/searchable Unicode text.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_ASSETS_FONTS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"

# Where LibreOffice is commonly installed on Linux/macOS hosts.
_COMMON_BINARIES = (
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/usr/bin/libreoffice",
    "/opt/libreoffice/program/soffice",
    "/opt/libreoffice6.4/program/soffice",
    "/snap/bin/libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)

# The Heroku apt buildpack (heroku-community/apt) extracts the packages listed
# in ``Aptfile`` under ``$HOME/.apt`` (``/app/.apt`` on a dyno) with ``dpkg -x``
# and prepends ``$HOME/.apt/usr/bin`` to PATH through ``.profile.d/000_apt.sh``.
# The binary is therefore normally found via PATH, but these explicit
# candidates make discovery independent of the profile script (for example
# under a stripped environment or a one-off ``heroku run`` with ``env -i``).
_APT_RELATIVE_BINARIES = (
    "usr/bin/soffice",
    "usr/bin/libreoffice",
    "usr/lib/libreoffice/program/soffice",
)

_DEFAULT_TIMEOUT = 300


def _apt_prefixes() -> list[Path]:
    prefixes = []
    home = os.getenv("HOME")
    if home:
        prefixes.append(Path(home) / ".apt")
    prefixes.append(Path("/app/.apt"))
    seen: set[Path] = set()
    unique = []
    for prefix in prefixes:
        if prefix not in seen:
            seen.add(prefix)
            unique.append(prefix)
    return unique


def apt_prefix_for(binary: str | Path) -> Path | None:
    """Return the ``.apt`` prefix a binary lives under, or None."""
    parts = Path(binary).resolve().parts
    if ".apt" in parts:
        index = parts.index(".apt")
        return Path(*parts[: index + 1])
    return None


def _prepend_path(env: dict[str, str], key: str, entries: list[str]) -> None:
    """Prepend ``entries`` to the ``key`` path list in ``env`` (no duplicates)."""
    current = [p for p in (env.get(key) or "").split(os.pathsep) if p]
    merged = [entry for entry in entries if entry not in current] + current
    env[key] = os.pathsep.join(merged)


def apt_environment(env: dict[str, str], soffice: str | Path) -> dict[str, str]:
    """Reproduce the apt buildpack's runtime environment for a relocated install.

    ``.profile.d/000_apt.sh`` does this for every dyno process, but a converter
    should not depend on having been started through that profile script.
    """
    prefix = apt_prefix_for(soffice)
    if prefix is None:
        return env
    lib_dirs = [
        str(prefix / "usr" / "lib" / "x86_64-linux-gnu"),
        str(prefix / "usr" / "lib" / "i386-linux-gnu"),
        str(prefix / "usr" / "lib"),
    ]
    _prepend_path(env, "LD_LIBRARY_PATH", lib_dirs)
    _prepend_path(env, "PATH", [str(prefix / "usr" / "bin")])
    return env


class PdfConversionError(RuntimeError):
    """The DOCX existed but could not be converted to PDF."""


class PdfConverterUnavailable(PdfConversionError):
    """No LibreOffice binary is available to perform the conversion."""


def find_soffice() -> str | None:
    """Return the LibreOffice binary to use, or None when it is not installed.

    ``SOFFICE_BIN`` always wins, which is how a Heroku buildpack or a custom
    image can point at a non-standard install location.
    """
    configured = (os.getenv("SOFFICE_BIN") or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.exists():
            return str(candidate)
        found = shutil.which(configured)
        if found:
            return found
        log.warning("SOFFICE_BIN=%s does not exist; falling back to discovery.", configured)

    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _COMMON_BINARIES:
        if Path(candidate).exists():
            return candidate
    for prefix in _apt_prefixes():
        for relative in _APT_RELATIVE_BINARIES:
            candidate = prefix / relative
            if candidate.exists():
                return str(candidate)
    return None


def converter_available() -> bool:
    return find_soffice() is not None


def soffice_version(timeout: int = 60) -> str | None:
    """Return LibreOffice's ``--version`` banner, or None when unavailable.

    Diagnostics only. Runs with the same isolated profile/HOME the converter
    uses so it never touches (or is blocked by) a shared LibreOffice profile.
    """
    soffice = find_soffice()
    if not soffice:
        return None
    with tempfile.TemporaryDirectory(prefix="legal-soffice-version-") as raw_tmp:
        tmp = Path(raw_tmp)
        home = tmp / "home"
        home.mkdir()
        profile = tmp / "profile"
        profile.mkdir()
        env = apt_environment(dict(os.environ), soffice)
        env["HOME"] = str(home)
        env.pop("DISPLAY", None)
        try:
            completed = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--headless",
                    "--version",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
    banner = (completed.stdout or completed.stderr or "").strip()
    return banner.splitlines()[0] if banner else None


def bundled_font_files() -> list[Path]:
    """Return the TrueType fonts shipped with this repository."""
    if not _ASSETS_FONTS_DIR.is_dir():
        return []
    return sorted(_ASSETS_FONTS_DIR.glob("*.ttf"))


def prepare_fonts(work_dir: Path) -> str | None:
    """Stage the bundled fonts and return a pinned ``FONTCONFIG_FILE``.

    Resolving fonts against the host would make the output non-deterministic: a
    newer Noto Sans Devanagari build renders with different metrics and emits a
    different ToUnicode CMap, so the same draft produced a different PDF on a
    different machine. The DOCX only names families this repository ships, so
    exposing just those is both sufficient and deterministic.

    The fonts are staged into ``work_dir`` rather than referenced in place
    because fontconfig writes a per-directory cache id (``.uuid``) into every
    directory it is pointed at, and the repository's assets must stay untouched.

    Returns the path for ``FONTCONFIG_FILE``, or None when no bundled fonts are
    available (the conversion then falls back to the host's fonts).
    """
    fonts = bundled_font_files()
    if not fonts:
        log.warning(
            "No bundled fonts found in %s; LibreOffice will resolve fonts against "
            "the host, so the PDF may differ from the DOCX on another machine.",
            _ASSETS_FONTS_DIR,
        )
        return None

    font_dir = work_dir / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)
    for source in fonts:
        destination = font_dir / source.name
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

    cache = work_dir / "fontcache"
    cache.mkdir(parents=True, exist_ok=True)
    config = work_dir / "fonts.conf"
    config.write_text(
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        "<fontconfig>\n"
        f"  <dir>{font_dir}</dir>\n"
        f"  <cachedir>{cache}</cachedir>\n"
        "</fontconfig>\n",
        encoding="utf-8",
    )
    return str(config)


def conversion_environment(soffice: str | Path, home: Path, fontconfig: str | None) -> dict[str, str]:
    """Environment for a headless conversion: private HOME, pinned fonts, no X."""
    env = apt_environment(dict(os.environ), soffice)
    env["HOME"] = str(home)
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    if fontconfig:
        # Pin the font set so the layout cannot depend on the host.
        env["FONTCONFIG_FILE"] = fontconfig
    # Never let a stale developer profile or an X connection interfere.
    env.pop("SAL_USE_VCLPLUGIN", None)
    env.pop("DISPLAY", None)
    return env


def conversion_command(soffice: str | Path, profile: Path, outdir: Path, docx: Path) -> list[str]:
    """The exact ``soffice`` command line used for DOCX -> PDF."""
    return [
        str(soffice),
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--invisible",
        "--nologo",
        "--nodefault",
        "--norestore",
        "--nolockcheck",
        "--nofirststartwizard",
        "--convert-to",
        "pdf:writer_pdf_Export",
        "--outdir",
        str(outdir),
        str(docx),
    ]


def convert(docx_path: str | Path, pdf_path: str | Path, timeout: int | None = None) -> Path:
    """Convert ``docx_path`` to ``pdf_path`` using headless LibreOffice.

    Raises :class:`PdfConverterUnavailable` when LibreOffice is missing and
    :class:`PdfConversionError` when the conversion itself fails.
    """
    soffice = find_soffice()
    if not soffice:
        raise PdfConverterUnavailable(
            "LibreOffice (soffice) was not found. Install it, or set SOFFICE_BIN "
            "to its full path, to render the PDF from the canonical DOCX."
        )

    source = Path(docx_path)
    destination = Path(pdf_path)
    if not source.is_file():
        raise PdfConversionError(f"DOCX to convert does not exist: {source}")

    timeout = int(timeout or os.getenv("DOCX_PDF_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT))
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="legal-docx-pdf-") as raw_tmp:
        tmp = Path(raw_tmp)
        home = tmp / "home"
        home.mkdir()
        fontconfig = prepare_fonts(tmp)

        # Unicode/space-safe staging name: the advocate-facing filename may be
        # Devanagari, but the converter only ever sees ASCII paths.
        work = tmp / "work"
        work.mkdir()
        staged_docx = work / "input.docx"
        shutil.copy2(source, staged_docx)
        outdir = work / "out"
        outdir.mkdir()

        profile = tmp / "profile"
        profile.mkdir()

        env = conversion_environment(soffice, home, fontconfig)
        command = conversion_command(soffice, profile, outdir, staged_docx)

        try:
            completed = subprocess.run(
                command,
                env=env,
                cwd=str(work),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfConversionError(
                f"LibreOffice DOCX->PDF conversion timed out after {timeout}s."
            ) from exc
        except OSError as exc:  # pragma: no cover - environment specific
            raise PdfConversionError(f"LibreOffice could not be executed: {exc}") from exc

        produced = outdir / "input.pdf"
        details = (completed.stderr or completed.stdout or "").strip()
        if not produced.is_file():
            raise PdfConversionError(
                "LibreOffice did not produce a PDF "
                f"(exit code {completed.returncode}). {details[:800]}"
            )
        # A zero-byte or non-PDF file means the export filter failed half way
        # (typically a missing shared library on a relocated install); treat it
        # as a failure rather than handing the advocate a broken file.
        with produced.open("rb") as handle:
            header = handle.read(5)
        if header != b"%PDF-":
            raise PdfConversionError(
                "LibreOffice wrote a file that is not a PDF "
                f"(exit code {completed.returncode}). {details[:800]}"
            )
        if details:
            # javaldx warnings and similar noise: keep for debugging only.
            log.debug("LibreOffice output: %s", details[:800])

        shutil.move(str(produced), str(destination))

    return destination
