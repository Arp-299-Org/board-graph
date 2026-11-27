import pathlib
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_KICAD = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_KICAD))
import render_board_views  # noqa: E402


class ExportLayerViewTests(unittest.TestCase):
    def test_uses_pdf_path_when_pymupdf_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = pathlib.Path(tmp) / "test.kicad_pcb"
            board.write_text("(kicad_pcb)\n", encoding="utf-8")
            png = pathlib.Path(tmp) / "front.png"
            calls = []

            def fake_cli(*args):
                calls.append(args)
                pathlib.Path(args[args.index("--output") + 1]).write_bytes(b"%PDF")

            with mock.patch.object(render_board_views, "has_pymupdf", return_value=True), \
                    mock.patch.object(render_board_views, "cli", side_effect=fake_cli), \
                    mock.patch.object(render_board_views, "pdf_to_png", side_effect=lambda p, o: o.write_bytes(b"PNG")):
                render_board_views.export_layer_view(board, "F.Cu,Edge.Cuts", png)

            self.assertTrue(png.is_file())
            self.assertFalse(png.with_suffix(".pdf").exists())
            self.assertEqual(calls[0][:3], ("pcb", "export", "pdf"))

    def test_falls_back_to_svg_wx_when_pymupdf_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = pathlib.Path(tmp) / "test.kicad_pcb"
            board.write_text("(kicad_pcb)\n", encoding="utf-8")
            png = pathlib.Path(tmp) / "front.png"
            calls = []

            def fake_cli(*args):
                calls.append(args)
                pathlib.Path(args[args.index("--output") + 1]).write_text("<svg/>", encoding="utf-8")

            with mock.patch.object(render_board_views, "has_pymupdf", return_value=False), \
                    mock.patch.object(render_board_views, "cli", side_effect=fake_cli), \
                    mock.patch.object(render_board_views, "svg_to_png", side_effect=lambda s, o: o.write_bytes(b"PNG")):
                render_board_views.export_layer_view(board, "F.Cu,Edge.Cuts", png)

            self.assertTrue(png.is_file())
            self.assertFalse(png.with_suffix(".svg").exists())
            self.assertEqual(calls[0][:3], ("pcb", "export", "svg"))


if __name__ == "__main__":
    unittest.main()
