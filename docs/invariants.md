# GridLedger invariants

A fixed grid ID scopes every cell (i,j) and directed side. A geometric face uses
the positive x or y direction as a stable reference. Interior faces have exactly
two opposite uses; exterior faces have one. Stored specifications reconstruct
the same rational geometry and identities on reopen.

Records carry immutable source IDs, units and complete grid-scoped logical keys.
A revision stores concrete source membership. Replacements never rewrite source
content or old revision membership. Validation finishes before a batch writes;
SQL still encloses the complete new revision and its sources in one transaction.

A window chooses exact endpoint records and clips every directed face to [a,b).
A sorted cursor proves coverage exactly once, including zero-rate segments.
A contribution stores both original and clipped intervals, and integral equals
outward total rate times clipped duration. Face length and direction are not
applied again. The common partition of each interior pair proves instantaneous
opposition, which is stronger than full-window integral cancellation.

For each cell, residual = M(b)-M(a)+sum(outward integrals). The anomaly set is
computed cell by cell. Independently, internal integrals cancel in pairs and
the sum of cell residuals equals global mass change plus exterior outflow.
Nonzero residuals are observations to report, not permission to alter input.

Plans save their concrete endpoints and contributions, and audit compares a
provided plan to its persisted binding. Results save the complete delivery
snapshot split into root, cell, exterior-face and source rows. Their counts and
ordinals must agree on reading. Publication commits all rows once or rolls all
of them back. Fixed reads decode persisted rows; they do not execute plans.

Fraction encoding preserves reduced integer numerator and positive denominator.
No float conversion is used. Every read creates detached containers. Exports
sort stable business identities and retain all source records and contributions.
A destination exception cannot mutate the stored result.

Schema initialization checks required columns, keys, relationships and version
inside its own transaction. Compatible user tables, indexes and triggers are
retained. Initialization never clears observations, recreates sample data or
advances business revisions. Physical grid and observation IDs are archive-local.

## Two-level hierarchies (V2)

A hierarchy registration freezes one coarse (grid, observation revision) pair
and an ordered set of fine (grid, observation revision, covered cell) triples
in one atomic row. Fine grids are native grid revisions with shape (2,2) and
exactly half the coarse spacing; the covered cell is inferred from exact
origin alignment. Registrations are append-only and survive reopening.

The joint audit reads only the registered revisions. Every registered grid,
covered coarse cells included, must independently satisfy the V1 endpoint,
coverage and same-level opposition rules; fine faces shared by two patches
must also oppose on every common time interval. Coarse-versus-fine
disagreement is input, not an error.

Covered coarse cells leave the active account after validation; their evidence
stays in the delivered sources. An active coarse face toward a refined cell
keeps its original integral and is corrected to minus the sum of the two
opposite fine outward integrals; each correction is delivered with per-segment
original, replacement, delta and the owning source IDs. All active internal
contributions cancel, refined exterior faces replace their covered coarse
counterparts on the boundary, and the global identity
total_residual = total_change + exterior outflow is verified before commit.

Hierarchy results publish root plus ordinal child rows in one transaction and
share the result ID space with V1 results, so one read/export path serves
both. Reads decode stored rows only; corrections, container edits and
reopening cannot move a fixed binding or result. See docs/hierarchy.md for
the full delivery note.
