"""Source inventory (instruction v2, section 2.3).

Lists candidate material with format, size, SHA-256 and whether text can be
extracted, sorts it into six classes, and records whether it may be sent out
(embeddings and Gemini are external sends). Only public statutes are indexed.
Meeting minutes (including the public assembly dataset), QA answer files and
fictional data are listed and kept out of the index. Originals are read in
place and never modified; zip archives are unpacked only through
`safe_extract`.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from .parse_law import EFFECTIVE, VERSION, annex_header, read_pdf_lines

CLASSES = (
    "법령·행정규칙",
    "기관 규정·업무 안내문",
    "별표·서식",
    "회의록",
    "QA 예제·가상 자료",
    "손상·미지원·권한 미확인",
)
TEXT_SUFFIXES = (".txt", ".md", ".html", ".htm", ".xml", ".json")
MAX_RATIO = 100  # uncompressed / compressed, per member
MAX_TOTAL = 200 * 1024 * 1024


@dataclass
class InventoryEntry:
    path: str  # relative to the folder it was found in, prefixed with that folder's label
    format: str
    size: int
    sha256: str | None
    text_extractable: str  # "예 (14/14쪽)", "아니오", "해당 없음"
    category: str
    external_send: str  # "허가(공개 법령)", "보류(출처·권한 불명)", "해당 없음(색인 제외)"
    indexed: bool
    reason: str
    title: str | None = None
    effective_date: str | None = None
    version_label: str | None = None
    notes: list[str] = field(default_factory=list)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def classify_file(path: Path, label: str) -> InventoryEntry:
    suffix = path.suffix.lower()
    base = dict(
        path=f"{label}/{path.name}",
        format=suffix.lstrip(".") or "unknown",
        size=path.stat().st_size,
        sha256=sha256_of(path),
    )
    if suffix == ".pdf":
        try:
            lines, pages = read_pdf_lines(path)
        except Exception as exc:  # unreadable or encrypted PDF
            return InventoryEntry(
                **base,
                text_extractable="아니오",
                category=CLASSES[5],
                external_send="보류(출처·권한 불명)",
                indexed=False,
                reason=f"PDF를 읽지 못함({type(exc).__name__})",
            )
        with_text = len({line.pdf_page for line in lines if line.text.strip() and not line.image})
        extractable = f"예 ({with_text}/{pages}쪽)" if with_text else "아니오"
        head = "\n".join(line.text for line in lines[:12])
        version, effective = VERSION.search(head), EFFECTIVE.search(head)
        title = next((line.text.strip() for line in lines if line.text.strip()), None)
        annex = annex_header(lines)
        if annex:
            label = f"{annex.group('law').strip()} [{annex.group('kind')} {annex.group('no')}]"
            chars = sum(len(line.text.strip()) for line in lines if not line.image)
            if chars < 40 or any(line.image for line in lines):
                return InventoryEntry(
                    **base,
                    text_extractable=extractable,
                    category=CLASSES[2],
                    external_send="허가(공개 법령 별표)",
                    indexed=False,
                    reason="이미지 별표 — 옮겨 적지 않고 미색인",
                    title=label,
                )
            return InventoryEntry(
                **base,
                text_extractable=extractable,
                category=CLASSES[2],
                external_send="허가(공개 법령 별표)",
                indexed=True,
                reason="공개 법령의 별표(글자 추출 가능)",
                title=annex.group("law").strip(),
                version_label=f"[{annex.group('kind')} {annex.group('no')}]"
                + (f" <{annex.group('note')}>" if annex.group("note") else ""),
                notes=["별표 파일 — 시행일은 적혀 있지 않음(미확인)"],
            )
        if not with_text:
            return InventoryEntry(
                **base,
                text_extractable=extractable,
                category=CLASSES[5],
                external_send="보류(출처·권한 불명)",
                indexed=False,
                reason="텍스트 없음(스캔 PDF로 보임, OCR하지 않음)",
            )
        if version:
            effective_date = (
                f"{effective.group(1)}-{int(effective.group(2)):02d}-{int(effective.group(3)):02d}"
                if effective
                else None
            )
            notes = ["법제처 국가법령정보센터 인쇄본(머리말 '법제처 n 국가법령정보센터' 제거)"]
            if any(line.image for line in lines):
                notes.append("이미지로 된 표가 있음(OCR하지 않고 미색인)")
            return InventoryEntry(
                **base,
                text_extractable=extractable,
                category=CLASSES[0],
                external_send="허가(공개 법령)",
                indexed=True,
                reason="공개 법령 원문",
                title=title,
                effective_date=effective_date,
                version_label=" ".join(version.group(1).split()),
                notes=notes,
            )
        return InventoryEntry(
            **base,
            text_extractable=extractable,
            category=CLASSES[1],
            external_send="보류(출처·권한 불명)",
            indexed=False,
            reason="출처·외부 전송 권한을 확인하지 못함",
            title=title,
        )
    if suffix in TEXT_SUFFIXES:
        return InventoryEntry(
            **base,
            text_extractable="예",
            category=CLASSES[1],
            external_send="보류(출처·권한 불명)",
            indexed=False,
            reason="출처·외부 전송 권한을 확인하지 못함",
        )
    return InventoryEntry(
        **base,
        text_extractable="아니오",
        category=CLASSES[5],
        external_send="보류(출처·권한 불명)",
        indexed=False,
        reason="지원하지 않는 형식",
    )


def repo_material(repo_root: Path) -> list[InventoryEntry]:
    """Material already in the repository that must stay out of the regulation index."""
    entries: list[InventoryEntry] = []
    data = repo_root / "data"

    def add(path: Path, category: str, reason: str, send: str = "해당 없음(색인 제외)") -> None:
        if path.is_file():
            entries.append(
                InventoryEntry(
                    path=str(PurePosixPath(path.relative_to(repo_root))),
                    format=path.suffix.lstrip("."),
                    size=path.stat().st_size,
                    sha256=sha256_of(path),
                    text_extractable="예",
                    category=category,
                    external_send=send,
                    indexed=False,
                    reason=reason,
                )
            )

    for sample in sorted((data / "samples").glob("*")):
        add(sample, CLASSES[3], "가상 회의록 — 회의 기능 전용")
    for gold in sorted((data / "eval").glob("*")):
        add(gold, CLASSES[4], "회의 추출 평가 정답 — 평가 폴더 전용")
    add(data / "roster.yaml", CLASSES[4], "가상 명단 — 회의 기능 전용")
    for evaluation in sorted((data / "regulations" / "eval").glob("*")):
        add(evaluation, CLASSES[4], "규정 Q&A 평가 질문·기대 근거 — 평가 전용, 색인 금지")
    external = data / "external"
    if external.is_dir():
        files = [p for p in external.rglob("*") if p.is_file()]
        entries.append(
            InventoryEntry(
                path="data/external/ (요약)",
                format="여러 형식",
                size=sum(p.stat().st_size for p in files),
                sha256=None,
                text_extractable="예",
                category=CLASSES[3],
                external_send="해당 없음(색인 제외)",
                indexed=False,
                reason=f"공개 국회 회의록 데이터셋 발췌·라벨 {len(files)}개 — 회의 평가 전용, 파일명은 적지 않음",
            )
        )
    state = data / "state.db"
    if state.is_file():
        entries.append(
            InventoryEntry(
                path="data/state.db",
                format="sqlite",
                size=state.stat().st_size,
                sha256=None,
                text_extractable="해당 없음",
                category=CLASSES[5],
                external_send="해당 없음(색인 제외)",
                indexed=False,
                reason="업무 상태 DB — 자료 아님, 읽지 않음",
            )
        )
    return entries


def transcription_entries(repo_root: Path) -> list[InventoryEntry]:
    """Tables a person or agent typed from images in a public statute (data/manual/tables/, committed)."""
    entries = []
    for path in sorted((repo_root / "data" / "manual" / "tables").glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        reviewed = "검수 완료" in text.split("---", 2)[1] if text.startswith("---") else False
        entries.append(
            InventoryEntry(
                path=str(PurePosixPath(path.relative_to(repo_root))),
                format="txt",
                size=path.stat().st_size,
                sha256=sha256_of(path),
                text_extractable="예",
                category=CLASSES[0],
                external_send="허가(공개 법령)",
                indexed=True,
                reason="공개 법령의 이미지 표를 옮겨 적은 전사본" + ("(검수 완료)" if reviewed else "(사람 검수 전)"),
                notes=["manual_transcription"],
            )
        )
    return entries


def build_inventory(source_dirs: dict[str, Path], repo_root: Path) -> dict:
    entries: list[InventoryEntry] = []
    missing: list[str] = []
    for label, folder in source_dirs.items():
        if not folder.is_dir():
            missing.append(label)
            continue
        for path in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")):
            entries.append(classify_file(path, label))
    entries += transcription_entries(repo_root)
    entries += repo_material(repo_root)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "classes": list(CLASSES),
        "source_folders": {label: ("있음" if label not in missing else "없음") for label in source_dirs},
        "entries": [asdict(entry) for entry in entries],
    }


def write_inventory(inventory: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class UnsafeArchive(ValueError):
    """The archive tries to write outside the target, uses links, or is a zip bomb."""


def safe_extract(archive: Path, target: Path) -> list[Path]:
    """Unpack a zip into `target` after refusing path escapes, absolute paths, links and bombs."""
    target = target.resolve()
    written: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        total = 0
        for info in zf.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or info.filename.startswith(("/", "\\")) or ":" in name.parts[0]:
                raise UnsafeArchive(f"절대 경로: {info.filename}")
            if ".." in name.parts:
                raise UnsafeArchive(f"경로 이탈: {info.filename}")
            mode = info.external_attr >> 16
            if (mode & 0o170000) == 0o120000:
                raise UnsafeArchive(f"심볼릭 링크: {info.filename}")
            total += info.file_size
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                raise UnsafeArchive(f"비정상 압축 비율: {info.filename}")
            if total > MAX_TOTAL:
                raise UnsafeArchive("압축 해제 크기가 너무 큽니다.")
            destination = (target / name).resolve()
            if target not in destination.parents and destination != target:
                raise UnsafeArchive(f"경로 이탈: {info.filename}")
        for info in zf.infolist():
            destination = (target / PurePosixPath(info.filename)).resolve()
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, destination.open("wb") as dst:
                dst.write(src.read())
            written.append(destination)
    return written
