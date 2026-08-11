// Keep browser requests same-origin.  Next.js proxies /api to the backend,
// which works both locally and when the portal is opened from another PC.
export const API = process.env.NEXT_PUBLIC_API_URL || "";

async function handle(res) {
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body.detail) detail = typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail);
    } catch { /* keep default */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export function apiGet(path, token) {
  return fetch(`${API}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    cache: "no-store",
  }).then(handle);
}

export function apiSend(path, method, body, token) {
  return fetch(`${API}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(handle);
}

// multipart/form-data upload (do NOT set Content-Type; the browser adds the
// boundary). `fields` are extra form fields alongside the file.
export function apiUpload(path, file, fieldName, token, fields) {
  const fd = new FormData();
  fd.append(fieldName, file);
  for (const [k, v] of Object.entries(fields || {})) fd.append(k, v);
  return fetch(`${API}${path}`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: fd,
  }).then(handle);
}

// Admin session helpers (browser only)
export function saveAdminSession(data) {
  sessionStorage.setItem("ats_admin", JSON.stringify(data));
  localStorage.removeItem("ats_admin");
}
export function adminSession() {
  try {
    const current = sessionStorage.getItem("ats_admin");
    const legacy = localStorage.getItem("ats_admin");
    if (!current && legacy) {
      sessionStorage.setItem("ats_admin", legacy);
      localStorage.removeItem("ats_admin");
    }
    return JSON.parse(sessionStorage.getItem("ats_admin") || "null");
  }
  catch { return null; }
}
export function clearAdminSession() {
  sessionStorage.removeItem("ats_admin");
  localStorage.removeItem("ats_admin");
}

// Public job-portal applicant session helpers (browser only)
export function saveApplicantSession(data) {
  sessionStorage.setItem("ats_applicant", JSON.stringify(data));
  localStorage.removeItem("ats_applicant");
}
export function applicantSession() {
  try {
    const current = sessionStorage.getItem("ats_applicant");
    const legacy = localStorage.getItem("ats_applicant");
    if (!current && legacy) {
      sessionStorage.setItem("ats_applicant", legacy);
      localStorage.removeItem("ats_applicant");
    }
    return JSON.parse(sessionStorage.getItem("ats_applicant") || "null");
  }
  catch { return null; }
}
export function clearApplicantSession() {
  sessionStorage.removeItem("ats_applicant");
  localStorage.removeItem("ats_applicant");
}
