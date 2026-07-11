function sortResults() {
  const tableElement = document.querySelector("#results-table");
  const table = document.querySelector("#results-table tbody");
  if (!tableElement || !table) return;
  const key = tableElement.dataset.sort || tableElement.dataset.defaultSort || "seeders";
  const direction = tableElement.dataset.direction || defaultSortDirection(key);
  const rows = Array.from(table.querySelectorAll("tr[data-title]"));
  const textKeys = new Set(["indexer", "title"]);
  rows.sort((a, b) => {
    const av = a.dataset[key] || "";
    const bv = b.dataset[key] || "";
    const result = textKeys.has(key)
      ? av.localeCompare(bv)
      : Number(av) - Number(bv);
    return direction === "asc" ? result : -result;
  });
  rows.forEach((row) => table.appendChild(row));
  updateSortHeaders(key, direction);
}

function defaultSortDirection(key) {
  return new Set(["indexer", "title"]).has(key) ? "asc" : "desc";
}

function updateSortHeaders(key, direction) {
  document.querySelectorAll(".sort-header").forEach((button) => {
    const active = button.dataset.sort === key;
    button.classList.toggle("active", active);
    button.dataset.direction = active ? direction : "";
    button.setAttribute("aria-sort", active ? (direction === "asc" ? "ascending" : "descending") : "none");
  });
}

function bindResultSortHeaders() {
  const table = document.querySelector("#results-table");
  if (!table) return;
  table.dataset.sort = table.dataset.defaultSort || "seeders";
  table.dataset.direction = defaultSortDirection(table.dataset.sort);
  document.querySelectorAll(".sort-header").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.dataset.sort;
      if (table.dataset.sort === key) {
        table.dataset.direction = table.dataset.direction === "asc" ? "desc" : "asc";
      } else {
        table.dataset.sort = key;
        table.dataset.direction = defaultSortDirection(key);
      }
      sortResults();
    });
  });
  sortResults();
}

function confirmForms() {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    if (form.dataset.confirmBound) return;
    form.dataset.confirmBound = "true";
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    });
  });
}

function bindBusyForms() {
  document.querySelectorAll("form button[data-busy-label]").forEach((button) => {
    const form = button.closest("form");
    if (form?.dataset.torrentControl !== undefined) return;
    if (!form || form.dataset.busyBound) return;
    form.dataset.busyBound = "true";
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      setBusyButton(event.submitter || button);
      document.body.dataset.formBusy = "true";
    });
  });
}

function bindTorrentControlForms() {
  document.querySelectorAll("form[data-torrent-control]").forEach((form) => {
    if (form.dataset.torrentBound) return;
    form.dataset.torrentBound = "true";
    form.addEventListener("submit", async (event) => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      const button = event.submitter || form.querySelector("button[type=submit]");
      setBusyButton(button);
      document.body.dataset.formBusy = "true";
      try {
        const response = await fetch(form.action, {
          method: "POST",
          headers: {"Accept": "application/json"},
          body: new FormData(form),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Torrent update failed.");
        setStatusMessage(data.status || "Torrent updated.");
        await refreshTorrents();
      } catch (error) {
        setStatusMessage(error.message);
        resetBusyButton(button);
      } finally {
        document.body.dataset.formBusy = "false";
      }
    });
  });
}

function setBusyButton(button) {
  if (!button) return;
  button.dataset.originalLabel = button.textContent;
  button.textContent = button.dataset.busyLabel || "Working...";
  button.classList.add("is-busy");
  button.disabled = true;
}

function resetBusyButton(button) {
  if (!button) return;
  button.textContent = button.dataset.originalLabel || button.textContent;
  button.classList.remove("is-busy");
  button.disabled = false;
}

function setStatusMessage(message) {
  const status = document.querySelector(".visible-status");
  if (status) status.textContent = message;
}

function renderTorrentRows(torrents, mediaKey) {
  const body = document.querySelector("#torrents-table tbody");
  if (!body) return;
  const query = document.querySelector('input[name="query"]')?.value || "";
  if (!torrents.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No torrents reported by Transmission.</td></tr>';
    return;
  }
  body.innerHTML = torrents.map((torrent) => {
    const action = torrent.paused ? "start" : "stop";
    const label = torrent.paused ? "Resume" : "Pause";
    const move = torrent.complete
      ? `<a class="button secondary" href="/torrents/${torrent.id}/move?media_key=${encodeURIComponent(mediaKey)}">Move</a>`
      : "";
    return `
      <tr>
        <td>${escapeHtml(torrent.state)}</td>
        <td>${escapeHtml(torrent.size)}</td>
        <td>${escapeHtml(torrent.progress)}</td>
        <td>${escapeHtml(torrent.eta)}</td>
        <td>${escapeHtml(torrent.download_rate)}</td>
        <td>${escapeHtml(torrent.peers)}</td>
        <td class="title-cell">${escapeHtml(torrent.name)}</td>
        <td class="actions">
          <form action="/torrents/${torrent.id}/${action}" method="post" data-torrent-control>
            <input type="hidden" name="media_key" value="${escapeHtml(mediaKey)}">
            <input type="hidden" name="query" value="${escapeHtml(query)}">
            <button type="submit" data-busy-label="${torrent.paused ? "Resuming..." : "Pausing..."}">${label}</button>
          </form>
          ${move}
          <form action="/torrents/${torrent.id}/remove_destroy" method="post" data-confirm="Remove torrent and delete local data?" data-torrent-control>
            <input type="hidden" name="media_key" value="${escapeHtml(mediaKey)}">
            <input type="hidden" name="query" value="${escapeHtml(query)}">
            <button type="submit" class="danger" data-busy-label="Removing...">Remove</button>
          </form>
        </td>
      </tr>`;
  }).join("");
  confirmForms();
  bindBusyForms();
  bindTorrentControlForms();
}

async function refreshTorrents() {
  const table = document.querySelector("#torrents-table");
  if (!table) return;
  const mediaKey = table.dataset.mediaKey || "";
  const response = await fetch("/api/torrents", {headers: {"Accept": "application/json"}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "torrent refresh failed");
  renderTorrentRows(data.torrents || [], mediaKey);
  const label = document.querySelector("#torrent-refresh-label");
  if (label) label.textContent = "Updated just now";
}

function pollTorrents() {
  if (!document.querySelector("#torrents-table")) return;
  window.setInterval(async () => {
    if (document.body.dataset.formBusy === "true") return;
    try {
      await refreshTorrents();
    } catch (error) {
      const label = document.querySelector("#torrent-refresh-label");
      if (label) label.textContent = error.message;
    }
  }, 5000);
}

function pollJob() {
  const status = document.querySelector("#job-status[data-job-id]");
  if (!status) return;
  const jobId = status.dataset.jobId;
  const timer = window.setInterval(async () => {
    const response = await fetch(`/api/jobs/${jobId}`, {headers: {"Accept": "application/json"}});
    const data = await response.json();
    if (!response.ok) {
      status.textContent = data.error || "Move status unavailable.";
      status.classList.add("error");
      window.clearInterval(timer);
      return;
    }
    status.textContent = data.error || data.message || data.status;
    status.classList.toggle("success", data.status === "complete");
    status.classList.toggle("error", data.status === "failed");
    if (data.status !== "running") {
      window.clearInterval(timer);
    }
  }, 1500);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

document.addEventListener("DOMContentLoaded", () => {
  bindResultSortHeaders();
  confirmForms();
  bindBusyForms();
  bindTorrentControlForms();
  pollTorrents();
  pollJob();
});
