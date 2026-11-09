// SPDX-License-Identifier: GPL-3.0-or-later
import {addGeography} from "./geography.js";
const palettes = ["#5b0b0b", "#8f1515", "#c52b1d", "#ef6c35", "#ffd0a8", "#ffffff"];

export function makeMap(prefs, t, onMove, stationAction) {
  const map = L.map("map", {preferCanvas: true, zoomControl: false, trackResize: false,
    maxBounds: [[-85.05, -180], [85.05, 180]], minZoom: 1, maxZoom: 18,
  }).setView([prefs.lat, prefs.lon], prefs.zoom);
  const zoom = L.control.zoom().addTo(map);
  map.attributionControl.setPrefix('<a href="https://leafletjs.com/">Leaflet</a>');
  const countriesPane = map.createPane("countries");
  countriesPane.style.zIndex = "200";
  countriesPane.style.pointerEvents = "none";
  const countryLabelsPane = map.createPane("country-labels");
  countryLabelsPane.style.zIndex = "410";
  countryLabelsPane.style.pointerEvents = "none";
  map.createPane("strikes").style.zIndex = "450";
  map.createPane("stations").style.zIndex = "460";
  const countryLabels = [];
  const geography = addGeography(map);
  function countryName(properties) {
    return (document.documentElement.lang === "ru" ? properties.NAME_RU : properties.NAME_EN)
      || properties.NAME;
  }
  function updateCountryLabels() {
    const level = map.getZoom();
    for (const entry of countryLabels) {
      const visible = level >= entry.properties.MIN_LABEL;
      if (visible && !map.hasLayer(entry.tooltip)) entry.tooltip.addTo(map);
      else if (!visible && map.hasLayer(entry.tooltip)) entry.tooltip.remove();
    }
  }
  if (prefs.tile_url) {
    const attribution = document.createElement("span");
    attribution.textContent = prefs.tile_attribution;
    const tileAttribution = attribution.innerHTML;
    L.tileLayer(prefs.tile_url, {attribution: tileAttribution, maxZoom: 18, noWrap: true}).addTo(map);
    if (prefs.tile_label_url) {
      L.tileLayer(prefs.tile_label_url, {attribution: tileAttribution, maxZoom: 18, noWrap: true}).addTo(map);
    }
  } else {
    map.attributionControl.addAttribution("Natural Earth");
    fetch("assets/vendor/natural-earth/countries.geojson")
      .then(response => {
        if (!response.ok) throw new Error("Base map");
        return response.json();
      })
      .then(data => {
        L.geoJSON(data, {interactive: false, pane: "countries",
          style: {color: "#7d8993", weight: 1, opacity: .8, fillColor: "#30383f", fillOpacity: .92},
        }).addTo(map);
        for (const feature of data.features) {
          const properties = feature.properties;
          if (!properties.NAME || !Number.isFinite(properties.LABEL_X)
              || !Number.isFinite(properties.LABEL_Y)) continue;
          const tooltip = L.tooltip({permanent: true, direction: "center", className: "country-label",
            interactive: false, opacity: 1, pane: "country-labels"})
            .setLatLng([properties.LABEL_Y, properties.LABEL_X])
            .setContent(countryName(properties));
          countryLabels.push({tooltip, properties});
        }
        updateCountryLabels();
      })
      .catch(() => { /* The strike layer remains usable on the coordinate grid. */ });
  }
  const stations = L.layerGroup().addTo(map);
  const events = L.layerGroup().addTo(map);
  const strikeRenderer = L.canvas({pane: "strikes", padding: .5, tolerance: 5});
  let strikeMarkers = [];
  const markersById = new Map();
  let selectedMarker;
  const strikeRadius = () => Math.max(1.7, 5 - Math.max(0, map.getZoom() - 2) * .36);
  function updateStrikeSize() {
    const radius = strikeRadius();
    for (const marker of strikeMarkers) marker.setRadius(radius);
  }
  map.on("moveend", onMove);
  map.on("zoomend", updateCountryLabels);
  map.on("zoomend", updateStrikeSize);
  const resize = () => map.invalidateSize({animate: false});
  new ResizeObserver(([entry]) => {
    if (entry.contentRect.width && entry.contentRect.height) resize();
  }).observe(map.getContainer());
  function localize() {
    map.getContainer().setAttribute("aria-label", t("mapKeyboard"));
    const controls = zoom.getContainer().querySelectorAll("a");
    controls.forEach((control, i) => {
      const label = t(i === 0 ? "zoomIn" : "zoomOut");
      control.title = label;
      control.setAttribute("aria-label", label);
    });
    for (const entry of countryLabels) entry.tooltip.setContent(countryName(entry.properties));
    geography.localize();
  }
  localize();
  return {
    localize,
    view: (lat, lon, level) => map.setView([lat, lon], level),
    focusStrike(item) {
      const marker = markersById.get(item.id);
      if (!marker) return;
      if (selectedMarker) selectedMarker.setStyle({color: "#102333", weight: .65, opacity: .65});
      selectedMarker = marker;
      map.setView([item.lat, item.lon], Math.max(map.getZoom(), 7));
      marker.setStyle({color: "#81f2b9", weight: 3, opacity: 1});
      marker.bringToFront().openPopup();
    },
    resize,
    area() {
      const bounds = map.getBounds();
      return {south: Math.max(-90, bounds.getSouth()), north: Math.min(90, bounds.getNorth()),
        west: Math.max(-180, bounds.getWest()), east: Math.min(180, bounds.getEast())};
    },
    drawStations(items, ownStation) {
      stations.clearLayers();
      for (const item of items) {
        if (item.lat === null || item.lon === null) continue;
        const own = item.bo_station_id === ownStation;
        const popup = document.createElement("button");
        popup.type = "button";
        popup.textContent = `#${item.bo_station_id} ${item.city || ""}${own ? " · " + t("own") : ""}`;
        popup.addEventListener("click", () => stationAction(item.id));
        L.circleMarker([item.lat, item.lon], {pane: "stations", radius: own ? 6 : 3,
          color: "#81f2b9", fillOpacity: .7, weight: 1})
          .bindPopup(popup).addTo(stations);
      }
    },
    drawStrikes(items, start, end, date) {
      events.clearLayers();
      strikeMarkers = [];
      markersById.clear();
      selectedMarker = undefined;
      for (const item of items) {
        const progress = Math.min(1, Math.max(0, (Date.parse(item.time) - start) / Math.max(1, end - start)));
        const color = palettes[Math.round(progress * (palettes.length - 1))];
        const popup = document.createElement("span");
        popup.textContent = `${date(item.time)} · ${item.lat.toFixed(3)}, ${item.lon.toFixed(3)}`;
        const marker = L.circleMarker([item.lat, item.lon], {
          renderer: strikeRenderer, radius: strikeRadius(), color: "#102333", opacity: .65,
          fillColor: color, fillOpacity: .9, weight: .65,
        });
        strikeMarkers.push(marker);
        markersById.set(item.id, marker);
        marker.bindPopup(popup).addTo(events);
      }
    },
    drawDensity(items, format) {
      events.clearLayers();
      strikeMarkers = [];
      markersById.clear();
      selectedMarker = undefined;
      for (const item of items) {
        const popup = document.createElement("span");
        popup.textContent = `${t("events")} · ${format(item.total)}`;
        L.circleMarker([item.lat, item.lon], {radius: Math.min(29, 4 + Math.sqrt(item.total)),
          color: "#ffb879", weight: 1, fillOpacity: .45}).bindPopup(popup).addTo(events);
      }
    },
  };
}
