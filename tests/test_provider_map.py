"""Exercise the TLS provider audit on versioned excerpts and synthetic inputs.

The fixtures are real excerpts of an sdk.elf.resolution.txt and an sdk.map from
a wl82 switch_demo build, with the absolute build paths scrubbed. No test reads
a committed SDK binary or a build tree, so the suite stays runnable without a
build.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tools.jieli_build.check_jieli_provider_map import (
    BLOCK,
    INFO,
    WARN,
    AllowlistEntry,
    ALLOWLIST,
    ArchiveMember,
    audit,
    diff,
    discover_stock_include,
    load_stock_symbols,
    parse_map,
    parse_resolution,
    provider_of,
    render,
    snapshot,
    split_archive_member,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RESOLUTION_FIXTURE = FIXTURES / "sdk_resolution_excerpt.txt"
MAP_FIXTURE = FIXTURES / "sdk_map_excerpt.txt"
STOCK_FIXTURE = FIXTURES / "stock_include"


def write_resolution(directory: Path, records: tuple[str, ...]) -> Path:
    """Write a synthetic resolution file from ``-r=`` lines."""
    path = directory / "sdk.elf.resolution.txt"
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


class ResolutionParsingTest(unittest.TestCase):
    def test_records_are_read_with_their_flags(self):
        records = parse_resolution(RESOLUTION_FIXTURE)
        prevailing = {record.symbol: record for record in records if record.prevailing}
        # Exactly one prevailing definition per symbol is the LTO invariant.
        self.assertEqual(
            len(prevailing), len({record.symbol for record in records if record.prevailing})
        )
        self.assertIn("mbedtls_ssl_write", prevailing)
        self.assertIn("mbedtls_net_connect", prevailing)

    def test_a_file_without_records_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "empty.txt"
            path.write_text("not a resolution file\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "no -r= resolution records"):
                parse_resolution(path)


class ProviderClassificationTest(unittest.TestCase):
    def test_archive_member_splitting(self):
        self.assertEqual(
            split_archive_member(
                "../../../../../cpu/wl82/liba/libmbedtls_3_4_0.a.llvm.1267558.net_sockets.c"
            ),
            ("libmbedtls_3_4_0.a", "net_sockets.c"),
        )
        self.assertEqual(
            split_archive_member("objs/apps/common/net/wifi_conf.c.o"),
            (None, "wifi_conf.c"),
        )

    def test_side_classification(self):
        self.assertEqual(provider_of("../../../../../cpu/wl82/liba/wl_wifi_sta.a.llvm.1.a.c"), "vendor")
        self.assertEqual(
            provider_of("<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.ssl_msg.c"), "tuya"
        )
        self.assertEqual(provider_of("objs/tuyaos_adapter/src/driver/tkl_wifi.c.o"), "tuya")
        self.assertEqual(provider_of("objs/apps/common/net/wifi_conf.c.o"), "vendor")
        self.assertEqual(provider_of("something/else.c.o"), "unknown")


class AllowlistTest(unittest.TestCase):
    def test_the_four_groups_pin_member_and_pattern(self):
        self.assertEqual([entry.group for entry in ALLOWLIST], ["A", "B", "C", "D"])
        for entry in ALLOWLIST:
            self.assertIsInstance(entry, AllowlistEntry)
            self.assertTrue(entry.member)
            self.assertTrue(entry.reason)

    def test_a_member_cannot_borrow_another_groups_allowance(self):
        # R4: net_sockets.c is allowed; the same symbol from ssl_tls.c is not.
        from tools.jieli_build.check_jieli_provider_map import allowlist_entry_for

        self.assertIsNotNone(
            allowlist_entry_for("mbedtls_net_connect", "libmbedtls_3_4_0.a", "net_sockets.c")
        )
        self.assertIsNone(
            allowlist_entry_for("mbedtls_net_connect", "libmbedtls_3_4_0.a", "ssl_tls.c")
        )
        self.assertIsNone(
            allowlist_entry_for("mbedtls_net_connect", "libcrypto_mbedtls.a", "net_sockets.c")
        )


class AuditRuleTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)

    def _audit(self, records, strict=False):
        resolution = write_resolution(self.directory, records)
        return audit(resolution, stock_include=STOCK_FIXTURE, strict=strict)

    def test_r1_blocks_a_stock_symbol_the_vendor_provides(self):
        result = self._audit(
            (
                "-r=../../../../../cpu/wl82/liba/libmbedtls_3_4_0.a.llvm.9.ssl_tls.c,mbedtls_ssl_write,pl",
                "-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.ssl_msg.c,mbedtls_ssl_write,l",
            )
        )
        self.assertEqual(result.verdict, BLOCK)
        blocks = [finding for finding in result.findings if finding.severity == BLOCK]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].rule, "R1")
        self.assertEqual(blocks[0].symbol, "mbedtls_ssl_write")

    def test_r1_allows_the_allowlisted_socket_wrapper(self):
        result = self._audit(
            (
                "-r=../../../../../cpu/wl82/liba/libmbedtls_3_4_0.a.llvm.9.net_sockets.c,mbedtls_net_connect,pl",
            )
        )
        self.assertNotEqual(result.verdict, BLOCK)
        owner = next(o for o in result.owners if o.symbol == "mbedtls_net_connect")
        self.assertEqual(owner.allowlist_group, "A")

    def test_r1_reports_a_vendor_extension_without_blocking(self):
        # mbedtls_ssl_read_ext is a JieLi extension, not a stock name.
        result = self._audit(
            (
                "-r=../../../../../cpu/wl82/liba/libmbedtls_3_4_0.a.llvm.9.net_sockets.c,mbedtls_ssl_read_ext,pl",
            )
        )
        self.assertNotEqual(result.verdict, BLOCK)
        infos = [finding for finding in result.findings if finding.severity == INFO]
        self.assertEqual([finding.symbol for finding in infos], ["mbedtls_ssl_read_ext"])

    def test_r2_blocks_a_symbol_prevailing_on_both_sides(self):
        result = self._audit(
            (
                "-r=../../../../../cpu/wl82/liba/libmbedtls_3_4_0.a.llvm.9.ssl_msg.c,mbedtls_ssl_write,pl",
                "-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.ssl_msg.c,mbedtls_ssl_write,pl",
            )
        )
        self.assertEqual(result.verdict, BLOCK)
        self.assertTrue(any(finding.rule == "R2" for finding in result.findings))

    def test_r3_warns_on_a_vendor_call_into_the_tuya_stack(self):
        result = self._audit(
            (
                "-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.ssl_msg.c,mbedtls_ssl_write,pl",
                "-r=../../../../../cpu/wl82/liba/https_cli.a.llvm.2.https_cli.c,mbedtls_ssl_write,l",
            )
        )
        self.assertEqual(result.verdict, WARN)
        self.assertEqual(len(result.cross_calls), 1)
        self.assertEqual(result.cross_calls[0].caller_archive, "https_cli.a")
        self.assertEqual(result.cross_calls[0].symbol, "mbedtls_ssl_write")

    def test_strict_promotes_r3_to_block(self):
        result = self._audit(
            (
                "-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.ssl_msg.c,mbedtls_ssl_write,pl",
                "-r=../../../../../cpu/wl82/liba/https_cli.a.llvm.2.https_cli.c,mbedtls_ssl_write,l",
            ),
            strict=True,
        )
        self.assertEqual(result.verdict, BLOCK)
        self.assertTrue(all(finding.severity == BLOCK for finding in result.findings))

    def test_a_tuya_only_symbol_is_quiet(self):
        result = self._audit(
            ("-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.aes.c,mbedtls_aes_init,pl",)
        )
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(result.findings, ())


class FixtureAuditTest(unittest.TestCase):
    def test_the_real_excerpt_matches_the_documented_rules(self):
        result = audit(
            RESOLUTION_FIXTURE, MAP_FIXTURE, stock_include=STOCK_FIXTURE
        )
        self.assertEqual(result.verdict, WARN)

        owners = {owner.symbol: owner for owner in result.owners}
        self.assertEqual(owners["mbedtls_net_connect"].side, "vendor")
        self.assertEqual(owners["mbedtls_net_connect"].allowlist_group, "A")
        self.assertEqual(owners["mbedtls_test_cas_pem"].allowlist_group, "B")
        self.assertEqual(
            owners["mbedtls_pk_write_pubkey_formatted"].allowlist_group, "C"
        )
        self.assertEqual(owners["mbedtls_ssl_read_ext"].allowlist_group, None)
        self.assertEqual(owners["mbedtls_ssl_write"].side, "tuya")

        # The only non-allowlisted vendor prevailing symbol is the extension.
        self.assertEqual(
            [finding.symbol for finding in result.findings if finding.severity == INFO],
            ["mbedtls_ssl_read_ext"],
        )
        self.assertEqual(
            {call.symbol for call in result.cross_calls},
            {"mbedtls_aes_init", "mbedtls_ssl_write", "mbedtls_ctr_drbg_seed"},
        )

    def test_the_map_excerpt_names_the_tls_provider_members(self):
        members = {f"{m.archive}({m.member})": m for m in parse_map(MAP_FIXTURE)}
        self.assertIn("libmbedtls_3_4_0.a(net_sockets.c)", members)
        self.assertIn("libmbedtls_3_4_0.a(certs.c)", members)
        self.assertIn("libmbedtls_3_4_0.a(version.z)", members)
        self.assertIn("libcrypto_mbedtls.a(crypto_mbedtls-ec.c)", members)
        # The metadata member has no referencing file, only a symbol.
        version = members["libmbedtls_3_4_0.a(version.z)"]
        self.assertEqual(version.referenced_by, ())
        self.assertEqual(version.pulled_for, ("lib_mbedtls_3_4_0_version",))
        self.assertIsInstance(version, ArchiveMember)

    def test_the_audit_only_reports_tls_provider_members(self):
        result = audit(RESOLUTION_FIXTURE, MAP_FIXTURE, stock_include=STOCK_FIXTURE)
        archives = {member.archive for member in result.vendor_members}
        self.assertEqual(archives, {"libmbedtls_3_4_0.a", "libcrypto_mbedtls.a"})
        self.assertEqual(len(result.vendor_members), 4)

    def test_the_report_renders_the_three_tables(self):
        result = audit(RESOLUTION_FIXTURE, MAP_FIXTURE, stock_include=STOCK_FIXTURE)
        report = render(result)
        self.assertIn("table 1: vendor member extraction", report)
        self.assertIn("table 2: public symbol ownership", report)
        self.assertIn("table 3: cross-provider calls", report)
        self.assertIn("verdict: WARN", report)


class SnapshotDiffTest(unittest.TestCase):
    def _snapshot(self, records):
        with tempfile.TemporaryDirectory() as temp_dir:
            resolution = write_resolution(Path(temp_dir), records)
            return snapshot(audit(resolution, stock_include=STOCK_FIXTURE))

    def test_a_new_vendor_symbol_shows_up_in_the_diff(self):
        before = self._snapshot(
            ("-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.aes.c,mbedtls_aes_init,pl",)
        )
        after = self._snapshot(
            (
                "-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.aes.c,mbedtls_aes_init,pl",
                "-r=../../../../../cpu/wl82/liba/libmbedtls_3_4_0.a.llvm.9.ssl_tls.c,mbedtls_ssl_write,pl",
            )
        )
        report = diff(before, after)
        self.assertIn("+ symbol mbedtls_ssl_write", report)

    def test_an_unchanged_snapshot_diffs_clean(self):
        before = self._snapshot(
            ("-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.aes.c,mbedtls_aes_init,pl",)
        )
        self.assertIn("no changes", diff(before, before))

    def test_the_snapshot_is_json_serialisable(self):
        current = self._snapshot(
            ("-r=<OPEN_ROOT>/apps/x/.build/lib/libtuyaos.a.llvm.1.aes.c,mbedtls_aes_init,pl",)
        )
        self.assertEqual(json.loads(json.dumps(current))["verdict"], "PASS")


class RealStockTableTest(unittest.TestCase):
    """The production stock table, when the main repository is alongside."""

    def setUp(self):
        self.open_root = Path(__file__).resolve().parents[3]
        self.include = self.open_root / "src/libtls/mbedtls-3.1.0/include"
        if not self.include.is_dir():
            self.skipTest("TuyaOpen src/libtls is not present")

    def test_stock_names_and_jieli_extensions_are_told_apart(self):
        stock = load_stock_symbols(self.include)
        self.assertIn("mbedtls_net_connect", stock)
        self.assertIn("mbedtls_ssl_write", stock)
        self.assertNotIn("mbedtls_ssl_read_ext", stock)
        self.assertNotIn("mbedtls_pk_write_pubkey_formatted", stock)

    def test_stock_include_is_discoverable_from_a_resolution_path(self):
        fake = self.open_root / "apps" / "tuya_cloud" / "switch_demo" / ".build" / (
            "jieli-staging/build/cpu/wl82/tools/sdk.elf.resolution.txt"
        )
        self.assertEqual(discover_stock_include(fake), self.include)


if __name__ == "__main__":
    unittest.main()
