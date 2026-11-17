import json
from gridledger.archive.schema import archive_transaction
from gridledger.delivery.codec import canonical_json, decode_value
from gridledger.hierarchy.model import HierarchyBinding

RESULT_TABLES = ("hierarchy_result_cells", "hierarchy_result_faces",
                 "hierarchy_result_sources", "hierarchy_result_corrections")

def allocate_result_id(connection):
    # V1 results and hierarchy results share one positive integer id space so
    # that read_result/export_result serve both kinds unambiguously. Callers
    # must hold an archive transaction (single-writer contract).
    v1 = connection.execute("SELECT MAX(id) FROM results").fetchone()[0]
    v2 = connection.execute("SELECT MAX(id) FROM hierarchy_results").fetchone()[0]
    return max(v1 if v1 is not None else 0, v2 if v2 is not None else 0) + 1

def save_hierarchy(connection, binding):
    body = {"coarse": list(binding.coarse),
            "patches": [[grid_id, observation_id, list(cell)]
                        for grid_id, observation_id, cell in binding.patches]}
    with archive_transaction(connection):
        cursor = connection.execute("INSERT INTO hierarchies(body) VALUES (?)",
                                    (canonical_json(body),))
        return cursor.lastrowid

def load_hierarchy(connection, hierarchy_id):
    if isinstance(hierarchy_id, bool) or not isinstance(hierarchy_id, int):
        raise ValueError("hierarchy identity must be an integer")
    row = connection.execute("SELECT body FROM hierarchies WHERE id=?",
                             (hierarchy_id,)).fetchone()
    if row is None:
        raise KeyError(hierarchy_id)
    data = decode_value(json.loads(row[0]))
    patches = tuple((entry[0], entry[1], tuple(entry[2])) for entry in data["patches"])
    return HierarchyBinding(hierarchy_id, tuple(data["coarse"]), patches)

def publish_hierarchy_result(connection, snapshot):
    header = {key: value for key, value in snapshot.items()
              if key not in ("cells", "boundary", "sources", "corrections")}
    boundary = dict(snapshot["boundary"])
    faces = boundary.pop("faces")
    header["boundary"] = boundary
    parts = [snapshot["cells"], faces, snapshot["sources"], snapshot["corrections"]]
    header["row_counts"] = [len(part) for part in parts]
    with archive_transaction(connection):
        result_id = allocate_result_id(connection)
        connection.execute(
            "INSERT INTO hierarchy_results(id,hierarchy_id,header) VALUES (?,?,?)",
            (result_id, snapshot["hierarchy_id"], canonical_json(header)))
        for table, records in zip(RESULT_TABLES, parts):
            for ordinal, record in enumerate(records):
                connection.execute("INSERT INTO " + table +
                                   "(result_id,ordinal,body) VALUES (?,?,?)",
                                   (result_id, ordinal, canonical_json(record)))
    return result_id

def load_hierarchy_result(connection, result_id):
    # Returns the detached snapshot, or None when no hierarchy result carries
    # this id (the caller then falls back to the V1 result tables).
    root = connection.execute("SELECT hierarchy_id,header FROM hierarchy_results WHERE id=?",
                              (result_id,)).fetchone()
    if root is None:
        return None
    header = decode_value(json.loads(root[1]))
    counts = header.pop("row_counts")
    if header["hierarchy_id"] != root[0]:
        raise ValueError("stored hierarchy result binding mismatch")
    if len(counts) != len(RESULT_TABLES):
        raise ValueError("invalid hierarchy result row counts")
    parts = []
    for table, count in zip(RESULT_TABLES, counts):
        rows = connection.execute("SELECT ordinal,body FROM " + table +
                                  " WHERE result_id=? ORDER BY ordinal", (result_id,)).fetchall()
        if [row[0] for row in rows] != list(range(count)):
            raise ValueError("incomplete stored hierarchy result")
        parts.append([decode_value(json.loads(row[1])) for row in rows])
    snapshot = dict(header)
    snapshot["cells"] = parts[0]
    snapshot["boundary"] = dict(header["boundary"])
    snapshot["boundary"]["faces"] = parts[1]
    snapshot["sources"] = parts[2]
    snapshot["corrections"] = parts[3]
    return snapshot
