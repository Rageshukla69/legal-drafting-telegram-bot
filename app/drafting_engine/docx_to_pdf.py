"""Canonical DOCX -> PDF conversion.

The legal renderer produces the canonical DOCX with python-docx and converts
that exact finalized DOCX to PDF with LibreOffice in headless mode, so both
files share one layout engine, identical line wrapping, identical pagination,
and a genuine searchable Unicode Devanagari text layer.

Deployment & Heroku Support
---------------------------
* Automatically detects LibreOffice (`soffice`) across standard Linux/macOS
  paths AND Heroku `heroku-community/apt` buildpack paths (`/app/.apt/...`).
* Automatically configures `LD_LIBRARY_PATH` and `PATH` when running under
  Heroku's `/app/.apt` environment so shared libraries load without error.
* Stages all bundled TrueType fonts (`NotoSansDevanagari-Regular.ttf`,
  `NotoSansDevanagari-Bold.ttf`, `DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`) into
  an isolated temporary directory and pins `FONTCONFIG_FILE` so rendering is
  100% deterministic across machines and never produces `.notdef` tofu boxes.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_ASSETS_FONTS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"

# Where LibreOffice is commonly installed across Linux, Heroku (/app/.apt), and macOS.
_COMMON_BINARIES = (
    "/app/.apt/usr/bin/soffice",
    "/app/.apt/usr/bin/libreoffice",
    "/app/.apt/usr/lib/libreoffice/program/soffice",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/lib/libreoffice/program/soffice",
    "/opt/libreoffice/program/soffice",
    "/opt/libreoffice6.4/program/soffice",
    "/snap/bin/libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)

_DEFAULT_TIMEOUT = 300


class PdfConversionError(RuntimeError):
    """The DOCX existed but could not be converted to PDF."""


class PdfConverterUnavailable(PdfConversionError):
    """No LibreOffice binary is available to perform the conversion."""


def find_soffice() -> str | None:
    """Return the LibreOffice binary to use, or None when it is not installed.

    `SOFFICE_BIN` always wins when configured. Otherwise searches PATH, Heroku
    `/app/.apt` locations, and standard system locations.
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
    for pattern in ("/opt/libreoffice*/program/soffice", "/app/.apt/opt/libreoffice*/program/soffice"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def converter_available() -> bool:
    return find_soffice() is not None


def bundled_font_files() -> list[Path]:
    """Return the TrueType fonts shipped with this repository."""
    if not _ASSETS_FONTS_DIR.is_dir():
        return []
    return sorted(_ASSETS_FONTS_DIR.glob("*.ttf"))


def prepare_fonts(work_dir: Path) -> str | None:
    """Stage the bundled fonts and return a pinned `FONTCONFIG_FILE`."""
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


def _augment_heroku_apt_env(env: dict[str, str]) -> None:
    """Ensure Heroku `/app/.apt` binaries and shared libraries are visible."""
    apt_root = Path("/app/.apt")
    if not apt_root.is_dir():
        return
    apt_bins = [
        str(apt_root / "usr" / "bin"),
        str(apt_root / "usr" / "local" / "bin"),
        str(apt_root / "usr" / "lib" / "libreoffice" / "program"),
    ]
    existing_path = env.get("PATH", "")
    env["PATH"] = ":".join([p for p in apt_bins if Path(p).is_dir()] + ([existing_path] if existing_path else []))

    apt_libs = [
        str(apt_root / "usr" / "lib" / "x86_64-linux-gnu"),
        str(apt_root / "usr" / "lib" / "i386-linux-gnu"),
        str(apt_root / "usr" / "lib"),
        str(apt_root / "lib" / "x86_64-linux-gnu"),
        str(apt_root / "lib"),
        str(apt_root / "usr" / "lib" / "libreoffice" / "program"),
    ]
    existing_ld = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join([p for p in apt_libs if Path(p).is_dir()] + ([existing_ld] if existing_ld else []))


def convert(docx_path: str | Path, pdf_path: str | Path, timeout: int | None = None) -> Path:
    """Convert `docx_path` to `pdf_path` using headless LibreOffice."""
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

        work = tmp / "work"
        work.mkdir()
        staged_docx = work / "input.docx"
        shutil.copy2(source, staged_docx)
        outdir = work / "out"
        outdir.mkdir()

        profile = tmp / "profile"
        profile.mkdir()

        env = dict(os.environ)
        _augment_heroku_apt_env(env)
        env["HOME"] = str(home)
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_CACHE_HOME"] = str(home / ".cache")
        if fontconfig:
            env["FONTCONFIG_FILE"] = fontconfig
        env.pop("SAL_USE_VCLPLUGIN", None)
        env.pop("DISPLAY", None)

        command = [
            soffice,
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
            str(staged_docx),
        ]

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
        if not produced.is_file():
            details = (completed.stderr or completed.stdout or "").strip()
            raise PdfConversionError(
                "LibreOffice did not produce a PDF "
                f"(exit code {completed.returncode}). {details[:800]}"
            )

        shutil.move(str(produced), str(destination))

    return destination
