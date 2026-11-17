from dataclasses import asdict
from fractions import Fraction
from gridledger.archive.grid_store import load_grid_revision
from gridledger.archive.observation_store import load_observation_revision
from gridledger.balances.execution import validate_internal_faces
from gridledger.delivery.codec import RESULT_FORMAT_VERSION
from gridledger.grids.faces import cell_face_uses
from gridledger.grids.geometry import exact_value
from gridledger.windows.compiler import compile_window_plan
from gridledger.windows.coverage import iter_common_intervals
from gridledger.hierarchy.registration import covered_cell

OPPOSITE_SIDE = {"west": "east", "east": "west", "south": "north", "north": "south"}
SIDE_OFFSET = {"west": (-1, 0), "east": (1, 0), "south": (0, -1), "north": (0, 1)}

def neighbor_cell(cell_id, side):
    di, dj = SIDE_OFFSET[side]
    return (cell_id[0] + di, cell_id[1] + dj)

def interface_fine_faces(side):
    # The two fine cells of the refining patch that sit against the shared
    # coarse edge, together with their outward side opposite to the coarse one.
    opposite = OPPOSITE_SIDE[side]
    if side in ("west", "east"):
        i = 0 if side == "east" else 1
        cells = ((i, 0), (i, 1))
    else:
        j = 0 if side == "north" else 1
        cells = ((0, j), (1, j))
    return tuple((cell, opposite) for cell in cells)

def face_total(plan, cell_id, side):
    return sum((c.integral for c in plan.face_contributions(cell_id, side)), Fraction(0))

def covering_contribution(contributions, start, end):
    matches = [c for c in contributions if c.start <= start and c.end >= end]
    if len(matches) != 1:
        raise ValueError("interface evidence does not cover a segment exactly once")
    return matches[0]

def source_sort_key(entry):
    # The V1 source ordering, extended across grids by its grid_id component.
    record = entry["record"]
    return (entry["kind"], record["grid_id"], record["cell_id"],
            record.get("side", ""), record.get("time", record.get("start")),
            record["source_id"])

def validate_cross_patch_faces(grids, plans, patch_grid_ids):
    # Fine faces of different patches that occupy the same geometric segment
    # must oppose instantaneously, exactly like V1 same-grid internal faces.
    # Point contact produces no common segment and is not a shared face.
    segment_uses = {}
    for grid_id in patch_grid_ids:
        for face in grids[grid_id].faces:
            key = tuple(sorted(face.endpoints))
            for use in face.uses:
                segment_uses.setdefault(key, []).append(use)
    for key, uses in segment_uses.items():
        if len({use.grid_id for use in uses}) < 2:
            continue
        if len(uses) != 2:
            raise ValueError("inconsistent cross-patch face ownership")
        left, right = uses
        lhs = plans[left.grid_id].face_contributions(left.cell_id, left.side)
        rhs = plans[right.grid_id].face_contributions(right.cell_id, right.side)
        for start, end, lrecord, rrecord in iter_common_intervals(lhs, rhs):
            if lrecord.rate + rrecord.rate != 0:
                raise ValueError("cross-patch face conflict on " + str((key, start, end,
                                 lrecord.source_id, rrecord.source_id)))

def build_interface_correction(coarse_plan, fine_plan, coarse_grid_id, cell_id, side,
                               fine_faces, a, b):
    # Segments are the successive intervals of the union of this coarse face's
    # and the two opposite fine faces' clipped source endpoints, including a,b.
    coarse_contributions = coarse_plan.face_contributions(cell_id, side)
    fine_contributions = [fine_plan.face_contributions(cell, fine_side)
                          for _, cell, fine_side in fine_faces]
    points = {a, b}
    for contribution in coarse_contributions:
        points.update((contribution.start, contribution.end))
    for contributions in fine_contributions:
        for contribution in contributions:
            points.update((contribution.start, contribution.end))
    ordered = sorted(points)
    segments, correction = [], Fraction(0)
    for start, end in zip(ordered, ordered[1:]):
        coarse_part = covering_contribution(coarse_contributions, start, end)
        fine_parts = [covering_contribution(contributions, start, end)
                      for contributions in fine_contributions]
        duration = end - start
        original = coarse_part.record.rate * duration
        replacement = -sum((part.record.rate for part in fine_parts), Fraction(0)) * duration
        delta = replacement - original
        correction += delta
        segments.append({"start": start, "end": end,
                         "coarse_source": coarse_part.record.source_id,
                         "fine_sources": [part.record.source_id for part in fine_parts],
                         "original": original, "replacement": replacement, "delta": delta})
    entry = {"coarse": {"grid_id": coarse_grid_id, "cell": list(cell_id), "side": side},
             "fine": [{"grid_id": grid_id, "cell": list(cell), "side": fine_side}
                      for grid_id, cell, fine_side in fine_faces],
             "segments": segments}
    return entry, correction

def execute_hierarchy(connection, binding, a, b):
    a, b = exact_value(a), exact_value(b)
    if a >= b:
        raise ValueError("window must satisfy a < b")
    coarse_grid_id, coarse_revision = binding.coarse
    ordered_pairs = [binding.coarse] + [(grid_id, revision)
                                        for grid_id, revision, _ in binding.patches]
    grids, plans, endpoints = {}, {}, {}
    for grid_id, revision_id in ordered_pairs:
        grid = load_grid_revision(connection, grid_id)
        observation = load_observation_revision(connection, revision_id)
        if observation.grid_id != grid.id:
            raise ValueError("hierarchy grid/observation mismatch")
        # V1 window compilation proves endpoint masses and exactly-once directed
        # coverage for every cell of every registered grid, covered ones included.
        plan = compile_window_plan(grid, observation, a, b)
        validate_internal_faces(grid, plan)
        if len(plan.endpoints) != len(grid.cells):
            raise ValueError("endpoint membership mismatch")
        for cell, pair in zip(grid.cells, plan.endpoints):
            endpoints[(grid_id, cell.id)] = pair
        grids[grid_id], plans[grid_id] = grid, plan
    coarse_grid = grids[coarse_grid_id]
    coverage = {}
    for grid_id, _, cell in binding.patches:
        if covered_cell(coarse_grid, grids[grid_id]) != cell:
            raise ValueError("stored hierarchy coverage mismatch")
        coverage[cell] = grid_id
    validate_cross_patch_faces(grids, plans, [grid_id for grid_id, _, _ in binding.patches])

    active_coarse = sorted(cell.id for cell in coarse_grid.cells if cell.id not in coverage)
    corrections, face_corrections = [], {}
    for cell_id in active_coarse:
        for use in cell_face_uses(coarse_grid, cell_id):
            neighbor = neighbor_cell(cell_id, use.side)
            if neighbor not in coverage:
                continue
            fine_grid_id = coverage[neighbor]
            fine_faces = sorted(((fine_grid_id, cell, fine_side)
                                 for cell, fine_side in interface_fine_faces(use.side)),
                                key=lambda item: (item[0], item[1], item[2]))
            entry, correction = build_interface_correction(
                plans[coarse_grid_id], plans[fine_grid_id], coarse_grid_id,
                cell_id, use.side, fine_faces, a, b)
            corrections.append(entry)
            face_corrections[(cell_id, use.side)] = correction
    corrections.sort(key=lambda entry: (entry["coarse"]["grid_id"],
                                        entry["coarse"]["cell"], entry["coarse"]["side"]))

    cells = []
    for cell_id in active_coarse:
        start_record, end_record = endpoints[(coarse_grid_id, cell_id)]
        change = end_record.value - start_record.value
        faces, original_outflow, correction_total = [], Fraction(0), Fraction(0)
        for use in cell_face_uses(coarse_grid, cell_id):
            original = face_total(plans[coarse_grid_id], cell_id, use.side)
            correction = face_corrections.get((cell_id, use.side), Fraction(0))
            faces.append({"face": list(use.face_id), "side": use.side, "original": original,
                          "correction": correction, "integral": original + correction})
            original_outflow += original
            correction_total += correction
        outflow = original_outflow + correction_total
        cells.append({"grid_id": coarse_grid_id, "cell": list(cell_id),
                      "mass_start": start_record.value, "mass_end": end_record.value,
                      "change": change, "original_outflow": original_outflow,
                      "correction": correction_total, "outflow": outflow,
                      "residual": change + outflow, "faces": faces})
    for grid_id, _, _ in binding.patches:
        grid = grids[grid_id]
        for cell in grid.cells:
            start_record, end_record = endpoints[(grid_id, cell.id)]
            change = end_record.value - start_record.value
            faces, original_outflow = [], Fraction(0)
            for use in cell_face_uses(grid, cell.id):
                original = face_total(plans[grid_id], cell.id, use.side)
                faces.append({"face": list(use.face_id), "side": use.side, "original": original,
                              "correction": Fraction(0), "integral": original})
                original_outflow += original
            cells.append({"grid_id": grid_id, "cell": list(cell.id),
                          "mass_start": start_record.value, "mass_end": end_record.value,
                          "change": change, "original_outflow": original_outflow,
                          "correction": Fraction(0), "outflow": original_outflow,
                          "residual": change + original_outflow, "faces": faces})
    cells.sort(key=lambda cell: (cell["grid_id"], cell["cell"]))

    shape = coarse_grid.shape
    boundary_faces = []
    for cell_id in active_coarse:
        for use in cell_face_uses(coarse_grid, cell_id):
            neighbor = neighbor_cell(cell_id, use.side)
            if not (0 <= neighbor[0] < shape[0] and 0 <= neighbor[1] < shape[1]):
                boundary_faces.append({"grid_id": coarse_grid_id, "cell": list(cell_id),
                                       "side": use.side, "face": list(use.face_id),
                                       "integral": face_total(plans[coarse_grid_id],
                                                              cell_id, use.side)})
    for grid_id, _, covered in binding.patches:
        grid = grids[grid_id]
        for cell in grid.cells:
            for use in cell_face_uses(grid, cell.id):
                fi, fj = cell.id
                on_patch_edge = ((use.side == "west" and fi == 0) or
                                 (use.side == "east" and fi == 1) or
                                 (use.side == "south" and fj == 0) or
                                 (use.side == "north" and fj == 1))
                if not on_patch_edge:
                    continue
                neighbor = neighbor_cell(covered, use.side)
                if 0 <= neighbor[0] < shape[0] and 0 <= neighbor[1] < shape[1]:
                    continue  # coarse/fine interface or cross-patch internal face
                boundary_faces.append({"grid_id": grid_id, "cell": list(cell.id),
                                       "side": use.side, "face": list(use.face_id),
                                       "integral": face_total(plans[grid_id],
                                                              cell.id, use.side)})
    boundary_faces.sort(key=lambda face: (face["grid_id"], face["cell"], face["side"]))

    total_outflow = sum((face["integral"] for face in boundary_faces), Fraction(0))
    total_change = sum((cell["change"] for cell in cells), Fraction(0))
    total_residual = sum((cell["residual"] for cell in cells), Fraction(0))
    if sum((cell["outflow"] for cell in cells), Fraction(0)) != total_outflow:
        raise ValueError("active internal contributions do not cancel")
    if total_residual != total_change + total_outflow:
        raise ValueError("global conservation identity violated")
    anomalies = [{"grid_id": cell["grid_id"], "cell": list(cell["cell"])}
                 for cell in cells if cell["residual"] != 0]

    sources = []
    for grid_id, _ in ordered_pairs:
        plan = plans[grid_id]
        for pair in plan.endpoints:
            for record in pair:
                sources.append({"kind": "mass", "record": asdict(record)})
        for contribution in plan.contributions:
            sources.append({"kind": "flux", "record": asdict(contribution.record),
                            "face": list(contribution.face_id),
                            "clipped": [contribution.start, contribution.end],
                            "duration": contribution.duration,
                            "integral": contribution.integral})
    sources.sort(key=source_sort_key)

    return {"format_version": RESULT_FORMAT_VERSION, "kind": "hierarchy",
            "hierarchy_id": binding.id, "window": [a, b],
            "bindings": {"coarse": [coarse_grid_id, coarse_revision],
                         "fine": [[grid_id, revision]
                                  for grid_id, revision, _ in binding.patches]},
            "covered": [list(cell) for cell in sorted(coverage)],
            "cells": cells, "anomalous_cells": anomalies,
            "boundary": {"faces": boundary_faces, "total_outflow": total_outflow,
                         "total_change": total_change, "total_residual": total_residual},
            "corrections": corrections, "sources": sources}
