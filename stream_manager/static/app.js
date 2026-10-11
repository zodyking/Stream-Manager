(function () {
  const base = window.INGRESS_PATH || "";

  function rooted(path) {
    if (base) return base + path;
    return String(path || "").replace(/^\//, "");
  }
  const main = document.getElementById("main");
  const nav = document.getElementById("nav");
  const menuBtn = document.getElementById("menu-btn");
  const backdrop = document.getElementById("backdrop");

  const iconOverview = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"></rect><rect x="14" y="3" width="7" height="7" rx="1"></rect><rect x="3" y="14" width="7" height="7" rx="1"></rect><rect x="14" y="14" width="7" height="7" rx="1"></rect></svg>';
  const iconCamera = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M4 8h4l2-2h4l2 2h4v11H4z"></path><circle cx="12" cy="13" r="3"></circle></svg>';

  const iconPlus = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>';
  const iconCheck = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"></path></svg>';

  const iconUp = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6"></path></svg>';
  const iconDown = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"></path></svg>';
  const iconTrash = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"></path></svg>';

  const sourceLabels = { rtsp: "RTSP", ha_camera: "Home Assistant camera", onvif: "ONVIF device", image: "Still image", media: "Video or GIF" };
  const operatorLabels = [["eq", "equals"], ["ne", "does not equal"], ["gt", "number above"], ["lt", "number below"]];

  function adoptParentTheme() {
    syncColorScheme();
  }

  function syncColorScheme() {
    const dark = !window.matchMedia("(prefers-color-scheme: light)").matches;
    document.documentElement.dataset.scheme = dark ? "dark" : "light";
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function rid() {
    return Math.random().toString(16).slice(2, 10);
  }

  async function api(path, options) {
    const response = await fetch(rooted(path), Object.assign({
      headers: { "Content-Type": "application/json" },
    }, options || {}));
    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        data = { error: text };
      }
    }
    if (!response.ok) throw new Error(data.error || response.statusText);
    return data;
  }

  function route() {
    const parts = (location.hash || "#/").replace(/^#/, "").split("/").filter(Boolean);
    if (parts[0] === "cameras" && parts[1] === "new") return { name: "edit", id: null };
    if (parts[0] === "cameras" && parts[2] === "status") return { name: "status", id: parts[1] };
    if (parts[0] === "cameras" && parts[1]) return { name: "edit", id: parts[1] };
    return { name: "overview" };
  }

  function setMenu(open) {
    nav.classList.toggle("open", open);
    menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    backdrop.hidden = !open;
  }

  menuBtn.addEventListener("click", function () {
    setMenu(!nav.classList.contains("open"));
  });
  backdrop.addEventListener("click", function () { setMenu(false); });
  nav.addEventListener("click", function (event) {
    if (event.target.closest("a")) setMenu(false);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") setMenu(false);
  });
  main.addEventListener("click", function (event) {
    const jump = event.target.closest("[data-scroll]");
    if (jump) {
      event.preventDefault();
      const target = document.getElementById(jump.getAttribute("data-scroll"));
      if (target) {
        target.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
        const heading = target.querySelector("h2");
        if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
      }
      return;
    }
    const button = event.target.closest(".copy-btn");
    if (!button) return;
    const value = button.getAttribute("data-copy") || "";
    const label = button.querySelector("span");
    navigator.clipboard.writeText(value).then(function () {
      if (!label) return;
      const previous = label.textContent;
      label.textContent = "Copied";
      window.setTimeout(function () { label.textContent = previous; }, 1200);
    });
  });

  function cameraState(camera) {
    const runtime = camera.runtime || {};
    if (!camera.enabled || runtime.reason === "disabled") return { kind: "off", label: "Disabled" };
    if (runtime.error) return { kind: "bad", label: "Error" };
    if (runtime.source_kind === "image" || runtime.source_type === "image") return { kind: "still", label: "Still image" };
    if (runtime.source_kind === "file" || runtime.source_type === "media") return { kind: "live", label: "File" };
    return { kind: "live", label: "Live" };
  }

  function renderNav(cameras, current) {
    const overviewOn = current.name === "overview";
    let html = '<div class="nav-brand">' +
      '<img class="brand-lockup brand-on-dark" src="' + rooted("/static/brand/stream-manager-horizontal-dark.svg") + '" alt="Stream Manager">' +
      '<img class="brand-lockup brand-on-light" src="' + rooted("/static/brand/stream-manager-horizontal-light.svg") + '" alt="">' +
      "</div>";
    html += '<a class="nav-link" href="#/"' + (overviewOn ? ' aria-current="page"' : "") + '><span class="nav-icon">' + iconOverview + '</span><span class="label">All cameras</span></a>';
    html += '<a class="nav-link" href="#/cameras/new"' + (current.name === "edit" && !current.id ? ' aria-current="page"' : "") + '><span class="nav-icon">' + iconPlus + '</span><span class="label">Add camera</span></a>';
    if (cameras && cameras.length) {
      html += '<div class="nav-section">Cameras</div>';
      html += cameras.map(function (camera) {
        const state = cameraState(camera);
        const on = current.id === camera.id;
        return '<a class="nav-link" href="#/cameras/' + camera.id + '"' + (on ? ' aria-current="page"' : "") + '>' +
          '<span class="dot ' + state.kind + '" title="' + esc(state.label) + '"></span>' +
          '<span class="label">' + esc(camera.name) + "</span></a>";
      }).join("");
    }
    nav.innerHTML = html;
  }

  function copyRow(label, value) {
    if (!value) return "";
    return '<div class="copy-row"><span class="copy-k">' + esc(label) + "</span><code>" + esc(value) +
      '</code><button type="button" class="copy-btn" data-copy="' + esc(value) + '" aria-label="Copy ' + esc(label) + ' address"><span>Copy</span></button></div>';
  }

  function copyList(pairs) {
    const rows = pairs.map(function (pair) { return copyRow(pair[0], pair[1]); }).join("");
    return rows ? '<div class="copy-list">' + rows + "</div>" : "";
  }

  function statusItem(kind, label, detail) {
    return '<span class="status-item" title="' + esc(detail) + '"><span class="dot ' + kind + '"></span>' + esc(label) + "</span>";
  }

  async function refreshStatusBar() {
    const bar = document.getElementById("status-bar");
    if (!bar) return null;
    try {
      const status = await api("/api/status");
      const ha = status.ha || {};
      const discovery = status.discovery || { bindings: [] };
      const engine = status.engine || {};
      const ports = status.ports || {};
      const bindings = discovery.bindings || [];
      const failed = bindings.find(function (item) { return item.error && item.ip !== "127.0.0.1"; });
      const answering = bindings.filter(function (item) { return !item.error; }).map(function (item) { return item.ip; });
      const haKind = ha.connected ? "live" : ha.mode === "off" ? "still" : "bad";
      const haLabel = ha.connected ? "Connected" : ha.mode === "off" ? "Not configured" : "Disconnected";
      const haDetail = ha.error || (ha.mode === "supervisor" ? "Using the Supervisor token." : ha.url || "Add a Home Assistant URL and token to use camera entities.");
      const discoveryDetail = failed
        ? failed.ip + ": " + failed.error
        : answering.length ? "Answering on " + answering.join(", ") + "." : "Add a camera on a real network to be discovered.";
      const moved = ports.rtsp_requested && ports.rtsp !== ports.rtsp_requested;
      const engineDetail = engine.error || ((engine.running ? "Publishing the active picture on the stable RTSP URL." : "ONVIF addresses are still advertised.") +
        (moved ? " RTSP port " + ports.rtsp_requested + " is used by another program, so streams use " + ports.rtsp + "." : ""));
      const rtspLabel = "RTSP " + (ports.rtsp || "—");
      const rtspDetail = moved
        ? "Port " + ports.rtsp_requested + " is used by another program, so streams use " + ports.rtsp + "."
        : "Streams are published on port " + (ports.rtsp || "—") + ".";
      const items = [
        statusItem(haKind, "Home Assistant · " + haLabel, haDetail),
        statusItem(discovery.ok ? "live" : "still", "Discovery · " + (discovery.ok ? "Ready" : "Check bindings"), discoveryDetail),
        statusItem(engine.running ? "live" : engine.binary ? "bad" : "still", "Engine · " + (engine.running ? "Running" : engine.binary ? "Not responding" : "Binary not found"), engineDetail),
        statusItem(moved ? "still" : "live", rtspLabel, rtspDetail),
      ];
      if (status.arp_isolation === "unavailable") {
        items.push(statusItem("bad", "ARP shared", "This host would not let Stream Manager stop it answering for camera addresses, so UniFi Protect may see the host MAC and merge cameras."));
      }
      bar.innerHTML = items.join("");
      return status;
    } catch (err) {
      bar.innerHTML = statusItem("bad", "Status unavailable", err.message);
      return null;
    }
  }

  async function showOverview() {
    main.innerHTML = '<p class="loading">Loading overview…</p>';
    const status = await api("/api/status");
    const cameras = (await api("/api/cameras")).cameras;
    renderNav(cameras, { name: "overview" });
    const arpBanner = status && status.arp_isolation === "unavailable"
      ? '<p class="banner warn" role="status">This host would not let Stream Manager stop it answering for camera addresses, so UniFi Protect may see the host MAC and merge cameras. Stream Manager keeps announcing each camera MAC to work around it.</p>'
      : "";
    const library = cameras.length
      ? '<div class="library"><div class="library-head"><span>Name</span><span>Source</span><span>Address</span><span>Video</span><span></span></div>' +
        cameras.map(function (camera) {
          const state = cameraState(camera);
          const runtime = camera.runtime || {};
          const output = outputOf(camera);
          const statusHref = "#/cameras/" + camera.id + "/status";
          return '<div class="library-line">' +
            '<a class="library-name" href="' + statusHref + '"><span class="dot ' + state.kind + '" title="' + esc(state.label) + '"></span><span>' + esc(camera.name) + "</span></a>" +
            '<a class="library-source" href="' + statusHref + '">' + esc(runtime.source_name || "No active source") + "</a>" +
            '<a class="library-address mono" href="' + statusHref + '">' + esc(camera.listen_ip) + "</a>" +
            '<a class="library-video" href="' + statusHref + '">' + output.height + "p · " + output.framerate + " fps</a>" +
            '<a class="row-edit" href="#/cameras/' + camera.id + '">Edit</a></div>';
        }).join("") + "</div>"
      : '<div class="panel empty"><h2>Publish your first camera</h2>' +
        '<p class="help">Pick a Home Assistant camera, RTSP stream, ONVIF device, still image, or video. Stream Manager gives it its own address so UniFi Protect and other ONVIF clients can adopt it.</p>' +
        '<a class="btn" href="#/cameras/new">' + iconPlus + "Add camera</a></div>";

    main.innerHTML = '<header class="page-head"><div class="page-title"><h1>Cameras</h1>' +
      (cameras.length ? '<span class="count">' + cameras.length + "</span>" : "") + "</div>" +
      (cameras.length ? '<a class="btn" href="#/cameras/new">' + iconPlus + "Add camera</a>" : "") + "</header>" +
      arpBanner + library;
  }

  const resolutions = [[1920, 1080], [1280, 720], [640, 480], [640, 360]];
  const h264Profiles = ["Baseline", "Main", "High"];
  const fallbackFixed = [
    ["Profile token", "main"],
    ["Profile fixed", "Yes"],
    ["Encoding", "H264"],
    ["H.264 level", "4.1"],
    ["Pixel format", "yuv420p"],
    ["Audio", "None"],
    ["Transport", "RTP/RTSP/TCP"],
    ["Multicast", "Off"],
    ["Encoder", "libx264, superfast, zerolatency"],
    ["ONVIF quality", "5"],
  ];

  function keyframeSeconds(output) {
    const fps = Number(output.framerate) || 15;
    const frames = Number(output.gov_length) || fps * 2;
    return String(Math.max(1, Math.min(4, Math.round(frames / fps))));
  }

  function outputOf(camera) {
    const output = camera.output || {};
    return {
      width: Number(output.width) || 1920,
      height: Number(output.height) || 1080,
      framerate: Number(output.framerate) || 15,
      bitrate_kbps: Number(output.bitrate_kbps) || 2048,
      h264_profile: output.h264_profile || "Main",
      gov_length: Number(output.gov_length) || 30,
    };
  }

  function encoderFixed(camera) {
    return fixedFacts(camera).filter(function (row) {
      return row[0] !== "Profile token" && row[0] !== "Profile fixed";
    });
  }

  function fixedFacts(camera) {
    const rows = camera.fixed_output;
    if (rows && rows.length) {
      return rows.map(function (item) { return [item.label, item.value]; });
    }
    return fallbackFixed;
  }

  function publishedFacts(camera) {
    const output = outputOf(camera);
    return [
      ["Resolution", output.width + "×" + output.height],
      ["Frame rate", output.framerate + " fps"],
      ["Bitrate", output.bitrate_kbps + " kbps"],
      ["H.264 profile", output.h264_profile],
      ["Keyframe interval", keyframeSeconds(output) + " seconds"],
    ].concat(fixedFacts(camera));
  }

  function factList(rows) {
    return '<dl class="facts">' + rows.map(function (row) {
      return '<div class="fact"><dt>' + esc(row[0]) + "</dt><dd>" + esc(row[1]) + "</dd></div>";
    }).join("") + "</dl>";
  }

  function outputFields(camera) {
    const output = outputOf(camera);
    const selected = output.width + "x" + output.height;
    const sizeOptions = resolutions.map(function (pair) {
      const value = pair[0] + "x" + pair[1];
      return '<option value="' + value + '"' + (value === selected ? " selected" : "") + ">" + pair[0] + "×" + pair[1] + "</option>";
    }).join("");
    const profileOptions = h264Profiles.map(function (profile) {
      return '<option value="' + profile + '"' + (output.h264_profile === profile ? " selected" : "") + ">" + profile + "</option>";
    }).join("");
    return '<div class="fields three">' +
      '<label class="field">Resolution<select name="out_size">' + sizeOptions + "</select></label>" +
      '<label class="field">Frame rate<span class="suffix-field"><input name="out_fps" type="number" min="1" max="30" required value="' + esc(output.framerate) + '"><span class="suffix">fps</span></span></label>' +
      '<label class="field">Bitrate<span class="suffix-field"><input name="out_bitrate" type="number" min="128" max="8192" step="64" required value="' + esc(output.bitrate_kbps) + '"><span class="suffix">kbps</span></span></label>' +
      '<label class="field">H.264 profile<select name="out_profile">' + profileOptions + "</select></label>" +
      '<label class="field">Keyframe every<span class="suffix-field"><input name="out_gop" type="number" min="1" max="4" required value="' + esc(keyframeSeconds(output)) + '"><span class="suffix">sec</span></span></label>' +
      "</div>" +
      '<details class="disclosure"><summary>Fixed encoder settings</summary>' +
      '<p class="help">Every camera publishes two ONVIF profiles, main and sub, on the same stream. These values cannot be changed.</p>' +
      factList(encoderFixed(camera)) + "</details>";
  }

  function closePickers(except) {
    document.querySelectorAll(".picker-menu").forEach(function (menu) {
      if (menu === except) return;
      menu.hidden = true;
      const button = menu.parentNode && menu.parentNode.querySelector(".picker-button");
      if (button) button.setAttribute("aria-expanded", "false");
    });
  }

  function enhanceSelects(root) {
    const chevron = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"></path></svg>';
    root.querySelectorAll("select").forEach(function (select) {
      if (select.closest(".picker")) return;
      const picker = document.createElement("div");
      picker.className = "picker";
      select.parentNode.insertBefore(picker, select);
      picker.appendChild(select);
      select.classList.add("picker-native");
      select.tabIndex = -1;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "picker-button";
      button.setAttribute("aria-haspopup", "listbox");
      button.setAttribute("aria-expanded", "false");
      const menu = document.createElement("ul");
      menu.className = "picker-menu";
      menu.hidden = true;
      menu.setAttribute("role", "listbox");
      function render() {
        const current = select.selectedOptions[0];
        button.innerHTML = "<span>" + esc(current ? current.textContent : "") + "</span>" + chevron;
        menu.innerHTML = "";
        Array.prototype.forEach.call(select.options, function (option, index) {
          const item = document.createElement("li");
          item.setAttribute("role", "option");
          item.tabIndex = -1;
          item.textContent = option.textContent;
          if (option.selected) item.setAttribute("aria-selected", "true");
          item.addEventListener("click", function () {
            select.selectedIndex = index;
            select.dispatchEvent(new Event("change", { bubbles: true }));
            closePickers();
            button.focus();
          });
          menu.appendChild(item);
        });
      }
      button.addEventListener("click", function () {
        const willOpen = menu.hidden;
        closePickers();
        if (!willOpen) return;
        menu.hidden = false;
        button.setAttribute("aria-expanded", "true");
        const selected = menu.querySelector("[aria-selected='true']") || menu.firstElementChild;
        if (selected) selected.focus();
      });
      select.addEventListener("change", render);
      render();
      picker.appendChild(button);
      picker.appendChild(menu);
    });
  }

  function blankSource() {
    return { id: rid(), type: "rtsp", name: "Source", url: "", entity_id: "", host: "", port: 80, username: "", password: "", image_path: "", image_url: "" };
  }

  function blankCamera() {
    const source = blankSource();
    source.name = "Primary";
    return {
      name: "",
      enabled: true,
      interface: "",
      listen_ip: "127.0.0.1",
      virtual_ip: false,
      service_port: 0,
      prefix_length: 24,
      requested_ip: "",
      source_interface: "",
      source_ip: "",
      username: "",
      password: "",
      rules_enabled: false,
      cycle_enabled: false,
      cycle_seconds: 30,
      fallback_source_id: source.id,
      sources: [source],
      rules: [],
      output: outputOf({}),
    };
  }

  function readForm(form, camera) {
    camera.name = form.name.value;
    camera.enabled = form.enabled.checked;
    camera.listen_ip = form.listen_ip.value;
    camera.requested_ip = form.requested_ip.value.trim();
    const option = form.iface.selectedOptions[0];
    camera.interface = option ? option.getAttribute("data-name") || "" : camera.interface;
    const sourceOption = form.source_iface.selectedOptions[0];
    camera.source_ip = sourceOption ? sourceOption.value : "";
    camera.source_interface = sourceOption ? sourceOption.getAttribute("data-name") || "" : "";
    camera.username = form.username.value;
    camera.password = form.password.value;
    camera.rules_enabled = form.rules_enabled.checked;
    camera.cycle_enabled = form.cycle_enabled.checked;
    camera.cycle_seconds = Math.min(3600, Math.max(5, Number(form.cycle_seconds.value) || 30));
    camera.sources.forEach(function (source, index) {
      const take = function (name, apply) {
        const node = form.querySelector('[name="' + name + '"][data-i="' + index + '"]');
        if (node) apply(node.value);
      };
      take("sname", function (value) { source.name = value; });
      take("stype", function (value) { source.type = value; });
      take("surl", function (value) { source.url = value; });
      take("sentity", function (value) { source.entity_id = value; });
      take("shost", function (value) { source.host = value; });
      take("sport", function (value) { source.port = Number(value || 80); });
      take("suser", function (value) { source.username = value; });
      take("spass", function (value) { source.password = value; });
      take("spath", function (value) { source.image_path = value; });
      take("simg", function (value) { source.image_url = value; });
    });
    camera.rules.forEach(function (rule, index) {
      const take = function (name, apply) {
        const node = form.querySelector('[name="' + name + '"][data-i="' + index + '"]');
        if (node) apply(node.value);
      };
      take("rentity", function (value) { rule.entity_id = value; });
      take("rop", function (value) { rule.operator = value; });
      take("rvalue", function (value) { rule.value = value; });
      take("rsource", function (value) { rule.source_id = value; });
    });
    if (form.fallback) camera.fallback_source_id = form.fallback.value;
    const size = String(form.out_size.value || "1920x1080").split("x");
    camera.output = {
      width: Number(size[0]),
      height: Number(size[1]),
      framerate: Number(form.out_fps.value),
      bitrate_kbps: Number(form.out_bitrate.value),
      h264_profile: form.out_profile.value,
      gov_length: (Number(form.out_gop.value) || 2) * (Number(form.out_fps.value) || 15),
    };
  }

  function sourceBlock(source, index, list) {
    const types = ["rtsp", "ha_camera", "onvif", "image", "media"].map(function (type) {
      return '<option value="' + type + '"' + (source.type === type ? " selected" : "") + ">" + sourceLabels[type] + "</option>";
    }).join("");
    let extra = "";
    if (source.type === "rtsp") {
      extra = '<label class="field">RTSP URL<input name="surl" data-i="' + index + '" value="' + esc(source.url) + '" placeholder="rtsp://USER:PASSWORD@CAMERA_HOST:554/stream"></label>';
    } else if (source.type === "ha_camera") {
      extra = '<label class="field">Camera entity<input name="sentity" data-i="' + index + '" list="camera-entities" value="' + esc(source.entity_id) + '" placeholder="camera.example"></label>';
    } else if (source.type === "onvif") {
      extra = '<div class="fields two"><label class="field">Host<input name="shost" data-i="' + index + '" value="' + esc(source.host) + '" placeholder="CAMERA_HOST"></label>' +
        '<label class="field">Port<input name="sport" data-i="' + index + '" type="number" min="1" max="65535" value="' + esc(source.port || 80) + '"></label>' +
        '<label class="field">Username<input name="suser" data-i="' + index + '" value="' + esc(source.username) + '" autocomplete="off"></label>' +
        '<label class="field">Password<input name="spass" data-i="' + index + '" type="password" value="' + esc(source.password) + '" autocomplete="off"></label></div>';
    } else if (source.type === "media") {
      extra = '<label class="field">File<input name="spath" data-i="' + index + '" value="' + esc(source.image_path) + '" placeholder="images/clip.mp4" readonly></label>' +
        '<label class="field">Upload video or GIF<input name="supload" data-i="' + index + '" type="file" accept="image/gif,video/mp4,video/webm,video/quicktime,.mkv,.m4v"></label>' +
        '<p class="help">MP4, WebM, MOV, MKV, or GIF, up to 64 MB. The file loops.</p>';
    } else {
      extra = '<label class="field">Image file in the data directory<input name="spath" data-i="' + index + '" value="' + esc(source.image_path) + '" placeholder="images/standby.jpg"></label>' +
        '<label class="field">Or image URL<input name="simg" data-i="' + index + '" value="' + esc(source.image_url) + '" placeholder="https://example.com/standby.jpg"></label>' +
        '<label class="field">Upload still<input name="supload" data-i="' + index + '" type="file" accept="image/jpeg,image/png,image/webp"></label>';
    }
    const label = esc(source.name || "Source");
    return '<article class="subcard"><div class="subcard-head"><span class="badge">' + (index + 1) + "</span><h3>" + label + "</h3>" +
      '<span class="type-tag">' + esc(sourceLabels[source.type] || source.type) + "</span>" +
      '<div class="tools">' +
      '<button type="button" class="tool" data-act="up" data-i="' + index + '" aria-label="Move ' + label + ' up"' + (index === 0 ? " disabled" : "") + ">" + iconUp + "</button>" +
      '<button type="button" class="tool" data-act="down" data-i="' + index + '" aria-label="Move ' + label + ' down"' + (list && index === list.length - 1 ? " disabled" : "") + ">" + iconDown + "</button>" +
      '<button type="button" class="tool danger" data-act="remove-source" data-i="' + index + '" aria-label="Remove ' + label + '">' + iconTrash + "</button></div></div>" +
      '<div class="fields two"><label class="field">Name<input name="sname" data-i="' + index + '" value="' + esc(source.name) + '"></label>' +
      '<label class="field">Type<select name="stype" data-i="' + index + '">' + types + "</select></label></div>" +
      '<div class="fields">' + extra + "</div></article>";
  }

  function ruleBlock(rule, index, sources) {
    const options = sources.map(function (source) {
      return '<option value="' + esc(source.id) + '"' + (rule.source_id === source.id ? " selected" : "") + ">" + esc(source.name || source.id) + "</option>";
    }).join("");
    return '<div class="rule"><span class="rule-word">When</span>' +
      '<input name="rentity" data-i="' + index + '" list="all-entities" aria-label="Entity" value="' + esc(rule.entity_id) + '" placeholder="switch.example">' +
      '<select name="rop" data-i="' + index + '" aria-label="Condition">' + operatorLabels.map(function (pair) {
        return '<option value="' + pair[0] + '"' + (rule.operator === pair[0] ? " selected" : "") + ">" + pair[1] + "</option>";
      }).join("") + "</select>" +
      '<input name="rvalue" data-i="' + index + '" aria-label="Value" value="' + esc(rule.value) + '" placeholder="on">' +
      '<span class="rule-word">show</span>' +
      '<select name="rsource" data-i="' + index + '" aria-label="Source">' + options + "</select>" +
      '<div class="tools"><button type="button" class="tool" data-act="rule-up" data-i="' + index + '" aria-label="Move rule up">' + iconUp + "</button>" +
      '<button type="button" class="tool" data-act="rule-down" data-i="' + index + '" aria-label="Move rule down">' + iconDown + "</button>" +
      '<button type="button" class="tool danger" data-act="remove-rule" data-i="' + index + '" aria-label="Remove rule">' + iconTrash + "</button></div></div>";
  }

  const editorModules = [
    ["sec-basics", "Camera"],
    ["sec-sources", "Sources"],
    ["sec-network", "Network"],
    ["sec-video", "Video"],
    ["sec-automation", "Switching"],
  ];

  function formSection(id, title, lead, body) {
    return '<section class="panel module-panel" id="' + id + '" aria-labelledby="' + id + '-title">' +
      '<header class="section-head"><div><h2 id="' + id + '-title">' + esc(title) +
      '</h2><p class="lead">' + esc(lead) + "</p></div></header>" +
      '<div class="section-body">' + body + "</div></section>";
  }

  function moduleNav(active) {
    return '<nav class="modules" aria-label="Sections">' + editorModules.map(function (item) {
      return '<button type="button" class="module-btn" data-module="' + item[0] + '"' +
        (item[0] === active ? ' aria-current="true"' : "") + ">" + esc(item[1]) + "</button>";
    }).join("") + "</nav>";
  }

  function inspectorBody(camera, id) {
    const urls = camera.urls || {};
    const address = camera.virtual_ip && camera.listen_ip ? camera.listen_ip : "";
    const rows = [];
    if (urls.onvif) rows.push(["ONVIF", urls.onvif]);
    if (urls.rtsp) rows.push(["RTSP", urls.rtsp]);
    const login = camera.username ? "Use " + camera.username + " and the password above." : "Any username and password work.";
    const steps = [
      [!!id, "Save the camera", id ? "Published at " + (address || camera.listen_ip) + "." : "Stream Manager reserves an address on your network."],
      [false, "Turn on discovery in Protect", "Settings › System › Discover Third-Party Cameras."],
      [false, "Click to Adopt", login],
    ];
    return '<section class="inspector-block"><p class="eyebrow">Published as</p>' +
      '<p class="summary-address">' + esc(address || (id ? camera.listen_ip : "Assigned on save")) + "</p>" +
      (camera.virtual_ip && camera.mac ? '<p class="help mono">MAC ' + esc(camera.mac) + (camera.link ? "" : " · shared with host") + "</p>" : "") +
      (rows.length ? copyList(rows) : "") + "</section>" +
      '<section class="inspector-block"><p class="eyebrow">Add to UniFi Protect</p><ol class="checklist">' +
      steps.map(function (step) {
        return '<li class="' + (step[0] ? "done" : "") + '"><span class="tick" aria-hidden="true">' + (step[0] ? iconCheck : "") + "</span><div><strong>" +
          esc(step[1]) + "</strong><span>" + esc(step[2]) + "</span></div></li>";
      }).join("") + "</ol></section>";
  }

  function assignedNote(loopback, address) {
    if (loopback) return "Loopback publishes only on this machine.";
    if (address) return "Assigned address " + address + ". ONVIF clients use http://" + address + "/onvif/device_service.";
    return "A free address on this network is reserved when you save. ONVIF clients use http://that-address/onvif/device_service.";
  }

  function wirelessNote(item) {
    return item && item.wireless
      ? "This is a Wi-Fi adapter. Wi-Fi cannot carry extra MAC addresses, so every camera on it shares the host MAC and UniFi Protect will merge them into one device. Use a wired network for more than one camera."
      : "";
  }

  async function showEditor(id) {
    main.innerHTML = '<p class="loading">Loading camera…</p>';
    const interfaces = (await api("/api/interfaces")).interfaces;
    const entityPayload = await api("/api/entities");
    const cameras = (await api("/api/cameras")).cameras;
    const camera = id ? await api("/api/cameras/" + id) : blankCamera();
    renderNav(cameras, { name: "edit", id: id });
    let activeModule = "sec-basics";

    function showModule(moduleId) {
      activeModule = moduleId;
      document.querySelectorAll(".module-panel").forEach(function (panel) {
        panel.hidden = panel.id !== moduleId;
      });
      document.querySelectorAll("[data-module]").forEach(function (button) {
        if (button.getAttribute("data-module") === moduleId) button.setAttribute("aria-current", "true");
        else button.removeAttribute("aria-current");
      });
    }

    function paint() {
      const known = interfaces.slice();
      if (camera.listen_ip && camera.listen_ip !== "0.0.0.0" && !camera.virtual_ip && !known.some(function (item) { return item.ip === camera.listen_ip; })) {
        known.unshift({ name: camera.interface || "saved", ip: camera.listen_ip });
      }
      const broadcast = (camera.interface && known.find(function (item) { return item.name === camera.interface; }))
        || (!id && known.find(function (item) { return item.ip !== "127.0.0.1"; }))
        || known.find(function (item) { return item.ip === camera.listen_ip; })
        || known[0];
      const loopbackBroadcast = !broadcast || broadcast.ip === "127.0.0.1";
      const keepAddress = !!(camera.virtual_ip && broadcast && camera.interface === broadcast.name && camera.listen_ip);
      const broadcastValue = loopbackBroadcast ? "127.0.0.1" : (keepAddress ? camera.listen_ip : "");
      const assignedText = assignedNote(loopbackBroadcast, keepAddress ? camera.listen_ip : "");
      const sourceKnown = interfaces.slice();
      if (camera.source_ip && !sourceKnown.some(function (item) { return item.ip === camera.source_ip; })) {
        sourceKnown.unshift({ name: camera.source_interface || "saved", ip: camera.source_ip });
      }
      function ifaceOption(item, selected) {
        return '<option value="' + esc(item.ip) + '" data-name="' + esc(item.name) + '"' + (selected ? " selected" : "") + ">" +
          esc(item.name + " (" + item.ip + ")") + "</option>";
      }
      const ifaceOptions = known.map(function (item) {
        return ifaceOption(item, broadcast && item.name === broadcast.name && item.ip === broadcast.ip);
      }).join("");
      const sourceOptions = '<option value="" data-name=""' + (camera.source_ip ? "" : " selected") + ">Follow the host route</option>" +
        sourceKnown.map(function (item) {
          return ifaceOption(item, camera.source_ip === item.ip);
        }).join("");
      const fallback = camera.sources.map(function (source) {
        return '<option value="' + esc(source.id) + '"' + (camera.fallback_source_id === source.id ? " selected" : "") + ">" + esc(source.name || source.id) + "</option>";
      }).join("");
      const cameraList = (entityPayload.entities || []).filter(function (item) { return item.domain === "camera"; })
        .map(function (item) { return '<option value="' + esc(item.entity_id) + '"></option>'; }).join("");
      const allList = (entityPayload.entities || []).map(function (item) {
        return '<option value="' + esc(item.entity_id) + '"></option>';
      }).join("");
      const state = cameraState(camera);
      const head = '<header class="page-head">' +
        '<div class="page-title"><a class="crumb" href="#/">Cameras</a>' +
        "<h1>" + esc(id ? camera.name || "Camera" : "Add camera") + "</h1>" +
        (id ? '<span class="pill ' + state.kind + '"><span class="dot ' + state.kind + '"></span>' + esc(state.label) + "</span>" : "") +
        "</div>" +
        (id ? '<div class="row"><a class="btn-secondary" href="#/cameras/' + id + '/status">Live status</a></div>' : "") +
        "</header>";

      const basics = formSection("sec-basics", "Camera", "The name UniFi Protect and other ONVIF clients show for this device.",
        '<div class="fields"><label class="field">Name<input name="name" required maxlength="64" value="' + esc(camera.name) + '" placeholder="Front printer"></label>' +
        '<label class="switch"><input type="checkbox" name="enabled"' + (camera.enabled ? " checked" : "") + '><span>Publish this camera</span></label></div>');

      const sources = formSection("sec-sources", "Picture sources", "Where the video comes from. The first source is used unless automation picks another.",
        '<div class="stack">' + camera.sources.map(sourceBlock).join("") + "</div>" +
        '<button type="button" class="btn-ghost add" id="add-source">' + iconPlus + "Add source</button>");

      const network = formSection("sec-network", "Publish on your network", "The camera gets its own address on this network, so it shows up as a separate device.",
        '<div class="fields">' +
        '<label class="field">Network<select name="iface">' + ifaceOptions + "</select></label>" +
        '<input type="hidden" name="listen_ip" value="' + esc(broadcastValue) + '">' +
        '<p class="help" data-assigned>' + esc(assignedText) + "</p>" +
        '<div class="banner warn" data-wireless role="note"' + (wirelessNote(broadcast) ? "" : " hidden") + ">" + esc(wirelessNote(broadcast)) + "</div>" +
        '<label class="field" data-requested' + (loopbackBroadcast ? " hidden" : "") + '><span>Camera address <span class="optional">Optional</span></span>' +
        '<input name="requested_ip" inputmode="decimal" autocomplete="off" value="' + esc(camera.requested_ip || "") + '" placeholder="Pick automatically"></label>' +
        '<p class="help" data-requested' + (loopbackBroadcast ? " hidden" : "") + ">Set this to match a fixed IP you reserved in your router.</p>" +
        "</div>" +
        '<h3 class="subhead">Login</h3>' +
        '<div class="fields two"><label class="field">Username<input name="username" value="' + esc(camera.username || "") + '" autocomplete="off" placeholder="Any"></label>' +
        '<label class="field">Password<input name="password" type="password" value="' + esc(camera.password || "") + '" autocomplete="new-password" placeholder="Any"></label></div>' +
        '<p class="help">UniFi Protect asks for these when you adopt the camera. Leave both empty to accept any login.</p>' +
        '<details class="disclosure"' + (camera.source_ip ? " open" : "") + "><summary>Reach sources from a different network</summary>" +
        '<div class="fields"><label class="field">Fetch sources from<select name="source_iface">' + sourceOptions + "</select></label>" +
        '<p class="help">Only needed when the source camera sits on another network than the one above. Home Assistant camera entities are always fetched through Home Assistant.</p></div></details>');

      const video = formSection("sec-video", "Video output", "What ONVIF clients receive. Protect works best with H.264 Main, 2 second keyframes and 2,000–4,000 kbps.",
        outputFields(camera));

      const automation = formSection("sec-automation", "Automatic switching", "Rotate the sources on a timer, or switch when a Home Assistant entity changes.",
        '<label class="switch"><input type="checkbox" name="cycle_enabled"' + (camera.cycle_enabled ? " checked" : "") + '><span>Cycle through the sources in order</span></label>' +
        '<div class="fields inset"><label class="field">Show each source for<span class="suffix-field"><input name="cycle_seconds" type="number" min="5" max="3600" required value="' + esc(camera.cycle_seconds || 30) + '"><span class="suffix">sec</span></span></label></div>' +
        '<p class="help">The picture changes at the next keyframe, so the switch stays short. A matching rule below still takes over.</p>' +
        '<label class="switch"><input type="checkbox" name="rules_enabled"' + (camera.rules_enabled ? " checked" : "") + '><span>Switch sources from entity state</span></label>' +
        '<div class="fields inset"><label class="field">When no rule matches, show<select name="fallback">' + fallback + "</select></label></div>" +
        '<div class="rules">' +
        (camera.rules.length ? camera.rules.map(function (rule, index) { return ruleBlock(rule, index, camera.sources); }).join("") : '<p class="help empty-line">No rules yet.</p>') +
        "</div>" +
        '<button type="button" class="btn-ghost add" id="add-rule">' + iconPlus + "Add rule</button>");

      main.innerHTML =
        '<form id="camera-form" class="workspace-frame">' +
        moduleNav(activeModule) +
        '<div class="canvas">' + head +
        (entityPayload.connected ? "" : '<div class="banner info">Home Assistant entities are unavailable. RTSP, ONVIF and image sources still work. ' + esc(entityPayload.error || "") + "</div>") +
        '<datalist id="camera-entities">' + cameraList + "</datalist>" +
        '<datalist id="all-entities">' + allList + "</datalist>" +
        basics + sources + network + video + automation +
        "</div>" +
        '<aside class="inspector" aria-label="Properties">' +
        inspectorBody(camera, id) +
        '<div class="inspector-actions"><p id="form-error" class="banner error" role="alert" hidden></p>' +
        '<button class="btn" type="submit">' + (id ? "Save changes" : "Create camera") + "</button>" +
        '<div class="row"><a class="btn-ghost" href="' + (id ? "#/cameras/" + id + "/status" : "#/") + '">Cancel</a>' +
        (id ? '<button type="button" class="btn-ghost danger push" id="delete-camera">Delete</button>' : "") +
        "</div></div></aside></form>";

      const form = document.getElementById("camera-form");
      const assignedInterface = camera.virtual_ip ? camera.interface : "";
      const assignedListen = camera.virtual_ip ? camera.listen_ip : "";
      form.iface.addEventListener("change", function () {
        const selected = form.iface.selectedOptions[0];
        const name = selected.getAttribute("data-name") || "";
        const note = form.querySelector("[data-assigned]");
        const loopback = selected.value === "127.0.0.1";
        if (loopback) {
          form.listen_ip.value = "127.0.0.1";
          camera.virtual_ip = false;
          camera.listen_ip = "127.0.0.1";
          note.textContent = assignedNote(true, "");
        } else if (assignedInterface === name && assignedListen) {
          form.listen_ip.value = assignedListen;
          camera.virtual_ip = true;
          camera.listen_ip = assignedListen;
          note.textContent = assignedNote(false, assignedListen);
        } else {
          form.listen_ip.value = "";
          camera.virtual_ip = false;
          camera.listen_ip = "";
          note.textContent = assignedNote(false, "");
        }
        const wireless = form.querySelector("[data-wireless]");
        const message = wirelessNote(interfaces.find(function (item) { return item.name === name && item.ip === selected.value; }));
        wireless.textContent = message;
        wireless.hidden = !message;
        form.querySelectorAll("[data-requested]").forEach(function (node) { node.hidden = loopback; });
        if (loopback) form.requested_ip.value = "";
        camera.interface = name;
      });
      form.source_iface.addEventListener("change", function () {
        const selected = form.source_iface.selectedOptions[0];
        camera.source_ip = selected.value;
        camera.source_interface = selected.getAttribute("data-name") || "";
      });
      form.addEventListener("change", function (event) {
        if (event.target.name !== "stype") return;
        readForm(form, camera);
        paint();
      });
      form.addEventListener("click", function (event) {
        const moduleBtn = event.target.closest("[data-module]");
        if (moduleBtn) {
          event.preventDefault();
          showModule(moduleBtn.getAttribute("data-module"));
          return;
        }
        const button = event.target.closest("button");
        if (!button) return;
        const act = button.getAttribute("data-act");
        if (!act && button.id !== "add-source" && button.id !== "add-rule") return;
        if (button.type === "submit") return;
        event.preventDefault();
        readForm(form, camera);
        const index = Number(button.getAttribute("data-i"));
        if (button.id === "add-source") camera.sources.push(blankSource());
        else if (button.id === "add-rule") {
          camera.rules.push({
            id: rid(),
            entity_id: "",
            operator: "eq",
            value: "",
            source_id: camera.sources[0] ? camera.sources[0].id : "",
          });
        } else if (act === "remove-source") {
          camera.sources.splice(index, 1);
          if (!camera.sources.some(function (source) { return source.id === camera.fallback_source_id; })) {
            camera.fallback_source_id = camera.sources[0] ? camera.sources[0].id : "";
          }
        } else if (act === "up" && index > 0) {
          camera.sources.splice(index - 1, 0, camera.sources.splice(index, 1)[0]);
        } else if (act === "down" && index < camera.sources.length - 1) {
          camera.sources.splice(index + 1, 0, camera.sources.splice(index, 1)[0]);
        } else if (act === "remove-rule") camera.rules.splice(index, 1);
        else if (act === "rule-up" && index > 0) camera.rules.splice(index - 1, 0, camera.rules.splice(index, 1)[0]);
        else if (act === "rule-down" && index < camera.rules.length - 1) camera.rules.splice(index + 1, 0, camera.rules.splice(index, 1)[0]);
        else return;
        paint();
      });
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        document.querySelectorAll(".module-panel").forEach(function (panel) { panel.hidden = false; });
        const invalid = form.querySelector(":invalid");
        if (invalid) {
          const panel = invalid.closest(".module-panel");
          showModule(panel ? panel.id : activeModule);
          invalid.reportValidity();
          return;
        }
        showModule(activeModule);
        const error = document.getElementById("form-error");
        error.hidden = true;
        readForm(form, camera);
        const uploads = form.querySelectorAll('input[name="supload"]');
        for (const input of uploads) {
          if (!input.files || !input.files[0]) continue;
          const body = new FormData();
          body.append("file", input.files[0]);
          const response = await fetch(rooted("/api/images"), { method: "POST", body: body });
          const payload = await response.json();
          if (!response.ok) {
            error.hidden = false;
            error.textContent = payload.error || "upload failed";
            return;
          }
          camera.sources[Number(input.getAttribute("data-i"))].image_path = payload.image_path;
        }
        try {
          const saved = id
            ? await api("/api/cameras/" + id, { method: "PUT", body: JSON.stringify(camera) })
            : await api("/api/cameras", { method: "POST", body: JSON.stringify(camera) });
          location.hash = "#/cameras/" + saved.id + "/status";
        } catch (err) {
          error.hidden = false;
          error.textContent = err.message;
        }
      });
      const del = document.getElementById("delete-camera");
      if (del) {
        del.addEventListener("click", async function () {
          if (!confirm("Delete this virtual camera?")) return;
          await api("/api/cameras/" + id, { method: "DELETE" });
          location.hash = "#/";
        });
      }
      enhanceSelects(form);
      showModule(activeModule);
    }

    paint();
  }

  function statusSentence(camera) {
    const runtime = camera.runtime || {};
    if (!camera.enabled || runtime.reason === "disabled") return "This camera is disabled.";
    if (runtime.reason === "no_source" || !runtime.source_name) return "No source is available.";
    if (runtime.source_kind === "image") return "Using the still image.";
    if (runtime.reason === "cycle") return "Cycling sources. Showing " + runtime.source_name + ".";
    if (runtime.source_kind === "file") return "Playing " + runtime.source_name + ".";
    return "Showing " + runtime.source_name + ".";
  }

  function statusDetail(camera) {
    const runtime = camera.runtime || {};
    if (runtime.reason === "rule" && runtime.rule_id) {
      const rule = (camera.rules || []).find(function (item) { return item.id === runtime.rule_id; });
      if (rule) {
        const operator = (operatorLabels.find(function (pair) { return pair[0] === rule.operator; }) || ["", rule.operator])[1];
        return "When " + rule.entity_id + " " + operator + " " + rule.value;
      }
      return "Matched a rule";
    }
    if (runtime.reason === "cycle") return "Each source plays for " + (camera.cycle_seconds || 30) + " seconds";
    if (runtime.reason === "fallback") return "Fallback source";
    if (runtime.reason === "disabled") return "Disabled";
    if (runtime.reason === "no_source") return "No source";
    return runtime.reason || "Unknown";
  }

  async function showStatus(id) {
    main.innerHTML = '<p class="loading">Loading status…</p>';
    const cameras = (await api("/api/cameras")).cameras;
    const camera = await api("/api/cameras/" + id);
    renderNav(cameras, { name: "status", id: id });
    const runtime = camera.runtime || {};
    const state = cameraState(camera);
    const urls = camera.urls || {};
    const network = [
      ["Address", camera.listen_ip],
      ["Network", camera.interface || "Loopback"],
    ];
    if (camera.virtual_ip && camera.mac) network.push(["MAC", camera.mac + (camera.link ? " · own virtual adapter" : " · shared with host")]);
    network.push(["Sources fetched from", camera.source_ip ? (camera.source_interface ? camera.source_interface + " (" + camera.source_ip + ")" : camera.source_ip) : "Host route"]);
    main.innerHTML =
      '<div class="workspace-frame two">' +
      '<div class="canvas">' +
      '<header class="page-head"><div class="page-title"><a class="crumb" href="#/">Cameras</a><h1>' + esc(camera.name) + "</h1>" +
      '<span class="pill ' + state.kind + '"><span class="dot ' + state.kind + '"></span>' + esc(state.label) + "</span></div></header>" +
      '<section class="panel status-hero"><span class="dot ' + state.kind + '"></span><div>' +
      '<p class="hero-title">' + esc(statusSentence(camera)) + "</p>" +
      '<p class="help">' + esc(statusDetail(camera)) + "</p></div></section>" +
      (runtime.error ? '<p class="banner error" role="alert">' + esc(runtime.error) + "</p>" : "") +
      (runtime.engine_error ? '<p class="banner error" role="alert">' + esc(runtime.engine_error) + "</p>" : "") +
      '<section class="panel"><h2>Video output</h2>' + factList(publishedFacts(camera).slice(0, 5).concat([["ONVIF profiles", "main, sub (Profile S)"]])) +
      '<details class="disclosure"><summary>Fixed encoder settings</summary>' + factList(encoderFixed(camera)) + "</details></section>" +
      '<section class="panel" aria-labelledby="check-title"><div class="card-head"><div><h2 id="check-title">Stream check</h2>' +
      '<p class="help">Opens the ONVIF stream UniFi Protect plays, then opens the camera\'s RTSP or RTSPS source, and reads video frames from both.</p></div>' +
      '<button type="button" class="btn-secondary" id="check-stream">Test stream</button></div>' +
      '<div id="probe-results"><p class="help">Not tested yet.</p></div>' +
      '<div id="stream-facts"></div></section>' +
      "</div>" +
      '<aside class="inspector" aria-label="Properties">' +
      '<section class="inspector-block"><p class="eyebrow">Connect a client</p>' +
      '<p class="help">These addresses stay the same when the picture source changes.</p>' +
      copyList([["ONVIF", runtime.onvif_url || urls.onvif], ["RTSP", runtime.rtsp_url || urls.rtsp]]) + "</section>" +
      '<section class="inspector-block"><p class="eyebrow">Network</p>' + factList(network) + "</section>" +
      '<div class="inspector-actions"><a class="btn" href="#/cameras/' + id + '">Edit camera</a>' +
      '<button type="button" class="btn-ghost" id="refresh">Refresh</button></div></aside></div>';
    document.getElementById("refresh").addEventListener("click", function () { showStatus(id); });
    document.getElementById("check-stream").addEventListener("click", function () { checkStream(camera); });
    loadStreamFacts(camera);
  }

  async function loadStreamFacts(camera) {
    const target = document.getElementById("stream-facts");
    if (!target) return;
    let info;
    try {
      info = await api("/api/cameras/" + camera.id + "/stream");
    } catch (err) {
      target.innerHTML = '<p class="banner error" role="alert">' + esc(err.message) + "</p>";
      return;
    }
    const producer = (info.producers || [])[0];
    const consumers = info.consumers || [];
    let source = "Idle until a client connects";
    if (producer && producer.bytes) {
      source = "Receiving " + ((producer.codecs || []).join(", ").toUpperCase() || "video") + " · " + formatBytes(producer.bytes);
    } else if (consumers.length) {
      source = "Connecting to the source…";
    }
    const rows = [
      ["Source", source],
      ["Watching", consumers.length ? consumers.map(function (item) { return (item.remote || "local") + (item.agent ? " (" + item.agent + ")" : ""); }).join(", ") : "No clients"],
    ];
    target.innerHTML = factList(rows) +
      ((info.log || []).length
        ? '<details class="disclosure"><summary>Recent engine messages</summary><pre class="log">' + esc(unescapeLog(info.log).join("\n")) + "</pre></details>"
        : "");
  }

  function unescapeLog(lines) {
    return lines.map(function (line) {
      return line.split("\\r\\n").join("\n").split("\\n").join("\n").split('\\"').join('"');
    });
  }

  function formatBytes(value) {
    if (value > 1048576) return (value / 1048576).toFixed(1) + " MB";
    if (value > 1024) return Math.round(value / 1024) + " KB";
    return value + " B";
  }

  function probeSummary(probe) {
    if (!probe) return "Not tested";
    if (!probe.ok) return probe.error || "No video arrived";
    const size = probe.width ? probe.width + "×" + probe.height + " · " : "";
    const slices = probe.slices > 1 ? probe.slices + " slices per frame" : "one slice per frame";
    return (probe.codec || "Video") + " " + size + probe.frames + " frames · " + slices;
  }

  async function checkStream(camera) {
    const results = document.getElementById("probe-results");
    const button = document.getElementById("check-stream");
    button.disabled = true;
    button.textContent = "Testing…";
    results.innerHTML = '<p class="help">Opening both streams. This can take up to 20 seconds.</p>';
    try {
      const info = await api("/api/cameras/" + camera.id + "/probe", { method: "POST" });
      const slices = info.onvif && info.onvif.slices > 1;
      results.innerHTML =
        (slices ? '<p class="banner warn" role="status">The published stream splits each frame into ' + info.onvif.slices + " slices. UniFi Protect shows that as a smeared picture.</p>" : "") +
        factList([["ONVIF stream", probeSummary(info.onvif)], ["Source stream", probeSummary(info.source)]]) +
        (info.onvif && info.onvif.url ? '<p class="help mono">' + esc(info.onvif.url) + "</p>" : "") +
        (info.source && info.source.url ? '<p class="help mono">' + esc(info.source.url) + "</p>" : "");
    } catch (err) {
      results.innerHTML = '<p class="banner error" role="alert">' + esc(err.message) + "</p>";
    }
    button.disabled = false;
    button.textContent = "Test again";
    loadStreamFacts(camera);
  }

  async function render() {
    const current = route();
    refreshStatusBar();
    try {
      if (current.name === "edit") await showEditor(current.id);
      else if (current.name === "status") await showStatus(current.id);
      else await showOverview();
    } catch (err) {
      main.innerHTML = '<div class="banner error" role="alert">' + esc(err.message) + "</div>";
    }
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest(".picker")) closePickers();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closePickers();
  });
  adoptParentTheme();
  syncColorScheme();
  window.addEventListener("hashchange", render);
  render();
})();
