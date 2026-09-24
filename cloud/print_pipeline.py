# [Modification] Generate SGray_8 PWG Raster for AirPrint/IPP printers.

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
PRINT_READY_DIR = STORAGE_DIR / "print_ready"
CONVERTER_CONFIG_PATH = Path(
    os.environ.get("PRINT_CONVERTER_CONFIG", str(BASE_DIR / "printer_profiles.local.json"))
)


@dataclass(frozen=True)
class PrintProfile:
    profile_id: str
    display_name: str
    output_suffix: str
    print_format: str
    command: str | None
    direct_passthrough_extensions: frozenset[str] = frozenset()


BUILTIN_PROFILES: dict[str, PrintProfile] = {
    "raw_passthrough": PrintProfile(
        profile_id="raw_passthrough",
        display_name="Raw passthrough for already printable files",
        output_suffix=".print",
        print_format="raw",
        command=None,
        direct_passthrough_extensions=frozenset({".pdf", ".jpg", ".jpeg", ".png", ".ps", ".pcl", ".prn", ".xqx"}),
    ),
    "pdf_passthrough": PrintProfile(
        profile_id="pdf_passthrough",
        display_name="PDF passthrough for PDF-capable IPP printers",
        output_suffix=".pdf",
        print_format="pdf",
        command=None,
        direct_passthrough_extensions=frozenset({".pdf"}),
    ),
    "hp_laserjet_p1005_xqx": PrintProfile(
        profile_id="hp_laserjet_p1005_xqx",
        display_name="HP LaserJet P1005/P1006 XQX stream",
        output_suffix=".xqx",
        print_format="xqx",
        # 实际部署时建议在 printer_profiles.local.json 里覆盖此命令。
        command=(
            "gs -dBATCH -dSAFER -dNOPAUSE -sDEVICE=pbmraw -r600 "
            "-sOutputFile=- \"{source}\" | foo2xqx -r600 -g{width_px}x{height_px} > \"{output}\""
        ),
    ),
    "pwg_raster": PrintProfile(
        profile_id="pwg_raster",
        display_name="PWG Raster for IPP Everywhere/AirPrint printers",
        output_suffix=".pwg",
        print_format="pwg_raster",
        # OpenCloudOS 可能缺少 gstoraster，直接用 Ghostscript 生成 CUPS Raster 后交给 rastertopwg。
        command=(
            "tmp=\"{output}.cups\"; "
            "gs -dBATCH -dSAFER -dNOPAUSE -sDEVICE=cups -r{dpi} -sOutputFile=\"$tmp\" "
            "-c \"<</cupsColorSpace 18/cupsBitsPerColor 8/cupsColorOrder 0>>setpagedevice\" "
            "-f \"{source}\" "
            "&& FINAL_CONTENT_TYPE=image/pwg-raster /usr/lib/cups/filter/rastertopwg "
            "1 root \"{source}\" 1 \"media={paper_size} print-color-mode=monochrome printer-resolution={dpi}dpi\" "
            "\"$tmp\" > \"{output}\"; "
            "status=$?; rm -f \"$tmp\"; exit $status"
        ),
    ),
    "urf": PrintProfile(
        profile_id="urf",
        display_name="Apple AirPrint URF raster",
        output_suffix=".urf",
        print_format="urf",
        command=(
            "cupsfilter -m image/urf "
            "-o media={paper_size} -o print-color-mode=monochrome "
            "-o printer-resolution={dpi}dpi \"{source}\" > \"{output}\""
        ),
    ),
    "pclm": PrintProfile(
        profile_id="pclm",
        display_name="PCLm for Mopria/HP printers",
        output_suffix=".pclm",
        print_format="pclm",
        command=(
            "cupsfilter -m application/PCLm "
            "-o media={paper_size} -o print-color-mode=monochrome "
            "-o printer-resolution={dpi}dpi \"{source}\" > \"{output}\""
        ),
    ),
}


class ConversionError(RuntimeError):
    pass


def load_profiles() -> dict[str, PrintProfile]:
    profiles = dict(BUILTIN_PROFILES)
    if not CONVERTER_CONFIG_PATH.exists():
        return profiles

    with CONVERTER_CONFIG_PATH.open("r", encoding="utf-8") as source:
        payload = json.load(source)

    for item in payload.get("profiles", []):
        profile_id = str(item["profile_id"]).strip()
        profiles[profile_id] = PrintProfile(
            profile_id=profile_id,
            display_name=str(item.get("display_name") or profile_id),
            output_suffix=str(item.get("output_suffix") or ".print"),
            print_format=str(item.get("print_format") or profile_id),
            command=str(item["command"]) if item.get("command") else None,
            direct_passthrough_extensions=frozenset(
                str(ext).lower() for ext in item.get("direct_passthrough_extensions", [])
            ),
        )
    return profiles


def get_profile(profile_id: str) -> PrintProfile:
    profiles = load_profiles()
    if profile_id not in profiles:
        raise ConversionError(f"Unknown printer profile: {profile_id}")
    return profiles[profile_id]


def convert_to_print_ready(
    *,
    source_path: Path,
    job_id: str,
    profile_id: str,
    options: dict,
) -> tuple[Path, str]:
    profile = get_profile(profile_id)
    extension = source_path.suffix.lower()
    output_path = PRINT_READY_DIR / f"{job_id}{profile.output_suffix}"
    PRINT_READY_DIR.mkdir(parents=True, exist_ok=True)

    if extension in profile.direct_passthrough_extensions and not profile.command:
        output_path.write_bytes(source_path.read_bytes())
        return output_path, profile.print_format

    if not profile.command:
        raise ConversionError(
            f"Profile {profile.profile_id} cannot convert {extension}; configure a converter command"
        )

    command_text = profile.command.format(
        source=str(source_path),
        output=str(output_path),
        paper_size=str(options.get("paper_size", "A4")),
        copies=int(options.get("copies", 1)),
        color="color" if options.get("color", True) else "mono",
        duplex="duplex" if options.get("duplex", False) else "simplex",
        width_px=paper_width_px(str(options.get("paper_size", "A4")), int(options.get("dpi", 600))),
        height_px=paper_height_px(str(options.get("paper_size", "A4")), int(options.get("dpi", 600))),
        dpi=int(options.get("dpi", 600)),
    )

    # 转换器通常是 Ghostscript/LibreOffice/foo2xqx 组合，允许使用管道和重定向。
    result = subprocess.run(
        command_text,
        shell=True,
        capture_output=True,
        text=True,
        timeout=int(options.get("convert_timeout_seconds", 300)),
    )
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise ConversionError(f"Convert failed for {profile.profile_id}: {detail}")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError(f"Converter did not create output for {profile.profile_id}")
    return output_path, profile.print_format


def paper_width_px(paper_size: str, dpi: int) -> int:
    width_mm, _ = paper_size_mm(paper_size)
    return round(width_mm / 25.4 * dpi)


def paper_height_px(paper_size: str, dpi: int) -> int:
    _, height_mm = paper_size_mm(paper_size)
    return round(height_mm / 25.4 * dpi)


def paper_size_mm(paper_size: str) -> tuple[float, float]:
    normalized = paper_size.upper()
    if normalized == "LETTER":
        return 215.9, 279.4
    return 210.0, 297.0
