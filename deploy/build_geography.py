"""Build bundled geography from Natural Earth v5.1.2 GeoJSON files.

Usage: python deploy/build_geography.py regions-source.geojson cities-source.geojson
Sources: ne_10m_admin_1_states_provinces_lines and ne_10m_populated_places.
"""
import json
import math
import sys
from pathlib import Path


def simplify(points, tolerance=.015):
    """Douglas-Peucker simplification in geographic degrees."""
    if len(points) <= 2:
        return points
    ax, ay = points[0]
    bx, by = points[-1]
    dx, dy = bx - ax, by - ay
    denominator = dx * dx + dy * dy
    maximum, index = 0, 0
    for i, (x, y) in enumerate(points[1:-1], 1):
        fraction = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / denominator)) if denominator else 0
        distance = math.hypot(x - ax - fraction * dx, y - ay - fraction * dy)
        if distance > maximum:
            maximum, index = distance, i
    if maximum <= tolerance:
        return [points[0], points[-1]]
    return simplify(points[:index + 1], tolerance)[:-1] + simplify(points[index:], tolerance)


def build(regions_source, cities_source):
    regions = json.loads(Path(regions_source).read_text())
    regions["features"] = [feature for feature in regions["features"] if feature["geometry"]]
    for feature in regions["features"]:
        feature.pop("bbox", None)
        feature["properties"] = {}
        geometry = feature["geometry"]
        lines = [geometry["coordinates"]] if geometry["type"] == "LineString" else geometry["coordinates"]
        lines = [[[round(x, 4), round(y, 4)] for x, y in simplify(line)] for line in lines]
        geometry["coordinates"] = lines[0] if geometry["type"] == "LineString" else lines
    cities = json.loads(Path(cities_source).read_text())
    selected = []
    for feature in sorted(cities["features"], key=lambda f: f["properties"]["POP_MAX"], reverse=True):
        properties = feature["properties"]
        population = properties["POP_MAX"]
        capital = properties["ADM0CAP"] == 1
        if population < 100000 and not capital:
            continue
        min_label = 4 if population >= 2000000 or capital else 5 if population >= 500000 else 6
        feature["properties"] = {key: properties[key] for key in ("NAME", "NAME_EN", "NAME_RU")}
        feature.pop("bbox", None)
        feature["properties"]["MIN_LABEL"] = min_label
        feature["geometry"]["coordinates"] = [round(value, 4) for value in feature["geometry"]["coordinates"]]
        selected.append(feature)
    cities["features"] = selected
    output = Path(__file__).resolve().parents[1] / "web/vendor/natural-earth"
    for name, data in (("regions", regions), ("cities", cities)):
        # Foreign collection metadata is unnecessary in the bundled assets.
        collection = {"type": "FeatureCollection", "features": data["features"]}
        (output / f"{name}.geojson").write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    build(*sys.argv[1:])
