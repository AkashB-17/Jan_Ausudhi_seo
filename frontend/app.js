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

/* ── State ──────────────────────────────────────────────────── */
let currentBrandId  = null;
let currentDrugCode = null;
let debounceTimer   = null;

/* ── Visibility helpers ─────────────────────────────────────── */
const show = (el) => el?.classList.remove("hidden");
const hide = (el) => el?.classList.add("hidden");

function setNav(which) {
  $navSearch.classList.toggle("is-active", which === "search");
  $navHow.classList.toggle("is-active", which === "how");
}

/** Toggle hero + examples + steps visibility */
function setHomeChrome(visible) {
  [$hero, $searchExs, $homeSteps, $searchHint].forEach((el) => {
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
$navHow.addEventListener("click", renderHowPage);
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
  const back = makeBackButton("← Back to search", () => {
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
    `← Back to ${shortBrandLabel(data.brand.name)}`,
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
function handleError(err) {
  setHomeChrome(false);
  clearContent();
  if (err instanceof ApiError) {
    if (err.status === 0 || /network error/i.test(err.detail || "")) {
      setStatus("error", "Could not reach the server. Is the backend running?");
    } else if (err.status === 404) {
      setStatus("error", "That medicine could not be found.");
    } else if (err.status === 400) {
      setStatus("error", err.detail || "Invalid request.");
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
