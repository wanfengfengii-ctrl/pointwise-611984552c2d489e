from fractions import Fraction as F
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from gridledger.delivery.service import LedgerService
from gridledger.delivery.codec import decode_value
from gridledger.archive.grid_store import load_grid_revision
from gridledger.observations.model import MassRecord, FluxRecord
from tests.test_balance import uniform_records, counts

HIERARCHY_TABLES = ("hierarchies", "hierarchy_results", "hierarchy_result_cells",
                    "hierarchy_result_faces", "hierarchy_result_sources",
                    "hierarchy_result_corrections")

def hierarchy_counts(service):
    return {name: service.connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in HIERARCHY_TABLES}

def put_uniform(service, grid_id, *, prefix="", values=None, rates=None, a=0, b=1):
    grid = load_grid_revision(service.connection, grid_id)
    masses, fluxes = uniform_records(grid, a=a, b=b, values=values, rates=rates, prefix=prefix)
    return service.put_observations(grid_id, masses, fluxes)

def build_cross(service):
    # 3x3 coarse grid; the four edge-neighbours of the centre are refined, so the
    # active centre cell carries one interface in each of the four directions.
    coarse = service.put_grid("cross-coarse", (3, 3), origin=(-1, -1))
    rates = {((1, 1), "east"): 6, ((1, 1), "west"): 4,
             ((1, 1), "north"): 10, ((1, 1), "south"): 2,
             ((2, 1), "west"): -6, ((0, 1), "east"): -4,
             ((1, 2), "south"): -10, ((1, 0), "north"): -2}
    cr = put_uniform(service, coarse, prefix="C", rates=rates)
    patches = []
    for name, cell, fine_rates in (
            ("pe", (2, 1), {((0, 0), "west"): -1, ((0, 1), "west"): -2}),
            ("pw", (0, 1), {((1, 0), "east"): -1, ((1, 1), "east"): -1}),
            ("pn", (1, 2), {((0, 0), "south"): -3, ((1, 0), "south"): -4}),
            ("ps", (1, 0), {((0, 1), "north"): -1, ((1, 1), "north"): -1})):
        origin = (-1 + cell[0], -1 + cell[1])
        fine = service.put_grid(name, (2, 2), origin=origin, spacing=(F(1, 2), F(1, 2)))
        fr = put_uniform(service, fine, prefix=name, rates=fine_rates)
        patches.append((fine, fr))
    return (coarse, cr), patches

def independent_audit_check(report):
    # Hand arithmetic over the delivered report only: no implementation calls.
    cells = {(c["grid_id"], tuple(c["cell"])): c for c in report["cells"]}
    for c in report["cells"]:
        assert c["change"] == c["mass_end"] - c["mass_start"]
        assert c["outflow"] == c["original_outflow"] + c["correction"]
        assert c["residual"] == c["change"] + c["outflow"]
        assert sum(f["integral"] for f in c["faces"]) == c["outflow"]
        assert sum(f["original"] for f in c["faces"]) == c["original_outflow"]
        assert sum(f["correction"] for f in c["faces"]) == c["correction"]
        for f in c["faces"]:
            assert f["integral"] == f["original"] + f["correction"]
    for entry in report["corrections"]:
        key = (entry["coarse"]["grid_id"], tuple(entry["coarse"]["cell"]))
        face = next(f for f in cells[key]["faces"] if f["side"] == entry["coarse"]["side"])
        assert len(entry["fine"]) == 2
        assert sum(s["delta"] for s in entry["segments"]) == face["correction"]
        assert sum(s["original"] for s in entry["segments"]) == face["original"]
        assert sum(s["replacement"] for s in entry["segments"]) == face["integral"]
        for s in entry["segments"]:
            assert s["delta"] == s["replacement"] - s["original"]
    boundary = report["boundary"]
    assert sum(f["integral"] for f in boundary["faces"]) == boundary["total_outflow"]
    assert sum(c["change"] for c in report["cells"]) == boundary["total_change"]
    assert sum(c["residual"] for c in report["cells"]) == boundary["total_residual"]
    assert boundary["total_residual"] == boundary["total_change"] + boundary["total_outflow"]
    anomalies = {(a["grid_id"], tuple(a["cell"])) for a in report["anomalous_cells"]}
    assert anomalies == {key for key, c in cells.items() if c["residual"] != 0}
    sources = {s["record"]["source_id"]: s["record"] for s in report["sources"]}
    for entry in report["corrections"]:
        for s in entry["segments"]:
            dt = s["end"] - s["start"]
            coarse_record = sources[s["coarse_source"]]
            fine_records = [sources[sid] for sid in s["fine_sources"]]
            assert coarse_record["rate"] * dt == s["original"]
            assert -sum(r["rate"] for r in fine_records) * dt == s["replacement"]
            for record in [coarse_record] + fine_records:
                assert record["start"] <= s["start"] < s["end"] <= record["end"]

class TestHierarchyRegistration(unittest.TestCase):
    def setUp(self):
        self.service = LedgerService()
        self.addCleanup(self.service.close)

    def test_four_directions_and_multiple_interfaces(self):
        coarse, patches = build_cross(self.service)
        coarse_id = coarse[0]
        h = self.service.register_hierarchy(coarse, patches)
        self.assertIsInstance(h, int) and self.assertGreater(h, 0)
        report = self.service.read_result(self.service.audit_hierarchy(h, 0, 1))
        self.assertEqual(report["kind"], "hierarchy")
        self.assertEqual(report["hierarchy_id"], h)
        self.assertEqual(sorted(report["covered"]), [[0, 1], [1, 0], [1, 2], [2, 1]])
        self.assertEqual(len(report["cells"]), 5 + 16)
        cells = {(c["grid_id"], tuple(c["cell"])): c for c in report["cells"]}
        centre = cells[(coarse_id, (1, 1))]
        # Hand arithmetic: originals 6+4+10+2=22; corrections -3-2-3+0=-8.
        self.assertEqual((centre["original_outflow"], centre["correction"],
                          centre["outflow"], centre["residual"]), (F(22), F(-8), F(14), F(14)))
        pe, pw, pn, ps = (p[0] for p in patches)
        expected_fine = {(pe, (0, 0)): -1, (pe, (0, 1)): -2, (pw, (1, 0)): -1,
                         (pw, (1, 1)): -1, (pn, (0, 0)): -3, (pn, (1, 0)): -4,
                         (ps, (0, 1)): -1, (ps, (1, 1)): -1}
        for (grid_id, cell), residual in expected_fine.items():
            self.assertEqual(cells[(grid_id, cell)]["residual"], F(residual))
            self.assertEqual(cells[(grid_id, cell)]["correction"], F(0))
        # One correction entry per active coarse/fine interface: 4 on the centre
        # plus 8 zero-delta entries on the refined edge neighbours.
        self.assertEqual(len(report["corrections"]), 12)
        order = [(tuple(e["coarse"]["cell"]), e["coarse"]["side"]) for e in report["corrections"]]
        self.assertEqual(order, sorted(order))
        centre_entries = {e["coarse"]["side"]: e for e in report["corrections"]
                          if tuple(e["coarse"]["cell"]) == (1, 1)}
        self.assertEqual(set(centre_entries), {"east", "north", "south", "west"})
        spans = {"east": (6, 3, -3), "west": (4, 2, -2),
                 "north": (10, 7, -3), "south": (2, 2, 0)}
        for side, (original, replacement, delta) in spans.items():
            segments = centre_entries[side]["segments"]
            self.assertEqual([(s["original"], s["replacement"], s["delta"]) for s in segments],
                             [(F(original), F(replacement), F(delta))])
            self.assertEqual([(s["start"], s["end"]) for s in segments], [(F(0), F(1))])
        self.assertEqual([f["cell"] for f in centre_entries["east"]["fine"]], [[0, 0], [0, 1]])
        self.assertEqual([f["side"] for f in centre_entries["east"]["fine"]], ["west", "west"])
        zero = [e for e in report["corrections"]
                if all(s["delta"] == 0 for s in e["segments"])]
        self.assertEqual(len(zero), 9)  # centre south plus the 8 edge interfaces
        self.assertEqual(report["boundary"]["total_outflow"], F(0))
        self.assertEqual(report["boundary"]["total_residual"], F(0))
        self.assertEqual(len(report["boundary"]["faces"]), 16)
        self.assertEqual(len(report["anomalous_cells"]), 9)
        independent_audit_check(report)
        print("POINTWISE_SCENARIO " + json.dumps({
            "case_id": "hierarchy-cross", "input": {"centre_rates": [6, 4, 10, 2]},
            "expected": "14", "actual": str(centre["residual"]),
            "downstream_expected": "0", "downstream_actual": str(report["boundary"]["total_residual"]),
            "code_paths": ["gridledger/hierarchy/registration.py", "gridledger/hierarchy/execution.py",
                           "gridledger/archive/hierarchy_store.py"],
            "oracle": "hand arithmetic 22-(3+2+3+0)=14; fine residuals -1-2-1-1-3-4-1-1=-14"}))

    def test_fractional_geometry_and_point_contact(self):
        coarse = self.service.put_grid("frac-coarse", (2, 2),
                                       origin=(F(-3, 2), F(1, 3)), spacing=(F(1, 2), F(2, 3)))
        rates = {((0, 0), "east"): 3, ((1, 0), "west"): -3,
                 ((0, 0), "north"): 1, ((0, 1), "south"): -1,
                 ((1, 1), "west"): 2, ((0, 1), "east"): -2,
                 ((1, 1), "south"): 4, ((1, 0), "north"): -4}
        cr = put_uniform(self.service, coarse, prefix="FC", rates=rates)
        # Diagonal patches meet at the single point (-1, 1): not a shared face.
        pd1 = self.service.put_grid("pd1", (2, 2), origin=(-1, F(1, 3)),
                                    spacing=(F(1, 4), F(1, 3)))
        pd2 = self.service.put_grid("pd2", (2, 2), origin=(F(-3, 2), 1),
                                    spacing=(F(1, 4), F(1, 3)))
        r1 = put_uniform(self.service, pd1, prefix="pd1",
                         rates={((0, 0), "west"): -1, ((0, 1), "west"): -1,
                                ((0, 1), "north"): -2, ((1, 1), "north"): -1})
        r2 = put_uniform(self.service, pd2, prefix="pd2",
                         rates={((0, 0), "south"): -1, ((1, 0), "east"): -1,
                                ((1, 1), "east"): -1})
        h = self.service.register_hierarchy((coarse, cr), [(pd1, r1), (pd2, r2)])
        report = self.service.read_result(self.service.audit_hierarchy(h, 0, 1))
        self.assertEqual(sorted(report["covered"]), [[0, 1], [1, 0]])
        cells = {(c["grid_id"], tuple(c["cell"])): c for c in report["cells"]}
        self.assertEqual(len(cells), 10)
        # (0,0): originals 3+1=4, corrections -1+0=-1. (1,1): 2+4=6, 0-1=-1.
        self.assertEqual((cells[(coarse, (0, 0))]["original_outflow"],
                          cells[(coarse, (0, 0))]["correction"],
                          cells[(coarse, (0, 0))]["residual"]), (F(4), F(-1), F(3)))
        self.assertEqual((cells[(coarse, (1, 1))]["original_outflow"],
                          cells[(coarse, (1, 1))]["correction"],
                          cells[(coarse, (1, 1))]["residual"]), (F(6), F(-1), F(5)))
        entries = {(tuple(e["coarse"]["cell"]), e["coarse"]["side"]): e
                   for e in report["corrections"]}
        self.assertEqual(set(entries), {((0, 0), "east"), ((0, 0), "north"),
                                        ((1, 1), "south"), ((1, 1), "west")})
        got = {key: [(s["original"], s["replacement"], s["delta"])
                     for s in entry["segments"]] for key, entry in entries.items()}
        self.assertEqual(got[((0, 0), "east")], [(F(3), F(2), F(-1))])
        self.assertEqual(got[((0, 0), "north")], [(F(1), F(1), F(0))])
        self.assertEqual(got[((1, 1), "south")], [(F(4), F(3), F(-1))])
        self.assertEqual(got[((1, 1), "west")], [(F(2), F(2), F(0))])
        self.assertEqual(report["boundary"]["total_residual"], F(0))
        self.assertEqual(len(report["anomalous_cells"]), 8)
        independent_audit_check(report)

    def test_invalid_geometry_rejected(self):
        coarse = self.service.put_grid("geom-coarse", (2, 2))
        cr = put_uniform(self.service, coarse, prefix="G")
        good = self.service.put_grid("geom-fine", (2, 2), origin=(1, 1),
                                     spacing=(F(1, 2), F(1, 2)))
        gr = put_uniform(self.service, good, prefix="GF")
        cases = []
        cases.append(self.service.put_grid("bad-shape", (2, 3), origin=(1, 1),
                                           spacing=(F(1, 2), F(1, 3))))
        cases.append(self.service.put_grid("bad-spacing", (2, 2), origin=(1, 1),
                                           spacing=(F(1, 3), F(1, 2))))
        cases.append(self.service.put_grid("misaligned", (2, 2), origin=(F(5, 4), 1),
                                           spacing=(F(1, 2), F(1, 2))))
        cases.append(self.service.put_grid("outside", (2, 2), origin=(2, 0),
                                           spacing=(F(1, 2), F(1, 2))))
        cases.append(self.service.put_grid("partial", (2, 2), origin=(1, F(1, 2)),
                                           spacing=(F(1, 2), F(1, 2))))
        for bad in cases:
            br = put_uniform(self.service, bad, prefix="B" + str(bad))
            before = hierarchy_counts(self.service)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.service.register_hierarchy((coarse, cr), [(bad, br)])
            self.assertEqual(hierarchy_counts(self.service), before)
        duplicate = self.service.put_grid("duplicate", (2, 2), origin=(1, 1),
                                          spacing=(F(1, 2), F(1, 2)))
        dr = put_uniform(self.service, duplicate, prefix="D")
        malformed = [
            lambda: self.service.register_hierarchy((coarse, cr), [(good, gr), (duplicate, dr)]),
            lambda: self.service.register_hierarchy((coarse, cr), [(good, gr), (good, gr)]),
            lambda: self.service.register_hierarchy((coarse, cr), [(coarse, cr)]),
            lambda: self.service.register_hierarchy((coarse, cr), []),
            lambda: self.service.register_hierarchy((coarse, cr), None),
            lambda: self.service.register_hierarchy((coarse, cr), [(good,)]),
            lambda: self.service.register_hierarchy((coarse, cr), [(True, gr)]),
            lambda: self.service.register_hierarchy((coarse, cr), [(good, 0)]),
            lambda: self.service.register_hierarchy((coarse, cr), [("1", gr)]),
            lambda: self.service.register_hierarchy((coarse,), [(good, gr)]),
            lambda: self.service.register_hierarchy((coarse, cr, cr), [(good, gr)]),
        ]
        for call in malformed:
            before = hierarchy_counts(self.service)
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()
            self.assertEqual(hierarchy_counts(self.service), before)
        too_many = []
        for n in range(9):
            g = self.service.put_grid("extra-" + str(n), (2, 2), origin=(1, 1),
                                      spacing=(F(1, 2), F(1, 2)))
            too_many.append((g, put_uniform(self.service, g, prefix="E" + str(n))))
        with self.assertRaises(ValueError):
            self.service.register_hierarchy((coarse, cr), too_many)
        big = self.service.put_grid("big-coarse", (4, 5))
        br = put_uniform(self.service, big, prefix="BIG")
        with self.assertRaises(ValueError):
            self.service.register_hierarchy((big, br), [(good, gr)])
        self.assertEqual(hierarchy_counts(self.service)["hierarchies"], 0)

    def test_wrong_bindings(self):
        coarse = self.service.put_grid("bind-coarse", (2, 1))
        fine = self.service.put_grid("bind-fine", (2, 2), origin=(1, 0),
                                     spacing=(F(1, 2), F(1, 2)))
        cr = put_uniform(self.service, coarse, prefix="BC")
        fr = put_uniform(self.service, fine, prefix="BF")
        before = hierarchy_counts(self.service)
        with self.assertRaises(ValueError):
            self.service.register_hierarchy((coarse, fr), [(fine, fr)])
        with self.assertRaises(ValueError):
            self.service.register_hierarchy((coarse, cr), [(fine, cr)])
        with self.assertRaises(KeyError):
            self.service.register_hierarchy((coarse, cr), [(fine + 100, fr)])
        with self.assertRaises(KeyError):
            self.service.register_hierarchy((coarse, cr + 100), [(fine, fr)])
        self.assertEqual(hierarchy_counts(self.service), before)
        h = self.service.register_hierarchy((coarse, cr), [(fine, fr)])
        with self.assertRaises(KeyError):
            self.service.audit_hierarchy(h + 100, 0, 1)
        for bad_h in (True, "1", 1.0, None):
            with self.subTest(bad_h=bad_h), self.assertRaises(ValueError):
                self.service.audit_hierarchy(bad_h, 0, 1)
        for a, b in ((1, 0), (0, 0), (0.0, 1), (False, 1), (0, None)):
            with self.subTest(a=a, b=b), self.assertRaises(ValueError):
                self.service.audit_hierarchy(h, a, b)
        self.assertEqual(hierarchy_counts(self.service)["hierarchy_results"], 0)

    def test_registration_sqlite_failure(self):
        coarse = self.service.put_grid("atomic-coarse", (2, 1))
        fine = self.service.put_grid("atomic-fine", (2, 2), origin=(1, 0),
                                     spacing=(F(1, 2), F(1, 2)))
        cr = put_uniform(self.service, coarse, prefix="AC")
        fr = put_uniform(self.service, fine, prefix="AF")
        self.service.connection.execute(
            "CREATE TRIGGER reject_registration BEFORE INSERT ON hierarchies"
            " BEGIN SELECT RAISE(ABORT,'blocked registration'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                self.service.register_hierarchy((coarse, cr), [(fine, fr)])
            self.assertEqual(str(caught.exception), "blocked registration")
            self.assertEqual(hierarchy_counts(self.service)["hierarchies"], 0)
            self.assertFalse(self.service.connection.in_transaction)
        finally:
            self.service.connection.execute("DROP TRIGGER reject_registration")
        h = self.service.register_hierarchy((coarse, cr), [(fine, fr)])
        self.assertEqual(hierarchy_counts(self.service)["hierarchies"], 1)
        self.assertIsInstance(h, int)

class TestHierarchyAudit(unittest.TestCase):
    def setUp(self):
        self.service = LedgerService()
        self.addCleanup(self.service.close)

    def corner_hierarchy(self, *, east_rate=2, north_rate=4):
        coarse = self.service.put_grid("corner-coarse", (2, 2))
        rates = {((1, 0), "north"): 5, ((1, 1), "south"): -5,
                 ((0, 1), "east"): 6, ((1, 1), "west"): -6,
                 ((1, 1), "east"): 9, ((1, 1), "north"): 7}
        cr = put_uniform(self.service, coarse, prefix="CC", rates=rates)
        fine = self.service.put_grid("corner-fine", (2, 2), origin=(1, 1),
                                     spacing=(F(1, 2), F(1, 2)))
        fr = put_uniform(self.service, fine, prefix="CF",
                         rates={((0, 0), "west"): -2, ((0, 1), "west"): -3,
                                ((0, 0), "south"): -4, ((1, 0), "south"): -2,
                                ((1, 0), "east"): east_rate, ((1, 1), "east"): 3,
                                ((0, 1), "north"): north_rate, ((1, 1), "north"): 1})
        return (coarse, cr), [(fine, fr)]

    def test_refined_exterior_boundary(self):
        coarse, patches = self.corner_hierarchy()
        coarse_id = coarse[0]
        h = self.service.register_hierarchy(coarse, patches)
        report = self.service.read_result(self.service.audit_hierarchy(h, 0, 1))
        self.assertEqual(report["covered"], [[1, 1]])
        self.assertEqual(len(report["cells"]), 7)
        self.assertNotIn((coarse_id, (1, 1)),
                         {(c["grid_id"], tuple(c["cell"])) for c in report["cells"]})
        boundary = report["boundary"]
        # The covered coarse cell's exterior faces (rates 9 and 7) stay out;
        # the refined exterior faces carry 2+3 east and 4+1 north.
        self.assertEqual(boundary["total_outflow"], F(10))
        self.assertEqual(boundary["total_residual"], F(10))
        coarse_boundary = [f for f in boundary["faces"] if f["grid_id"] == coarse_id]
        self.assertEqual(len(coarse_boundary), 6)
        self.assertNotIn([1, 1], [f["cell"] for f in coarse_boundary])
        fine_boundary = {(tuple(f["cell"]), f["side"]): f["integral"]
                         for f in boundary["faces"] if f["grid_id"] != coarse_id}
        self.assertEqual(fine_boundary, {((1, 0), "east"): F(2), ((1, 1), "east"): F(3),
                                         ((0, 1), "north"): F(4), ((1, 1), "north"): F(1)})
        cells = {(c["grid_id"], tuple(c["cell"])): c for c in report["cells"]}
        # (1,0): original 5, replacement 4+2=6. (0,1): original 6, replacement 2+3=5.
        self.assertEqual((cells[(coarse_id, (1, 0))]["original_outflow"],
                          cells[(coarse_id, (1, 0))]["correction"],
                          cells[(coarse_id, (1, 0))]["residual"]), (F(5), F(1), F(6)))
        self.assertEqual((cells[(coarse_id, (0, 1))]["original_outflow"],
                          cells[(coarse_id, (0, 1))]["correction"],
                          cells[(coarse_id, (0, 1))]["residual"]), (F(6), F(-1), F(5)))
        entries = {(tuple(e["coarse"]["cell"]), e["coarse"]["side"]): e
                   for e in report["corrections"]}
        self.assertEqual(set(entries), {((1, 0), "north"), ((0, 1), "east")})
        self.assertEqual([(s["original"], s["replacement"], s["delta"])
                          for s in entries[((1, 0), "north")]["segments"]],
                         [(F(5), F(6), F(1))])
        self.assertEqual([(s["original"], s["replacement"], s["delta"])
                          for s in entries[((0, 1), "east")]["segments"]],
                         [(F(6), F(5), F(-1))])
        # Covered inputs remain delivered as evidence.
        covered_sources = {s["record"]["source_id"] for s in report["sources"]
                           if s["record"]["grid_id"] == coarse_id
                           and tuple(s["record"]["cell_id"]) == (1, 1)}
        self.assertIn("CC(1, 1)-start", covered_sources)
        self.assertIn("CC(1, 1)-east-0", covered_sources)
        independent_audit_check(report)

    def test_zero_correction_interface(self):
        coarse = self.service.put_grid("zc-coarse", (2, 1))
        cr = put_uniform(self.service, coarse, prefix="ZC",
                         rates={((0, 0), "east"): 5, ((1, 0), "west"): -5})
        fine = self.service.put_grid("zc-fine", (2, 2), origin=(1, 0),
                                     spacing=(F(1, 2), F(1, 2)))
        fr = put_uniform(self.service, fine, prefix="ZF",
                         rates={((0, 0), "west"): -2, ((0, 1), "west"): -3})
        h = self.service.register_hierarchy((coarse, cr), [(fine, fr)])
        report = self.service.read_result(self.service.audit_hierarchy(h, 0, 1))
        self.assertEqual(len(report["corrections"]), 1)
        entry = report["corrections"][0]
        self.assertEqual([(s["original"], s["replacement"], s["delta"])
                          for s in entry["segments"]], [(F(5), F(5), F(0))])
        active = next(c for c in report["cells"] if c["grid_id"] == coarse)
        self.assertEqual((active["original_outflow"], active["correction"],
                          active["outflow"], active["residual"]),
                         (F(5), F(0), F(5), F(5)))
        self.assertEqual(report["boundary"]["total_residual"], F(0))
        self.assertEqual(len(report["anomalous_cells"]), 3)
        independent_audit_check(report)

    def test_missing_and_conflicting_inputs(self):
        counter = [0]
        def build(*, coarse_rates=None, fine_rates=None, drop=None):
            counter[0] += 1
            tag = str(counter[0])
            coarse = self.service.put_grid("mc-coarse-" + tag, (2, 1))
            fine = self.service.put_grid("mc-fine-" + tag, (2, 2), origin=(1, 0),
                                         spacing=(F(1, 2), F(1, 2)))
            grid = load_grid_revision(self.service.connection, coarse)
            masses, fluxes = uniform_records(
                grid, prefix="MC" + tag,
                rates=coarse_rates or {((0, 0), "east"): 8, ((1, 0), "west"): -8})
            if drop == "coarse-covered-mass":
                masses = [m for m in masses if m.cell_id != (1, 0)]
            cr = self.service.put_observations(coarse, masses, fluxes)
            fine_grid = load_grid_revision(self.service.connection, fine)
            fmasses, ffluxes = uniform_records(
                fine_grid, prefix="MF" + tag,
                rates=fine_rates or {((0, 0), "west"): -2, ((0, 1), "west"): -3})
            if drop == "fine-mass":
                fmasses = fmasses[1:]
            fr = self.service.put_observations(fine, fmasses, ffluxes)
            return self.service.register_hierarchy((coarse, cr), [(fine, fr)])
        for drop in ("coarse-covered-mass", "fine-mass"):
            with self.subTest(drop=drop):
                h = build(drop=drop)
                before = hierarchy_counts(self.service)
                with self.assertRaises(ValueError):
                    self.service.audit_hierarchy(h, 0, 1)
                self.assertEqual(hierarchy_counts(self.service), before)
        temporal = [
            {((0, 0), "west"): [(0, F(1, 3), -2), (F(1, 2), 1, -2)],
             ((0, 1), "west"): -3},                                  # gap
            {((0, 0), "west"): [(0, F(2, 3), -2), (F(1, 3), 1, -2)],
             ((0, 1), "west"): -3},                                  # overlap
        ]
        for rates in temporal:
            with self.subTest(rates=rates):
                h = build(fine_rates=rates)
                before = hierarchy_counts(self.service)
                with self.assertRaises(ValueError):
                    self.service.audit_hierarchy(h, 0, 1)
                self.assertEqual(hierarchy_counts(self.service), before)
        conflicts = [
            {((0, 0), "east"): 8, ((1, 0), "west"): -7},             # coarse same-level
            None,                                                    # placeholder
        ]
        h = build(coarse_rates=conflicts[0])
        before = hierarchy_counts(self.service)
        with self.assertRaises(ValueError):
            self.service.audit_hierarchy(h, 0, 1)
        self.assertEqual(hierarchy_counts(self.service), before)
        fine_conflict = {((0, 0), "east"): 3, ((1, 0), "west"): -2,
                         ((0, 0), "west"): -2, ((0, 1), "west"): -3}
        h = build(fine_rates=fine_conflict)
        before = hierarchy_counts(self.service)
        with self.assertRaises(ValueError):
            self.service.audit_hierarchy(h, 0, 1)
        self.assertEqual(hierarchy_counts(self.service), before)

    def test_cross_patch_north_south(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict), LedgerService() as service:
                coarse = service.put_grid("ns-coarse", (1, 2))
                cr = put_uniform(service, coarse, prefix="NC")
                low = service.put_grid("ns-low", (2, 2), spacing=(F(1, 2), F(1, 2)))
                high = service.put_grid("ns-high", (2, 2), origin=(0, 1),
                                        spacing=(F(1, 2), F(1, 2)))
                lr = put_uniform(service, low, prefix="NL", rates={
                    ((0, 1), "north"): [(0, F(1, 2), 2), (F(1, 2), 1, 6)],
                    ((1, 1), "north"): 1})
                right = -6 if conflict else -2
                hr = put_uniform(service, high, prefix="NH", rates={
                    ((0, 0), "south"): [(0, F(1, 2), right), (F(1, 2), 1, -6)],
                    ((1, 0), "south"): -1})
                h = service.register_hierarchy((coarse, cr), [(low, lr), (high, hr)])
                before = hierarchy_counts(service)
                if conflict:
                    # Whole-window integrals cancel (+4-4) but the [0,1/2)
                    # segment does not: segmentwise rejection is required.
                    with self.assertRaises(ValueError):
                        service.audit_hierarchy(h, 0, 1)
                    self.assertEqual(hierarchy_counts(service), before)
                else:
                    report = service.read_result(service.audit_hierarchy(h, 0, 1))
                    self.assertEqual(report["corrections"], [])
                    self.assertEqual(len(report["cells"]), 8)
                    residuals = {(c["grid_id"], tuple(c["cell"])): c["residual"]
                                 for c in report["cells"]}
                    self.assertEqual(residuals[(low, (0, 1))], F(4))
                    self.assertEqual(residuals[(low, (1, 1))], F(1))
                    self.assertEqual(residuals[(high, (0, 0))], F(-4))
                    self.assertEqual(residuals[(high, (1, 0))], F(-1))
                    self.assertEqual(report["boundary"]["total_residual"], F(0))
                    self.assertEqual(len(report["anomalous_cells"]), 4)
                    independent_audit_check(report)

    def test_audit_leaves_no_v1_rows(self):
        coarse, patches = self.corner_hierarchy()
        h = self.service.register_hierarchy(coarse, patches)
        before = counts(self.service)
        rid = self.service.audit_hierarchy(h, 0, 1)
        after = counts(self.service)
        self.assertEqual(before, after)  # no V1 plans/results are created
        self.assertEqual(hierarchy_counts(self.service)["hierarchy_results"], 1)
        with self.assertRaises(KeyError):
            self.service.read_result(rid + 100)

class TestHierarchyDelivery(unittest.TestCase):
    def build_file_hierarchy(self, service):
        coarse = service.put_grid("file-coarse", (2, 1))
        cr = put_uniform(service, coarse, prefix="FC",
                         rates={((0, 0), "east"): 8, ((1, 0), "west"): -8})
        fine = service.put_grid("file-fine", (2, 2), origin=(1, 0),
                                spacing=(F(1, 2), F(1, 2)))
        fr = put_uniform(service, fine, prefix="FF",
                         rates={((0, 0), "west"): [(0, F(1, 3), -1), (F(1, 3), 1, F(-5, 2))],
                                ((0, 1), "west"): [(0, F(1, 2), -2), (F(1, 2), 1, -4)]})
        return (coarse, cr), (fine, fr)

    def test_publication_failure_and_retry(self):
        with LedgerService() as service:
            coarse, patch = self.build_file_hierarchy(service)
            h = service.register_hierarchy(coarse, [patch])
            old_id = service.audit_hierarchy(h, 0, 1)
            old = service.read_result(old_id)
            before = hierarchy_counts(service)
            for table in ("hierarchy_result_cells", "hierarchy_result_faces",
                          "hierarchy_result_sources", "hierarchy_result_corrections"):
                with self.subTest(table=table):
                    service.connection.execute(
                        "CREATE TRIGGER reject_hierarchy BEFORE INSERT ON " + table +
                        " BEGIN SELECT RAISE(ABORT,'blocked hierarchy publication'); END")
                    try:
                        with self.assertRaises(sqlite3.IntegrityError) as caught:
                            service.audit_hierarchy(h, 0, 1)
                        self.assertEqual(str(caught.exception), "blocked hierarchy publication")
                        self.assertEqual(hierarchy_counts(service), before)
                        self.assertEqual(service.read_result(old_id), old)
                        self.assertFalse(service.connection.in_transaction)
                    finally:
                        service.connection.execute("DROP TRIGGER reject_hierarchy")
            new_id = service.audit_hierarchy(h, 0, 1)
            self.assertNotEqual(new_id, old_id)
            self.assertEqual(service.read_result(new_id), old)
            self.assertEqual(hierarchy_counts(service)["hierarchy_results"],
                             before["hierarchy_results"] + 1)

    def test_export_write_failure(self):
        class DestinationFailure(Exception):
            pass
        failure = DestinationFailure("disk destination unavailable")
        class BrokenTarget:
            def write(self, text):
                raise failure
        with LedgerService() as service:
            coarse, patch = self.build_file_hierarchy(service)
            h = service.register_hierarchy(coarse, [patch])
            rid = service.audit_hierarchy(h, 0, 1)
            baseline, before = service.read_result(rid), hierarchy_counts(service)
            with self.assertRaises(DestinationFailure) as caught:
                service.export_result(rid, BrokenTarget())
            self.assertIs(caught.exception, failure)
            self.assertEqual(service.read_result(rid), baseline)
            self.assertEqual(hierarchy_counts(service), before)

    def test_reopen_correction_and_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hierarchy.sqlite"
            with LedgerService(path) as service:
                # A V1 chain shares the archive and the result id space.
                v1_grid = service.put_grid("v1-single", (1, 1))
                m, f = uniform_records(load_grid_revision(service.connection, v1_grid),
                                       rates={((0, 0), "east"): F(1, 3)})
                v1_rev = service.put_observations(v1_grid, m, f)
                v1_rid = service.audit(service.compile_window(v1_grid, v1_rev, 0, 1))
                v1_baseline = service.read_result(v1_rid)
                coarse, patch = self.build_file_hierarchy(service)
                fine, fr = patch
                patches = [patch]
                h = service.register_hierarchy(coarse, patches)
                rid = service.audit_hierarchy(h, 0, 1)
                self.assertNotEqual(v1_rid, rid)
                baseline = service.read_result(rid)
                target = io.StringIO()
                service.export_result(rid, target)
                text = target.getvalue()
                # Returned-container edits cannot drift the stored result.
                altered = service.read_result(rid)
                altered["cells"].clear()
                altered["corrections"][0]["segments"].clear()
                altered["boundary"]["faces"].clear()
                altered["sources"].clear()
                self.assertEqual(service.read_result(rid), baseline)
                # Caller-owned containers cannot drift the fixed binding.
                patches.clear()
                self.assertEqual(service.read_result(service.audit_hierarchy(h, 0, 1)),
                                 baseline)
                # A source correction creates a new revision; the registered
                # binding still reads the revision it was registered with. The
                # correction replaces both asynchronous segments' logical keys.
                newer = service.put_observations(fine, fluxes=[
                    FluxRecord(fine, (0, 0), "west", 0, F(1, 3), -4, "corrected-F-west-0"),
                    FluxRecord(fine, (0, 0), "west", F(1, 3), 1, -4, "corrected-F-west-1")])
                h2 = service.register_hierarchy(coarse, [(fine, newer)])
                changed = service.read_result(service.audit_hierarchy(h2, 0, 1))
                active = next(c for c in changed["cells"] if c["grid_id"] == coarse[0])
                self.assertEqual((active["original_outflow"], active["correction"],
                                  active["outflow"]), (F(8), F(-1), F(7)))
                self.assertEqual(service.read_result(service.audit_hierarchy(h, 0, 1)),
                                 baseline)
                self.assertEqual(service.read_result(rid), baseline)
            with LedgerService(path) as service:
                self.assertEqual(service.read_result(v1_rid), v1_baseline)
                self.assertEqual(service.read_result(rid), baseline)
                target = io.StringIO()
                service.export_result(rid, target)
                self.assertEqual(target.getvalue(), text)
                decoded = decode_value(json.loads(text))
                self.assertEqual(decoded, baseline)
                independent_audit_check(decoded)
                # Detached spot arithmetic on the asynchronous interface.
                entry = decoded["corrections"][0]
                spans = [(s["start"], s["end"], s["original"], s["replacement"], s["delta"])
                         for s in entry["segments"]]
                self.assertEqual(spans, [(F(0), F(1, 3), F(8, 3), F(1), F(-5, 3)),
                                         (F(1, 3), F(1, 2), F(4, 3), F(3, 4), F(-7, 12)),
                                         (F(1, 2), F(1), F(4), F(13, 4), F(-3, 4))])
                again = io.StringIO()
                service.export_result(rid, again)
                self.assertEqual(again.getvalue(), text)
