// SPDX-License-Identifier: GPL-3.0-or-later
export function addGeography(map) {
  for (const [name, zIndex] of [["regions", "390"], ["cities", "420"]]) {
    const pane = map.createPane(name);
    pane.style.zIndex = zIndex;
    pane.style.pointerEvents = "none";
  }
  const cities = L.layerGroup().addTo(map);
  const regionRenderer = L.canvas({pane: "regions", padding: .5});
  let places, boundaries, loadingPlaces = false, loadingBoundaries = false;
  const name = properties => properties[document.documentElement.lang === "ru" ? "NAME_RU" : "NAME_EN"]
    || properties.NAME;
  async function load(file) {
    const response = await fetch(`assets/vendor/natural-earth/${file}.geojson`);
    if (!response.ok) throw new Error("Map geography");
    return response.json();
  }
  function drawCities() {
    cities.clearLayers();
    if (!places || map.getZoom() < 4) return;
    const bounds = map.getBounds();
    const occupied = [];
    for (const feature of places.features) {
      const properties = feature.properties;
      const [lon, lat] = feature.geometry.coordinates;
      if (map.getZoom() < properties.MIN_LABEL || !bounds.contains([lat, lon])) continue;
      const label = name(properties);
      const point = map.latLngToContainerPoint([lat, lon]);
      const box = {left: point.x - 4, right: point.x + 12 + label.length * 7,
        top: point.y - 10, bottom: point.y + 10};
      if (occupied.some(other => box.left < other.right && box.right > other.left
          && box.top < other.bottom && box.bottom > other.top)) continue;
      occupied.push(box);
      const text = document.createElement("span");
      text.textContent = label;
      L.circleMarker([lat, lon], {pane: "cities", interactive: false, radius: 2,
        color: "#c7d1da", weight: 1, fillColor: "#c7d1da", fillOpacity: 1})
        .bindTooltip(text, {permanent: true, direction: "right", offset: [4, 0],
          pane: "cities", className: "city-label", opacity: 1}).addTo(cities);
    }
  }
  function update() {
    const zoom = map.getZoom();
    if (zoom >= 4 && !places && !loadingPlaces) {
      loadingPlaces = true;
      load("cities").then(data => { places = data; drawCities(); })
        .catch(() => { /* Lightning remains usable if the optional geography fails. */ })
        .finally(() => { loadingPlaces = false; });
    }
    if (zoom >= 5 && !boundaries && !loadingBoundaries) {
      loadingBoundaries = true;
      load("regions").then(data => {
        boundaries = L.geoJSON(data, {pane: "regions", interactive: false,
          renderer: regionRenderer,
          style: {color: "#81909c", weight: .8, opacity: .55, dashArray: "3 4"},
        });
        if (map.getZoom() >= 5) boundaries.addTo(map);
      }).catch(() => { /* Keep the base map and strikes available. */ })
        .finally(() => { loadingBoundaries = false; });
    }
    if (boundaries) {
      if (zoom >= 5 && !map.hasLayer(boundaries)) boundaries.addTo(map);
      else if (zoom < 5 && map.hasLayer(boundaries)) boundaries.remove();
    }
    drawCities();
  }
  map.on("moveend", update);
  update();
  return {localize: drawCities};
}
