from pathlib import Path
import unittest

from lxml import html

WEBSITE = Path(__file__).resolve().parents[1] / "website" / "deploy"


class PageNavigationTests(unittest.TestCase):
    def test_loading_previews_initialize_before_body_can_paint(self):
        for filename in ('index.html', 'all.html', 'people.html'):
            with self.subTest(page=filename):
                page = html.parse(str(WEBSITE / filename))
                helpers = page.xpath('//head/script[starts-with(@src, "assets/loading-preview.js?")]')
                self.assertEqual(len(helpers), 1)
                self.assertNotIn('async', helpers[0].attrib)
                self.assertNotIn('defer', helpers[0].attrib)
                self.assertNotEqual(helpers[0].get('type'), 'module')

    def test_front_page_introduces_the_population_and_links_to_all(self):
        page = html.parse(str(WEBSITE / "index.html"))
        main = page.getroot().get_element_by_id("main")
        self.assertTrue(page.xpath("//*[@data-home-total]"))
        self.assertIn("program committees", main.text_content())
        self.assertTrue(page.xpath("//main//a[@href='people.html']"))
        self.assertEqual(page.xpath("//script/@src"), ["assets/loading-preview.js?v=5", "assets/home-trends.js?v=5", "assets/home.js?v=35", "assets/home-methodology.js?v=1"])
        self.assertTrue(page.xpath("//nav/button[@data-open-methodology]"))
        self.assertTrue(page.xpath("//*[@id='methodology-dialog']"))
        self.assertEqual(page.xpath("//nav/a[@href='all.html']/text()"), ["All Insights"])
        self.assertEqual(page.xpath("//nav/a[@aria-current='page']/@href"), ["index.html"])
        explorer = html.parse(str(WEBSITE / "all.html"))
        self.assertEqual(page.xpath("string(//a[@class='brand-title-link'])"),
                         explorer.xpath("string(//a[@class='brand-title-link'])"))

    def test_insights_retains_charts_and_dialogs_without_the_explorer(self):
        page = html.parse(str(WEBSITE / "all.html"))
        self.assertFalse(page.xpath("//*[@id='explore']"))
        self.assertTrue(page.xpath("//*[@id='global-timeline']"))
        self.assertTrue(page.xpath("//*[@id='person-dialog']"))
        self.assertTrue(page.xpath("//*[@id='methodology-dialog']"))
        self.assertEqual(page.xpath("//nav/a[@aria-current='page']/@href"), ["all.html"])
        self.assertTrue(page.xpath("//nav/a[@href='index.html']"))
        for src in page.xpath("//script/@src"):
            self.assertTrue((WEBSITE / src.split('?', 1)[0]).is_file(), src)

    def test_committee_overlap_is_integrated_before_db_venue_ml_rates(self):
        page = html.parse(str(WEBSITE / "all.html"))
        sections = page.xpath("//main/section/@id")
        self.assertLess(sections.index("db-overlap"), sections.index("matrix"))
        self.assertTrue(page.xpath("//*[@id='overlap-distribution']"))
        self.assertTrue(page.xpath("//*[@id='overlap-matrix']"))
        self.assertTrue(page.xpath("//*[@id='overlap-dialog']"))
        self.assertIn("assets/db-venue-overlap.js?v=17", page.xpath("//script/@src"))
        self.assertLess(sections.index("db-overlap"), sections.index("population-focus"))
        self.assertLess(sections.index("population-focus"), sections.index("matrix"))
        self.assertFalse((WEBSITE / "db-venue-overlap.html").exists())

    def test_data_coverage_remains_in_insights_with_a_separate_audit_dialog(self):
        page = html.parse(str(WEBSITE / 'all.html'))
        sections = page.xpath('//main/section/@id')
        self.assertEqual(sections[-1], 'data-coverage')
        self.assertNotIn('explore', sections)
        self.assertTrue(page.xpath("//*[@id='coverage-dialog']"))
        self.assertIn('assets/data-coverage.js', [src.split('?')[0] for src in page.xpath('//script/@src')])
        self.assertIn('assets/data-coverage.css', [src.split('?')[0] for src in page.xpath('//link/@href')])
        coverage=page.xpath("//*[@id='data-coverage']")[0]
        ids=coverage.xpath('.//*[@id]/@id')
        self.assertIn('coverage-flows', ids)
        self.assertFalse({'coverage-review', 'coverage-people', 'coverage-papers',
                          'coverage-classification-chart', 'coverage-evidence-chart'} & set(ids))
        self.assertTrue(page.xpath("//*[@id='coverage-related-groups']"))

    def test_people_page_has_the_explorer_and_shared_navigation(self):
        for filename in ('index.html', 'people.html', 'all.html'):
            page = html.parse(str(WEBSITE / filename))
            links = page.xpath('//nav[@class="top-nav"]/a')
            self.assertEqual([link.get('href') for link in links], ['index.html','people.html','all.html'])
            self.assertEqual([''.join(link.itertext()) for link in links], ['Home','People Explorer','All Insights'])
            self.assertEqual(page.xpath('//nav/a[@aria-current="page"]/@href'), [filename])
        page = html.parse(str(WEBSITE / 'people.html'))
        self.assertEqual(page.xpath('string(//h1)'), 'People Explorer')
        for identifier in ('people-body','search-input','chart-selection-banner','empty-state','person-dialog','methodology-dialog'):
            self.assertTrue(page.xpath(f'//*[@id="{identifier}"]'))
        self.assertFalse(page.xpath('//*[@id="global-timeline" or @id="data-coverage"]'))
        for src in page.xpath('//script/@src'):
            self.assertTrue((WEBSITE / src.split('?',1)[0]).is_file(), src)


if __name__ == "__main__":
    unittest.main()
