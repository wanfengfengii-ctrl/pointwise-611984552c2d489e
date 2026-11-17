"""Two-level native-grid mass audit extension
========================================

This file is the public V2 contract, finite input builder and non-answer arithmetic
checker. README.md, RUNBOOK.md and all original tests describe the unchanged V1.
There is no hierarchy implementation in this initial tree.

Business result
---------------
Register a fixed coarse/fine observation binding, then publish one report for
the same physical region. Covered coarse cells disappear from the active account;
their fine cells replace them. A surviving coarse neighbour must use the opposing
fine face totals, while retaining its original coarse measurement and correction.
The report must retain local anomalies even when their global sum is zero.
Use only the Python standard library, Fraction and the existing SQLite archive.
Full scans, sorting all relevant endpoints and full recomputation are allowed.
No interpolation, solver, index, concurrency or recovery feature is required.

Public calls on the existing LedgerService
-----------------------------------------
register_hierarchy(coarse, patches) -> positive integer hierarchy ID.
coarse is (grid_id, observation_id); patches is a nonempty list/tuple of such
pairs. IDs are positive int, excluding bool. The coarse grid has at most 16 cells;
there are at most 8 patches. Each fine grid has shape (2,2), half the coarse
spacing on both axes, and exactly covers one complete coarse cell. Origins,
spacing and extents must agree exactly, including negative/fractional coordinates.
Infer the covered cell from geometry; callers do not provide correspondence maps.
Patches may be disjoint, edge-adjacent or meet at a point. Repeated grids, repeated
coverage, partial/outside/misaligned coverage or grid/observation mismatch fail.
All grid and observation IDs refer to existing immutable V1 revisions.
Registration stores this complete binding atomically; it does not require a
particular window to be complete yet. It copies caller-owned containers and
survives reopening. A later registration creates a new binding, never alters h.

audit_hierarchy(h, a, b) -> published result ID.
h is a registered integer ID; a,b follow V1 exact numeric rules and a<b.
Read the revisions registered in h, never current heads. All cells of every
registered grid, INCLUDING covered coarse cells, must have masses at a,b and
exactly-once directed-face coverage of [a,b). Validate every original grid's
internal face rates at every common time interval, as V1 does. Also validate
opposition on equal fine faces shared by different adjacent patches at every
common interval; matching whole-window integrals alone do not suffice.
Coarse-versus-fine disagreement is expected input, not a rejection condition.

Only uncovered coarse cells and all fine cells enter the published account.
For an active coarse face against a refined cell, replace its original integral
by MINUS the sum of the two opposite fine outward total-rate integrals. These
are whole-face mass/time measurements: do not multiply by face length or area.
Its correction is replacement minus original. Other active faces keep their
original integral, and fine cells receive no correction. Covered coarse masses
and flows are validated but not added. A common boundary between fine patches
is internal, not an exterior boundary and not a coarse correction. A refined
face on the coarse domain's exterior is exterior; its covered coarse counterpart
does not contribute. Point contact is not a shared face.
Per cell: change=M(b)-M(a); original_outflow sums its four original directed
integrals; correction sums its interface corrections; outflow=original_outflow
+correction; residual=change+outflow. Zero and negative values remain valid.
Anomalies are exactly the active cells with nonzero residual. Global totals use
only active cells, and their total residual equals total change plus true
exterior outflow. All active internal contributions must cancel.

Missing IDs raise KeyError. Malformed arguments, mismatch, illegal geometry,
missing endpoints, gap/overlap or same-level face conflict raise ValueError.
Invalid registration leaves no hierarchy. Any failed audit leaves no new result
or partial result rows and preserves existing hierarchies, inputs and results.
SQLite errors propagate unchanged. No automatic filling or averaging is allowed.
Reuse the V1 transaction facilities; storage organization is otherwise open.

Report delivered through existing read_result/export_result
-----------------------------------------------------------
V1 result shape and behavior stay unchanged. Hierarchy reads return the following
dictionary (extra implementation metadata is allowed if fixed and exportable):
kind: "hierarchy"; hierarchy_id: h; window: [a,b];
bindings: {"coarse":[grid_id,observation_id],
           "fine":[[grid_id,observation_id],...]}, fine sorted by grid ID;
covered: sorted coarse [i,j] cells;
cells: sorted by (grid_id,i,j), each with grid_id, cell:[i,j], mass_start,
mass_end, change, original_outflow, correction, outflow, residual and faces.
Every cell has four faces, each with face: V1 local [axis,i,j], side,
original, correction and integral. integral is the corrected outward amount.
anomalous_cells: sorted [{"grid_id":g,"cell":[i,j]},...] using the same cell order;
boundary: faces sorted by (grid_id,cell,side), each with grid_id, cell, side,
face (V1 local ID), integral, plus total_outflow,total_change,total_residual;
sources: all original window endpoint masses and clipped flux contributions
from all registered grids, in the existing V1 source-entry representation and
V1 source sort order. Covered inputs remain available as evidence. There are
no implicit synthetic source records; source IDs still have their V1 meanings.

corrections: sorted by (coarse.grid_id,coarse.cell,coarse.side), one entry per
active coarse/fine interface, INCLUDING zero correction:
{"coarse":{"grid_id":g,"cell":[i,j],"side":s},
 "fine":[{"grid_id":g1,"cell":[u,v],"side":opposite_s},...],
 "segments":[{"start":u,"end":v,"coarse_source":source_id,
              "fine_sources":[source_id,...],
              "original":amount,"replacement":amount,"delta":amount},...]}.
The fine list sorts by (grid_id,cell,side) and has exactly two faces;
fine_sources follows that list. Segments are the nonempty successive intervals
from the UNION of this coarse face's and these two fine faces' clipped source
endpoints, including a,b. Do not introduce unrelated face breakpoints or merge
across a source change. original and replacement are integrals on that interval,
oriented outward from the ACTIVE coarse cell; delta=replacement-original.
Source entries give the full original records, so a reviewer can check direction,
duration and both amounts without opening the archive or running the algorithm.
Local IDs in this report are always scoped by grid_id; equal local IDs in
different grids must never be merged. Physical values/times are Fraction.

A read only decodes the saved report, not re-executes the hierarchy or loads
current inputs. Source corrections, caller-list edits, returned-container edits
and reopening cannot change an existing hierarchy or result. Export all V2
fields through the existing exact codec; decoding an export equals read_result,
and repeated exports are byte-identical. Export errors follow the V1 rules.
Keep compatible existing archives and their V1 reports/user SQLite objects.

Worked public inputs and observable results
------------------------------------------
example(service) creates a 2x1 coarse grid and a 2x2 patch over its right cell.
Endpoint masses are zero. Coarse internal outward rates are +8 and -8;
patch west lower and upper rates are -2 and -3. Other rates are zero.
coarse, patches = example(service)
h = service.register_hierarchy(coarse, patches)
rid = service.audit_hierarchy(h, 0, 1)
report = service.read_result(rid)
The active left coarse cell has (original_outflow,correction,outflow,residual)
=(8,-3,5,5). Fine west cells have residuals -2,-3 and the other fine cells 0.
There are five active cells. Global residual and exterior outflow are zero;
the coarse cell and those two fine cells remain anomalous.

example(service, asynchronous=True) uses west lower rate -1 on [0,1/3),
then -5/2 on [1/3,1); upper rate -2 on [0,1/2), then -4 on [1/2,1).
Their integrals remain -2,-3. The correction has intervals [0,1/3),[1/3,1/2),
[1/2,1), with original amounts 8/3,4/3,4; replacement amounts 1,3/4,13/4;
and deltas -5/3,-7/12,-3/4. No endpoint rounding is permitted.

adjacent(service) covers both coarse cells with separate patches. At their
common lower fine face, rates +1,+3 oppose -1,-3 on halves of [0,1).
All eight fine cells are active, corrections are empty and exterior total is 0;
two local residuals are +2,-2. adjacent(service, conflict=True) instead uses
-3,-1 on the right: every grid passes its own V1 audit and integrals cancel,
but the hierarchy audit must reject the segmentwise cross-patch conflict.

Delivery and validation
-----------------------
Preserve every original interface and test byte. Implement spatial coverage
registration, its handoff to the balance responsibilities, and persistent result
delivery. Explain the representation, direction/source ownership and active-cell
invariants in a short delivery note. Do not require a particular internal class
or file arrangement.

Use the existing .pointwise-runtime.json format (version 1, kind cli; setup is a
list of command argv arrays; verify and demo are command argv arrays).
Update verify to run all original regressions AND hierarchy business checks;
demo must show a real asynchronous hierarchy report, not a success message.
The following public commands already name the input/check entry points:
python3.11 -B hierarchy_cases.py v1       # normal V1 input checks
python3.11 -B hierarchy_cases.py verify   # extension checks, needs implementation
python3.11 -B hierarchy_cases.py demo     # actual extension export
The supplied checker is a starter oracle, not the whole validation obligation.
Also independently check all four interface directions, nonunit/fractional
geometry, multiple interfaces of one coarse cell, refined exterior boundaries,
zero corrections, invalid shape/alignment/overlap, wrong bindings, temporal
overlap and original same-grid conflicts. Check a real SQLite publication failure
against the chosen storage, no partial result, unchanged older result and a
successful retry. Retain original full discovery and its nonzero failure exit.
Reopen both V1 and hierarchy results, correct a source after registration, mutate
input/output containers, and verify detached export arithmetic. All are the
public business boundaries above; no performance benchmark is required.
"""
from fractions import Fraction as F
import io
import json
import sys
import tempfile
from pathlib import Path
from gridledger.delivery.service import LedgerService
from gridledger.delivery.codec import decode_value
from gridledger.archive.grid_store import load_grid_revision
from gridledger.observations.model import FluxRecord
from tests.test_balance import uniform_records

def put(service, grid_id, *, prefix, rates=None, values=None, omit=None):
    grid = load_grid_revision(service.connection, grid_id)
    masses, fluxes = uniform_records(grid, prefix=prefix, rates=rates, values=values)
    if omit == "mass":
        masses = masses[1:]
    if omit == "flux":
        fluxes = fluxes[1:]
    return service.put_observations(grid_id, masses, fluxes)

def example(service, *, asynchronous=False, omit=None):
    coarse = service.put_grid("coarse", (2, 1))
    fine = service.put_grid("right-refinement", (2, 2), origin=(1, 0),
                            spacing=(F(1, 2), F(1, 2)))
    cr = put(service, coarse, prefix="C",
             rates={((0, 0), "east"): 8, ((1, 0), "west"): -8})
    west0 = [(0, F(1, 3), -1), (F(1, 3), 1, F(-5, 2))] if asynchronous else -2
    west1 = [(0, F(1, 2), -2), (F(1, 2), 1, -4)] if asynchronous else -3
    fr = put(service, fine, prefix="F", omit=omit,
             rates={((0, 0), "west"): west0, ((0, 1), "west"): west1})
    return (coarse, cr), [(fine, fr)]

def adjacent(service, *, conflict=False):
    coarse = service.put_grid("both-refined", (2, 1))
    cr = put(service, coarse, prefix="A-C")
    left = service.put_grid("left", (2, 2), spacing=(F(1, 2), F(1, 2)))
    right = service.put_grid("right", (2, 2), origin=(1, 0),
                             spacing=(F(1, 2), F(1, 2)))
    lr = put(service, left, prefix="A-L",
             rates={((1, 0), "east"): [(0, F(1, 2), 1), (F(1, 2), 1, 3)]})
    rates = [-3, -1] if conflict else [-1, -3]
    rr = put(service, right, prefix="A-R",
             rates={((0, 0), "west"): [(0, F(1, 2), rates[0]), (F(1, 2), 1, rates[1])]})
    return (coarse, cr), [(left, lr), (right, rr)]

def exported(service, result_id):
    target = io.StringIO()
    service.export_result(result_id, target)
    text = target.getvalue()
    return text, decode_value(json.loads(text))

def check_example(report, coarse, fine, *, asynchronous=False):
    assert report["kind"] == "hierarchy"
    assert report["bindings"] == {"coarse": list(coarse), "fine": [list(fine)]}
    assert report["covered"] == [[1, 0]]
    cells = {(c["grid_id"], tuple(c["cell"])): c for c in report["cells"]}
    expected = {(coarse[0], (0, 0)): (8, -3, 5, 5),
                (fine[0], (0, 0)): (-2, 0, -2, -2),
                (fine[0], (0, 1)): (-3, 0, -3, -3),
                (fine[0], (1, 0)): (0, 0, 0, 0),
                (fine[0], (1, 1)): (0, 0, 0, 0)}
    assert set(cells) == set(expected)
    for key, values in expected.items():
        c = cells[key]
        assert (c["original_outflow"], c["correction"], c["outflow"], c["residual"]) == values
        assert c["change"] == 0
        assert sum(f["integral"] for f in c["faces"]) == c["outflow"]
    assert report["boundary"]["total_outflow"] == 0
    assert report["boundary"]["total_change"] == 0
    assert report["boundary"]["total_residual"] == 0
    actual_anomalies = {(x["grid_id"], tuple(x["cell"])) for x in report["anomalous_cells"]}
    assert actual_anomalies == {k for k, v in expected.items() if v[-1]}
    assert len(report["corrections"]) == 1
    entry = report["corrections"][0]
    assert entry["coarse"] == {"grid_id": coarse[0], "cell": [0, 0], "side": "east"}
    assert entry["fine"] == [
        {"grid_id": fine[0], "cell": [0, 0], "side": "west"},
        {"grid_id": fine[0], "cell": [0, 1], "side": "west"}]
    spans = [(0, F(1, 3), F(8, 3), 1, F(-5, 3)),
             (F(1, 3), F(1, 2), F(4, 3), F(3, 4), F(-7, 12)),
             (F(1, 2), 1, 4, F(13, 4), F(-3, 4))] if asynchronous else [(0, 1, 8, 5, -3)]
    got = [(x["start"], x["end"], x["original"], x["replacement"], x["delta"])
           for x in entry["segments"]]
    assert got == spans
    sources = {s["record"]["source_id"]: s["record"] for s in report["sources"]}
    for segment in entry["segments"]:
        dt = segment["end"] - segment["start"]
        cs = sources[segment["coarse_source"]]
        fs = [sources[sid] for sid in segment["fine_sources"]]
        assert (cs["grid_id"], tuple(cs["cell_id"]), cs["side"]) == (coarse[0], (0, 0), "east")
        assert [(r["grid_id"], tuple(r["cell_id"]), r["side"]) for r in fs] == [
            (fine[0], (0, 0), "west"), (fine[0], (0, 1), "west")]
        for r in [cs] + fs:
            assert r["start"] <= segment["start"] < segment["end"] <= r["end"]
        assert cs["rate"] * dt == segment["original"]
        assert -sum(r["rate"] for r in fs) * dt == segment["replacement"]
        assert segment["replacement"] - segment["original"] == segment["delta"]

def v1_inputs():
    for asynchronous in (False, True):
        with LedgerService() as service:
            coarse, patches = example(service, asynchronous=asynchronous)
            reports = [service.read_result(service.audit(service.compile_window(g, r, 0, 1)))
                       for g, r in [coarse] + patches]
            active_coarse = reports[0]["cells"][0]
            fine_total = sum(c["outflow"] for c in reports[1]["cells"])
            assert (active_coarse["outflow"], fine_total) == (8, -5)
            assert active_coarse["outflow"] + fine_total == 3
    for conflict in (False, True):
        with LedgerService() as service:
            coarse, patches = adjacent(service, conflict=conflict)
            for g, r in [coarse] + patches:
                service.audit(service.compile_window(g, r, 0, 1))
    print("V1_INPUTS_OK independent active interface = 8-5 = 3; required joint interface = 5-5 = 0")

def expect_value_error(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("expected ValueError")

def verify():
    for asynchronous in (False, True):
        with LedgerService() as service:
            coarse, patches = example(service, asynchronous=asynchronous)
            h = service.register_hierarchy(coarse, patches)
            rid = service.audit_hierarchy(h, 0, 1)
            report = service.read_result(rid)
            check_example(report, coarse, patches[0], asynchronous=asynchronous)
            text, decoded = exported(service, rid)
            assert decoded == report
            assert exported(service, rid)[0] == text
            report["cells"].clear()
            report["corrections"].clear()
            check_example(service.read_result(rid), coarse, patches[0], asynchronous=asynchronous)
            expect_value_error(lambda: service.register_hierarchy(coarse, patches + patches))
            expect_value_error(lambda: service.audit_hierarchy(h, 1, 0))
    for omit in ("mass", "flux"):
        with LedgerService() as service:
            coarse, patches = example(service, omit=omit)
            h = service.register_hierarchy(coarse, patches)
            expect_value_error(lambda: service.audit_hierarchy(h, 0, 1))
    for conflict in (False, True):
        with LedgerService() as service:
            coarse, patches = adjacent(service, conflict=conflict)
            h = service.register_hierarchy(coarse, patches)
            if conflict:
                expect_value_error(lambda: service.audit_hierarchy(h, 0, 1))
            else:
                r = service.read_result(service.audit_hierarchy(h, 0, 1))
                assert len(r["cells"]) == 8
                assert r["corrections"] == []
                assert r["boundary"]["total_residual"] == 0
                assert sorted(c["residual"] for c in r["cells"]) == [-2, 0, 0, 0, 0, 0, 0, 2]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hierarchy.sqlite"
        with LedgerService(path) as service:
            coarse, patches = example(service)
            h = service.register_hierarchy(coarse, patches)
            rid = service.audit_hierarchy(h, 0, 1)
            original = service.read_result(rid)
            original_text = exported(service, rid)[0]
            fine, fr = patches[0]
            newer = service.put_observations(fine, fluxes=[
                FluxRecord(fine, (0, 0), "west", 0, 1, -4, "corrected-F-west")])
            newer_patches = [(fine, newer)]
            new_h = service.register_hierarchy(coarse, newer_patches)
            new = service.read_result(service.audit_hierarchy(new_h, 0, 1))
            active = next(c for c in new["cells"] if c["grid_id"] == coarse[0])
            assert (active["original_outflow"], active["correction"], active["outflow"]) == (8, -1, 7)
            patches.clear()
            assert service.read_result(rid) == original
        with LedgerService(path) as service:
            assert service.read_result(rid) == original
            assert exported(service, rid)[0] == original_text
            check_example(service.read_result(service.audit_hierarchy(h, 0, 1)), coarse, (fine, fr))
    print("HIERARCHY_CHECKS_OK")

def demo():
    with LedgerService() as service:
        coarse, patches = example(service, asynchronous=True)
        h = service.register_hierarchy(coarse, patches)
        rid = service.audit_hierarchy(h, 0, 1)
        check_example(service.read_result(rid), coarse, patches[0], asynchronous=True)
        print(exported(service, rid)[0], end="")

if __name__ == "__main__":
    modes = {"v1": v1_inputs, "verify": verify, "demo": demo}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        raise SystemExit("usage: python3.11 -B hierarchy_cases.py v1|verify|demo")
    modes[sys.argv[1]]()
