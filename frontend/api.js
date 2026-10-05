/**
 * Centralized API client for the Jan Aushadhi Medicine API.
 * All endpoint calls go through this module.
 */

const API_BASE = "http://127.0.0.1:8000";

class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request(path) {
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`);
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

export { ApiError };
