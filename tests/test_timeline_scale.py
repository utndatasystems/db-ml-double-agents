from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "website" / "deploy" / "assets" / "app.js"


class TimelineScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text(encoding="utf-8")

    def test_timelines_use_direct_linear_scaling(self):
        self.assertIn(
            "const barHeight = linearBarHeight(metric, maxMetric, 162);",
            self.source,
        )
        self.assertIn(
            "const totalHeight = linearBarHeight(total, maxTotal, 24);",
            self.source,
        )

    def test_timeline_scaling_has_no_sqrt_or_minimum_height_distortion(self):
        self.assertNotIn("Math.sqrt(total / maxTotal)", self.source)
        self.assertNotIn("Math.max(0.8, segmentHeight)", self.source)

    def test_linear_helper_is_directly_proportional(self):
        self.assertIn(
            "return numericHeight * numericValue / numericMax;",
            self.source,
        )

    def test_author_position_bars_are_also_linear(self):
        self.assertIn(
            "const width = 100 * row.count / maxCount;",
            self.source,
        )
        self.assertNotIn("Math.max(4, Math.round((count / maxCount) * 100))", self.source)


if __name__ == "__main__":
    unittest.main()
