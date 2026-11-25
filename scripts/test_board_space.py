"""The height report must not mistake construction points for a part's top."""
import hashlib
import pathlib
import tempfile
import unittest

from board_space import body_extent, measure_model, report, transform_point


STEP_HEADER = "ISO-10303-21;DATA;#1=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));\n"
STEP_END = 'ENDSEC;END-ISO-10303-21;'
STEP_POINTS = '''#10=CARTESIAN_POINT('',(1.,2.,3.));
#11=CARTESIAN_POINT('',(-2.,-1.,4.));
#12=CARTESIAN_POINT('',(2.,0.,-1.));
#13=CARTESIAN_POINT('',(999.,999.,999.));
#20=VERTEX_POINT('',#10);
#21=VERTEX_POINT('',#11);
#22=VERTEX_POINT('',#12);
ENDSEC;END-ISO-10303-21;'''


class BodyExtentTests(unittest.TestCase):
    def test_few_far_points_are_stray(self):
        self.assertEqual(body_extent([0.0] * 500 + [3.3] * 40 + [10.3] * 4), (3.3, 10.3))

    def test_real_top_face_stays(self):
        self.assertEqual(body_extent([0.0] * 500 + [10.3] * 40), (10.3, 10.3))

    def test_points_near_the_top_are_not_stray(self):
        self.assertEqual(body_extent([0.0] * 500 + [3.0] * 40 + [3.5] * 2), (3.5, 3.5))


def box_step(xs, ys, zs):
    """A STEP whose solid corners are the eight corners of a box."""
    corners = [(x, y, z) for x in xs for y in ys for z in zs]
    points = ''.join(f"#{100 + i}=CARTESIAN_POINT('',({x},{y},{z}));\n" for i, (x, y, z) in enumerate(corners))
    vertices = ''.join(f"#{200 + i}=VERTEX_POINT('',#{100 + i});\n" for i in range(len(corners)))
    return STEP_HEADER + points + vertices + STEP_END


class RotatedHeightTests(unittest.TestCase):
    """The height report moves each point by its model transform before reading Z."""

    def heights(self, step, *footprints):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'model.step').write_text(step)
            board = root / 'fixture.kicad_pcb'
            board.write_text('(kicad_pcb ' + ' '.join(
                f'''(footprint "x" (layer "{layer}") (at 10 20 {angle}) (property "Reference" "{ref}")
                  (model "${{KIPRJMOD}}/model.step" (offset (xyz {offset})) (scale (xyz {scale}))
                    (rotate (xyz {rotate}))))''' for ref, layer, angle, offset, scale, rotate in footprints) + ')')
            return {row['ref']: (row['bottom_mm'], row['body_top_mm']) for row in report(board)}

    def test_a_box_turned_270_degrees_about_x_stands_on_its_old_y_extent(self):
        # The jacks' saved transform: Rx(-270) carries the model's Y onto Z, so a box from Y -5.5 to 3.5
        # lifted by 2.5 spans Z -3.0 to 6.0. Read without the turn, its Z would give 13.05.
        step = box_step((-5.75, 5.75), (-5.5, 3.5), (-6.95, 10.55))
        found = self.heights(step, ('TURNED', 'B.Cu', 90, '-0.4 -0.05 2.5', '1 1 1', '270 0 90'),
                             ('FLAT', 'B.Cu', 90, '-0.4 -0.05 2.5', '1 1 1', '0 0 90'))
        self.assertEqual(found['TURNED'], (-3.0, 6.0))
        self.assertEqual(found['FLAT'], (-4.45, 13.05))

    def test_a_box_tilted_30_degrees_about_x_or_y_reads_its_tilted_corners(self):
        # Rx(-30): z' = -0.5 y + 0.866 z. Ry(-30): z' = 0.5 x + 0.866 z. Corners at x, y = +-2 and z = 0, 1.
        step = box_step((-2, 2), (-2, 2), (0, 1))
        found = self.heights(step, ('ABOUT_X', 'F.Cu', 0, '0 0 0', '1 1 1', '30 0 0'),
                             ('ABOUT_Y', 'F.Cu', 0, '0 0 0', '1 1 1', '0 30 0'))
        self.assertEqual(found['ABOUT_X'], (-1.0, 1.87))
        self.assertEqual(found['ABOUT_Y'], (-1.0, 1.87))

    def test_the_footprint_angle_and_face_leave_the_height_from_the_mount_face_alone(self):
        step = box_step((0, 3), (0, 1), (0, 2))
        found = self.heights(step, ('FRONT', 'F.Cu', 0, '0 0 0.1', '1 1 1', '0 90 0'),
                             ('BACK', 'B.Cu', 0, '0 0 0.1', '1 1 1', '0 90 0'),
                             ('TURNED', 'B.Cu', 135, '0 0 0.1', '1 1 1', '0 90 0'))
        self.assertEqual(found['FRONT'], (0.1, 3.1))
        self.assertEqual(found['BACK'], found['FRONT'])
        self.assertEqual(found['TURNED'], found['FRONT'])

    def test_scale_and_a_turn_about_z_alone_keep_the_untilted_reading(self):
        step = box_step((0, 3), (0, 1), (0, 2))
        found = self.heights(step, ('SPUN', 'F.Cu', 0, '0 0 0.5', '1 1 1', '-0 -0 -90'),
                             ('TALL', 'F.Cu', 0, '0 0 0.5', '1 1 2', '0 0 0'))
        self.assertEqual(found['SPUN'], (0.5, 2.5))
        self.assertEqual(found['TALL'], (0.5, 4.5))


class TransformedPointTests(unittest.TestCase):
    def test_nonuniform_scale_three_rotations_and_translation_follow_kicad(self):
        # Hand calculation: S=(2,6,12), Rx(-90)=(2,12,-6), Ry(-90)=(6,12,2),
        # Rz(-90)=(12,-6,2), then add (10,20,30).
        actual = transform_point((1, 2, 3), (2, 3, 4), (90, 90, 90), (10, 20, 30))
        for found, expected in zip(actual, (22, 14, 32)):
            self.assertAlmostEqual(found, expected)

    def test_vertex_bounds_use_the_transformed_asymmetric_points(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'sample.step'
            data = (STEP_HEADER + STEP_POINTS).encode()
            path.write_bytes(data)
            result = measure_model(path, scale=(2, 3, 4), rotate=(90, 90, 90), offset=(10, 20, 30))
            self.assertEqual(result['status'], 'sampled')
            self.assertEqual(result['source_sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(result['method'], 'step_vertex_points')
            self.assertEqual(result['count'], 3)
            self.assertEqual((result['z_min_mm'], result['z_max_mm']), (26, 34))
            self.assertTrue(any('not solid extrema' in text for text in result['limits']))

    def test_cartesian_fallback_is_labeled_as_including_construction_points(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'sample.step'
            path.write_text(STEP_HEADER + "#10=CARTESIAN_POINT('',(1.,2.,3.));" + STEP_END)
            result = measure_model(path, scale=(1, 1, 1), rotate=(90, 0, 0), offset=(0, 0, 4))
            self.assertEqual((result['status'], result['method'], result['count']), ('sampled', 'step_cartesian_points', 1))
            self.assertEqual(result['z_max_mm'], 2)
            self.assertTrue(any('construction points' in text for text in result['limits']))

    def test_missing_points_units_transforms_and_references_cannot_sample(self):
        samples = [
            ("", 'complete'),
            ('ISO-10303-21;DATA;' + STEP_END, 'units'),
            (STEP_HEADER + STEP_END, 'No STEP points'),
            (STEP_HEADER + "#3=VERTEX_POINT('',#99);" + STEP_END, 'unresolved'),
            (STEP_HEADER + "#42=MAPPED_ITEM('',#1,#2);" + STEP_POINTS, 'unsupported'),
            ("ISO-10303-21;DATA;#1=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT($,.METRE.));" + STEP_POINTS, 'millimetres'),
            ('version https://git-lfs.github.com/spec/v1\noid sha256:123\n', 'not an available'),
            (STEP_HEADER + "#2=CARTESIAN_POINT('',(1E999,0,0));" + STEP_END, 'non-finite'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'sample.step'
            for content, reason in samples:
                with self.subTest(reason=reason):
                    path.write_text(content)
                    result = measure_model(path, scale=(1, 1, 1), rotate=(0, 0, 0), offset=(0, 0, 0))
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertIsNone(result['z_max_mm'])
                    self.assertIn(reason, result['reason'])

    def test_opt_in_report_preserves_saved_transform_and_the_height_report_applies_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'model.step').write_text(STEP_HEADER + STEP_POINTS)
            footprint = '''(footprint "fixture" (layer "B.Cu") (property "Reference" "REF")
              (model "${KIPRJMOD}/model.step" (offset (xyz 10 20 30))
                (scale (xyz 2 3 4)) (rotate (xyz 90 90 90))))'''
            board = root / 'fixture.kicad_pcb'
            board.write_text('(kicad_pcb ' + ' '.join(footprint.replace('"REF"', f'"{ref}"')
                                                        for ref in ('J1', 'J4', 'J5', 'OTHER')) + ')')
            heights = report(board)
            self.assertTrue(all((row['bottom_mm'], row['body_top_mm']) == (26, 34) for row in heights))
            rows = report(board, references={'J1', 'J4', 'J5'}, transformed=True)
            self.assertEqual({row['ref'] for row in rows}, {'J1', 'J4', 'J5'})
            for row in rows:
                self.assertEqual(row['face'], 'back')
                self.assertEqual(row['model_path'], '${KIPRJMOD}/model.step')
                self.assertEqual(row['transform'], {'scale_xyz': [2, 3, 4], 'rotate_deg_xyz': [90, 90, 90],
                                                    'offset_mm_xyz': [10, 20, 30]})
                self.assertEqual(row['point_measurement']['z_max_mm'], 34)
                self.assertNotIn('body_top_mm', row)
            with self.assertRaisesRegex(ValueError, 'missing'):
                report(board, references={'J1', 'J4', 'J5', 'missing'}, transformed=True)

    def test_malformed_transform_leaves_only_that_part_unmeasured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'model.step').write_text(STEP_HEADER + STEP_POINTS)
            board = root / 'fixture.kicad_pcb'
            board.write_text('''(kicad_pcb
              (footprint "a" (layer "F.Cu") (property "Reference" "J1")
                (model "${KIPRJMOD}/model.step" (scale (xyz 1 2))))
              (footprint "b" (layer "F.Cu") (property "Reference" "J4")
                (model "${KIPRJMOD}/model.step" (at (xyz 0 0 1))))
              (footprint "c" (layer "F.Cu") (property "Reference" "J5") (model "${KIPRJMOD}/model.step")))''')
            rows = {row['ref']: row for row in report(board)}
            self.assertIn('three XYZ', rows['J1']['note'])
            self.assertIn('at-offset', rows['J4']['note'])
            self.assertNotIn('body_top_mm', rows['J1'])
            self.assertEqual(rows['J5']['body_top_mm'], 4)

    def test_missing_model_and_malformed_transform_are_reported_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            board = pathlib.Path(directory) / 'fixture.kicad_pcb'
            board.write_text('''(kicad_pcb
              (footprint "a" (layer "F.Cu") (property "Reference" "J1"))
              (footprint "b" (layer "F.Cu") (property "Reference" "J4")
                (model "${KIPRJMOD}/missing.step" (scale (xyz 1 2))))
              (footprint "c" (layer "F.Cu") (property "Reference" "J5")
                (model "${KIPRJMOD}/missing.step" (at (xyz 0 0 1)))))''')
            rows = report(board, references={'J1', 'J4', 'J5'}, transformed=True)
            self.assertEqual([row['point_measurement']['status'] for row in rows], ['unavailable'] * 3)
            self.assertTrue(all(row['point_measurement']['z_max_mm'] is None for row in rows))

    def test_transformed_report_wont_read_a_model_outside_the_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'outside.step').write_text(STEP_HEADER + STEP_POINTS)
            board = root / 'snapshot' / 'board' / 'fixture.kicad_pcb'
            board.parent.mkdir(parents=True)
            board.write_text('''(kicad_pcb (footprint "x" (layer "F.Cu") (property "Reference" "J1")
              (model "${KIPRJMOD}/../../outside.step")))''')
            sample = report(board, references={'J1'}, transformed=True)[0]['point_measurement']
            self.assertEqual(sample['status'], 'unavailable')
            self.assertIsNone(sample['source_sha256'])
            self.assertIn('escapes', sample['reason'])

    def test_hidden_model_is_not_reported_as_a_displayed_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'model.step').write_text(STEP_HEADER + STEP_POINTS)
            board = root / 'fixture.kicad_pcb'
            board.write_text('''(kicad_pcb (footprint "x" (layer "F.Cu") (property "Reference" "J1")
              (model "${KIPRJMOD}/model.step" (hide yes))))''')
            sample = report(board, references={'J1'}, transformed=True)[0]['point_measurement']
            self.assertEqual(sample['status'], 'unavailable')
            self.assertIn('hidden', sample['reason'])


if __name__ == "__main__":
    unittest.main()
