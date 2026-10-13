"""Audit which TLS provider wins each symbol in a real Jieli link.

Two TLS stacks meet on every Jieli image. TuyaOpen builds its own mbedTLS
3.1.0 into ``libtuyaos.a``; the vendor ships a second, partial mbedTLS 3.4.0 as
``libmbedtls_3_4_0.a`` (plus ``libcrypto_mbedtls.a`` on wl83) and links it from
its Wi-Fi archives. Whether the audited stack or the vendor's wins a given
``mbedtls_*`` symbol is decided by the linker, not by anything in the tree, so it
has to be read off a real link.

This tool reads that link and reports, read-only:

* **Vendor member extraction** -- which vendor archive members the linker pulled
  in, and which object pulled each one.
* **Public symbol ownership** -- for every ``mbedtls_*`` symbol in the link,
  which side holds the prevailing definition and from which member.
* **Cross-provider calls** -- vendor objects that reference ``mbedtls_*``
  symbols the TuyaOpen side defines.

Rules, from the prior audit:

* ``R1`` a stock ``mbedtls_*`` symbol whose prevailing definition comes from a
  vendor object, and which no allowlist group pins, is a **BLOCK**. A vendor
  extension that is *not* a stock mbedTLS symbol is reported but allowed: the
  stock table comes from ``src/libtls/mbedtls-3.1.0/include``, so "stock" means
  "upstream mbedTLS has this name".
* ``R2`` the same symbol prevailing on both sides is a **BLOCK**.
* ``R3`` a vendor object referencing an app-defined ``mbedtls_*`` is a
  **WARN** (a **BLOCK** under ``--strict``).
* ``R4`` an allowlist entry pins both the member and the symbol pattern, so a
  ``ssl_tls.c`` member cannot borrow ``net_sockets.c``'s allowance.

Inputs are the two products a Jieli build writes next to the ELF:

* ``sdk.elf.resolution.txt`` -- LLVM LTO resolution records, one
  ``-r=<object>,<symbol>,<flags>`` per object and symbol. ``p`` in the flags
  marks the prevailing definition; every other record for that symbol is a
  reference. It is the authority for "who defines this symbol".
* ``sdk.map`` -- the ``Archive member included to satisfy reference by file
  (symbol)`` section, which is the authority for "what pulled this member in".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

# Invoked as a script, only tools/jieli_build is on sys.path, so the platform
# root has to be added before the package imports below can resolve.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.jieli_build.errors import BuildError

PASS = "PASS"
INFO = "INFO"
WARN = "WARN"
BLOCK = "BLOCK"
SEVERITY_RANK = {PASS: 0, INFO: 1, WARN: 2, BLOCK: 3}

SCHEMA_VERSION = 1
RESOLUTION_NAME = "sdk.elf.resolution.txt"
MAP_NAME = "sdk.map"
STOCK_INCLUDE_RELATIVE = Path("src") / "libtls" / "mbedtls-3.1.0" / "include"
SYMBOL_PREFIX = "mbedtls_"

# Archives whose members provide or reach the vendor TLS stack. The member
# extraction table reports these in full; other archives are only counted.
TLS_PROVIDER_ARCHIVES = ("libmbedtls_3_4_0.a", "libcrypto_mbedtls.a")

# The link products embed the archive member in the object name as
# "<archive>.llvm.<id>.<member>"; the map writes it as "<archive>(<member>)"
# with a ".o" suffix. Both are normalised to (archive, member-without-.o).
_LLVM_MARKER = ".llvm."


@dataclass(frozen=True)
class ResolutionRecord:
    """One ``-r=`` record: an object, a symbol, and the LTO flags."""

    source: str
    symbol: str
    flags: str

    @property
    def prevailing(self) -> bool:
        return "p" in self.flags


@dataclass(frozen=True)
class ArchiveMember:
    """One ``<archive>(<member>)`` block from the map's inclusion section."""

    archive: str
    member: str
    referenced_by: tuple[str, ...]
    pulled_for: tuple[str, ...]


@dataclass(frozen=True)
class AllowlistEntry:
    """One pinned allowance: a symbol pattern *and* the member that may hold it.

    Pinning both is the point: ``mbedtls_net_*`` is allowed only from
    ``libmbedtls_3_4_0.a(net_sockets.c)``, so the same name appearing in
    ``ssl_tls.c`` is not covered by the group.
    """

    group: str
    pattern: "re.Pattern[str]"
    archive: Optional[str]
    member: str
    reason: str


ALLOWLIST: tuple[AllowlistEntry, ...] = (
    AllowlistEntry(
        group="A",
        pattern=re.compile(r"^mbedtls_net_"),
        archive="libmbedtls_3_4_0.a",
        member="net_sockets.c",
        reason="pure socket wrapper plus JieLi extensions",
    ),
    AllowlistEntry(
        group="B",
        pattern=re.compile(r"^mbedtls_test_"),
        archive="libmbedtls_3_4_0.a",
        member="certs.c",
        reason="const test certificate and key data blocks",
    ),
    AllowlistEntry(
        group="C",
        pattern=re.compile(r"^mbedtls_pk_write_pubkey_formatted$"),
        archive="libcrypto_mbedtls.a",
        member="crypto_mbedtls-ec.c",
        reason="JieLi private extension; upstream mbedTLS has no such symbol",
    ),
    AllowlistEntry(
        group="D",
        pattern=re.compile(r".*"),
        archive=None,
        member="version.z",
        reason="archive metadata member; carries only lib_*_version symbols",
    ),
)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    symbol: str
    detail: str


@dataclass(frozen=True)
class SymbolOwner:
    symbol: str
    side: str  # 'tuya' | 'vendor' | 'unknown'
    source: str
    archive: Optional[str]
    member: str
    allowlist_group: Optional[str]


@dataclass(frozen=True)
class CrossCall:
    """A vendor object referencing a symbol the TuyaOpen side defines."""

    symbol: str
    caller_source: str
    caller_archive: Optional[str]
    caller_member: str


@dataclass
class AuditResult:
    resolution_path: Path
    map_path: Optional[Path]
    stock_include: Optional[Path]
    owners: tuple[SymbolOwner, ...]
    vendor_members: tuple[ArchiveMember, ...]
    cross_calls: tuple[CrossCall, ...]
    findings: tuple[Finding, ...]
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        severity = PASS
        for finding in self.findings:
            if SEVERITY_RANK[finding.severity] > SEVERITY_RANK[severity]:
                severity = finding.severity
        return severity


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def normalise_member(name: str) -> str:
    """Strip the object suffix the map adds so both products name a member alike."""
    return name[:-2] if name.endswith(".o") else name


def split_archive_member(source: str) -> tuple[Optional[str], str]:
    """Return (archive, member) for an object name, or (None, name) for a bare object."""
    name = source.replace("\\", "/").rsplit("/", 1)[-1]
    if _LLVM_MARKER not in name:
        return None, normalise_member(name)
    archive, rest = name.split(_LLVM_MARKER, 1)
    _, _, member = rest.partition(".")
    return archive, normalise_member(member)


def provider_of(source: str) -> str:
    """Classify an object as TuyaOpen's own, the SDK's, or neither.

    The split follows what the object is *built from*: TuyaOpen's libraries and
    its TKL adapter are 'tuya'; the vendor's prebuilt archives and the vendor
    sources the staging tree compiles are 'vendor'. Anything else is 'unknown'
    and never decides a rule on its own.
    """
    path = source.replace("\\", "/")
    if "/liba/" in path or path.startswith("liba/"):
        return "vendor"
    if ".build/lib/" in path:
        return "tuya"
    for marker in ("objs/tuyaos_adapter/", "objs/tuyaos/", "objs/tuya_utilities/"):
        if marker in path or path.startswith(marker):
            return "tuya"
    if "/objs/" in path or path.startswith("objs/"):
        return "vendor"
    return "unknown"


def parse_resolution(path: Path) -> list[ResolutionRecord]:
    """Read the ``-r=`` records, ignoring the bare object-name lines."""
    records: list[ResolutionRecord] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("-r="):
            continue
        parts = line[3:].rsplit(",", 2)
        if len(parts) != 3:
            continue
        records.append(ResolutionRecord(parts[0], parts[1], parts[2]))
    if not records:
        raise BuildError(f"{path}: no -r= resolution records; is this an LTO resolution file?")
    return records


# A member line is not indented; the reference and bare-symbol lines under it
# are. Anchoring on a non-space first character keeps an indented
# "(lib_x_version)" metadata line from parsing as a member header.
_MEMBER_RE = re.compile(r"^(?P<archive>[^\s(][^(]*)\((?P<member>[^()]+)\)$")
_REF_RE = re.compile(r"^\s+(?P<ref>.+?)\s*\(symbol from plugin\)\s*\((?P<symbol>[^()]*)\)\s*$")
_BARE_SYMBOL_RE = re.compile(r"^\s+\((?P<symbol>[^()]*)\)\s*$")


def parse_map(path: Path) -> list[ArchiveMember]:
    """Read the map's 'Archive member included to satisfy reference by file' section."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    end = len(lines)
    for index, line in enumerate(lines):
        if line.startswith("Discarded input sections"):
            end = index
            break
    members: list[ArchiveMember] = []
    archive: Optional[str] = None
    member: Optional[str] = None
    refs: list[str] = []
    symbols: list[str] = []

    def flush() -> None:
        if archive is not None and member is not None:
            members.append(
                ArchiveMember(archive, normalise_member(member), tuple(refs), tuple(symbols))
            )

    for line in lines[1:end]:
        block = _MEMBER_RE.match(line)
        if block:
            flush()
            archive = block.group("archive").replace("\\", "/").rsplit("/", 1)[-1]
            member = block.group("member")
            refs, symbols = [], []
            continue
        if archive is None:
            continue
        reference = _REF_RE.match(line)
        if reference:
            refs.append(reference.group("ref").strip())
            symbols.append(reference.group("symbol"))
            continue
        bare = _BARE_SYMBOL_RE.match(line)
        if bare:
            symbols.append(bare.group("symbol"))
    flush()
    return members


_SYMBOL_RE = re.compile(r"\bmbedtls_[A-Za-z0-9_]+\b")


def load_stock_symbols(include_dir: Path) -> frozenset[str]:
    """Collect the upstream mbedTLS names TuyaOpen's headers declare.

    A vendor symbol in this set is a stock name the vendor should not be
    providing; a vendor symbol outside it is an extension, which the audit
    reports but allows.
    """
    if not include_dir.is_dir():
        raise BuildError(
            f"mbedTLS stock headers are missing: {include_dir}. Pass "
            f"--stock-include pointing at <TuyaOpen>/src/libtls/mbedtls-3.1.0/include."
        )
    symbols: set[str] = set()
    for header in sorted(include_dir.rglob("*.h")):
        symbols.update(_SYMBOL_RE.findall(header.read_text(encoding="utf-8", errors="replace")))
    if not symbols:
        raise BuildError(f"{include_dir}: no mbedtls_* names found in the stock headers")
    return frozenset(symbols)


def discover_stock_include(resolution_path: Path) -> Optional[Path]:
    """Walk up from the resolution file looking for TuyaOpen's mbedTLS headers."""
    for directory in (resolution_path.resolve(), *resolution_path.resolve().parents):
        candidate = directory / STOCK_INCLUDE_RELATIVE
        if candidate.is_dir():
            return candidate
    return None


def allowlist_entry_for(symbol: str, archive: Optional[str], member: str) -> Optional[AllowlistEntry]:
    """Return the allowlist group that covers this (symbol, member) pair.

    The pattern is matched with ``re.match``, so the prefix groups (``A`` and
    ``B``) cover a family while the fully anchored group ``C`` covers one name.
    """
    for entry in ALLOWLIST:
        if not entry.pattern.match(symbol):
            continue
        if entry.archive is not None and entry.archive != archive:
            continue
        if entry.member != member:
            continue
        return entry
    return None


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def _owners(
    records: Sequence[ResolutionRecord], stock: frozenset[str]
) -> dict[str, SymbolOwner]:
    owners: dict[str, SymbolOwner] = {}
    for record in records:
        if not record.prevailing or not record.symbol.startswith(SYMBOL_PREFIX):
            continue
        archive, member = split_archive_member(record.source)
        side = provider_of(record.source)
        entry = allowlist_entry_for(record.symbol, archive, member)
        owners[record.symbol] = SymbolOwner(
            symbol=record.symbol,
            side=side,
            source=record.source,
            archive=archive,
            member=member,
            allowlist_group=entry.group if entry else None,
        )
    return owners


def audit(
    resolution_path: Path,
    map_path: Optional[Path] = None,
    stock_include: Optional[Path] = None,
    strict: bool = False,
) -> AuditResult:
    """Run every rule over the two link products and return the findings."""
    resolution_path = Path(resolution_path)
    if not resolution_path.is_file():
        raise BuildError(f"resolution file not found: {resolution_path}")
    stock_include = stock_include or discover_stock_include(resolution_path)
    stock = load_stock_symbols(stock_include) if stock_include else frozenset()
    records = parse_resolution(resolution_path)
    owners = _owners(records, stock)

    findings: list[Finding] = []

    # R1 -- a stock symbol the vendor prevails is the vendor stack winning.
    for owner in sorted(owners.values(), key=lambda item: item.symbol):
        if owner.side != "vendor" or owner.allowlist_group is not None:
            continue
        if owner.symbol in stock:
            findings.append(
                Finding(
                    rule="R1",
                    severity=BLOCK,
                    symbol=owner.symbol,
                    detail=(
                        f"stock mbedTLS symbol is provided by the vendor object "
                        f"{owner.archive or owner.source}({owner.member})"
                    ),
                )
            )
        else:
            findings.append(
                Finding(
                    rule="R1",
                    severity=INFO,
                    symbol=owner.symbol,
                    detail=(
                        f"vendor extension (not a stock mbedTLS name) from "
                        f"{owner.archive or owner.source}({owner.member})"
                    ),
                )
            )

    # R2 -- one prevailing definition per symbol is an LTO invariant. Two sides
    # prevailing would mean both providers define it, which cannot link.
    seen: dict[str, tuple[str, str]] = {}
    for record in records:
        if not record.prevailing or not record.symbol.startswith(SYMBOL_PREFIX):
            continue
        side = provider_of(record.source)
        previous = seen.get(record.symbol)
        if previous is None:
            seen[record.symbol] = (side, record.source)
            continue
        if previous[0] != side:
            findings.append(
                Finding(
                    rule="R2",
                    severity=BLOCK,
                    symbol=record.symbol,
                    detail=(
                        f"prevailing definition on both sides: {previous[0]} "
                        f"({split_archive_member(previous[1])[1]}) and {side} "
                        f"({split_archive_member(record.source)[1]})"
                    ),
                )
            )

    # R3 -- vendor objects referencing symbols the TuyaOpen side defines.
    cross_calls: list[CrossCall] = []
    for record in records:
        if record.prevailing or not record.symbol.startswith(SYMBOL_PREFIX):
            continue
        owner = owners.get(record.symbol)
        if owner is None or owner.side != "tuya":
            continue
        if provider_of(record.source) != "vendor":
            continue
        archive, member = split_archive_member(record.source)
        cross_calls.append(CrossCall(record.symbol, record.source, archive, member))
    cross_calls.sort(key=lambda call: (call.caller_archive or "", call.caller_member, call.symbol))

    findings.extend(
        Finding(
            rule="R3",
            severity=BLOCK if strict else WARN,
            symbol=call.symbol,
            detail=(
                f"vendor object {call.caller_archive or ''}({call.caller_member}) "
                f"references a symbol defined by the TuyaOpen side"
            ),
        )
        for call in cross_calls
    )

    vendor_members: tuple[ArchiveMember, ...] = ()
    if map_path is not None:
        map_path = Path(map_path)
        if not map_path.is_file():
            raise BuildError(f"map file not found: {map_path}")
        vendor_members = tuple(
            member
            for member in parse_map(map_path)
            if member.archive in TLS_PROVIDER_ARCHIVES
        )

    counts = {
        "records": len(records),
        "mbedtls_symbols": len(owners),
        "tuya_prevailing": sum(1 for owner in owners.values() if owner.side == "tuya"),
        "vendor_prevailing": sum(1 for owner in owners.values() if owner.side == "vendor"),
        "allowlisted": sum(1 for owner in owners.values() if owner.allowlist_group),
        "cross_calls": len(cross_calls),
        "block": sum(1 for finding in findings if finding.severity == BLOCK),
        "warn": sum(1 for finding in findings if finding.severity == WARN),
        "info": sum(1 for finding in findings if finding.severity == INFO),
    }
    return AuditResult(
        resolution_path=resolution_path,
        map_path=map_path,
        stock_include=stock_include,
        owners=tuple(sorted(owners.values(), key=lambda item: item.symbol)),
        vendor_members=vendor_members,
        cross_calls=tuple(cross_calls),
        findings=tuple(findings),
        counts=counts,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _rows(items: Sequence[object], limit: int) -> list[object]:
    return list(items) if limit <= 0 else list(items[:limit])


def render(result: AuditResult, max_rows: int = 0) -> str:
    """Render the verdict, the three tables and the finding list."""
    lines: list[str] = []
    counts = result.counts
    lines.append(f"verdict: {result.verdict}")
    lines.append(f"resolution: {result.resolution_path}")
    lines.append(f"map: {result.map_path if result.map_path else '(not supplied)'}")
    lines.append(f"stock symbols: {result.stock_include if result.stock_include else '(not found)'}")
    lines.append(
        "counts: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    lines.append("")

    lines.append("== table 1: vendor member extraction ==")
    if result.map_path is None:
        lines.append("  (no sdk.map supplied)")
    elif not result.vendor_members:
        lines.append("  no members of the TLS provider archives were extracted")
    else:
        for member in _rows(result.vendor_members, max_rows):
            assert isinstance(member, ArchiveMember)
            pulled = ", ".join(sorted(set(member.pulled_for))) or "-"
            lines.append(f"  {member.archive}({member.member})")
            lines.append(f"      pulled for: {pulled}")
            for reference in sorted(set(member.referenced_by)):
                lines.append(f"      referenced by: {reference}")
        lines.append(f"  ({len(result.vendor_members)} members)")
    lines.append("")

    lines.append("== table 2: public symbol ownership ==")
    for owner in _rows(result.owners, max_rows):
        assert isinstance(owner, SymbolOwner)
        group = f" allowlist={owner.allowlist_group}" if owner.allowlist_group else ""
        lines.append(
            f"  {owner.side:8s} {owner.symbol:44s} "
            f"{owner.archive or owner.source}({owner.member}){group}"
        )
    lines.append(f"  ({len(result.owners)} mbedtls_* symbols)")
    lines.append("")

    lines.append("== table 3: cross-provider calls (vendor -> TuyaOpen) ==")
    for call in _rows(result.cross_calls, max_rows):
        assert isinstance(call, CrossCall)
        lines.append(
            f"  {call.caller_archive or ''}({call.caller_member}) -> {call.symbol}"
        )
    lines.append(f"  ({len(result.cross_calls)} calls)")
    lines.append("")

    lines.append("== findings ==")
    if not result.findings:
        lines.append("  none")
    else:
        for finding in result.findings:
            lines.append(
                f"  {finding.severity:5s} {finding.rule} {finding.symbol}: {finding.detail}"
            )
    return "\n".join(lines)


def snapshot(result: AuditResult) -> dict[str, object]:
    """The machine-readable state a later run can diff against."""
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": result.verdict,
        "resolution": str(result.resolution_path),
        "symbols": {
            owner.symbol: {
                "side": owner.side,
                "member": f"{owner.archive}({owner.member})" if owner.archive else owner.member,
                "allowlist": owner.allowlist_group,
            }
            for owner in result.owners
        },
        "cross_calls": sorted(
            f"{call.caller_archive or ''}({call.caller_member}) -> {call.symbol}"
            for call in result.cross_calls
        ),
        "vendor_members": sorted(
            f"{member.archive}({member.member})" for member in result.vendor_members
        ),
    }


def diff(baseline: Mapping[str, object], current: Mapping[str, object]) -> str:
    """Report what changed between two snapshots, so drift is visible."""
    lines: list[str] = ["== diff vs baseline =="]
    before = dict(baseline.get("symbols") or {})
    after = dict(current.get("symbols") or {})

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        symbol
        for symbol in set(before) & set(after)
        if before[symbol] != after[symbol]
    )
    for symbol in added:
        lines.append(f"  + symbol {symbol}: {after[symbol]}")
    for symbol in removed:
        lines.append(f"  - symbol {symbol}: {before[symbol]}")
    for symbol in changed:
        lines.append(f"  ~ symbol {symbol}: {before[symbol]} -> {after[symbol]}")

    before_calls = set(baseline.get("cross_calls") or ())
    after_calls = set(current.get("cross_calls") or ())
    for call in sorted(after_calls - before_calls):
        lines.append(f"  + cross-call {call}")
    for call in sorted(before_calls - after_calls):
        lines.append(f"  - cross-call {call}")

    before_members = set(baseline.get("vendor_members") or ())
    after_members = set(current.get("vendor_members") or ())
    for member in sorted(after_members - before_members):
        lines.append(f"  + vendor-member {member}")
    for member in sorted(before_members - after_members):
        lines.append(f"  - vendor-member {member}")

    if len(lines) == 1:
        lines.append("  no changes")
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_jieli_provider_map.py",
        description=(
            "Audit which TLS provider wins each mbedtls_* symbol in a Jieli link. "
            "Read-only: it opens the build's resolution file and map and writes nothing."
        ),
    )
    parser.add_argument(
        "--resolution",
        required=True,
        help=f"path to {RESOLUTION_NAME} from a Jieli build",
    )
    parser.add_argument("--map", dest="map_path", default=None, help=f"path to {MAP_NAME}")
    parser.add_argument(
        "--stock-include",
        default=None,
        help=(
            "TuyaOpen mbedtls-3.1.0 include directory used for the stock symbol "
            "table (default: discovered by walking up from --resolution)"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="promote the R3 cross-provider warnings to BLOCK",
    )
    parser.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    parser.add_argument("--snapshot", default=None, help="write a snapshot JSON here")
    parser.add_argument("--baseline", default=None, help="diff against this snapshot JSON")
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="cap the rows printed per table (0 = no cap)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = audit(
            Path(args.resolution),
            Path(args.map_path) if args.map_path else None,
            Path(args.stock_include) if args.stock_include else None,
            strict=args.strict,
        )
    except BuildError as error:
        print(f"check_jieli_provider_map: {error}", file=sys.stderr)
        return 2

    current = snapshot(result)
    if args.snapshot:
        Path(args.snapshot).write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(current, indent=2))
    else:
        print(render(result, max_rows=args.max_rows))
        if args.baseline and Path(args.baseline).is_file():
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            print()
            print(diff(baseline, current))
    return 1 if result.verdict == BLOCK else 0


if __name__ == "__main__":
    sys.exit(main())
