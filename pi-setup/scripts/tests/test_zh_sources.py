#!/usr/bin/env python3
"""
test_zh_sources.py — unit tests for the ZH source registry.

The registry is pure data: typed dataclasses with no behaviour beyond
URL/path templating and lookup helpers. These tests pin the contract
so a careless edit (renaming a source, dropping a fallback flag,
swapping TC ↔ SC) breaks loudly in CI rather than silently in
production at the booth.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PI_SETUP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PI_SETUP))

from scripts import zh_sources  # noqa: E402
from scripts.zh_sources import (  # noqa: E402
    Lang, LocalMirrorSource, RemoteWebSource, SourceKind,
)


class LangAndKindEnumsTest(unittest.TestCase):
    """Wire-format pinning — these values appear in destination paths
    and JSON, so changing them silently moves all the user's data."""

    def test_lang_values_are_lowercase_iso_like(self):
        self.assertEqual(Lang.TC.value, "tc")
        self.assertEqual(Lang.SC.value, "sc")

    def test_source_kind_values(self):
        self.assertEqual(SourceKind.LOCAL_MIRROR.value, "local_mirror")
        self.assertEqual(SourceKind.REMOTE_WEB.value, "remote_web")


class RegistryShapeTest(unittest.TestCase):

    def test_all_sources_includes_known_three(self):
        names = {s.name for s in zh_sources.all_sources()}
        self.assertIn("PTCG-CHS-Datasets", names)
        self.assertIn("ptcg.tw", names)
        self.assertIn("mycardart-tc", names)

    def test_sc_has_local_mirror_primary(self):
        sc = zh_sources.sources_for(Lang.SC)
        self.assertEqual(len(sc), 1, "expected exactly one default SC source")
        self.assertIsInstance(sc[0], LocalMirrorSource)
        self.assertEqual(sc[0].name, "PTCG-CHS-Datasets")

    def test_tc_default_excludes_fallback(self):
        tc = zh_sources.sources_for(Lang.TC)
        names = [s.name for s in tc]
        self.assertIn("ptcg.tw", names)
        self.assertNotIn("mycardart-tc", names,
                         "MyCardArt is fallback-only and must not appear "
                         "in the default TC list")

    def test_tc_with_fallback_includes_mycardart(self):
        tc = zh_sources.sources_for(Lang.TC, include_fallback=True)
        names = {s.name for s in tc}
        self.assertEqual(names, {"ptcg.tw", "mycardart-tc"})

    def test_source_by_name_finds_known(self):
        s = zh_sources.source_by_name("ptcg.tw")
        self.assertIsNotNone(s)
        self.assertEqual(s.lang, Lang.TC)

    def test_source_by_name_unknown_returns_none(self):
        self.assertIsNone(zh_sources.source_by_name("does-not-exist"))


class LocalMirrorSourceTest(unittest.TestCase):

    def test_local_path_resolves_template(self):
        s = LocalMirrorSource(
            name="testfork", lang=Lang.SC,
            repo_dir="testfork-clone",
            image_path_template="img/{set_id}/{card_num}.png",
        )
        p = s.local_path("/mnt/cards", "42", "7")
        self.assertEqual(str(p), "/mnt/cards/testfork-clone/img/42/7.png")

    def test_kind_discriminator_is_set(self):
        s = LocalMirrorSource(
            name="x", lang=Lang.SC, repo_dir="x",
            image_path_template="{set_id}/{card_num}.png",
        )
        self.assertEqual(s.kind, SourceKind.LOCAL_MIRROR)


class RemoteWebSourceTest(unittest.TestCase):

    def test_url_for_resolves_template(self):
        s = RemoteWebSource(
            name="x", lang=Lang.TC,
            image_url_template="https://x.example/{set_id}/{card_num}.jpg",
            rate_limit_seconds=0.0,
        )
        self.assertEqual(s.url_for("svi", "001"),
                         "https://x.example/svi/001.jpg")

    def test_headers_dict_includes_user_agent(self):
        s = RemoteWebSource(
            name="x", lang=Lang.TC,
            image_url_template="https://x.example/{set_id}/{card_num}.jpg",
            rate_limit_seconds=0.0,
            extra_headers=(("X-Custom", "v1"),),
        )
        h = s.headers_dict
        self.assertIn("User-Agent", h)
        self.assertEqual(h["X-Custom"], "v1")

    def test_default_user_agent_is_browser_style(self):
        # Some sites serve different content to obvious python-urllib
        # UAs. Pin that we ship a Mozilla-style prefix.
        s = zh_sources.PTCG_TW
        self.assertTrue(s.user_agent.startswith("Mozilla/"),
                        f"PTCG_TW UA should be browser-style, got: {s.user_agent}")

    def test_kind_discriminator_is_set(self):
        s = RemoteWebSource(
            name="x", lang=Lang.TC,
            image_url_template="https://x.example/{set_id}/{card_num}.jpg",
            rate_limit_seconds=0.0,
        )
        self.assertEqual(s.kind, SourceKind.REMOTE_WEB)


if __name__ == "__main__":
    unittest.main()
