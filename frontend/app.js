/* City dashboard frontend. Vue for the panel, Leaflet for the map (kept outside Vue's reactivity). */
const RAMP = ['#cde2fb', '#86b6ef', '#3987e5', '#1c5cab', '#0d366b'];   // one hue, light -> dark
const STATUS = [
  { key: 'done', label: 'Done', icon: '✓' },
  { key: 'in_progress', label: 'In progress', icon: '◐' },
  { key: 'not_started', label: 'Not started', icon: '○' },
];
const VARS = {
  canopy_cover: ['Canopy cover', 'Canopy'],
  canopy_m2_per_resident: ['Canopy per resident (m²)', 'Canopy/resident'],
  green_cover: ['Green cover (NAIP)', 'Green'],
  green_not_canopy_cover: ['Green but not canopy (lawns, shrubs)', 'Green, no canopy'],
  trees_per_km2: ['Trees per km²', 'Trees/km²'],
  crown_volume_m3_per_km2: ['Crown volume per km² (m³)', 'Volume/km²'],
  median_height_m: ['Median tree height (m)', 'Height'],
  trees_per_resident: ['Trees per resident', 'Trees/resident'],
  crown_volume_m3_per_resident: ['Crown volume per resident (m³)', 'Volume/resident'],
  median_household_income: ['Median household income ($)', 'Income'],
  renter_share: ['Share of renters', 'Renters'],
  residents_per_km2: ['Residents per km²', 'Residents/km²'],
  median_year_built: ['Median year homes were built', 'Year built'],
  median_home_value: ['Median home value ($)', 'Home value'],
  share_white_non_hispanic: ['Share non-Hispanic white', 'White n.H.'],
  population: ['Residents', 'Residents'],
  trees: ['Trees', 'Trees'],
  covered_share: ['Share inside processed area', 'Inside'],
};
const TREE_VARS = ['canopy_cover', 'canopy_m2_per_resident', 'green_cover', 'green_not_canopy_cover', 'trees_per_km2', 'crown_volume_m3_per_km2', 'median_height_m', 'trees_per_resident', 'crown_volume_m3_per_resident'];
const PLACE_VARS = ['median_household_income', 'renter_share', 'residents_per_km2', 'median_year_built', 'median_home_value', 'share_white_non_hispanic'];

let map, markerLayer, shapeLayer, outlineLayer, limitsLayer, addMarker, pictureLayer, problemLayer, problemRenderer;

const isNum = v => typeof v === 'number' && isFinite(v);

function niceTicks(lo, hi, n = 4) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, out = [];
  for (let v = a; v <= b + step / 2; v += step) out.push(+v.toPrecision(12));
  return out;
}
function ranks(a) {
  const idx = a.map((v, i) => [v, i]).sort((p, q) => p[0] - q[0]), r = new Array(a.length);
  for (let i = 0; i < idx.length;) {
    let j = i; while (j + 1 < idx.length && idx[j + 1][0] === idx[i][0]) j++;
    for (let k = i; k <= j; k++) r[idx[k][1]] = (i + j) / 2 + 1;
    i = j + 1;
  }
  return r;
}
function spearman(x, y) {
  const rx = ranks(x), ry = ranks(y), n = x.length, m = (n + 1) / 2;
  let sxy = 0, sx = 0, sy = 0;
  for (let i = 0; i < n; i++) { sxy += (rx[i] - m) * (ry[i] - m); sx += (rx[i] - m) ** 2; sy += (ry[i] - m) ** 2; }
  return sx && sy ? sxy / Math.sqrt(sx * sy) : 0;
}

Vue.createApp({
  data: () => ({
    cities: [], dataFound: true, refresh: { cluster: '', pc: '' },
    city: null, tab: 'todo', run: null, results: null,
    mapVar: 'trees_per_km2', xVar: 'median_household_income', wholeOnly: true, picture: 'none',
    hover: null, open: {}, copied: null, showRefresh: false, showTests: false, hasLimits: false,
    adding: false, newCity: { label: '', state: '', place: '', lon: null, lat: null }, error: '',
    W: 500, H: 320, M: { l: 58, r: 30, t: 12, b: 44 },
    // trees the pipeline leaves out (taller than 70 m, on a structure): /api/city/{name}/problems
    problems: null, showProb: { tall: true, structure: true }, pickTiles: [],
    view: 'cities', datasetsLoaded: false,   // top tabs: Cities (map) | Datasets (registry, loaded on first visit)
    latlonOn: false,            // map button: clicks show the lat/lon of the spot (off = clicks as before)
  }),
  computed: {
    // commands to get the picked problem tiles labelled by the model (SAVE_LAS=1) for the labelling tool
    tileCommands() {
      const P = this.problems;
      if (!P || !P.available || !this.pickTiles.length || !this.city) return null;
      const name = this.city.name, tag = 'look_' + this.run;
      const idx = this.pickTiles.map(t => parseInt(t, 10)).join(',');
      const label = (this.city.label || name).replace(/[^A-Za-z0-9]/g, '');
      return {
        // one ordinary job for exactly these tiles (an array job would need the tile number as task id,
        // which can exceed the cluster's array limit); same environment and models as hpc/city_process.slurm
        cluster: `cd ${P.repo}\nconda activate pdal\npython src/city/fetch.py --city-dir data/cities/${name} --tiles ${idx} --pdal ${P.pdal}\n` +
                 `conda deactivate\nsbatch --job-name="${label} problem-tile check" --partition=gpu_a100_il --gres=gpu:1 \\\n` +
                 `  --cpus-per-task=4 --mem=24G --time=00:45:00 --output=../../logs/problem_tiles_%j.out \\\n` +
                 `  --wrap="module purge; module load devel/cuda/11.8 compiler/gnu/11; \\\n` +
                 `  source \\$(ws_find urban_trees)/miniconda3/etc/profile.d/conda.sh; conda activate urban-trees; \\\n` +
                 `  python src/city/process.py --city-dir data/cities/${name} \\\n` +
                 `  --run models/instance_checkpoints/inst_d5_full_offset_only_run --semantic-run models/finetune_checkpoints/d5_final_run \\\n` +
                 `  --tiles ${idx} --target-density ${P.density} --tag ${tag} --save-las"`,
        pc: `mkdir -p problem_tiles/${name}\nrsync -av ${this.pickTiles.map(t => `--include='${t}.laz'`).join(' ')} --exclude='*' \\\n  ${P.login}:${P.data}/cities/${name}/runs/${tag}/labelled/ problem_tiles/${name}/`,
      };
    },
    statusList: () => STATUS,
    treeVars() { return TREE_VARS.filter(v => this.hasVar(v)); },
    placeVars() { return PLACE_VARS.filter(v => this.hasVar(v)); },
    // the x axis can also be another tree measure, e.g. canopy cover against green cover (NDVI)
    xTreeVars() { return this.treeVars.filter(v => v !== this.yVar); },
    pictures() { return (this.results && this.results.layers && this.results.layers.items) || {}; },
    pictureLegend() {
      const L = this.results && this.results.layers;
      if (this.picture === 'ndvi') return { title: 'NDVI (NAIP)', rows: [
        { color: '#a6611a', label: 'bare: roofs, roads, soil (≤ −0.4)' },
        { color: '#f2f0eb', label: `this city's green cut-off (${L.green_ndvi})` },
        { color: '#1b7837', label: 'dense vegetation (≥ 0.6)' }] };
      if (this.picture === 'compare') return { title: 'Our canopy vs NDVI', rows: [
        { color: '#1b7837', label: 'our canopy, green' },
        { color: '#9c3d9c', label: 'our canopy, not green (dry crowns, palms, or a mistake)' },
        { color: '#c49a00', label: 'green, not our canopy (lawns, shrubs, or a missed tree)' }] };
      if (this.picture === 'canopy') return { title: 'Our canopy', rows: [{ color: '#1b7837', label: 'canopy' }] };
      return null;
    },
    tableVars() { return ['population', 'covered_share', 'trees', ...this.treeVars, ...this.placeVars].filter(v => this.hasVar(v)); },
    yVar() { return TREE_VARS.includes(this.mapVar) ? this.mapVar : (this.hasVar('canopy_cover') ? 'canopy_cover' : 'trees_per_km2'); },
    sortedCities() {
      const order = { in_progress: 0, done: 1, not_started: 2, no_lidar: 3 };
      return [...this.cities].sort((a, b) => order[a.status] - order[b.status] || a.label.localeCompare(b.label));
    },
    lastUpdate() { return this.cities.map(c => c.updated).filter(Boolean).sort().pop() || ''; },
    // null when the tables were made before the tree definition existed: the page must not claim it was applied
    def() { return (this.results && this.results.summary && this.results.summary.tree_definition) || null; },
    studyRuns() { return this.city ? this.city.runs.filter(r => /^(d4|d8|full)$/.test(r)) : []; },
    testRuns() { return this.city ? this.city.runs.filter(r => !/^(d4|d8|full)$/.test(r)) : []; },
    visibleRuns() { return this.showTests || !this.studyRuns.length ? this.city.runs : this.studyRuns; },
    isTest() { return !!this.run && !/^(d4|d8|full)$/.test(this.run); },
    // point density of the city's lidar: measured on processed tiles, else the survey estimate
    densityText() {
      const c = this.city; if (!c) return '';
      if (isNum(c.density_measured)) return `${c.density_measured.toFixed(1)} points per m² (measured)`;
      const e = c.plan && c.plan.survey_density_est;
      return isNum(e) ? `about ${e} points per m² (survey estimate)` : '';
    },
    // what this run used: thinned to N, or all points, from what the lidar has
    runDensityText() {
      const s = this.results && this.results.summary; if (!s) return '';
      const raw = isNum(s.median_density_raw) ? `${s.median_density_raw.toFixed(1)} points per m²` : null;
      if (isNum(s.target_density) && s.target_density > 0)
        return `Thinned to ${s.target_density} points per m²` + (raw ? ` (the lidar has ${raw})` : '');
      return raw ? `All points: ${raw}` : 'All points';
    },
    rows() { return this.results ? this.results.rows : []; },
    warnCount() { return this.results && this.results.checks ? this.results.checks.filter(c => c.status === 'warn').length : 0; },
    checkGroups() {
      const groups = [];
      for (const c of (this.results && this.results.checks) || []) {
        let g = groups.find(x => x.name === c.group);
        if (!g) groups.push(g = { name: c.group, items: [] });
        g.items.push(c);
      }
      return groups;
    },
    shown() {
      return this.rows.filter(r => !this.wholeOnly || (r.covered_share >= 0.95 && (r.population ?? 0) >= 50));
    },
    headline() {
      const s = this.results.summary, whole = this.rows.filter(r => r.covered_share >= 0.95 && isNum(r.trees_per_resident));
      const med = a => { const b = [...a].sort((p, q) => p - q); return b.length ? b[Math.floor(b.length / 2)] : null; };
      const m = med(whole.map(r => r.trees_per_resident));
      return [
        { label: 'Trees', value: this.fmt(s.trees) },
        { label: 'Trees per km²', value: this.fmt(Math.round(s.trees_per_km2)) },
        { label: 'Canopy cover', value: s.canopy_cover != null ? (s.canopy_cover * 100).toFixed(1) + '%' : '–' },
        { label: 'Area processed', value: s.area_km2 + ' km²' },
        { label: 'Whole block groups', value: this.rows.length ? this.rows.filter(r => r.covered_share >= 0.95).length + ' of ' + this.rows.length : '–' },
        { label: 'Median trees per resident', value: m != null ? m.toFixed(2) : '–' },
      ];
    },
    classes() {            // quantile classes of the map variable over the block groups that are drawn
      const vals = this.shown.map(r => r[this.mapVar]).filter(isNum).sort((a, b) => a - b);
      if (!vals.length) return [];
      const k = Math.min(RAMP.length, new Set(vals).size), cuts = [];
      for (let i = 1; i < k; i++) cuts.push(vals[Math.floor(i * vals.length / k)]);
      const uniq = [...new Set(cuts)];                      // ties can make two cuts equal: fewer classes then
      const n = uniq.length + 1, last = RAMP.length - 1;
      const colors = n === 1 ? [RAMP[2]] : Array.from({ length: n }, (_, i) => RAMP[Math.round(i * last / (n - 1))]);
      return { cuts: uniq, colors, lo: vals[0], hi: vals[vals.length - 1] };
    },
    legend() {
      const c = this.classes; if (!c.colors) return [];
      const edges = [c.lo, ...c.cuts, c.hi];
      return c.colors.map((color, i) => ({ color, label: this.fmtVar(this.mapVar, edges[i]) + ' – ' + this.fmtVar(this.mapVar, edges[i + 1]) }));
    },
    scatter() {
      const pts = this.shown.filter(r => isNum(r[this.xVar]) && isNum(r[this.yVar]));
      const xs = pts.map(r => r[this.xVar]), ys = pts.map(r => r[this.yVar]);
      const { W, H, M } = this;
      if (!pts.length) return { points: [], xTicks: [], yTicks: [], rho: 0, bar: 1 };
      const xt = niceTicks(Math.min(...xs), Math.max(...xs)), yt = niceTicks(Math.min(...ys), Math.max(...ys));
      const sx = v => M.l + (v - xt[0]) / (xt[xt.length - 1] - xt[0]) * (W - M.l - M.r);
      const sy = v => H - M.b - (v - yt[0]) / (yt[yt.length - 1] - yt[0]) * (H - M.t - M.b);
      return {
        points: pts.map(r => ({ id: r.GEOID, x: sx(r[this.xVar]), y: sy(r[this.yVar]), row: r })),
        xTicks: xt.map(v => ({ v, p: sx(v), label: this.fmtTick(this.xVar, v) })),
        yTicks: yt.map(v => ({ v, p: sy(v), label: this.fmtTick(this.yVar, v) })),
        rho: pts.length >= 4 ? spearman(xs, ys) : 0, bar: 1.96 / Math.sqrt(Math.max(pts.length - 3, 1)),
      };
    },
  },
  watch: {
    showProb: { deep: true, handler() { this.drawProblems(); } },
    mapVar() { if (this.xVar === this.yVar && this.placeVars.length) this.xVar = this.placeVars[0]; this.drawShapes(false); }, wholeOnly() { this.drawShapes(false); },
    picture() { this.drawPicture(); this.drawShapes(false); },
    tab() { this.drawPicture(); this.drawShapes(true); this.drawProblems(); },
    hover() { this.highlightShape(); },
  },
  methods: {
    setView(v) {
      this.view = v;
      if (v === 'datasets') this.datasetsLoaded = true;
      history.replaceState(null, '', v === 'datasets' ? '#datasets' : location.pathname);
      if (v === 'cities') this.$nextTick(() => map && map.invalidateSize());   // the map was hidden
    },
    // ---------- formatting
    fmt(n) { return isNum(n) ? n.toLocaleString('en-US') : '–'; },
    varLabel(v) { return (VARS[v] || [v])[0]; },
    varShort(v) { return (VARS[v] || [v, v])[1]; },
    hasVar(v) { return this.rows.some(r => isNum(r[v])); },
    fmtVar(v, x) {
      if (!isNum(x)) return '–';
      if (v === 'canopy_cover' || v.endsWith('_cover')) return (x * 100).toFixed(1) + '%';
      if (v.includes('share')) return (x * 100).toFixed(0) + '%';
      if (v === 'median_year_built') return String(Math.round(x));
      if (v.includes('income') || v.includes('home_value')) return '$' + Math.round(x).toLocaleString('en-US');
      if (v === 'trees_per_resident') return x.toFixed(2);
      if (v === 'median_height_m') return x.toFixed(1) + ' m';
      if (Math.abs(x) >= 1e6) return (x / 1e6).toFixed(2) + 'M';
      return Math.round(x).toLocaleString('en-US');
    },
    fmtTick(v, x) {
      if (v.includes('share') || v.endsWith('_cover')) return Math.round(x * 100) + '%';
      if (v === 'median_year_built') return String(x);
      const a = Math.abs(x), money = v.includes('income') || v.includes('home_value') ? '$' : '';
      if (a >= 1e6) return money + +(x / 1e6).toFixed(1) + 'M';
      if (a >= 1e4) return money + +(x / 1e3).toFixed(0) + 'k';
      return money + x.toLocaleString('en-US');
    },
    statusLabel(s) { return (STATUS.find(x => x.key === s) || { label: 'No public lidar' }).label; },
    stateIcon(s) { return { done: '✓', partial: '', todo: '', warn: '!' }[s]; },
    stateLabel(s) { return { done: 'Done', partial: 'Partly done', todo: 'To do', warn: 'Done, with a caveat' }[s]; },
    runLabel(r) {
      const m = /^d(\d+)$/.exec(r);
      if (m) return m[1] + ' points per m²' + (m[1] === '4' ? ' (main run)' : m[1] === '8' ? '' : ' (test)');
      if (r === 'full') return 'all points (full density)';
      return (r === 'native' ? 'all points' : r) + ' (test)';
    },
    countBy(k) { return this.cities.filter(c => c.status === k).length; },
    async copy(text, key) {
      try { await navigator.clipboard.writeText(text); }
      catch { const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); }
      this.copied = key; setTimeout(() => { if (this.copied === key) this.copied = null; }, 1500);
    },

    // ---------- data
    async loadCities() {
      const d = await (await fetch('/api/cities')).json();
      this.cities = d.cities; this.refresh = d.refresh; this.dataFound = d.data_found;
      this.drawMarkers();
    },
    async openCity(name) {
      this.city = await (await fetch('/api/city/' + name)).json();
      this.open = {}; this.results = null; this.hover = null;
      this.showTests = false;
      this.run = this.city.runs.includes('d4') ? 'd4' : this.city.runs.includes('d8') ? 'd8' : this.city.runs[0] || null;
      this.tab = this.city.status === 'done' ? 'results' : 'todo';
      markerLayer.remove();
      this.focusCity();
      if (this.run) await this.loadResults(); else this.drawShapes(true);
    },
    async loadResults() {
      this.hover = null;
      this.results = await (await fetch(`/api/city/${this.city.name}/results?run=${this.run}`)).json();
      // canopy cover is the most reliable measure: show it first whenever the run has it
      if (this.hasVar('canopy_cover') && (this.mapVar === 'trees_per_km2' || !this.hasVar(this.mapVar))) this.mapVar = 'canopy_cover';
      else if (!this.hasVar(this.mapVar)) this.mapVar = 'trees_per_km2';
      if (!this.hasVar(this.xVar) && this.placeVars.length) this.xVar = this.placeVars[0];
      if (!this.pictures[this.picture]) this.picture = 'none';
      this.drawPicture();
      this.drawShapes(true);
      await this.loadProblems();
    },
    async loadProblems() {
      this.problems = null; this.pickTiles = [];
      try { this.problems = await (await fetch(`/api/city/${this.city.name}/problems?run=${this.run}`)).json(); }
      catch { this.problems = { available: false }; }
      if (this.problems.available) this.pickTiles = this.problems.tiles.slice(0, 5).map(t => t.tile);
      this.drawProblems();
    },
    drawProblems() {
      if (problemLayer) { problemLayer.remove(); problemLayer = null; }
      const P = this.problems;
      if (!this.city || this.tab !== 'results' || !P || !P.available) return;
      problemLayer = L.layerGroup();
      const style = { tall: { color: '#b00020', radius: 6 }, structure: { color: '#e8710a', radius: 4 } };
      for (const t of P.trees) {
        if (!this.showProb[t.reason]) continue;
        const s = style[t.reason];
        L.circleMarker([t.lat, t.lon], { renderer: problemRenderer, pane: 'problems', radius: s.radius, color: '#fff', weight: 1,
                                         fillColor: s.color, fillOpacity: 0.95 })
          .bindTooltip(`<b>${t.reason === 'tall' ? 'Taller than 70 m' : 'On a structure'}</b><br>Height ${isNum(t.height_m) ? t.height_m.toFixed(1) + ' m' : '–'}` +
                       `${isNum(t.crown_area_m2) ? ' · crown ' + Math.round(t.crown_area_m2) + ' m²' : ''}<br>Tile ${t.tile || '–'}<br>${t.lat.toFixed(6)}, ${t.lon.toFixed(6)}`)
          .addTo(problemLayer);
      }
      problemLayer.addTo(map);
    },
    zoomTile(t) { map.setView([t.lat, t.lon], 17); },
    toggleTile(id) { const i = this.pickTiles.indexOf(id); if (i >= 0) this.pickTiles.splice(i, 1); else this.pickTiles.push(id); },
    closeCity() {
      this.city = null; this.results = null; this.picture = 'none';
      if (shapeLayer) { shapeLayer.remove(); shapeLayer = null; }
      if (pictureLayer) { pictureLayer.remove(); pictureLayer = null; }
      if (outlineLayer) { outlineLayer.remove(); outlineLayer = null; }
      if (limitsLayer) { limitsLayer.remove(); limitsLayer = null; }
      if (problemLayer) { problemLayer.remove(); problemLayer = null; }
      this.problems = null;
      markerLayer.addTo(map); map.setView([38.5, -96.5], 4);
    },
    async reload() {
      this.showRefresh = false;
      await this.loadCities();
      if (this.city) { const tab = this.tab; await this.openCity(this.city.name); this.tab = tab; }
    },

    // ---------- map
    drawMarkers() {
      markerLayer.clearLayers();
      for (const c of this.cities) {
        const icon = L.divIcon({ className: '', html: `<div class="city-marker st-${c.status}" style="width:16px;height:16px"></div>`, iconSize: [16, 16], iconAnchor: [8, 8] });
        const dens = c.density_measured ?? c.density_est;
        L.marker([c.lat, c.lon], { icon, title: c.label })
          .bindTooltip(`<b>${c.label}</b>, ${c.state}<br>${this.statusLabel(c.status)} · ${c.steps_done}/${c.steps_total} steps` +
                       (dens ? `<br>Lidar: ${dens} points per m²${c.density_measured ? '' : ' (estimate)'}` : '') +
                       (c.next_step ? `<br>Next: ${c.next_step}` : ''), { direction: 'top', offset: [0, -8] })
          .on('click', () => { if (!this.adding) this.openCity(c.name); })
          .addTo(markerLayer);
      }
    },
    async focusCity() {
      if (outlineLayer) { outlineLayer.remove(); outlineLayer = null; }
      if (limitsLayer) { limitsLayer.remove(); limitsLayer = null; }
      // dashed rectangle = the area that was planned (processed); solid line = the official city limits
      const b = this.city.plan && this.city.plan.bbox_lonlat;
      if (b) outlineLayer = L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: '#52514e', weight: 1.5, dashArray: '5 4', fill: false, interactive: false })
        .bindTooltip('Planned area', { sticky: true }).addTo(map);
      const limits = await (await fetch('/api/city/' + this.city.name + '/outline')).json();
      this.hasLimits = !!limits;
      if (limits) limitsLayer = L.geoJSON(limits, { style: { color: '#0b0b0b', weight: 2.5, fill: false }, interactive: false }).addTo(map);
      const bounds = limitsLayer ? limitsLayer.getBounds() : outlineLayer ? outlineLayer.getBounds() : null;
      if (bounds && outlineLayer && limitsLayer) bounds.extend(outlineLayer.getBounds());
      if (bounds) map.fitBounds(bounds, { padding: [30, 30] });
      else map.setView([this.city.lat, this.city.lon], 10);
    },
    colorFor(v) {
      const c = this.classes; if (!isNum(v) || !c.colors) return null;
      let i = 0; while (i < c.cuts.length && v >= c.cuts[i]) i++;
      return c.colors[Math.min(i, c.colors.length - 1)];
    },
    drawShapes(fit) {
      if (shapeLayer) { shapeLayer.remove(); shapeLayer = null; }
      if (!this.city || this.tab !== 'results' || !this.results || !this.results.shapes || !this.rows.length) return;
      const byId = Object.fromEntries(this.shown.map(r => [r.GEOID, r]));
      const feats = this.results.shapes.features.filter(f => byId[f.properties.GEOID]);
      shapeLayer = L.geoJSON({ type: 'FeatureCollection', features: feats }, {
        style: f => {
          const col = this.colorFor(byId[f.properties.GEOID][this.mapVar]);
          if (this.picture !== 'none') return { color: '#0b0b0b', weight: 0.6, opacity: 0.5, fillOpacity: 0 };   // outlines over the picture
          return { color: '#fff', weight: 2, fillColor: col || '#d6d4cd', fillOpacity: col ? 0.85 : 0.35 };   // 2 px surface gap between fills
        },
        onEachFeature: (f, layer) => {
          const r = byId[f.properties.GEOID];
          layer.bindTooltip(() => `<b>Block group ${r.GEOID.slice(-7)}</b><br>${this.varLabel(this.mapVar)}: ${this.fmtVar(this.mapVar, r[this.mapVar])}` +
                                  `<br>Residents: ${this.fmt(r.population)}<br>Inside processed area: ${this.fmtVar('covered_share', r.covered_share)}`, { sticky: true });
          layer.on('mouseover', () => { this.hover = this.scatter.points.find(p => p.id === r.GEOID) || null; layer.setStyle({ color: '#0b0b0b', weight: 2.5 }); layer.bringToFront(); });
          layer.on('mouseout', () => { this.hover = null; shapeLayer && shapeLayer.resetStyle(layer); });
        },
      }).addTo(map);
      if (fit && feats.length) {
        const bb = shapeLayer.getBounds();
        if (limitsLayer) bb.extend(limitsLayer.getBounds());
        map.fitBounds(bb, { padding: [30, 30] });
      }
    },
    drawPicture() {
      if (pictureLayer) { pictureLayer.remove(); pictureLayer = null; }
      const item = this.pictures[this.picture];
      if (!this.city || this.tab !== 'results' || !item) return;
      pictureLayer = L.imageOverlay(item.url, this.results.layers.bounds, { opacity: 0.9, className: 'picture' }).addTo(map);
    },
    highlightShape() {
      if (!shapeLayer) return;
      shapeLayer.eachLayer(l => {
        if (this.hover && l.feature.properties.GEOID === this.hover.id) { l.setStyle({ color: '#0b0b0b', weight: 2.5 }); l.bringToFront(); }
        else shapeLayer.resetStyle(l);
      });
    },
    setHover(p) { this.hover = p; },

    // ---------- add a city
    toggleAdd() {
      this.adding = !this.adding; this.error = '';
      if (addMarker) { addMarker.remove(); addMarker = null; }
      if (this.adding) { if (this.city) this.closeCity(); this.newCity = { label: '', state: '', place: '', lon: null, lat: null }; }
    },
    async saveCity() {
      const r = await fetch('/api/cities', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: this.newCity.label, state: this.newCity.state, place: this.newCity.place || '', lon: this.newCity.lon, lat: this.newCity.lat }) });
      const d = await r.json();
      if (!r.ok) { this.error = d.detail || 'Could not add the city'; return; }
      this.toggleAdd(); await this.loadCities(); this.openCity(d.name);
    },
  },
  mounted() {
    map = L.map('map', { zoomSnap: 0.25 }).setView([38.5, -96.5], 4);
    // OpenStreetMap's own tiles: free, no key. (CARTO's basemap now answers "API key required".)
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    markerLayer = L.layerGroup().addTo(map);
    // problem trees above the block groups and pictures
    map.createPane('problems').style.zIndex = 450;
    problemRenderer = L.canvas({ pane: 'problems' });
    map.on('click', e => {
      if (!this.adding) {
        if (!this.latlonOn) return;             // lat/lon button off: map clicks do nothing extra
        // lat/lon button on: latitude / longitude of the clicked spot, with a copy button
        const txt = `${e.latlng.lat.toFixed(6)}, ${e.latlng.lng.toFixed(6)}`;
        const box = document.createElement('div');
        box.className = 'latlon-pop';
        box.innerHTML = `<code>${txt}</code> <button class="btn small">Copy</button>`;
        const btn = box.querySelector('button');
        btn.onclick = () => { this.copy(txt, 'latlon'); btn.textContent = 'Copied'; };
        L.popup({ closeButton: true, autoPan: false }).setLatLng(e.latlng).setContent(box).openOn(map);
        return;
      }
      this.newCity.lon = e.latlng.lng; this.newCity.lat = e.latlng.lat;
      if (addMarker) addMarker.remove();
      addMarker = L.circleMarker(e.latlng, { radius: 8, color: '#0b0b0b', weight: 2, fillColor: '#fff', fillOpacity: 1 }).addTo(map);
    });
    this.loadCities();
    if (location.hash === '#datasets') this.setView('datasets');
  },
}).mount('#app');
