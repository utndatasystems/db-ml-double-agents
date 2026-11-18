from pathlib import Path
import csv
import json
import os
import sys
import tempfile
import unittest

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_site_data import load_main_venue_year_totals
from scan_dblp_publications import (
    TargetPerson,
    main_venue_year_for_record,
    scan_cache_is_current,
    target_fingerprint,
    write_main_venue_year_totals,
    write_scan_cache,
)
APP_JS = ROOT / "website" / "deploy" / "assets" / "app.js"
ALL_HTML = ROOT / "website" / "deploy" / "all.html"


class TimelineNormalizationTests(unittest.TestCase):
    def test_icml_proceedings_cycles_are_main_conference_papers(self):
        for cycle in (1, 2, 3):
            paper = etree.fromstring(
                f'<inproceedings key="conf/icml/example13"><booktitle>ICML ({cycle})</booktitle><year>2013</year></inproceedings>'.encode()
            )
            self.assertEqual(main_venue_year_for_record(paper), ("ICML", 2013))
        for book in ("ICML Workshop", "ICML (Workshop)", "ICMLC", "ICML (4)"):
            paper = etree.fromstring(
                f'<inproceedings key="conf/icml/example"><booktitle>{book}</booktitle><year>2013</year></inproceedings>'.encode()
            )
            self.assertIsNone(main_venue_year_for_record(paper))

    def test_main_venue_total_classifier_uses_same_strict_main_conference_rule(self):
        paper = etree.fromstring(
            b'<inproceedings key="conf/nips/example"><booktitle>NeurIPS</booktitle><year>2024</year><stream>streams/conf/nips</stream></inproceedings>'
        )
        workshop = etree.fromstring(
            b'<inproceedings key="conf/nips/workshop"><booktitle>NeurIPS Workshop</booktitle><year>2024</year><stream>streams/conf/nips</stream></inproceedings>'
        )
        self.assertEqual(main_venue_year_for_record(paper), ("NeurIPS", 2024))
        self.assertIsNone(main_venue_year_for_record(workshop))

    def test_main_venue_totals_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "totals.csv"
            write_main_venue_year_totals(
                path,
                {
                    ("NeurIPS", 2024): 4200,
                    ("ICML", 2024): 2600,
                    ("ICLR", 2024): 2300,
                },
            )
            loaded = load_main_venue_year_totals(path)
            self.assertEqual(loaded["NeurIPS"]["2024"], 4200)
            self.assertEqual(loaded["ICML"]["2024"], 2600)
            self.assertEqual(loaded["ICLR"]["2024"], 2300)

    def test_publication_target_fingerprint_is_order_independent(self):
        first = [
            TargetPerson(
                pid="1/Example",
                canonical_name="Ada Example",
                aliases=("Ada Example", "ADA EXAMPLE", "A. Example"),
                orcids=("0000-0002-0000-0002", "0000-0001-0000-0001"),
            )
        ]
        second = [
            TargetPerson(
                pid="1/Example",
                canonical_name="Ada Example",
                aliases=("A. Example", "ADA EXAMPLE", "Ada Example"),
                orcids=("0000-0001-0000-0001", "0000-0002-0000-0002"),
            )
        ]
        self.assertEqual(target_fingerprint(first), target_fingerprint(second))

    def test_publication_scan_cache_survives_mtime_only_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "publications.csv"
            totals = root / "totals.csv"
            snapshot = root / "snapshot.xml.gz"
            output.write_text("paper-a\n", encoding="utf-8")
            totals.write_text("venue,year,paper_count\nICML,2025,1\n", encoding="utf-8")
            snapshot.write_bytes(b"pinned-snapshot")
            targets = [
                TargetPerson(
                    pid="1/Example",
                    canonical_name="Ada Example",
                    aliases=("Ada Example",),
                    orcids=(),
                )
            ]

            write_scan_cache(
                output,
                totals,
                snapshot,
                targets,
                row_count=1,
                stats={},
            )
            self.assertTrue(scan_cache_is_current(output, totals, snapshot, targets))

            # Simulate a pre-hash v3 sidecar; a normal cache hit upgrades it.
            cache_path = output.with_suffix(output.suffix + ".scan.json")
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached.pop("output_sha256", None)
            cached.pop("main_totals_output_sha256", None)
            cache_path.write_text(json.dumps(cached), encoding="utf-8")
            self.assertTrue(scan_cache_is_current(output, totals, snapshot, targets))
            upgraded = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("output_sha256", upgraded)
            self.assertIn("main_totals_output_sha256", upgraded)

            for path in (output, totals, snapshot):
                stat = path.stat()
                os.utime(
                    path,
                    ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
                )

            self.assertTrue(scan_cache_is_current(output, totals, snapshot, targets))

            # Same-size content corruption must still invalidate the cache.
            output.write_text("paper-b\n", encoding="utf-8")
            self.assertFalse(scan_cache_is_current(output, totals, snapshot, targets))

    def test_main_ml_output_uses_one_line_chart_with_measurement_and_time_switches(self):
        source = APP_JS.read_text(encoding="utf-8")
        html = ALL_HTML.read_text(encoding="utf-8")
        css = (ROOT / "website" / "deploy" / "assets" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-publication-scale="absolute"', source)
        self.assertIn('data-publication-scale="relative"', source)
        self.assertIn('data-publication-view="annual"', source)
        self.assertIn('data-publication-view="cumulative"', source)
        render = source.split('  function renderGlobalTimeline() {', 1)[1].split('  function renderMlParticipants()', 1)[0]
        self.assertEqual(render.count('publicationLineChartMarkup('), 1)
        self.assertIn("const cumulative = state.publicationView === 'cumulative';", render)
        self.assertIn('publicationLineChartMarkup(timeline, { cumulative, relative })', render)
        self.assertNotIn('groupedPublicationChartMarkup(', render)
        self.assertIn('class="publication-line-chart"', source)
        self.assertIn('data-publication-chart="${cumulative ? ', source)
        self.assertIn('class="publication-year-columns"', source)
        self.assertIn('data-publication-venue="${escapeAttr(venue)}"', source)
        self.assertIn('class="publication-plot"', source)
        self.assertIn('class="publication-column-fill', source)
        self.assertIn('.publication-year-columns { display: flex;', css)
        self.assertIn('.publication-chart-scroll { overflow-x: auto;', css)
        self.assertNotIn('data-timeline-view="cumulative"', source)
        self.assertNotIn("function renderMlPresence", source)
        self.assertNotIn('id="ml-presence-chart"', html)
        self.assertIn('data-provenance-metric="publication_relative"', source)
        self.assertNotIn('Unique papers, split by venue. Explorer filters', html)

    def test_grouped_publication_totals_respect_visible_venues_and_keep_relative_spacing(self):
        source = APP_JS.read_text(encoding="utf-8")
        css = (ROOT / "website" / "deploy" / "assets" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("const yearPoints = visibleVenues.map", source)
        self.assertIn("const yearTotal = yearPoints.reduce", source)
        self.assertIn("cumulative ? point.cumulative : point.annual", source)
        self.assertIn("const totalLabel = relative ? '&nbsp;' : fmt.format(yearTotal);", source)
        self.assertIn("publication-year-total${relative ?", source)
        self.assertIn(".publication-year-total.is-hidden { visibility: hidden; }", css)
        self.assertIn("bottom: calc(100% + 10px);", css)
        self.assertNotIn("--publication-peak", source)
        self.assertIn("padding-top: 36px;", css)

    def test_publication_questions_use_info_buttons_not_redundant_chart_copy(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn('data-provenance-metric="person_publication_relative"', source)
        self.assertIn("metricId === 'publication_annual' || metricId === 'publication_cumulative'", source)
        self.assertIn("? 'publication_activity' : metricId", source)
        self.assertNotIn('Unique main ML papers across the selected researchers and visible venues', source)
        self.assertNotIn('unique main ML papers across the selected researchers and visible venues', source)
        self.assertNotIn('Annual unique papers, separately for each ML venue.', source)
        self.assertNotIn('Cumulative unique papers, separately for each ML venue.', source)
        self.assertNotIn('Absolute: unique papers involving the selected DB researchers', source)
        self.assertNotIn('publication-mode-note', source)
        self.assertNotIn('${description}</p>', source)

    def test_distinct_db_researchers_chart_uses_grouped_chart_dimensions_and_drilldown(self):
        html = ALL_HTML.read_text(encoding="utf-8")
        source = APP_JS.read_text(encoding="utf-8")
        css = (ROOT / "website" / "deploy" / "assets" / "styles.css").read_text(encoding="utf-8")
        section = html.split('<section id="ml-participants"', 1)[1].split('</section>', 1)[0]
        self.assertIn('Senior DB researchers publishing at NeurIPS, ICML or ICLR each year', section)
        self.assertNotIn('data-provenance-metric="ml_participants"', section)
        self.assertIn('id="ml-participants-legend" class="timeline-legend"', section)
        self.assertIn('Toggle visibility of author groups.', section)
        self.assertNotIn('class="section-note"', section)
        self.assertIn("metricId === 'ml_participants'", source)
        self.assertIn('participant-column-fill', source)
        self.assertIn('data-participants-year', source)
        self.assertIn("selectResearchersFromChart('ml-returning-year'", source)
        self.assertIn('class="publication-plot"', source)
        self.assertIn('.participant-column-fill { background: var(--participant-first); }', css)
        self.assertNotIn("color: 'var(--accent)'", source)

    def test_portfolio_histogram_is_fine_grained_full_width_with_drilldown(self):
        html = ALL_HTML.read_text(encoding='utf-8')
        source = APP_JS.read_text(encoding='utf-8')
        css = (ROOT / 'website' / 'deploy' / 'assets' / 'styles.css').read_text(encoding='utf-8')
        section = html.split('<section id="ml-portfolio"', 1)[1].split('</section>', 1)[0]
        self.assertIn('data-provenance-metric="ml_portfolio"', source)
        self.assertNotIn('class="section-note"', section)
        self.assertIn('for (let upper = 1; upper <= upperBound; upper += 1)', source)
        self.assertIn('Math.ceil(maximum) + 1', source)
        self.assertNotIn("{ label: '>40%'", source)
        self.assertIn('data-portfolio-bin', source)
        self.assertIn("selectResearchersFromChart('portfolio'", source)
        self.assertIn('--portfolio-bin-count:${bins.length}', source)
        self.assertIn('grid-template-columns: repeat(var(--portfolio-bin-count, 2), minmax(0, 1fr))', css)

    def test_main_ml_crossover_authorship_roles_are_exclusive_and_self_describing(self):
        html = ALL_HTML.read_text(encoding='utf-8')
        source = APP_JS.read_text(encoding='utf-8')
        css = (ROOT / 'website' / 'deploy' / 'assets' / 'styles.css').read_text(encoding='utf-8')
        section = html.split('<section id="ml-authorship"', 1)[1].split('</section>', 1)[0]
        self.assertIn('Author positions of senior DB researchers publishing at NeurIPS, ICML or ICLR', section)
        self.assertIn('Hierarchically ordered left to right by author position. Select a segment to open People Explorer.', section)
        self.assertNotIn('data-provenance-metric="crossover_authorship"', section)
        self.assertIn('id="ml-authorship-chart"', section)
        self.assertNotIn('class="section-note"', section)
        self.assertIn('function crossoverAuthorshipDistribution()', source)
        self.assertIn('function crossoverAuthorshipRole(paper)', source)
        self.assertIn('Number.isInteger(position)', source)
        self.assertIn('Number.isInteger(authorCount)', source)
        self.assertIn("if (authorCount === 1) return 'sole';", source)
        self.assertIn("if (position === 0) return 'first';", source)
        self.assertIn("if (position === authorCount - 1) return 'last';", source)
        self.assertIn("return 'other';", source)
        self.assertIn('counts[role] += 1;', source)
        self.assertIn('allPeople().filter(hasMain)', source)
        self.assertIn('renderCrossoverAuthorship();', source)
        self.assertIn('const percentage = (count) =>', source)
        self.assertIn('data-authorship-role="${role.key}"', source)
        self.assertIn('.viz-authorship-bar { display: flex;', css)
        self.assertIn('.viz-authorship-legend { display: grid;', css)

    def test_main_ml_authorship_roles_drill_down_to_unique_people(self):
        source = APP_JS.read_text(encoding='utf-8')
        css = (ROOT / 'website' / 'deploy' / 'assets' / 'styles.css').read_text(encoding='utf-8')
        self.assertIn("if (selection.kind === 'authorship-role')", source)
        self.assertIn('crossoverAuthorshipRole(paper) === selection.value', source)
        self.assertIn("state.explorerCohort = 'crossovers';", source)
        self.assertIn("selectResearchersFromChart('authorship-role', role.key", source)
        self.assertIn("querySelectorAll('button[data-authorship-role]')", source)
        self.assertIn('roleResearcherCounts', source)
        self.assertIn('class="viz-authorship-segment', source)
        self.assertIn('class="viz-authorship-legend-action"', source)
        self.assertIn('.viz-authorship-segment:focus-visible', css)
        self.assertIn('.viz-authorship-legend-action:focus-visible', css)

    def test_charts_filter_only_research_explorer_and_keep_reset_visible(self):
        source = APP_JS.read_text(encoding='utf-8')
        html = (ROOT / 'website' / 'deploy' / 'people.html').read_text(encoding='utf-8')
        self.assertIn('const people = scopePeople();', source)
        self.assertIn('state.chartDrilldown = { kind, value, label };', source)
        self.assertIn("selectResearchersFromChart('venue-service'", source)
        self.assertIn("selectResearchersFromChart('venue-ml'", source)
        self.assertIn("selectResearchersFromChart('ml-returning-year'", source)
        self.assertIn("selectResearchersFromChart('portfolio'", source)
        self.assertIn('id="chart-selection-banner"', html)
        self.assertIn('id="clear-chart-selection"', html)
        self.assertIn('state.chartDrilldown = null;', source)
        self.assertIn('if (state.chartDrilldown && !matchesChartDrilldown(person))', source)

    def test_chart_drilldown_shows_selection_above_the_table_and_explains_filter(self):
        source = APP_JS.read_text(encoding='utf-8')
        html = (ROOT / 'website' / 'deploy' / 'people.html').read_text(encoding='utf-8')
        self.assertIn('Chart: ${sources[state.chartDrilldown.kind]', source)
        self.assertIn('<thead id="researcher-table-columns" tabindex="-1">', html)
        self.assertIn("const target = $('#researcher-table-columns') || $('#explore');", source)
        self.assertIn('selectionBox.getBoundingClientRect().top - headerHeight - 16', source)
        self.assertIn("document.querySelector('.site-header')?.getBoundingClientRect().height", source)
        self.assertIn("window.matchMedia('(prefers-reduced-motion: reduce)').matches", source)
        self.assertIn('window.scrollTo({ top: Math.max(0, top)', source)
        self.assertNotIn('? els.chartSelectionBanner :', source)

    def test_active_chart_filter_pulses_subtly_and_respects_reduced_motion(self):
        css = (ROOT / 'website' / 'deploy' / 'assets' / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('.chart-selection-banner:not([hidden])', css)
        self.assertIn('animation: chart-selection-outline-pulse 4.5s ease-in-out infinite;', css)
        self.assertIn('@keyframes chart-selection-outline-pulse', css)
        self.assertIn('0 0 0 3px var(--accent-glow)', css)
        self.assertIn('@media (prefers-reduced-motion: reduce)', css)
        reduced_motion_section = css.split('/* A quiet neon-green pulse', 1)[1].split('#explore {', 1)[0]
        self.assertIn('animation: none;', reduced_motion_section)

    def test_explorer_has_explicit_crossover_and_all_db_cohort_modes(self):
        html = (ROOT / 'website' / 'deploy' / 'people.html').read_text(encoding='utf-8')
        source = APP_JS.read_text(encoding='utf-8')
        section = html.split('<section id="explore"', 1)[1].split('</section>', 1)[0]
        self.assertIn('data-explorer-cohort="crossovers"', section)
        self.assertIn('data-explorer-cohort="all"', section)
        self.assertIn('>Senior DB researchers with an ML paper</button>', section)
        self.assertIn('>All</button>', section)
        self.assertIn("explorerCohort: 'crossovers'", source)
        self.assertIn("state.explorerCohort === 'crossovers' && (person.focus?.included === false || !hasMain(person))", source)
        self.assertIn("state.explorerCohort === 'all' ? explorerPeople() : scopePeople()", source)
        self.assertIn("const people = explorerScopePeople();", source)
        self.assertIn("state.explorerCohort = 'crossovers';", source)
        self.assertIn("kind === 'venue-service' || kind === 'portfolio'", source)
        self.assertIn("kind === 'ml-status' && value === 'without'", source)
        self.assertIn('updateExplorerCohortSwitch();', source)
        # Publication activity remains cohort-wide and unaffected by the explorer switch.
        self.assertIn('const people = scopePeople();', source)

    def test_person_timeline_uses_same_grouped_columns_and_shared_switch(self):
        source = APP_JS.read_text(encoding='utf-8')
        self.assertIn('data-person-publication-scale="absolute"', source)
        self.assertIn('data-person-publication-scale="relative"', source)
        self.assertIn('data-person-publication-view="annual"', source)
        self.assertIn('data-person-publication-view="cumulative"', source)
        render = source.split('  function renderPersonDialog(person) {', 1)[1].split('  function openPerson(id)', 1)[0]
        self.assertEqual(render.count('groupedPublicationChartMarkup(personMainTimeline'), 1)
        self.assertIn("cumulative: dialogMainView === 'cumulative'", render)
        context = render.split('const contextTimelineBody = contextReady', 1)[1].split('const dbVenues', 1)[0]
        self.assertLess(context.index('Other publication filters'), context.index('personTimelineMarkup('))
        self.assertLess(context.index('context-publication-groups'), context.index('authorshipSummaryMarkup('))
        self.assertIn('const personMainTimeline = publicationTimelineData(personMainEntries)', source)
        self.assertIn('groupedPublicationChartMarkup(personMainTimeline', source)
        self.assertIn('data-person-publication-year', source)
        self.assertIn('person-authorship-section', source)
        self.assertNotIn('data-person-main-normalize', source.split('function bindPersonTimeline(person)', 1)[1])

    def test_db_venue_rows_preserve_clean_bar_style_without_button_box(self):
        css = (ROOT / 'website' / 'deploy' / 'assets' / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('.viz-rate-row.viz-chart-drilldown { background: transparent; border: 0;', css)
        self.assertIn('.viz-rate-row.viz-chart-drilldown:first-child { border-top:', css)
        self.assertIn('data-venue-ml-drilldown', APP_JS.read_text(encoding='utf-8'))

    def test_researcher_modal_info_appears_inside_dialog(self):
        source = APP_JS.read_text(encoding='utf-8')
        self.assertIn("(button.closest('dialog[open]') || document.body).appendChild(panel);", source)
        self.assertIn("|| metricId === 'person_publication_annual' || metricId === 'person_publication_cumulative'", source)
        self.assertIn("metricId === 'person_publication_annual' || metricId === 'person_publication_cumulative'", source)

    def test_person_authorship_uses_inline_ordinal_green_bars(self):
        source = APP_JS.read_text(encoding='utf-8')
        css = (ROOT / 'website' / 'deploy' / 'assets' / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('function recordedOrdinalPosition(paper)', source)
        self.assertIn("if (raw == null || raw === '') return null;", source)
        self.assertIn('return recordedOrdinalPosition(paper) ===', source)
        self.assertIn('data-authorship-filter="${escapeAttr(row.filter)}"', source)
        self.assertIn('class="authorship-inline authorship-inline-', source)
        self.assertIn("{ filter: 'sole', label: 'Sole', count: counts.sole }", source)
        self.assertIn("{ filter: 'first', label: 'First', count: counts.first }", source)
        self.assertIn("{ filter: 'last', label: 'Last', count: counts.last }", source)
        self.assertIn('Authorship roles', source)
        self.assertIn('Position in the author list', source)
        self.assertIn('const priorityBars = priorityRows.map((row) => {', source)
        self.assertIn('const maxCount = Math.max(1, ...rows.map((row) => row.count), ...priorityRows.map((row) => row.count));', source)
        self.assertNotIn('class="authorship-info"', source)
        self.assertNotIn('class="authorship-stat', source)
        self.assertIn('.authorship-inline .author-position-row .author-position-track i,', css)
        self.assertIn('background: var(--accent); opacity: 1;', css)
        self.assertIn('person-authorship-section', source)

    def test_explorer_count_shows_researchers_in_the_selected_population(self):
        source = APP_JS.read_text(encoding='utf-8')
        self.assertIn('const crossoverCount = people.filter(hasMain).length;', source)
        self.assertIn("<strong>${fmt.format(total)}</strong> ${total === 1 ? 'researcher' : 'researchers'}", source)
        self.assertIn("DB venue PC members", source)
        self.assertIn('with at least one ML paper at ${venues}${year}', source)
        self.assertIn('return `All ${group}`', source)
        self.assertIn('data-overview-ml="with"', source)
        self.assertIn('data-overview-ml="without"', source)
        self.assertIn('class="viz-donut-hit-areas"', source)
        self.assertIn('class="viz-donut-hit-segment"', source)
        self.assertIn("if (button instanceof SVGElement)", source)
        self.assertIn("activateSelection();", source)
        self.assertIn("selectResearchersFromChart('ml-status'", source)
        self.assertIn("if (selection.kind === 'ml-status')", source)
        self.assertIn("selection.value === 'with' ? hasMain(person) : !hasMain(person)", source)

    def test_person_charts_and_position_bars_use_full_dialog_width(self):
        css = (ROOT / 'website' / 'deploy' / 'assets' / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('.person-dialog .person-grouped-publications .publication-year-group { max-width: none; }', css)
        self.assertIn('.person-dialog .person-grouped-publications .publication-column { max-width: 26px; }', css)
        self.assertIn('.person-dialog .authorship-inline .authorship-popover-head,', css)
        self.assertIn('.person-dialog .authorship-inline .author-position-distribution,', css)
        self.assertIn('font-size: var(--text-subheading);', css)
        self.assertIn('assets/app.js?v=168', ALL_HTML.read_text(encoding='utf-8'))
        self.assertIn('assets/styles.css?v=153', ALL_HTML.read_text(encoding='utf-8'))

    def test_relative_publication_columns_use_each_conference_denominator(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("annualShare: denominator > 0 ? 100 * annual / denominator : null", source)
        self.assertIn("cumulativeShare: denominator > 0 && cumulativeDenominator > 0", source)
        self.assertIn("100 * cumulativeCovered / cumulativeDenominator", source)
        self.assertIn("if (denominator > 0)", source)
        self.assertIn("'unavailable (no conference-wide total)'", source)
        self.assertIn("Math.min(first, coverageStart)", source)
        self.assertIn("cumulative: state.publicationView === 'cumulative'", source)
        self.assertIn("relative: state.publicationScale === 'relative'", source)
        self.assertIn('data-person-publication-scale', source)
        self.assertIn('mainMlVenueYearTotals', source)
        self.assertIn('total / publishedTotal', source)
        self.assertIn('Normalize by conference size', source)

    def test_frontend_has_no_satellite_ml_tier(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertNotIn("data-timeline-satellite", source)
        self.assertNotIn("data-person-main-satellite", source)
        self.assertNotIn("Include satellite publications", source)
        self.assertNotIn("Satellite publications", source)
        self.assertNotIn("satellitePapersFor", source)
        self.assertIn("const crossoverCount = people.filter(hasMain).length;", source)

    def test_normalized_bar_label_uses_one_decimal_but_tooltip_keeps_precision(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("const shareFmt = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 3 });", source)
        self.assertIn("const shareLabelFmt = new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });", source)
        self.assertIn("shareLabelFmt.format(metric)", source)
        self.assertIn("shareFmt.format(share)", source)
        self.assertIn("shareFmt.format(count / venueTotal)", source)

    def test_main_ml_output_section_can_expand_for_chart_and_controls(self):
        css = (ROOT / "website" / "deploy" / "assets" / "styles.css").read_text(encoding="utf-8")
        start = css.index("/* Let the publication section grow")
        end = css.index(".timeline-section>.shell", start)
        section_rules = css[start:end]
        self.assertIn("height: auto;", section_rules)
        self.assertIn("max-height: none;", section_rules)
        self.assertIn("overflow: visible;", section_rules)
        self.assertNotIn("overflow: hidden;", section_rules)

    def test_asset_version_is_bumped_for_browser_cache_busting(self):
        html = ALL_HTML.read_text(encoding="utf-8")
        self.assertIn("assets/styles.css?v=153", html)
        self.assertIn("data/site_data.js?v=54", html)
        self.assertIn("assets/app.js?v=168", html)

    def test_db_venue_rates_have_percentage_absolute_switch_without_matrix(self):
        source = APP_JS.read_text(encoding="utf-8")
        html = ALL_HTML.read_text(encoding="utf-8")
        css = (ROOT / "website" / "deploy" / "assets" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("Senior DB researchers publishing at NeurIPS, ICML or ICLR", html)
        self.assertIn("<p class=\"eyebrow\">Across DB PC venues</p>", html)
        self.assertNotIn("Researchers with service at each DB venue and at least one main NeurIPS, ICML, or ICLR paper.", html)
        self.assertIn("A researcher may have served on PCs at multiple venues.", source)
        self.assertIn('data-db-rates-view="percentage"', html)
        self.assertIn('data-db-rates-view="absolute"', html)
        self.assertIn("dbVenueRatesView: 'percentage'", source)
        self.assertIn("const barWidth = isPercentage ? row.share : 100 * row.participating / maximum;", source)
        self.assertIn('class="viz-rate-numerator"', source)
        self.assertIn('class="viz-rate-denominator"', source)
        self.assertIn("b.total - a.total", source)
        self.assertIn("button.setAttribute('aria-pressed', String(selected))", source)
        self.assertIn(".viz-rate-track span { display: block; height: 100%; background: var(--accent); }", css)
        self.assertNotIn('id="matrix-details"', html)
        self.assertNotIn('id="matrix-filter-banner"', html)
        self.assertNotIn("renderMatrix()", source)
        self.assertNotIn("applyMatrixFilter", source)

    def test_frontend_exposes_quiet_contextual_info_and_methodology(self):
        source = APP_JS.read_text(encoding="utf-8")
        html = ALL_HTML.read_text(encoding="utf-8")
        self.assertIn("data-open-methodology", html)
        self.assertIn('id="methodology-dialog"', html)
        self.assertIn("data-provenance-metric=\"publication_relative\"", source)
        self.assertIn("info-icon-button", source)
        self.assertIn("function publicationProvenanceMarkup", source)
        self.assertIn("Merged versions", source)
        self.assertIn("paper?.resolvedVersions", source)
        self.assertNotIn("Satellite evidence", source)
        self.assertIn("function eventProvenanceMarkup", source)
        self.assertIn("data-confirm-source-download", source)
        self.assertIn("authorship-inline", source)
        self.assertNotIn("method.description", source)
        self.assertNotIn("method.technical", source)
        self.assertIn("function researcherInfoMarkup", source)
        self.assertIn("function researcherAffiliationEvidence", source)
        self.assertIn("Last Known Affiliation", source)
        self.assertIn("Affiliation evidence", source)
        self.assertNotIn("Affiliation confidence", source)
        self.assertNotIn("Affiliation selection", source)
        self.assertNotIn("Affiliation conflict", source)
        self.assertIn("function toggleMetricInfo", source)
        self.assertIn("metric-context-panel", source)
        self.assertIn("Publication sources and merged versions", source)
        self.assertIn("eventSourceArrowMarkup", source)
        self.assertIn("entry-provenance", source)
        self.assertIn("entry-meta-row", source)
        self.assertIn("timeline-paper-meta-row", source)
        self.assertIn("paperAuthorPositionSuffix", source)
        self.assertIn("data-person-context-jump", source)
        self.assertIn("See ${fmt.format(contextPaperCount)} other", source)
        self.assertIn("Select a point to inspect that venue’s papers for the year", html)
        self.assertNotIn("openMethodology(metric.dataset.provenanceMetric", source)
        self.assertIn(">Methodology</button>", html)
        self.assertNotIn(">Data &amp; methodology</button>", html)
        self.assertIn('id="footer-data-refresh"', html)
        self.assertIn("renderFooterRefresh", source)
        self.assertIn("Data refreshed", source)
        self.assertNotIn("DB community service through", source)
        self.assertNotIn("How calculated", source)
        self.assertNotIn("matrix-cell-button", source)
        self.assertNotIn("applyMatrixFilter", source)
        self.assertIn("publicationSearchUrl", source)
        self.assertIn("Database community service", source)
        self.assertIn("papersHeading: label ? `Papers at ${label}`", source)
        self.assertIn("Author order does not necessarily measure contribution.", source)
        self.assertIn("Senior DB researchers by DB venue", source)
        self.assertIn("Ratio of senior DB researchers with a main-conference paper at NeurIPS, ICML or ICLR", source)
        self.assertIn("People may appear at more than one venue.", source)
        self.assertIn("pcEventsFor(person).map(event => event.year)", source)
        self.assertIn("const serviceYears = recordedYearRange(observedServiceYears)", source)
        self.assertNotIn("What counts as a crossover?", html)
        self.assertNotIn("More filters", html)
        self.assertNotIn("View options", source)
        self.assertIn("function renderDbVenueRates", source)
        self.assertIn("function renderMlPortfolio", source)
        self.assertIn("function personPortfolioMarkup", source)
        self.assertNotIn('id="ml-presence-chart"', html)
        self.assertIn("DBLP record", source)
        self.assertIn("arXiv evidence", source)
        self.assertIn("Data fingerprint", source)
        self.assertNotIn("Show input fingerprints", source)
        self.assertNotIn("artifact.filename", source)
        self.assertIn("Last refreshed", source)
        self.assertIn("data-open-methodology-metric", source)
        self.assertIn("How this matrix is calculated", source)
        self.assertIn("How publication activity is calculated", source)
        self.assertIn("normalizeSearch", source)
        self.assertIn("state.explorerCohort = 'all'", source)
        self.assertNotIn("Source values</dt>", source)
        self.assertNotIn("The data fingerprint is one SHA-256 identifier", source)


if __name__ == "__main__":
    unittest.main()
