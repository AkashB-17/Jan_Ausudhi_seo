/**
 * Jan Aushadhi Finder — Redesigned Frontend
 *
 * User journey: Search → Brand detail → Matches → Price comparison
 * All API calls are proxied through api.js (unchanged).
 */

import {
  searchMedicines,
  fetchBrandDetail,
  fetchBrandMatches,
  fetchComparison,
  scanPrescription,
  matchPrescriptionText,
  fetchKendraStates,
  fetchKendraDistricts,
  searchKendras,
  ApiError,
} from "./api.js";

/* ── DOM references ─────────────────────────────────────────── */
const $searchInput  = document.getElementById("search-input");
const $searchClear  = document.getElementById("search-clear");
const $statusArea   = document.getElementById("status-area");
const $contentArea  = document.getElementById("content-area");
const $hero         = document.getElementById("hero");
const $searchExs    = document.getElementById("search-examples");
const $homeSteps    = document.getElementById("home-steps");
const $navSearch    = document.getElementById("nav-search");
const $navHow       = document.getElementById("nav-how");
const $brandHome    = document.getElementById("brand-home");
const $searchHint   = document.getElementById("search-hint");
const $searchWrap   = document.getElementById("search-wrap");
const $navScan      = document.getElementById("nav-scan");
const $navKendra    = document.getElementById("nav-kendra");
const $scanCard     = document.getElementById("scan-card");
const $scanInput    = document.getElementById("scan-input");
const $scanDrop     = document.getElementById("scan-drop");

/* ── State ──────────────────────────────────────────────────── */
let currentBrandId  = null;
let currentDrugCode = null;
let debounceTimer   = null;
// Last prescription scan, kept so brand/compare pages can return to it.
let lastScan        = null;   // { data, previewUrl, fileName }
let fromScan        = false;

/* ── Visibility helpers ─────────────────────────────────────── */
const show = (el) => el?.classList.remove("hidden");
const hide = (el) => el?.classList.add("hidden");

function setNav(which) {
  $navSearch.classList.toggle("is-active", which === "search");
  $navScan.classList.toggle("is-active", which === "scan");
  $navKendra.classList.toggle("is-active", which === "kendra");
  $navHow.classList.toggle("is-active", which === "how");
}

/** Toggle hero + examples + steps visibility */
function setHomeChrome(visible) {
  [$hero, $searchExs, $homeSteps, $searchHint, $scanCard].forEach((el) => {
    if (!el) return;
    visible ? show(el) : hide(el);
  });
  // Slightly tighten search-wrap top padding when hero is hidden
  if ($searchWrap) {
    $searchWrap.style.paddingTop = visible ? "" : "1.25rem";
  }
}

function updateClearButton() {
  $searchInput.value.length > 0 ? show($searchClear) : hide($searchClear);
}

/* ── Status strip ───────────────────────────────────────────── */
function setStatus(type, message) {
  if (!message) {
    hide($statusArea);
    $statusArea.innerHTML = "";
    return;
  }

  let html;
  if (type === "loading") {
    html = `<div class="status-strip status-strip--loading">
              <span class="spinner" aria-hidden="true"></span>
              ${escHtml(message)}
            </div>`;
  } else if (type === "error") {
    html = `<div class="status-strip status-strip--error" role="alert">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
                <circle cx="12" cy="12" r="10"/>
                <path d="M12 8v4M12 16h.01"/>
              </svg>
              ${escHtml(message)}
            </div>`;
  } else {
    html = `<div class="status-strip status-strip--empty">${escHtml(message)}</div>`;
  }

  $statusArea.innerHTML = html;
  show($statusArea);
}

function clearContent() { $contentArea.innerHTML = ""; }

/* ── Formatting utilities ───────────────────────────────────── */
function escHtml(str) {
  const d = document.createElement("div");
  d.textContent = str == null ? "" : String(str);
  return d.innerHTML;
}

function formatPrice(p) {
  return "₹\u202f" + Number(p).toFixed(2);
}

function formatMethod(method) {
  const raw = String(method || "").replace(/_/g, " ").trim();
  if (!raw) return "";
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function shortBrandLabel(name) {
  const n = String(name || "").trim();
  if (!n) return "medicine";
  const cut = n.split(/\s+\d/)[0].trim();
  return cut || n;
}

/** Confidence → badge class and label */
function confidenceInfo(validation_status, confidence) {
  const pct = Math.round(confidence * 100);
  const status = String(validation_status || "").toLowerCase();
  if (status === "strong") {
    return { cls: "conf-badge--strong", label: `Strong match · ${pct}%` };
  }
  if (status === "moderate") {
    return { cls: "conf-badge--moderate", label: `Moderate match · ${pct}%` };
  }
  return { cls: "conf-badge--ambiguous", label: `${validation_status || "Ambiguous"} · ${pct}%` };
}

function confBadgeHtml(validation_status, confidence) {
  const { cls, label } = confidenceInfo(validation_status, confidence);
  return `<span class="conf-badge ${cls}">
            <span class="conf-badge__dot" aria-hidden="true"></span>
            ${escHtml(label)}
          </span>`;
}

/* ── Pack comparison helpers (carried over from original logic) ── */
function parseUnitCount(label) {
  const s = String(label || "").trim().toLowerCase();
  if (!s) return null;
  let m = s.match(/^(\d+)\s*'s$/);  if (m) return Number(m[1]);
  m = s.match(/^(\d+)\s*s$/);       if (m) return Number(m[1]);
  m = s.match(/strip\s+of\s+(\d+)\s+tablets?/); if (m) return Number(m[1]);
  m = s.match(/^(\d+)\s+tablets?$/);if (m) return Number(m[1]);
  m = s.match(/\b(\d+)\s+tablets?\b/); if (m) return Number(m[1]);
  return null;
}

function isTabletForm(form) {
  const f = String(form || "").toLowerCase();
  return f.includes("tablet") || f.includes("tab");
}

function comparableTabletCount(brand, pmbi) {
  if (!isTabletForm(brand.norm_dosage_form) || !isTabletForm(pmbi.norm_dosage_form)) return null;
  const a = parseUnitCount(brand.pack_size_label);
  const b = parseUnitCount(pmbi.unit_size);
  if (a && b && a === b && a >= 1 && a <= 20) return a;
  return null;
}

/* ── Keyboard-activate helper ───────────────────────────────── */
function bindActivate(el, fn) {
  el.addEventListener("click", fn);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); }
  });
}

/* ── Navigation ─────────────────────────────────────────────── */
function goHome() {
  setNav("search");
  currentBrandId  = null;
  currentDrugCode = null;
  fromScan        = false;
  const q = $searchInput.value.trim();
  if (q.length < 2) {
    setStatus(null);
    clearContent();
    setHomeChrome(true);
    $searchInput.focus();
  } else {
    setHomeChrome(false);
    doSearch(q);
  }
}

/* ── Event listeners ────────────────────────────────────────── */
$searchInput.addEventListener("input", () => {
  updateClearButton();
  clearTimeout(debounceTimer);
  const q = $searchInput.value.trim();
  setNav("search");
  if (q.length < 2) {
    setStatus(null);
    clearContent();
    setHomeChrome(true);
    return;
  }
  setHomeChrome(false);
  setStatus("loading", "Searching…");
  debounceTimer = setTimeout(() => doSearch(q), 320);
});

$searchClear.addEventListener("click", () => {
  $searchInput.value = "";
  updateClearButton();
  goHome();
  $searchInput.focus();
});

$navSearch.addEventListener("click", goHome);
$navScan.addEventListener("click", () => (lastScan ? renderScanResults(lastScan.data) : renderScanPage()));
$navHow.addEventListener("click", renderHowPage);
$navKendra.addEventListener("click", () => renderKendraPage());

$scanInput.addEventListener("change", () => {
  const file = $scanInput.files && $scanInput.files[0];
  $scanInput.value = "";   // allow re-selecting the same file
  if (file) handleScanFile(file);
});

["dragenter", "dragover"].forEach((type) =>
  $scanDrop.addEventListener(type, (e) => {
    e.preventDefault();
    $scanDrop.classList.add("is-dragging");
  }),
);
["dragleave", "drop"].forEach((type) =>
  $scanDrop.addEventListener(type, (e) => {
    e.preventDefault();
    $scanDrop.classList.remove("is-dragging");
  }),
);
$scanDrop.addEventListener("drop", (e) => {
  const file = e.dataTransfer && e.dataTransfer.files[0];
  if (file) handleScanFile(file);
});
$brandHome.addEventListener("click", (e) => { e.preventDefault(); goHome(); });

document.querySelectorAll(".pill-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const q = chip.getAttribute("data-query") || "";
    $searchInput.value = q;
    updateClearButton();
    setNav("search");
    setHomeChrome(false);
    setStatus("loading", "Searching…");
    doSearch(q.trim());
  });
});

/* ── Search ─────────────────────────────────────────────────── */
async function doSearch(query) {
  try {
    const data = await searchMedicines(query);
    // Stale-query guard: if input changed while we were fetching, discard
    if ($searchInput.value.trim() !== query) return;
    setStatus(null);
    if (data.count === 0) {
      setHomeChrome(false);
      clearContent();
      setStatus("empty", `No branded medicines found for "${query}". Try a different spelling.`);
      return;
    }
    renderSearchResults(data);
  } catch (err) {
    handleError(err);
  }
}

/* ── Render: search results ─────────────────────────────────── */
function renderSearchResults(data) {
  fromScan = false;
  setHomeChrome(false);
  clearContent();
  setStatus(null);

  const heading = document.createElement("p");
  heading.className = "section-title fade-in";
  const resultWord = data.count === 1 ? "result" : "results";
  heading.textContent = `${data.count} ${resultWord} for "${data.query}"`;
  $contentArea.appendChild(heading);

  const list = document.createElement("div");
  list.className = "card-list fade-in";

  data.results.forEach((med) => {
    list.appendChild(createMedCard(med));
  });
  $contentArea.appendChild(list);
}

function createMedCard(med) {
  const card = document.createElement("div");
  card.className = "med-card";
  card.setAttribute("role", "button");
  card.setAttribute("tabindex", "0");
  card.setAttribute("aria-label", `View details for ${med.name}`);
  bindActivate(card, () => navigateToBrand(med.brand_id));

  const metaItems = [med.norm_dosage_form, med.pack_size_label].filter(Boolean);
  const metaHtml = metaItems
    .map((s) => `<span class="meta-tag">${escHtml(s)}</span>`)
    .join("");

  card.innerHTML = `
    <div class="med-card__top">
      <div class="med-card__name">${escHtml(med.name)}</div>
      <div class="price-tag">${formatPrice(med.price)}</div>
    </div>
    <div class="med-card__mfg">${escHtml(med.manufacturer_name)}</div>
    ${med.norm_composition ? `<div class="med-card__composition">${escHtml(med.norm_composition)}</div>` : ""}
    ${metaHtml ? `<div class="med-card__meta">${metaHtml}</div>` : ""}
    <div class="card-cta">View details <span aria-hidden="true">→</span></div>
  `;
  return card;
}

/* ── Navigate: brand detail ─────────────────────────────────── */
async function navigateToBrand(brandId) {
  currentBrandId  = brandId;
  currentDrugCode = null;
  setNav("search");
  setHomeChrome(false);
  clearContent();
  setStatus("loading", "Loading medicine details…");

  try {
    const [detail, matchesData] = await Promise.all([
      fetchBrandDetail(brandId),
      fetchBrandMatches(brandId),
    ]);
    setStatus(null);
    renderBrandDetail(detail, matchesData);
  } catch (err) {
    handleError(err);
  }
}

/* ── Render: brand detail ───────────────────────────────────── */
function renderBrandDetail(detail, matchesData) {
  setHomeChrome(false);
  clearContent();

  // Back button
  const back = fromScan && lastScan
    ? makeBackButton("Back to prescription", () => renderScanResults(lastScan.data))
    : makeBackButton("Back to search", () => {
        currentBrandId = null;
        goHome();
      });
  $contentArea.appendChild(back);

  // Detail header
  const article = document.createElement("article");
  article.className = "fade-in";

  const headerEl = document.createElement("div");
  headerEl.className = "detail-header";
  headerEl.innerHTML = `
    <div class="detail-header__left">
      <h1 class="detail-name">${escHtml(detail.name)}</h1>
      <div class="detail-mfg">${escHtml(detail.manufacturer_name)}</div>
    </div>
    <div class="detail-price-block">
      <div class="detail-price">${formatPrice(detail.price)}</div>
      <div class="detail-price-label">MRP</div>
    </div>
  `;
  article.appendChild(headerEl);

  // Properties
  const propRows = [
    ["Pack size",    detail.pack_size_label],
    ["Type",         detail.type],
    detail.norm_dosage_form ? ["Dosage form", detail.norm_dosage_form] : null,
    detail.norm_composition ? ["Composition",  detail.norm_composition] : null,
  ].filter(Boolean);

  const propGrid = document.createElement("div");
  propGrid.className = "prop-grid";
  propRows.forEach(([label, value]) => {
    propGrid.innerHTML += `
      <div class="prop-row">
        <div class="prop-row__label">${escHtml(label)}</div>
        <div class="prop-row__value">${escHtml(value)}</div>
      </div>`;
  });
  article.appendChild(propGrid);

  // Components table
  const compHeading = document.createElement("h2");
  compHeading.className = "section-heading";
  compHeading.textContent = "Active ingredients";
  article.appendChild(compHeading);

  if (detail.components.length > 0) {
    const table = document.createElement("table");
    table.className = "comp-table";
    table.innerHTML = `
      <thead><tr><th>#</th><th>Ingredient</th><th>Strength</th></tr></thead>
      <tbody>
        ${detail.components.map((c) => `
          <tr>
            <td>${c.seq}</td>
            <td>${escHtml(c.ingredient)}</td>
            <td>${c.strength ? escHtml(c.strength) : "—"}</td>
          </tr>`).join("")}
      </tbody>`;
    article.appendChild(table);
  } else {
    const noComp = document.createElement("p");
    noComp.className = "match-empty__body";
    noComp.textContent = "No component data available for this product.";
    article.appendChild(noComp);
  }

  $contentArea.appendChild(article);

  // Matches section
  const matchSection = document.createElement("section");
  matchSection.className = "matches-block fade-in";

  const matchHeading = document.createElement("h2");
  matchHeading.className = "section-heading";
  matchHeading.textContent = "Jan Aushadhi matches";
  matchSection.appendChild(matchHeading);

  if (matchesData.matches.length === 0) {
    matchSection.innerHTML += `
      <div class="match-empty">
        <div class="match-empty__icon" aria-hidden="true">🔍</div>
        <div class="match-empty__title">No strong match found</div>
        <p class="match-empty__body">
          No Jan Aushadhi product met the current composition-matching rules for this medicine.
        </p>
      </div>`;
  } else {
    const introEl = document.createElement("p");
    introEl.className = "match-intro";
    introEl.innerHTML = `
      Match confidence reflects how closely the compositions align in our data pipeline —
      it is <strong>not a clinical equivalence score</strong> and does not mean these
      medicines are interchangeable. Consult your pharmacist or doctor before switching.`;
    matchSection.appendChild(introEl);

    matchesData.matches.forEach((m) => {
      matchSection.appendChild(createMatchCard(detail.brand_id, m));
    });
  }

  $contentArea.appendChild(matchSection);
}

/* ── Match card ─────────────────────────────────────────────── */
function createMatchCard(brandId, match) {
  const card = document.createElement("div");
  card.className = "match-card";
  card.setAttribute("role", "button");
  card.setAttribute("tabindex", "0");
  card.setAttribute("aria-label", `Compare ${match.generic_name} — click to view price comparison`);
  bindActivate(card, () => navigateToComparison(brandId, match.drug_code));

  const metaItems = [formatMethod(match.match_method), match.norm_dosage_form, match.unit_size].filter(Boolean);
  const metaHtml = metaItems
    .map((s) => `<span class="meta-tag">${escHtml(s)}</span>`)
    .join("");

  card.innerHTML = `
    <div class="match-card__header">
      <div class="match-card__name">${escHtml(match.generic_name)}</div>
      <div class="match-card__mrp">${formatPrice(match.mrp)}</div>
    </div>
    ${metaHtml ? `<div class="match-card__meta">${metaHtml}</div>` : ""}
    <div class="match-card__footer">
      ${confBadgeHtml(match.validation_status, match.match_confidence)}
      <span class="card-cta">Compare prices <span aria-hidden="true">→</span></span>
    </div>
  `;
  return card;
}

/* ── Navigate: comparison ───────────────────────────────────── */
async function navigateToComparison(brandId, drugCode) {
  currentBrandId  = brandId;
  currentDrugCode = drugCode;
  setNav("search");
  setHomeChrome(false);
  clearContent();
  setStatus("loading", "Loading price comparison…");

  try {
    const data = await fetchComparison(brandId, drugCode);
    setStatus(null);
    renderComparison(data);
  } catch (err) {
    handleError(err);
  }
}

/* ── Render: price comparison ───────────────────────────────── */
function renderComparison(data) {
  setHomeChrome(false);
  clearContent();

  // Back button
  const back = makeBackButton(
    `Back to ${shortBrandLabel(data.brand.name)}`,
    () => navigateToBrand(data.brand.brand_id),
  );
  $contentArea.appendChild(back);

  const brandPrice = Number(data.brand.price);
  const janPrice   = Number(data.pmbi.mrp);
  const diff       = Number(data.price_comparison.difference); // brand - pmbi
  const absDiff    = Math.abs(diff);

  // Savings hero
  const heroEl = document.createElement("section");
  heroEl.setAttribute("aria-label", "Price difference summary");

  let heroClass = "savings-hero";
  let kickerText, amountText, pctText;

  if (diff > 0) {
    heroClass += "";  // green (default)
    kickerText  = "Jan Aushadhi is listed at";
    amountText  = `${formatPrice(absDiff)} less`;
    pctText     = brandPrice > 0 ? `${Math.round((diff / brandPrice) * 100)}% lower than the branded price` : "";
  } else if (diff < 0) {
    heroClass += " savings-hero--higher";
    kickerText  = "Jan Aushadhi is listed at";
    amountText  = `${formatPrice(absDiff)} more`;
    pctText     = brandPrice > 0 ? `${Math.round((absDiff / brandPrice) * 100)}% higher than the branded price` : "";
  } else {
    heroClass += " savings-hero--same";
    kickerText  = "Listed price";
    amountText  = "Same price";
    pctText     = "The listed MRP is identical for both products.";
  }

  heroEl.className = heroClass + " fade-in";
  heroEl.innerHTML = `
    <div class="savings-hero__kicker">${escHtml(kickerText)}</div>
    <div class="savings-hero__amount">${escHtml(amountText)}</div>
    ${pctText ? `<div class="savings-hero__pct">${escHtml(pctText)}</div>` : ""}
    <p class="savings-hero__caveat">
      Arithmetic difference of stored MRP values.
      Pack sizes are not normalised — this is not a guaranteed saving.
      Verify prices at your nearest Jan Aushadhi store.
    </p>
  `;
  $contentArea.appendChild(heroEl);

  // Compare pair cards
  const qty = comparableTabletCount(data.brand, data.pmbi);

  const pairEl = document.createElement("div");
  pairEl.className = "compare-pair fade-in";

  // Brand card
  const brandCard = document.createElement("div");
  brandCard.className = "compare-card compare-card--brand";
  let brandPackHtml;
  if (qty) {
    const perUnit = brandPrice / qty;
    brandPackHtml = `
      <div class="pack-dots" aria-hidden="true">${"<span class='pack-dot'></span>".repeat(qty)}</div>
      <div class="compare-card__pack">${qty} tablets / strip</div>
      <div class="compare-card__price">${formatPrice(brandPrice)}</div>
      <div class="compare-card__per-unit">${formatPrice(perUnit)} per tablet</div>`;
  } else {
    brandPackHtml = `
      <div class="compare-card__pack">${escHtml(data.brand.pack_size_label)}</div>
      <div class="compare-card__price">${formatPrice(brandPrice)}</div>`;
  }
  brandCard.innerHTML = `
    <div class="compare-card__badge">Branded medicine</div>
    <div class="compare-card__name">${escHtml(data.brand.name)}</div>
    <div class="compare-card__sub">${escHtml(data.brand.manufacturer_name)}</div>
    ${brandPackHtml}
  `;

  // Jan card
  const janCard = document.createElement("div");
  janCard.className = "compare-card compare-card--jan";
  let janPackHtml;
  if (qty) {
    const perUnit = janPrice / qty;
    janPackHtml = `
      <div class="pack-dots" aria-hidden="true">${"<span class='pack-dot'></span>".repeat(qty)}</div>
      <div class="compare-card__pack">${qty} tablets / strip</div>
      <div class="compare-card__price">${formatPrice(janPrice)}</div>
      <div class="compare-card__per-unit">${formatPrice(perUnit)} per tablet</div>`;
  } else {
    janPackHtml = `
      <div class="compare-card__pack">${escHtml(data.pmbi.unit_size)}</div>
      <div class="compare-card__price">${formatPrice(janPrice)}</div>`;
  }
  janCard.innerHTML = `
    <div class="compare-card__badge">Jan Aushadhi / PMBI</div>
    <div class="compare-card__name">${escHtml(data.pmbi.generic_name)}</div>
    <div class="compare-card__sub">${escHtml(data.pmbi.group_name)}</div>
    ${janPackHtml}
  `;

  pairEl.appendChild(brandCard);
  pairEl.appendChild(janCard);
  $contentArea.appendChild(pairEl);

  // Pack size caveat (when not normalised)
  if (!qty) {
    const caveat = document.createElement("p");
    caveat.className = "compare-disclaimer fade-in";
    caveat.innerHTML = `
      <strong>Note:</strong> Pack sizes could not be normalised for a per-unit comparison.
      The prices above are total MRPs for their respective pack/unit sizes —
      "${escHtml(data.brand.pack_size_label)}" vs "${escHtml(data.pmbi.unit_size)}".
      The arithmetic difference may not reflect the true per-dose cost difference.`;
    $contentArea.appendChild(caveat);
  }

  // Match status row
  const confPct  = Math.round(data.match.match_confidence * 100);
  const method   = formatMethod(data.match.match_method);
  const formText = data.brand.norm_dosage_form || data.pmbi.norm_dosage_form || "";
  const sizeText = qty ? `${qty} tablets` : data.pmbi.unit_size;

  const statusRow = document.createElement("div");
  statusRow.className = "match-status-row fade-in";
  statusRow.setAttribute("aria-label", "Match information");
  statusRow.innerHTML = `
    ${confBadgeHtml(data.match.validation_status, data.match.match_confidence)}
    ${method   ? `<span><strong>Method:</strong> ${escHtml(method)}</span>` : ""}
    ${formText ? `<span><strong>Form:</strong> ${escHtml(formText)}</span>` : ""}
    ${sizeText ? `<span><strong>Pack:</strong> ${escHtml(sizeText)}</span>` : ""}
  `;
  $contentArea.appendChild(statusRow);

  // Composition comparison table
  const maxLen = Math.max(data.brand.components.length, data.pmbi.components.length);
  const tableWrap = document.createElement("div");
  tableWrap.className = "compare-table-wrap fade-in";

  const rows = [];
  if (maxLen > 0) {
    for (let i = 0; i < maxLen; i++) {
      const bc = data.brand.components[i];
      const pc = data.pmbi.components[i];
      const left  = bc ? `${bc.ingredient}${bc.strength ? " " + bc.strength : ""}` : "—";
      const right = pc ? `${pc.ingredient}${pc.strength ? " " + pc.strength : ""}` : "—";
      rows.push(`<tr><td>${escHtml(left)}</td><td>${escHtml(right)}</td></tr>`);
    }
  } else {
    const left  = data.brand.norm_composition || "—";
    const right = data.pmbi.norm_composition  || "—";
    rows.push(`<tr><td>${escHtml(left)}</td><td>${escHtml(right)}</td></tr>`);
  }
  // Dosage form row
  rows.push(`<tr>
    <td>${escHtml(data.brand.norm_dosage_form || "—")}</td>
    <td>${escHtml(data.pmbi.norm_dosage_form  || "—")}</td>
  </tr>`);
  // Pack size row
  rows.push(`<tr>
    <td>${escHtml(qty ? `${qty} tablets` : data.brand.pack_size_label)}</td>
    <td>${escHtml(data.pmbi.unit_size)}</td>
  </tr>`);

  tableWrap.innerHTML = `
    <table class="compare-table" aria-label="Composition comparison">
      <thead>
        <tr>
          <th scope="col">${escHtml(data.brand.name)} (Branded)</th>
          <th scope="col">${escHtml(data.pmbi.generic_name)} (Jan Aushadhi)</th>
        </tr>
      </thead>
      <tbody>${rows.join("")}</tbody>
    </table>`;
  $contentArea.appendChild(tableWrap);
  $contentArea.appendChild(kendraPrompt());

  // Medical disclaimer
  const disclaimer = document.createElement("p");
  disclaimer.className = "compare-disclaimer fade-in";
  disclaimer.innerHTML = `
    <strong>Not medical advice.</strong>
    A data match is not proof that these medicines are clinically interchangeable.
    Formulations, excipients, bioequivalence, and regulatory status may differ.
    Consult your doctor or a registered pharmacist before switching any medication.`;
  $contentArea.appendChild(disclaimer);
}

/* ── Prescription scanning ──────────────────────────────────── */
const MAX_SCAN_BYTES = 10 * 1024 * 1024;
const SCAN_TYPES = /^(image\/(jpeg|png|webp|bmp|tiff)|application\/pdf)$/;

const REVIEW_TEXT = {
  low_text_similarity:   "Name only partly matches",
  low_name_similarity:   "Brand name is a loose match",
  strength_not_found:    "Written strength not found for this brand",
  strength_not_written:  "No strength written; several strengths exist",
  ambiguous_name:        "Several similar brand names",
  low_ocr_confidence:    "Handwriting was hard to read",
  no_jan_aushadhi_match: "No Jan Aushadhi equivalent found",
  alternative_reading:   "Matched another possible reading of the handwriting",
};

const SOURCE_TEXT = {
  validated_match:   "Validated match",
  composition_match: "Same composition",
  ingredient_lookup: "Same ingredient",
};

function renderScanPage() {
  setNav("scan");
  currentBrandId  = null;
  currentDrugCode = null;
  setStatus(null);
  clearContent();
  setHomeChrome(false);
  show($scanCard);
  $scanDrop.focus();
}

async function handleScanFile(file) {
  if (!SCAN_TYPES.test(file.type)) {
    setStatus("error", "Please upload a JPG, PNG, WEBP, BMP, TIFF image or a PDF.");
    return;
  }
  if (file.size > MAX_SCAN_BYTES) {
    setStatus("error", "That file is larger than 10 MB. Try a smaller photo.");
    return;
  }

  setNav("scan");
  setHomeChrome(false);
  clearContent();
  setStatus("loading", "Reading the prescription… the first scan can take up to a minute while the OCR model loads.");

  if (lastScan && lastScan.previewUrl) URL.revokeObjectURL(lastScan.previewUrl);
  const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : null;

  try {
    const data = await scanPrescription(file);
    lastScan = { data, previewUrl, fileName: file.name };
    setStatus(null);
    renderScanResults(data);
  } catch (err) {
    handleError(err);
    show($scanCard);
  }
}

async function rematchText(text) {
  setStatus("loading", "Matching corrected text…");
  try {
    const data = await matchPrescriptionText(text);
    lastScan = { ...lastScan, data };
    setStatus(null);
    renderScanResults(data);
  } catch (err) {
    handleError(err);
  }
}

/** Swap one detected medicine for a different brand the user picked. */
async function chooseAlternative(index, alt) {
  setStatus("loading", `Looking up ${alt.name}…`);
  try {
    const result = await matchPrescriptionText(alt.name);
    const replacement = result.medicines.find((m) => m.brand && m.brand.brand_id === alt.brand_id)
      || result.medicines[0];
    if (!replacement) throw new Error("No match for alternative");
    const data = lastScan.data;
    const original = data.medicines[index];
    // Keep the original prescription line; the user confirmed the brand themselves.
    data.medicines[index] = {
      ...replacement,
      line: original.line,
      needs_review: false,
      review_reasons: replacement.review_reasons.filter((r) => r === "no_jan_aushadhi_match"),
      alternatives: original.alternatives
        .filter((a) => a.brand_id !== alt.brand_id)
        .concat(original.brand ? [original.brand] : []),
    };
    setStatus(null);
    renderScanResults(data);
  } catch (err) {
    handleError(err);
  }
}

function renderScanResults(data) {
  setNav("scan");
  setHomeChrome(false);
  clearContent();
  currentBrandId = null;
  fromScan = true;

  $contentArea.appendChild(makeBackButton("Scan another prescription", () => {
    fromScan = false;
    renderScanPage();
  }));

  // Summary
  const meds      = data.medicines;
  const toReview  = meds.filter((m) => m.needs_review).length;
  const withJan   = meds.filter((m) => m.generics.length > 0).length;
  const savings   = meds.reduce((sum, m) => sum + (m.price_difference > 0 ? m.price_difference : 0), 0);

  const summary = document.createElement("section");
  summary.className = "scan-summary fade-in";
  summary.innerHTML = `
    <div class="scan-summary__main">
      <h1 class="scan-summary__title">
        ${meds.length === 0 ? "No medicines recognised" : `${meds.length} medicine${meds.length === 1 ? "" : "s"} found`}
      </h1>
      <p class="scan-summary__sub">
        ${withJan} with a Jan Aushadhi option${toReview ? ` · <strong>${toReview} to double-check</strong>` : ""}
        · read with ${escHtml(data.engine)}
      </p>
    </div>
    ${savings > 0 ? `
      <div class="scan-summary__save">
        <div class="scan-summary__save-amount">${formatPrice(savings)}</div>
        <div class="scan-summary__save-label">listed MRP difference*</div>
      </div>` : ""}
  `;
  $contentArea.appendChild(summary);

  if (lastScan && lastScan.previewUrl) {
    const preview = document.createElement("details");
    preview.className = "scan-preview fade-in";
    preview.innerHTML = `
      <summary>View uploaded prescription${lastScan.fileName ? ` (${escHtml(lastScan.fileName)})` : ""}</summary>
      <img src="${lastScan.previewUrl}" alt="Uploaded prescription">`;
    $contentArea.appendChild(preview);
  }

  if (data.warning === "not_a_prescription") {
    const empty = document.createElement("div");
    empty.className = "match-empty fade-in";
    empty.innerHTML = `
      <div class="match-empty__icon" aria-hidden="true">📄</div>
      <div class="match-empty__title">This doesn't look like a prescription</div>
      <p class="match-empty__body">
        No line had medicine details such as Tab/Cap, a strength (500 mg) or dosing (1-0-1, BD).
        If it is a prescription, correct the recognised text below and match again.
      </p>`;
    $contentArea.appendChild(empty);
  } else if (meds.length === 0) {
    const empty = document.createElement("div");
    empty.className = "match-empty fade-in";
    empty.innerHTML = `
      <div class="match-empty__icon" aria-hidden="true">📝</div>
      <div class="match-empty__title">We couldn't match any medicine names</div>
      <p class="match-empty__body">
        Try a sharper, well-lit photo, or correct the recognised text below and match again.
      </p>`;
    $contentArea.appendChild(empty);
  }

  const list = document.createElement("div");
  list.className = "rx-list fade-in";
  meds.forEach((m, i) => list.appendChild(createRxCard(m, i)));
  $contentArea.appendChild(list);
  if (withJan > 0) $contentArea.appendChild(kendraPrompt());

  if (data.unmatched_lines.length > 0) {
    const other = document.createElement("details");
    other.className = "scan-unmatched fade-in";
    other.innerHTML = `
      <summary>${data.unmatched_lines.length} other line${data.unmatched_lines.length === 1 ? "" : "s"} not recognised as medicines</summary>
      <ul>${data.unmatched_lines.map((l) => `<li>${escHtml(l.text)}</li>`).join("")}</ul>`;
    $contentArea.appendChild(other);
  }

  // Editable OCR text
  const editor = document.createElement("section");
  editor.className = "scan-editor fade-in";
  editor.innerHTML = `
    <h2 class="section-heading">Recognised text</h2>
    <p class="scan-editor__help">
      Handwriting is often misread. Fix any medicine names below (one per line) and match again.
    </p>
    <label class="sr-only" for="scan-text">Recognised prescription text</label>
    <textarea id="scan-text" class="scan-editor__text" rows="8" spellcheck="false"></textarea>
    <button type="button" class="btn-primary" id="scan-rematch">Match again</button>`;
  const $text = editor.querySelector("#scan-text");
  $text.value = data.lines.map((l) => l.text).join("\n");
  editor.querySelector("#scan-rematch").addEventListener("click", () => {
    const text = $text.value.trim();
    if (text) rematchText(text);
  });
  $contentArea.appendChild(editor);

  const disclaimer = document.createElement("p");
  disclaimer.className = "compare-disclaimer fade-in";
  disclaimer.innerHTML = `
    <strong>Check before you buy.</strong> ${escHtml(data.disclaimer)}
    * Sum of brand MRP minus the cheapest Jan Aushadhi MRP per medicine; pack sizes are not normalised.`;
  $contentArea.appendChild(disclaimer);
}

function createRxCard(med, index) {
  const card = document.createElement("article");
  card.className = "rx-card" + (med.needs_review ? " rx-card--review" : "");

  const reasons = med.review_reasons.map((r) => REVIEW_TEXT[r] || r);
  const badge = med.needs_review
    ? `<span class="rx-badge rx-badge--review" title="${escHtml(reasons.join(" · "))}">Check this</span>`
    : `<span class="rx-badge rx-badge--ok">Matched · ${Math.round(med.match_score)}%</span>`;

  let headHtml;
  if (med.kind === "brand") {
    const b = med.brand;
    headHtml = `
      <div class="rx-card__top">
        <button type="button" class="rx-card__name rx-link" data-brand="${b.brand_id}">${escHtml(b.name)}</button>
        <div class="price-tag">${formatPrice(b.price)}</div>
      </div>
      <div class="med-card__mfg">${escHtml(b.manufacturer_name)} · ${escHtml(b.pack_size_label)}</div>
      ${b.norm_composition ? `<div class="med-card__composition">${escHtml(b.norm_composition)}</div>` : ""}`;
  } else {
    headHtml = `
      <div class="rx-card__top">
        <div class="rx-card__name">${escHtml(med.ingredients.map(capitalise).join(" + "))}</div>
      </div>
      <div class="med-card__mfg">Generic name written on prescription</div>`;
  }

  const reasonsHtml = med.needs_review && reasons.length
    ? `<ul class="rx-reasons">${reasons.map((r) => `<li>${escHtml(r)}</li>`).join("")}</ul>`
    : "";

  let janHtml;
  if (med.generics.length === 0) {
    janHtml = `<p class="rx-none">No Jan Aushadhi product with the same composition was found.</p>`;
  } else {
    janHtml = `
      <div class="rx-jan__label">Jan Aushadhi options</div>
      <ul class="rx-jan">
        ${med.generics.slice(0, 4).map((g) => {
          const comparable = med.kind === "brand" && g.match_source === "validated_match";
          return `
          <li class="rx-jan__item${comparable ? " is-clickable" : ""}"
              ${comparable ? `data-compare="${g.drug_code}" tabindex="0" role="button"` : ""}>
            <div class="rx-jan__name">${escHtml(g.generic_name)}</div>
            <div class="rx-jan__meta">
              <span class="meta-tag">${escHtml(g.unit_size)}</span>
              <span class="meta-tag meta-tag--${g.match_source}">${escHtml(SOURCE_TEXT[g.match_source] || g.match_source)}</span>
              ${comparable ? `<span class="card-cta">Compare →</span>` : ""}
            </div>
            <div class="rx-jan__mrp">${formatPrice(g.mrp)}</div>
          </li>`;
        }).join("")}
      </ul>`;
  }

  let saveHtml = "";
  const perTablet = med.kind === "brand" ? perTabletPrices(med.brand, med.generics) : null;
  if (perTablet) {
    const cheaper = perTablet.jan < perTablet.brand;
    saveHtml = `<div class="rx-save${cheaper ? "" : " rx-save--higher"}">
        Per tablet: ${formatPrice(perTablet.brand)} branded vs
        <strong>${formatPrice(perTablet.jan)}</strong> Jan Aushadhi
        ${cheaper ? `(${Math.round((1 - perTablet.jan / perTablet.brand) * 100)}% less)` : ""}
      </div>`;
  } else if (med.price_difference != null) {
    const d = med.price_difference;
    saveHtml = d > 0
      ? `<div class="rx-save">Jan Aushadhi listed <strong>${formatPrice(d)} less</strong> (pack sizes may differ)</div>`
      : `<div class="rx-save rx-save--higher">Jan Aushadhi listed ${formatPrice(Math.abs(d))} ${d < 0 ? "more" : "same"}</div>`;
  }

  const altHtml = med.alternatives.length
    ? `<div class="rx-alts">
         <span class="rx-alts__label">Not this one?</span>
         ${med.alternatives.slice(0, 4).map((a, i) =>
           `<button type="button" class="pill-chip" data-alt="${i}">${escHtml(a.name)}</button>`).join("")}
       </div>`
    : "";

  card.innerHTML = `
    <div class="rx-card__line">
      <span class="rx-card__read">Read as “${escHtml(med.line.text)}”</span>
      ${badge}
    </div>
    ${med.line.note ? `<p class="rx-card__note">${escHtml(med.line.note)}</p>` : ""}
    ${headHtml}
    ${reasonsHtml}
    ${janHtml}
    ${saveHtml}
    ${altHtml}`;

  const brandLink = card.querySelector("[data-brand]");
  if (brandLink) brandLink.addEventListener("click", () => navigateToBrand(med.brand.brand_id));
  card.querySelectorAll("[data-compare]").forEach((el) =>
    bindActivate(el, () => navigateToComparison(med.brand.brand_id, Number(el.dataset.compare))),
  );
  card.querySelectorAll("[data-alt]").forEach((el) =>
    el.addEventListener("click", () => chooseAlternative(index, med.alternatives[Number(el.dataset.alt)])),
  );
  return card;
}

/** Per-tablet prices vs the cheapest generic, when both pack sizes are tablet counts. */
function perTabletPrices(brand, generics) {
  if (!isTabletForm(brand.norm_dosage_form) || generics.length === 0) return null;
  const brandCount = parseUnitCount(brand.pack_size_label);
  if (!brandCount) return null;
  let best = null;
  generics.forEach((g) => {
    const count = isTabletForm(g.norm_dosage_form) ? parseUnitCount(g.unit_size) : null;
    if (count && (best === null || g.mrp / count < best)) best = g.mrp / count;
  });
  return best === null ? null : { brand: brand.price / brandCount, jan: best };
}

function capitalise(s) {
  const str = String(s || "");
  return str.charAt(0).toUpperCase() + str.slice(1);
}

/* ── Render: How it works page ──────────────────────────────── */
function renderHowPage() {
  setNav("how");
  currentBrandId  = null;
  currentDrugCode = null;
  setStatus(null);
  setHomeChrome(false);
  clearContent();

  $contentArea.innerHTML = `
    <div class="how-page fade-in">
      <h1 class="how-page__title">How Jan Aushadhi Finder works</h1>

      <div class="how-step">
        <div class="how-step__num" aria-hidden="true">1</div>
        <div>
          <div class="how-step__label">Search</div>
          <p>Enter any branded medicine name. The search uses our database of brand medicines.</p>
        </div>
      </div>
      <div class="how-step">
        <div class="how-step__num" aria-hidden="true">2</div>
        <div>
          <div class="how-step__label">Match</div>
          <p>
            We compare composition, active ingredient strength and dosage form against
            PMBI's published Jan Aushadhi drug list. Matches are ranked by confidence.
          </p>
        </div>
      </div>
      <div class="how-step">
        <div class="how-step__num" aria-hidden="true">3</div>
        <div>
          <div class="how-step__label">Compare</div>
          <p>
            View the listed MRPs side-by-side. Where pack sizes are identical, a
            per-tablet price is shown. Where they differ, the raw MRPs are shown with
            a clear caveat.
          </p>
        </div>
      </div>

      <div class="how-note" role="note">
        <strong>Match confidence explained:</strong>
        <br>
        A <em>Strong</em> data match means our matching pipeline found a high-confidence
        composition and form alignment. It is <strong>not</strong> a clinical equivalence
        rating and does not constitute a substitution recommendation. Regulatory approval,
        bioequivalence studies, excipients, and formulation differences can vary.
        Always consult a pharmacist or doctor before changing medication.
      </div>
    </div>
  `;
}

/* ── Error handling ─────────────────────────────────────────── */
/* ── Kendra finder ──────────────────────────────────────────── */
const KENDRA_PIN_KEY = "ja-kendra-pin";
const MATCH_TEXT = { same_pin: "Same PIN", nearby_pin: "Nearby PIN" };

function rememberedPin() {
  try { return localStorage.getItem(KENDRA_PIN_KEY) || ""; } catch (_) { return ""; }
}

function rememberPin(pin) {
  try { localStorage.setItem(KENDRA_PIN_KEY, pin); } catch (_) {}
}

function renderKendraPage() {
  setNav("kendra");
  currentBrandId  = null;
  currentDrugCode = null;
  setStatus(null);
  setHomeChrome(false);
  clearContent();

  const page = document.createElement("section");
  page.className = "kendra-page fade-in";
  page.innerHTML = `
    <h1 class="kendra-page__title">Find a Jan Aushadhi Kendra</h1>
    <p class="kendra-page__lead">
      Jan Aushadhi medicines are sold at PMBI Kendras. Enter your PIN code to see the
      stores in it and the nearest PIN codes around it, or browse by state and district.
    </p>
    <form class="kendra-form" id="kendra-pin-form" novalidate>
      <label class="kendra-form__label" for="kendra-pin">PIN code</label>
      <div class="kendra-form__row">
        <input id="kendra-pin" class="kendra-form__input" inputmode="numeric" maxlength="6"
               autocomplete="postal-code" placeholder="e.g. 110029" pattern="[1-9][0-9]{5}">
        <button type="submit" class="btn-primary">Find Kendras</button>
      </div>
    </form>
    <div class="kendra-browse">
      <span class="kendra-browse__or">or browse</span>
      <label class="sr-only" for="kendra-state">State</label>
      <select id="kendra-state" class="kendra-form__select"><option value="">State…</option></select>
      <label class="sr-only" for="kendra-district">District</label>
      <select id="kendra-district" class="kendra-form__select" disabled><option value="">District…</option></select>
    </div>
    <div id="kendra-results" class="kendra-results" aria-live="polite"></div>`;
  $contentArea.appendChild(page);

  const $pin      = page.querySelector("#kendra-pin");
  const $state    = page.querySelector("#kendra-state");
  const $district = page.querySelector("#kendra-district");
  const $results  = page.querySelector("#kendra-results");

  page.querySelector("#kendra-pin-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const pin = $pin.value.trim();
    if (!/^[1-9]\d{5}$/.test(pin)) {
      setStatus("error", "Enter a valid 6-digit PIN code.");
      $pin.focus();
      return;
    }
    rememberPin(pin);
    $state.value = "";
    $district.innerHTML = `<option value="">District…</option>`;
    $district.disabled = true;
    loadKendras($results, { pincode: pin });
  });

  $state.addEventListener("change", async () => {
    $district.innerHTML = `<option value="">District…</option>`;
    $district.disabled = true;
    if (!$state.value) return;
    try {
      const districts = await fetchKendraDistricts($state.value);
      $district.innerHTML += districts
        .map((d) => `<option value="${escHtml(d.name)}">${escHtml(d.name)} (${d.count})</option>`)
        .join("");
      $district.disabled = false;
      $district.focus();
    } catch (err) {
      handleKendraError(err);
    }
  });

  $district.addEventListener("change", () => {
    if (!$district.value) return;
    $pin.value = "";
    loadKendras($results, { state: $state.value, district: $district.value });
  });

  fetchKendraStates()
    .then((states) => {
      $state.innerHTML += states
        .map((s) => `<option value="${escHtml(s.name)}">${escHtml(s.name)} (${s.count})</option>`)
        .join("");
      const total = states.reduce((sum, s) => sum + s.count, 0);
      page.querySelector(".kendra-page__lead").insertAdjacentHTML(
        "beforeend", ` <strong>${total.toLocaleString("en-IN")} Kendras</strong> are listed.`);
    })
    .catch(handleKendraError);

  const saved = rememberedPin();
  if (saved) {
    $pin.value = saved;
    loadKendras($results, { pincode: saved });
  } else {
    $pin.focus();
  }
}

async function loadKendras($results, params, offset = 0) {
  setStatus(offset ? null : "loading", "Finding Kendras…");
  try {
    const data = await searchKendras(params, offset);
    setStatus(null);
    if (offset === 0) {
      $results.innerHTML = `
        <p class="section-title">${data.total.toLocaleString("en-IN")} Kendra${data.total === 1 ? "" : "s"}
          ${params.pincode ? `in and around PIN ${escHtml(params.pincode)}` : `in ${escHtml(data.query)}`}</p>
        <div class="kendra-list"></div>
        <p class="kendra-source">
          From PMBI's official Kendra list (${escHtml(data.source)}). Stores open and close;
          call ahead or check <a href="https://janaushadhi.gov.in/locate-kendra" target="_blank" rel="noopener">janaushadhi.gov.in</a>.
          Toll-free helpline 1800-180-8080.
        </p>`;
      if (data.total === 0) {
        $results.querySelector(".kendra-list").innerHTML = `
          <div class="match-empty">
            <div class="match-empty__title">No Kendras found</div>
            <p class="match-empty__body">Try a nearby PIN code, or browse by state and district.</p>
          </div>`;
      }
    }
    const list = $results.querySelector(".kendra-list");
    data.kendras.forEach((k) => list.appendChild(createKendraCard(k)));
    $results.querySelector(".kendra-more")?.remove();
    const shown = offset + data.kendras.length;
    if (shown < data.total) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "btn-secondary kendra-more";
      more.textContent = `Show more (${(data.total - shown).toLocaleString("en-IN")} left)`;
      more.addEventListener("click", () => loadKendras($results, params, shown));
      list.after(more);
    }
  } catch (err) {
    handleKendraError(err);
  }
}

function createKendraCard(k) {
  const card = document.createElement("article");
  card.className = "kendra-card";
  const place = [k.district, k.state].filter(Boolean).join(", ");
  const mapsQuery = `Jan Aushadhi Kendra ${k.address}, ${place} ${k.pincode}`.trim();
  card.innerHTML = `
    <div class="kendra-card__top">
      <span class="kendra-card__code">${escHtml(k.kendra_code)}</span>
      ${MATCH_TEXT[k.match] ? `<span class="kendra-card__badge kendra-card__badge--${k.match}">${MATCH_TEXT[k.match]}</span>` : ""}
    </div>
    <p class="kendra-card__address">${escHtml(k.address || "Address not listed")}</p>
    <p class="kendra-card__place">${escHtml(place)}${k.pincode ? ` · <strong>${escHtml(k.pincode)}</strong>` : ""}</p>
    <a class="kendra-card__map" target="_blank" rel="noopener"
       href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(mapsQuery)}">
      Open in Maps <span aria-hidden="true">↗</span>
    </a>`;
  return card;
}

function handleKendraError(err) {
  if (err instanceof ApiError && err.status !== 0 && typeof err.detail === "string") {
    setStatus("error", err.detail);
  } else {
    setStatus("error", "Could not reach the server. Is the backend running?");
  }
}

/** A "find a store" prompt for pages where the user has decided what to buy. */
function kendraPrompt() {
  const el = document.createElement("aside");
  el.className = "kendra-prompt fade-in";
  el.innerHTML = `
    <div>
      <div class="kendra-prompt__title">Buy at a Jan Aushadhi Kendra</div>
      <p class="kendra-prompt__body">These medicines are sold at PMBI stores. Find one near you by PIN code.</p>
    </div>
    <button type="button" class="btn-primary">Find a Kendra</button>`;
  el.querySelector("button").addEventListener("click", () => {
    renderKendraPage();
    window.scrollTo(0, 0);
  });
  return el;
}

function handleError(err) {
  setHomeChrome(false);
  clearContent();
  if (err instanceof ApiError) {
    if (err.status === 0 || /network error/i.test(err.detail || "")) {
      setStatus("error", "Could not reach the server. Is the backend running?");
    } else if (err.status === 404) {
      setStatus("error", "That medicine could not be found.");
    } else if (err.status === 400 || err.status === 422 || err.status === 503) {
      setStatus("error", typeof err.detail === "string" ? err.detail : "Invalid request.");
    } else {
      setStatus("error", "Something went wrong. Please try again.");
    }
  } else {
    setStatus("error", "An unexpected error occurred. Please try again.");
    console.error(err);
  }
}

/* ── Helpers ────────────────────────────────────────────────── */
function makeBackButton(label, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn-back";
  btn.textContent = label;
  btn.addEventListener("click", onClick);
  return btn;
}

/* ── Init ───────────────────────────────────────────────────── */
updateClearButton();
$searchInput.focus();
