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
            self.assertEqual(calls[0][calls[0].index("--scale") + 1], "0")

    @unittest.skipUnless(render_board_views.has_pymupdf(), "PyMuPDF is unavailable")
    def test_paths_entirely_off_page_do_not_create_zero_height_clip(self):
        import fitz

        with tempfile.TemporaryDirectory() as tmp:
            pdf = pathlib.Path(tmp) / "off-page.pdf"
            png = pdf.with_suffix(".png")
            with fitz.open() as doc:
                page = doc.new_page(width=100, height=100)
                page.draw_rect(fitz.Rect(20, 140, 40, 160))
                self.assertEqual(len(page.get_drawings()), 1)
                doc.save(str(pdf))

            render_board_views.pdf_to_png(pdf, png, dpi=72)
            pixmap = fitz.Pixmap(str(png))
            self.assertEqual((pixmap.width, pixmap.height), (100, 100))

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
