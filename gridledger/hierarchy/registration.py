from gridledger.archive.grid_store import load_grid_revision
from gridledger.archive.observation_store import load_observation_revision
from gridledger.hierarchy.model import HierarchyBinding

MAX_COARSE_CELLS = 16
MAX_PATCHES = 8

def parse_binding_pair(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(label + " must be a (grid_id, observation_id) pair")
    grid_id, observation_id = value
    for candidate in (grid_id, observation_id):
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
            raise ValueError(label + " identities must be positive integers")
    return grid_id, observation_id

def covered_cell(coarse_grid, fine_grid):
    # A fine grid refines one coarse cell: shape (2,2), exactly half the coarse
    # spacing on both axes, and an origin that lands exactly on a coarse cell
    # corner. All comparisons are exact Fraction arithmetic.
    if fine_grid.shape != (2, 2):
        raise ValueError("fine grid must have shape (2,2)")
    if tuple(fine_grid.spacing) != tuple(d / 2 for d in coarse_grid.spacing):
        raise ValueError("fine spacing must be half the coarse spacing")
    offsets = []
    for axis in (0, 1):
        offset = (fine_grid.origin[axis] - coarse_grid.origin[axis]) / coarse_grid.spacing[axis]
        if offset.denominator != 1:
            raise ValueError("fine grid is misaligned with the coarse cells")
        offsets.append(int(offset))
    cell = tuple(offsets)
    if any(cell[axis] < 0 or cell[axis] >= coarse_grid.shape[axis] for axis in (0, 1)):
        raise ValueError("fine grid does not cover a complete coarse cell")
    return cell

def build_hierarchy_binding(connection, coarse, patches):
    # Validates the public registration contract and returns an immutable binding
    # holding detached copies of the caller-owned containers. Nothing is stored
    # here; a failed validation therefore leaves no hierarchy behind.
    coarse_pair = parse_binding_pair(coarse, "coarse")
    if not isinstance(patches, (list, tuple)) or len(patches) == 0:
        raise ValueError("patches must be a nonempty list or tuple")
    if len(patches) > MAX_PATCHES:
        raise ValueError("at most 8 patches per hierarchy")
    patch_pairs = [parse_binding_pair(patch, "patch") for patch in patches]
    coarse_grid = load_grid_revision(connection, coarse_pair[0])
    coarse_observation = load_observation_revision(connection, coarse_pair[1])
    if coarse_observation.grid_id != coarse_grid.id:
        raise ValueError("coarse grid/observation mismatch")
    if len(coarse_grid.cells) > MAX_COARSE_CELLS:
        raise ValueError("coarse grid has more than 16 cells")
    seen_grids, seen_cells, entries = set(), set(), []
    for grid_id, observation_id in patch_pairs:
        if grid_id in seen_grids or grid_id == coarse_grid.id:
            raise ValueError("repeated grid in hierarchy")
        seen_grids.add(grid_id)
        fine_grid = load_grid_revision(connection, grid_id)
        observation = load_observation_revision(connection, observation_id)
        if observation.grid_id != fine_grid.id:
            raise ValueError("fine grid/observation mismatch")
        cell = covered_cell(coarse_grid, fine_grid)
        if cell in seen_cells:
            raise ValueError("repeated coarse cell coverage")
        seen_cells.add(cell)
        entries.append((grid_id, observation_id, cell))
    entries.sort(key=lambda entry: entry[0])
    return HierarchyBinding(None, coarse_pair, tuple(entries))
