import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "music_library_tool.py"
FIXTURES = ROOT / "tests" / "fixtures"


def load_tool():
    spec = importlib.util.spec_from_file_location("music_library_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()


class LyricsSidecarTests(unittest.TestCase):
    def test_synthetic_lrc_has_content(self):
        self.assertTrue(tool.lrc_has_lyric_content(FIXTURES / "synthetic_valid.lrc"))

    def test_metadata_only_lrc_is_blank_for_readiness(self):
        self.assertFalse(tool.lrc_has_lyric_content(FIXTURES / "metadata_only.lrc"))

    def test_placeholder_lrc_is_blank_for_readiness(self):
        self.assertFalse(tool.lrc_has_lyric_content(FIXTURES / "blank_placeholder.lrc"))

    def test_lrc_needs_fetch_when_sidecar_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "01 Sample.mp3"
            info = tool.AudioInfo(path=audio, kind="mp3")
            self.assertTrue(tool.lrc_needs_fetch(info))

    def test_lrc_needs_fetch_when_sidecar_has_synthetic_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "01 Sample.mp3"
            lrc = audio.with_suffix(".lrc")
            lrc.write_text("[00:01.00] sample line one\n", encoding="utf-8")
            info = tool.AudioInfo(path=audio, kind="mp3")
            self.assertFalse(tool.lrc_needs_fetch(info))


class PathSanitizationTests(unittest.TestCase):
    def test_clean_segment_removes_unsafe_path_pieces(self):
        self.assertEqual(tool.clean_segment("  A/B:C \n Name  ", "Fallback"), "A_B _C Name")

    def test_clean_segment_uses_fallback_for_empty_values(self):
        self.assertEqual(tool.clean_segment(" / : \t ", "Fallback"), "_ _")
        self.assertEqual(tool.clean_segment("", "Fallback"), "Fallback")


if __name__ == "__main__":
    unittest.main()
