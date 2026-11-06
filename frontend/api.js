/**
 * Centralized API client for the Jan Aushadhi Medicine API.
 * All endpoint calls go through this module.
 */

// Override with ?api=http://127.0.0.1:8001 when port 8000 is taken.
const API_BASE = new URLSearchParams(location.search).get("api") || "http://127.0.0.1:8000";

class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, options) {
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, options);
  } catch (err) {
    throw new ApiError(0, "Network error — is the API server running?");
  }
  if (!res.ok) {
    let detail = `Server returned ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch (_) {}
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

/** GET /health */
export function fetchHealth() {
  return request("/health");
}

/** GET /medicines/search?q= */
export function searchMedicines(query) {
  return request(`/medicines/search?q=${encodeURIComponent(query)}`);
}

/** GET /medicines/{brand_id} */
export function fetchBrandDetail(brandId) {
  return request(`/medicines/${brandId}`);
}

/** GET /medicines/{brand_id}/matches */
export function fetchBrandMatches(brandId) {
  return request(`/medicines/${brandId}/matches`);
}

/** GET /pmbi/{drug_code} */
export function fetchPmbiDetail(drugCode) {
  return request(`/pmbi/${drugCode}`);
}

/** GET /compare/{brand_id}/{drug_code} */
export function fetchComparison(brandId, drugCode) {
  return request(`/compare/${brandId}/${drugCode}`);
}

/** POST /prescriptions/scan — image or PDF upload */
export function scanPrescription(file) {
  const body = new FormData();
  body.append("file", file);
  return request("/prescriptions/scan", { method: "POST", body });
}

/** POST /prescriptions/match-text — re-match corrected text */
export function matchPrescriptionText(text) {
  return request("/prescriptions/match-text", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

/** GET /kendras/states */
export function fetchKendraStates() {
  return request("/kendras/states");
}

/** GET /kendras/districts?state= */
export function fetchKendraDistricts(state) {
  return request(`/kendras/districts?state=${encodeURIComponent(state)}`);
}

/** GET /kendras/search — by { pincode } or { state, district } */
export function searchKendras(params, offset = 0) {
  const query = new URLSearchParams({ ...params, offset: String(offset), limit: "20" });
  return request(`/kendras/search?${query}`);
}

export { ApiError };
