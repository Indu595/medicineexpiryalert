const state = { medicines: [], status: "all", lowStock: false, query: "", editingId: null };
const $ = (selector) => document.querySelector(selector);
const dateFormatter = new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric" });
let dashboardRequest = 0;
let currentUser = null;
let dashboardInitialized = false;
let speakAlertsOnLoad = false;
let passwordResetToken = null;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("toast-error", isError);
  toast.classList.add("toast-visible");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove("toast-visible"), 3200);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = response.headers.get("content-type")?.includes("application/json")
    ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(data?.error || "Something went wrong. Please try again.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function statusLabel(medicine) {
  if (medicine.status === "expired") return "Expired";
  if (medicine.days_remaining === 0) return "Expires today";
  if (medicine.days_remaining === 1) return "1 day left";
  if (medicine.days_remaining <= 7) return `${medicine.days_remaining} days left`;
  if (medicine.days_remaining <= 15) return "Within 15 days";
  if (medicine.status === "soon") return "Within 30 days";
  if (medicine.status === "upcoming") return "Within 90 days";
  return "Safe";
}

function statusClass(status) {
  return status === "expired" ? "badge-expired"
    : status === "urgent" ? "badge-urgent"
      : status === "soon" ? "badge-soon"
        : status === "upcoming" ? "badge-upcoming" : "badge-safe";
}

function quantityMarkup(medicine) {
  return `<span class="quantity-value ${medicine.low_stock ? "quantity-low" : ""}">${medicine.quantity}<small> units</small></span>${medicine.low_stock && medicine.quantity > 0 ? '<span class="low-stock-tag">Low stock</span>' : ""}`;
}

function displayDate(value) {
  return value ? dateFormatter.format(new Date(`${value}T00:00:00`)) : "Not recorded";
}

function availabilityMarkup(medicine) {
  if (medicine.availability === "expired_stock") {
    return '<span class="availability-badge availability-expired-stock"><i></i>Expired — do not use</span>';
  }
  return medicine.availability === "in_stock"
    ? '<span class="availability-badge availability-in-stock"><i></i>In stock</span>'
    : '<span class="availability-badge availability-out-of-stock"><i></i>Out of stock</span>';
}

function medicineMarkup(medicine, mobile = false) {
  const name = escapeHtml(medicine.name);
  const batch = escapeHtml(medicine.batch_number);
  const expiry = displayDate(medicine.expiry_date);
  const manufactured = displayDate(medicine.manufacture_date);
  const icon = `<span class="medicine-icon ${medicine.status === "expired" || medicine.status === "urgent" ? "medicine-icon-alert" : ""}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4h8a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3Zm1 5h6m-6 4h6m-6 4h3"/></svg></span>`;
  const actionButtons = `<div class="row-actions"><button class="row-action" data-action="edit" data-id="${medicine.id}" aria-label="Edit ${name}" title="Edit"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 5 5 5M4 20l4.5-1 11-11a2.12 2.12 0 0 0-3-3l-11 11L4 20Z"/></svg></button><button class="row-action row-delete" data-action="delete" data-id="${medicine.id}" aria-label="Delete ${name}" title="Delete"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16m-10 4v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button></div>`;
  if (mobile) {
    return `<article class="mobile-medicine-card"><div class="mobile-card-top">${icon}<div class="mobile-medicine-name"><strong>${name}</strong><span>${batch}</span></div>${actionButtons}</div><div class="mobile-card-details"><span><small>MANUFACTURED</small><strong>${manufactured}</strong></span><span><small>EXPIRES</small><strong>${expiry}</strong></span><span><small>QUANTITY</small>${quantityMarkup(medicine)}</span><span><small>AVAILABILITY</small>${availabilityMarkup(medicine)}</span><span><small>EXPIRY STATUS</small><span class="status-badge ${statusClass(medicine.status)}"><i></i>${statusLabel(medicine)}</span></span></div></article>`;
  }
  return `<tr><td><div class="medicine-cell">${icon}<span class="medicine-name"><strong>${name}</strong><small>Medicine</small></span></div></td><td><span class="batch-number">${batch}</span></td><td><span class="manufacture-date">${manufactured}</span></td><td><span class="expiry-date">${expiry}</span></td><td>${quantityMarkup(medicine)}</td><td>${availabilityMarkup(medicine)}</td><td><span class="status-badge ${statusClass(medicine.status)}"><i></i>${statusLabel(medicine)}</span></td><td>${actionButtons}</td></tr>`;
}

function renderMedicines(medicines) {
  const rows = $("#medicineRows");
  const mobile = $("#mobileInventory");
  if (!medicines.length) {
    const empty = `<div class="empty-state"><span class="empty-icon">⌕</span><strong>No medicines found</strong><span>Try adjusting your search or filters.</span></div>`;
    rows.innerHTML = `<tr><td colspan="8">${empty}</td></tr>`;
    mobile.innerHTML = empty;
  } else {
    rows.innerHTML = medicines.map((medicine) => medicineMarkup(medicine)).join("");
    mobile.innerHTML = medicines.map((medicine) => medicineMarkup(medicine, true)).join("");
  }
  $("#inventoryCount").textContent = `${medicines.length} ${medicines.length === 1 ? "batch" : "batches"}`;
  $("#tableSummary").textContent = `Showing ${medicines.length} ${medicines.length === 1 ? "medicine batch" : "medicine batches"}`;
}

async function refreshDashboard() {
  if (!currentUser || currentUser.role !== "branch_admin") return;
  const requestId = ++dashboardRequest;
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  if (state.status !== "all") params.set("status", state.status);
  if (state.lowStock) params.set("low_stock", "true");
  try {
    const [medicines, stats, allMedicines] = await Promise.all([
      api(`/api/medicines?${params.toString()}`),
      api("/api/stats"),
      api("/api/medicines"),
    ]);
    if (requestId !== dashboardRequest) return;
    state.medicines = allMedicines;
    renderMedicines(medicines);
    $("#statExpired").textContent = stats.expired;
    $("#statUrgent").textContent = stats.urgent;
    $("#statSoon").textContent = stats.urgent + stats.soon;
    $("#statUpcoming").textContent = stats.urgent + stats.soon + stats.upcoming;
    $("#navTotal").textContent = stats.total;
    updateAlert(stats);
    sendBrowserAlerts(state.medicines);
    if (speakAlertsOnLoad) {
      speakAlertsOnLoad = false;
      speakExpiryAlerts();
    }
  } catch (error) {
    if (requestId !== dashboardRequest) return;
    if (error.status === 401) {
      showAuth();
      return;
    }
    showToast(error.message, true);
    $("#medicineRows").innerHTML = `<tr><td colspan="8" class="loading-cell">Unable to load expiry alerts. Please refresh to try again.</td></tr>`;
  }
}

function speakExpiryAlerts() {
  if (!("speechSynthesis" in window) || !("SpeechSynthesisUtterance" in window)) {
    showToast("Voice alerts are not supported in this browser.", true);
    return;
  }
  const expiredMedicines = state.medicines.filter(
    (medicine) => medicine.days_remaining < 0,
  );
  const expiringMedicines = state.medicines.filter(
    (medicine) => medicine.days_remaining >= 0 && medicine.days_remaining <= 15,
  );
  const describeMedicine = (medicine) => {
    const timing = medicine.days_remaining < 0
      ? "has expired"
      : medicine.days_remaining === 0
        ? "expires today"
        : `expires in ${medicine.days_remaining} day${medicine.days_remaining === 1 ? "" : "s"}`;
    return `${medicine.name}, batch ${medicine.batch_number}, ${timing}`;
  };
  const expiredMessage = expiredMedicines.length
    ? `Expired medicines: ${expiredMedicines.map(describeMedicine).join(". ")}`
    : "No medicines have expired";
  const expiringMessage = expiringMedicines.length
    ? `Medicines expiring within 15 days: ${expiringMedicines.map(describeMedicine).join(". ")}`
    : "no medicines expire within 15 days";
  const announcement = new SpeechSynthesisUtterance(
    `Expiry alert for ${currentUser.branch_name}. ${expiredMessage}. ${expiringMessage}. Please review the expiry dashboard.`,
  );
  announcement.lang = document.documentElement.lang || "en";
  announcement.rate = 0.95;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(announcement);
}

function updateAlert(stats) {
  const banner = $("#alertBanner");
  const urgentCount = stats.expired + stats.urgent;
  const soonCount = stats.soon + stats.upcoming;
  banner.classList.toggle("alert-banner-clear", !urgentCount && !soonCount);
  if (stats.expired) {
    $("#alertTitle").textContent = `${stats.expired} expired${stats.urgent ? ` · ${stats.urgent} expiring within 7 days` : ""}`;
    $("#alertDescription").textContent = stats.urgent
      ? "Remove expired medicines from use and review the urgent expiry alerts."
      : "Remove expired medicines from use immediately.";
  } else if (urgentCount) {
    $("#alertTitle").textContent = `${urgentCount} ${urgentCount === 1 ? "medicine expires" : "medicines expire"} within 7 days`;
    $("#alertDescription").textContent = "Check these items and plan their use or replacement.";
  } else if (soonCount) {
    $("#alertTitle").textContent = `${soonCount} ${soonCount === 1 ? "medicine is" : "medicines are"} on the expiry watch`;
    $("#alertDescription").textContent = "Review medicines approaching their expiry date.";
  } else {
    $("#alertTitle").textContent = "You’re all caught up";
    $("#alertDescription").textContent = "No medicines are approaching their expiry date.";
  }
  $("#reviewAlerts").textContent = "Review alerts →";
}

function openModal(medicine = null) {
  state.editingId = medicine?.id ?? null;
  $("#medicineForm").reset();
  $("#formError").textContent = "";
  $("#modalTitle").textContent = medicine ? "Edit medicine" : "Add a medicine";
  $("#saveMedicine").textContent = medicine ? "Save changes" : "Save medicine";
  if (medicine) {
    $("#medicineForm").elements.name.value = medicine.name;
    $("#medicineForm").elements.batch_number.value = medicine.batch_number;
    $("#medicineForm").elements.quantity.value = medicine.quantity;
    $("#medicineForm").elements.manufacture_date.value = medicine.manufacture_date || "";
    $("#medicineForm").elements.expiry_date.value = medicine.expiry_date;
  }
  $("#medicineModal").classList.add("modal-open");
  $("#medicineModal").setAttribute("aria-hidden", "false");
  $("#medicineForm").elements.name.focus();
}

function closeModal() {
  $("#medicineModal").classList.remove("modal-open");
  $("#medicineModal").setAttribute("aria-hidden", "true");
  state.editingId = null;
}

async function handleFormSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const editingId = state.editingId;
  const medicine = Object.fromEntries(new FormData(form).entries());
  medicine.quantity = Number(medicine.quantity);
  const button = $("#saveMedicine");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    await api(editingId ? `/api/medicines/${editingId}` : "/api/medicines", {
      method: editingId ? "PUT" : "POST",
      body: JSON.stringify(medicine),
    });
    closeModal();
    await refreshDashboard();
    showToast(editingId ? "Medicine updated." : "Medicine added.");
  } catch (error) {
    $("#formError").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = editingId ? "Save changes" : "Save medicine";
  }
}

async function handleRowAction(event) {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const medicine = state.medicines.find((item) => item.id === Number(button.dataset.id));
  if (!medicine) {
    await refreshDashboard();
    return;
  }
  if (button.dataset.action === "edit") {
    openModal(medicine);
    return;
  }
  if (!window.confirm(`Delete ${medicine.name} (batch ${medicine.batch_number})? This cannot be undone.`)) return;
  try {
    await api(`/api/medicines/${medicine.id}`, { method: "DELETE" });
    await refreshDashboard();
    showToast("Medicine deleted.");
  } catch (error) {
    showToast(error.message, true);
  }
}

function sendBrowserAlerts(medicines) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const today = new Date().toISOString().slice(0, 10);
  const notifiedKey = `expirycare-notified-${currentUser.branch_id}-${today}`;
  const notified = new Set(JSON.parse(localStorage.getItem(notifiedKey) || "[]"));
  medicines.filter((medicine) => medicine.notification_due)
    .forEach((medicine) => {
      const key = `${currentUser.branch_id}-${medicine.id}-${medicine.status}`;
      if (notified.has(key)) return;
      const detail = medicine.status === "expired" ? "is expired"
        : `expires in ${medicine.days_remaining} day${medicine.days_remaining === 1 ? "" : "s"}`;
      new Notification(`${currentUser.branch_name} medicine expiry alert`, {
        body: `${medicine.name} (batch ${medicine.batch_number}) ${detail}.`,
      });
      notified.add(key);
    });
  localStorage.setItem(notifiedKey, JSON.stringify([...notified]));
}

async function enableNotifications() {
  if (!("Notification" in window)) {
    showToast("Browser notifications are not supported in this browser.", true);
    return;
  }
  if (Notification.permission === "denied") {
    showToast("Notifications are blocked. Allow them in your browser settings.", true);
    return;
  }
  const permission = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
  if (permission === "granted") {
    $("#notificationButton").classList.add("notifications-enabled");
    sendBrowserAlerts(state.medicines);
    showToast("Browser alerts enabled.");
  }
}

function init() {
  $("#todayLabel").textContent = new Intl.DateTimeFormat("en", { weekday: "short", month: "short", day: "numeric" }).format(new Date());
  $("#medicineForm").elements.manufacture_date.max = new Date().toISOString().slice(0, 10);
  $("#authView").hidden = false;
  $("#appShell").hidden = true;
  startApp();
  $("#loginForm").addEventListener("submit", handleLogin);
  $("#forgotPasswordButton").addEventListener("click", showPasswordResetRequest);
  $("#passwordResetRequestForm").addEventListener("submit", handlePasswordResetRequest);
  $("#passwordResetForm").addEventListener("submit", handlePasswordReset);
  $("#backToLoginFromRequest").addEventListener("click", () => {
    $("#loginRole").value = "main_admin";
    showAuthForm("login");
    updateLoginFields();
  });
  $("#setupForm").addEventListener("submit", handleInitialSetup);
  $("#showSignup").addEventListener("click", () => showAuthForm("signup"));
  $("#showLogin").addEventListener("click", () => showAuthForm("login"));
  $("#loginRole").addEventListener("change", updateLoginFields);
  $("#adminBranchSelect").addEventListener("change", updateNewBranchField);
  $("#branchAccessForm").addEventListener("submit", handleBranchAccessSubmit);
  $("#branchRows").addEventListener("click", (event) => {
    const button = event.target.closest("[data-branch]");
    if (!button) return;
    $("#adminBranchSelect").value = button.dataset.branch;
    updateNewBranchField();
    $("#branchAccessForm").scrollIntoView({ behavior: "smooth", block: "start" });
    $("#branchAccessForm").elements.email.focus();
  });
  $("#logoutButton").addEventListener("click", logout);
  bindDashboardEvents();
}

function bindDashboardEvents() {
  if (dashboardInitialized) return;
  dashboardInitialized = true;
  window.setInterval(() => {
    if (!document.hidden && currentUser?.role === "branch_admin") refreshDashboard();
  }, 60 * 60 * 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && currentUser?.role === "branch_admin") refreshDashboard();
  });
  $("#addMedicineButton").addEventListener("click", () => openModal());
  $("#addMedicineButtonMobile").addEventListener("click", () => openModal());
  $("#closeModal").addEventListener("click", closeModal);
  $("#cancelModal").addEventListener("click", closeModal);
  $("#medicineModal").addEventListener("click", (event) => {
    if (event.target === $("#medicineModal")) closeModal();
  });
  $("#medicineForm").addEventListener("submit", handleFormSubmit);
  $("#medicineRows").addEventListener("click", handleRowAction);
  $("#mobileInventory").addEventListener("click", handleRowAction);
  $("#searchInput").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    window.clearTimeout(init.searchTimer);
    init.searchTimer = window.setTimeout(refreshDashboard, 160);
  });
  $("#statusFilter").addEventListener("change", (event) => {
    state.status = event.target.value;
    refreshDashboard();
  });
  $("#lowStockFilter").addEventListener("click", (event) => {
    state.lowStock = !state.lowStock;
    event.currentTarget.classList.toggle("filter-chip-active", state.lowStock);
    refreshDashboard();
  });
  $("#reviewAlerts").addEventListener("click", () => {
    state.status = "all";
    $("#statusFilter").value = "all";
    document.querySelector(".alert-banner").classList.add("alert-banner-hidden");
    $("#inventory").scrollIntoView({ behavior: "smooth" });
  });
  $("#dismissAlert").addEventListener("click", () => $("#alertBanner").classList.add("alert-banner-hidden"));
  $("#notificationButton").addEventListener("click", enableNotifications);
  $("#readExpiryAlerts").addEventListener("click", speakExpiryAlerts);
  $("#exportButton").addEventListener("click", () => { window.location.href = "/api/export.csv"; });
  $("#mobileMenu").addEventListener("click", () => $("#sidebar").classList.toggle("sidebar-open"));
  document.querySelectorAll("[data-status-link]").forEach((link) => link.addEventListener("click", () => {
    state.status = link.dataset.statusLink;
    $("#statusFilter").value = state.status;
    refreshDashboard();
  }));
  document.addEventListener("keydown", (event) => {
    if (!$("#appShell").hidden && currentUser?.role === "branch_admin" && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("#searchInput").focus();
    }
    if (event.key === "Escape") closeModal();
  });
}

function showAuth() {
  currentUser = null;
  state.medicines = [];
  $("#authView").hidden = false;
  $("#appShell").hidden = true;
}

async function startApp() {
  try {
    const setup = await api("/api/auth/setup-status");
    populateBranches(setup.branches);
    passwordResetToken = new URLSearchParams(window.location.search).get("reset_token");
    if (passwordResetToken) {
      window.history.replaceState({}, "", window.location.pathname);
      showPasswordResetForm();
      return;
    }
    if (setup.needs_setup) {
      $("#authSwitch").hidden = false;
      showAuthForm("signup");
      $("#authTitle").textContent = "Create main administrator";
      $("#authDescription").textContent = "Sign up as the administrator who grants branch access.";
      return;
    }
    $("#authSwitch").hidden = true;
    showAuthForm("login");
    $("#setupForm").hidden = true;
    $("#authTitle").textContent = "Sign in to ExpiryCare";
    $("#authDescription").textContent = "Sign in with the account provided by your hospital administrator.";
    updateLoginFields();
    try {
      const response = await api("/api/me");
      showAuthenticated(response.user);
    } catch (error) {
      if (error.status !== 401) throw error;
    }
  } catch (error) {
    $("#loginForm").hidden = false;
    $("#setupForm").hidden = true;
    $("#loginError").textContent = error.message;
  }
}

function showAuthForm(form) {
  const signingUp = form === "signup";
  $("#loginForm").hidden = signingUp;
  $("#setupForm").hidden = !signingUp;
  $("#passwordResetRequestForm").hidden = true;
  $("#passwordResetForm").hidden = true;
  $("#forgotPasswordButton").hidden = signingUp || $("#loginRole").value !== "main_admin";
  $("#showSignup").classList.toggle("auth-switch-active", signingUp);
  $("#showLogin").classList.toggle("auth-switch-active", !signingUp);
  if (signingUp) {
    $("#authTitle").textContent = "Create main administrator";
    $("#authDescription").textContent = "Sign up as the administrator who grants branch access.";
  } else {
    $("#authTitle").textContent = "Sign in to ExpiryCare";
    $("#authDescription").textContent = "Sign in with the account provided by your hospital administrator.";
  }
}

function populateBranches(branches) {
  const select = $("#loginBranch");
  select.replaceChildren();
  branches.forEach((branch) => {
    const option = document.createElement("option");
    option.value = branch.id;
    option.textContent = branch.name;
    select.append(option);
  });
  if (!branches.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No branches configured";
    select.append(option);
  }
}

function showAuthenticated(user) {
  currentUser = user;
  speakAlertsOnLoad = user.role === "branch_admin";
  $("#authView").hidden = true;
  $("#appShell").hidden = false;
  $("#profileUsername").textContent = user.email;
  $("#profileRole").textContent = user.role === "main_admin" ? "Main administrator" : "Branch head";
  $("#topAvatar").textContent = user.email.slice(0, 2).toUpperCase();
  $("#workspaceName").textContent = user.role === "main_admin" ? "Hospital network" : user.branch_name;
  $("#workspaceType").textContent = user.role === "main_admin" ? "Organization access" : "Branch workspace";
  $("#breadcrumbContext").textContent = user.role === "main_admin" ? "Hospital network" : user.branch_name;
  $("#breadcrumbCurrent").textContent = user.role === "main_admin" ? "Branch access" : "Expiry alerts";
  document.querySelectorAll(".branch-admin-only").forEach((element) => {
    element.hidden = user.role !== "branch_admin";
  });
  document.querySelectorAll(".main-admin-only").forEach((element) => {
    element.hidden = user.role !== "main_admin";
  });
  $("#notificationButton").hidden = user.role !== "branch_admin";
  if ("Notification" in window && Notification.permission === "granted") {
    $("#notificationButton").classList.add("notifications-enabled");
  } else {
    $("#notificationButton").classList.remove("notifications-enabled");
  }
  state.status = "all";
  state.lowStock = false;
  state.query = "";
  $("#searchInput").value = "";
  $("#statusFilter").value = "all";
  $("#lowStockFilter").classList.remove("filter-chip-active");
  if (user.role === "main_admin") refreshBranchManagement();
  else refreshDashboard();
}

function updateLoginFields() {
  const isBranchHead = $("#loginRole").value === "branch_admin";
  $("#loginBranchField").hidden = !isBranchHead;
  $("#loginBranch").required = isBranchHead;
  $("#forgotPasswordButton").hidden = isBranchHead || $("#loginForm").hidden;
}

function showPasswordResetRequest() {
  $("#loginForm").hidden = true;
  $("#setupForm").hidden = true;
  $("#passwordResetForm").hidden = true;
  $("#passwordResetRequestForm").hidden = false;
  $("#resetRequestEmail").value = $("#loginForm").elements.email.value;
  $("#passwordResetRequestError").textContent = "";
  $("#passwordResetRequestSuccess").textContent = "";
  $("#authTitle").textContent = "Forgot password?";
  $("#authDescription").textContent = "Request a secure reset link for the main administrator account.";
}

function showPasswordResetForm() {
  $("#authSwitch").hidden = true;
  $("#loginForm").hidden = true;
  $("#setupForm").hidden = true;
  $("#passwordResetRequestForm").hidden = true;
  $("#passwordResetForm").hidden = false;
  $("#authTitle").textContent = "Reset administrator password";
  $("#authDescription").textContent = "Use the reset link from your email to choose a new password.";
}

function updateNewBranchField() {
  const isNewBranch = !$("#adminBranchSelect").value;
  $("#newBranchField").hidden = !isNewBranch;
  $("#branchAccessForm").elements.branch_name.required = isNewBranch;
}

async function refreshBranchManagement() {
  try {
    const { branches } = await api("/api/admin/branches");
    const select = $("#adminBranchSelect");
    select.replaceChildren(new Option("Create a new branch…", ""));
    $("#branchRows").innerHTML = branches.length
      ? branches.map((branch) => `<tr><td>${escapeHtml(branch.name)}</td><td>${escapeHtml(branch.head_email || "Not assigned")}</td><td><button class="button button-secondary" type="button" data-branch="${branch.id}">${branch.head_email ? "Update access" : "Grant access"}</button></td></tr>`).join("")
      : '<tr><td colspan="3">No hospital branches have been created yet.</td></tr>';
    branches.forEach((branch) => select.add(new Option(branch.name, branch.id)));
    updateNewBranchField();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function handleBranchAccessSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  if (!values.branch_id) delete values.branch_id;
  const button = $("#branchAccessButton");
  $("#branchAccessError").textContent = "";
  button.disabled = true;
  try {
    await api("/api/admin/branch-access", { method: "POST", body: JSON.stringify(values) });
    form.reset();
    await refreshBranchManagement();
    showToast("Branch head access saved. Share the credentials securely.");
  } catch (error) {
    $("#branchAccessError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function handleLogin(event) {
  event.preventDefault();
  $("#loginError").textContent = "";
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  const button = $("#loginButton");
  button.disabled = true;
  try {
    const result = await api("/api/auth/login", { method: "POST", body: JSON.stringify(values) });
    form.reset();
    showAuthenticated(result.user);
  } catch (error) {
    $("#loginError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function handlePasswordResetRequest(event) {
  event.preventDefault();
  const button = $("#passwordResetRequestButton");
  $("#passwordResetRequestError").textContent = "";
  $("#passwordResetRequestSuccess").textContent = "";
  button.disabled = true;
  try {
    const result = await api("/api/auth/password-reset/request", {
      method: "POST",
      body: JSON.stringify({ email: $("#resetRequestEmail").value }),
    });
    $("#passwordResetRequestSuccess").textContent = result.message;
  } catch (error) {
    $("#passwordResetRequestError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function handlePasswordReset(event) {
  event.preventDefault();
  const password = $("#resetPassword").value;
  $("#passwordResetError").textContent = "";
  if (password !== $("#confirmResetPassword").value) {
    $("#passwordResetError").textContent = "Passwords do not match.";
    return;
  }
  const button = $("#passwordResetSubmitButton");
  button.disabled = true;
  try {
    const result = await api("/api/auth/password-reset/confirm", {
      method: "POST",
      body: JSON.stringify({ token: passwordResetToken, password }),
    });
    passwordResetToken = null;
    window.history.replaceState({}, "", window.location.pathname);
    $("#passwordResetForm").reset();
    $("#loginRole").value = "main_admin";
    showAuthForm("login");
    updateLoginFields();
    $("#authTitle").textContent = "Sign in to ExpiryCare";
    $("#authDescription").textContent = result.message;
  } catch (error) {
    $("#passwordResetError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function handleInitialSetup(event) {
  event.preventDefault();
  $("#setupError").textContent = "";
  const button = $("#setupButton");
  button.disabled = true;
  try {
    const values = Object.fromEntries(new FormData(event.currentTarget).entries());
    const result = await api("/api/auth/setup", { method: "POST", body: JSON.stringify(values) });
    showToast("Main administrator account created.");
    showAuthenticated(result.user);
  } catch (error) {
    $("#setupError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
    showAuth();
    $("#loginForm").reset();
    $("#loginError").textContent = "";
    await startApp();
  } catch (error) {
    showToast(error.message, true);
  }
}

document.addEventListener("DOMContentLoaded", init);
