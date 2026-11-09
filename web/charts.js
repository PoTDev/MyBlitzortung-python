// SPDX-License-Identifier: GPL-3.0-or-later
const NS = "http://www.w3.org/2000/svg";

function svgNode(tag, attrs, text) {
  const node = document.createElementNS(NS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

export function drawChart(target, points, t, format, xAxis = "") {
  target.replaceChildren();
  if (!points.length) {
    target.textContent = t("empty");
    return;
  }
  const [left, right, top, bottom] = [48, 369, 11, 123];
  const values = points.map(point => point.y);
  const lo = Math.min(0, ...values);
  const max = Math.max(...values);
  const hi = Math.max(1, max);
  const first = points[0].x;
  const last = points.at(-1).x;
  const x = n => first === last ? (left + right) / 2 : left + (n - first) * (right - left) / (last - first);
  const y = n => bottom - (n - lo) * (bottom - top) / (hi - lo);
  const description = `${t("range")}: ${format(lo)} – ${format(max)}`;
  const svg = svgNode("svg", {viewBox: "0 0 380 151", role: "img", "aria-label": description});
  for (let i = 0; i <= 3; i++) {
    const value = lo + (hi - lo) * i / 3;
    const py = y(value);
    svg.append(svgNode("path", {d: `M ${left} ${py} H ${right}`, class: "grid"}));
    svg.append(svgNode("text", {x: left - 5, y: py + 3, "text-anchor": "end"}, format(value)));
  }
  const trace = points.map(point => `${x(point.x).toFixed(2)},${y(point.y).toFixed(2)}`).join(" ");
  svg.append(svgNode("polygon", {points: `${x(first)},${bottom} ${trace} ${x(last)},${bottom}`, class: "fill"}));
  svg.append(svgNode("polyline", {points: trace, class: "trace"}));
  if (points.length === 1) svg.append(svgNode("circle", {cx: x(first), cy: y(points[0].y), r: 3, class: "chart-point"}));
  svg.append(svgNode("text", {x: left, y: 143}, points[0].label));
  if (last !== first) svg.append(svgNode("text", {x: right, y: 143, "text-anchor": "end"}, points.at(-1).label));
  const caption = document.createElement("p");
  caption.className = "chart-description";
  caption.textContent = [xAxis, description].filter(Boolean).join(" · ");
  svg.addEventListener("pointermove", event => {
    const ratio = (event.clientX - svg.getBoundingClientRect().left) / svg.getBoundingClientRect().width;
    const estimate = first + ((ratio * 380 - left) / (right - left)) * (last - first);
    const point = points.reduce((nearest, candidate) =>
      Math.abs(candidate.x - estimate) < Math.abs(nearest.x - estimate) ? candidate : nearest);
    caption.textContent = `${point.label} · ${format(point.y)}`;
  });
  svg.addEventListener("pointerleave", () => { caption.textContent = [xAxis, description].filter(Boolean).join(" · "); });
  target.append(svg, caption);
}

export function drawDirectionRose(target, sectors, t, format) {
  target.replaceChildren();
  if (!sectors.length) {
    target.textContent = t("directionUnavailable");
    return;
  }
  const directions = t("compass").split(",");
  const max = Math.max(1, ...sectors.map(item => item.count));
  const cx = 180, cy = 145, radius = 102, labelRadius = 124;
  const point = (angle, distance) => {
    const radians = angle * Math.PI / 180;
    return [cx + Math.sin(radians) * distance, cy - Math.cos(radians) * distance];
  };
  const svg = svgNode("svg", {
    viewBox: "0 0 360 295", role: "img", "aria-label": t("directionActivity"),
  });
  for (let ring = 1; ring <= 4; ring++) {
    const distance = radius * ring / 4;
    svg.append(svgNode("circle", {cx, cy, r: distance, class: "grid"}));
    svg.append(svgNode("text", {x: cx + 4, y: cy - distance + 11}, format(max * ring / 4)));
  }
  svg.append(svgNode("path", {d: `M ${cx - radius} ${cy} H ${cx + radius} M ${cx} ${cy - radius} V ${cy + radius}`, class: "grid"}));
  for (const item of sectors) {
    const angle = item.sector * 22.5;
    const extent = radius * item.count / max;
    const [x1, y1] = point(angle - 7.5, extent);
    const [x2, y2] = point(angle + 7.5, extent);
    const bar = svgNode("path", {
      d: `M ${cx} ${cy} L ${x1} ${y1} A ${extent} ${extent} 0 0 1 ${x2} ${y2} Z`,
      class: "rose-sector",
    });
    const direction = directions[item.sector];
    bar.append(svgNode("title", {}, `${direction}: ${format(item.count)}`));
    svg.append(bar);
    const [lx, ly] = point(angle, labelRadius);
    svg.append(svgNode("text", {
      x: lx, y: ly + 3, "text-anchor": Math.abs(lx - cx) < 4 ? "middle" : lx < cx ? "end" : "start",
      class: item.sector % 2 === 0 ? "rose-cardinal" : "rose-intercardinal",
    }, direction));
  }
  const total = sectors.reduce((sum, item) => sum + item.count, 0);
  const caption = document.createElement("p");
  caption.className = "chart-description";
  caption.textContent = `${t("directionTotal")}: ${format(total)}`;
  target.append(svg, caption);
}

export function spectrum(values, interval) {
  const n = Math.min(512, values.length);
  if (n < 2) return [];
  const mean = values.slice(0, n).reduce((total, v) => total + v, 0) / n;
  return Array.from({length: Math.floor(n / 2) + 1}, (_, k) => {
    let real = 0;
    let imaginary = 0;
    for (let i = 0; i < n; i++) {
      const phase = 2 * Math.PI * k * i / n;
      const sample = values[i] - mean;
      real += sample * Math.cos(phase);
      imaginary -= sample * Math.sin(phase);
    }
    return {x: interval ? k * 1e9 / (n * interval) : k, y: Math.hypot(real, imaginary) / n};
  });
}
