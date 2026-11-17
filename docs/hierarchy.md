# Two-level hierarchy delivery note (V2)

This note is the handoff summary for the coarse/fine joint audit: what is
stored, who owns each direction and source, and which invariants the active
account keeps. The public contract and worked inputs live in
`hierarchy_cases.py`; the V1 behaviour is unchanged (see README.md).

## Representation

* A hierarchy is one fixed registration row in `hierarchies`: a coarse
  `(grid_id, observation_id)` pair plus one to eight fine
  `(grid_id, observation_id, covered_cell)` triples sorted by grid ID, encoded
  with the exact JSON codec. It is validated and inserted atomically; a failed
  registration leaves no row. It never changes afterwards — a later
  registration is a new row.
* A fine grid is an ordinary immutable V1 grid revision with shape `(2,2)` and
  exactly half the coarse spacing on both axes. The covered coarse cell is
  inferred from exact origin alignment (Fraction arithmetic, negative and
  fractional coordinates included); callers never supply correspondence maps.
  Repeated grids, repeated coverage, partial/outside/misaligned coverage and
  grid/observation mismatches are rejected.
* `audit_hierarchy(h, a, b)` reads exactly the revisions named in the binding —
  never current heads — and compiles a V1 window per registered grid. Every
  cell of every registered grid, covered coarse cells included, must prove
  endpoint masses and exactly-once directed-face coverage of `[a,b)`.
* The published report is split into `hierarchy_results` plus ordinal child
  rows (cells, boundary faces, sources, corrections), committed in one
  transaction. Hierarchy and V1 results share one positive integer ID space,
  so `read_result`/`export_result` serve both kinds; reads only decode stored
  rows and never re-execute the audit.

## Direction and source ownership

* Rates stay outward total rates of their owning cell, already integrated over
  the face. Nothing is multiplied by face length or area at any level.
* Same-level opposition is validated segmentwise before any accounting: V1
  internal-face validation runs on each registered grid (covered coarse faces
  included), and fine faces shared by two different patches must oppose on
  every common time interval. Coarse-versus-fine disagreement is expected
  input, not an error. Point contact is not a shared face.
* On an active-coarse/fine interface the coarse face keeps its original
  integral as evidence and gains a corrected integral: MINUS the sum of the
  two opposite fine outward integrals. The correction is replacement minus
  original; fine faces themselves are never corrected.
* Each correction entry carries the union of the three faces' clipped source
  endpoints as segments. Every segment names its coarse source and its two
  fine sources and stores original, replacement and delta, so a reviewer can
  recompute direction, duration and both amounts from `sources` alone, without
  opening the archive or running the algorithm. There are no synthetic source
  records; source IDs keep their V1 meaning.

## Active-cell invariants

* Covered coarse cells are fully validated but leave the active account; their
  masses and flows remain in `sources` as evidence. Active cells are the
  uncovered coarse cells plus all fine cells.
* Every active internal contribution cancels: same-level pairs by validated
  opposition, coarse/fine interfaces by construction of the replacement, and
  cross-patch pairs by the segmentwise check. The implementation verifies the
  cancellation and the global identity before publishing.
* A refined face on the coarse domain's exterior is exterior; the covered
  coarse counterpart does not contribute. A fine face toward an active coarse
  cell or another patch is internal, never boundary.
* Per active cell: `change = M(b)-M(a)`,
  `outflow = original_outflow + correction`, `residual = change + outflow`.
  Anomalies are exactly the active cells with nonzero residual; a zero global
  residual never hides them. Global totals use active cells only and satisfy
  `total_residual = total_change + true exterior outflow`.
* Fixed bindings and published results do not drift under source corrections,
  caller-container edits, returned-container edits or reopening. Failed
  registrations and failed audits leave no partial rows; SQLite errors
  propagate unchanged and roll back their transaction.
