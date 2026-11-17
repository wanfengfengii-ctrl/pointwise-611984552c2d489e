from dataclasses import dataclass

@dataclass(frozen=True)
class HierarchyBinding:
    # Fixed two-level registration: one coarse (grid_id, observation_id) pair and
    # fine (grid_id, observation_id, covered coarse cell) triples sorted by grid ID.
    id: int | None
    coarse: tuple
    patches: tuple
