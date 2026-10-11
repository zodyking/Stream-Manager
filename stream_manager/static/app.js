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

  const sourceLabels = { rtsp: "RTSP", ha_camera: "Home Assistant camera", onvif: "ONVIF device", image: "Still image" };
  const operatorLabels = [["eq", "equals"], ["ne", "does not equal"], ["gt", "number above"], ["lt", "number below"]];

  function adoptParentTheme() {
    syncColorScheme();
  }

  function syncColorScheme() {
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--primary-background-color").trim();
    const match = raw.match(/(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/) || raw.match(/^#([0-9a-f]{6})$/i);
    let dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (match) {
      const channels = match[2] !== undefined && raw.indexOf("#") !== 0
        ? [match[1], match[2], match[3]]
        : [parseInt(match[1].slice(0, 2), 16), parseInt(match[1].slice(2, 4), 16), parseInt(match[1].slice(4, 6), 16)];
      const luminance = (Number(channels[0]) * 299 + Number(channels[1]) * 587 + Number(channels[2]) * 114) / 1000;
      dark = luminance < 140;
    }
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
    return { kind: "live", label: "Live" };
  }

  function renderNav(cameras, current) {
    const overviewOn = current.name === "overview";
    let html = '<div class="nav-brand">' +
      '<img class="brand-lockup brand-on-dark" src="' + rooted("/static/brand/stream-manager-horizontal-dark.svg") + '" alt="Stream Manager">' +
      '<img class="brand-lockup brand-on-light" src="' + rooted("/static/brand/stream-manager-horizontal-light.svg") + '" alt="">' +
      "</div>";
    html += '<a class="nav-link" href="#/"' + (overviewOn ? ' aria-current="page"' : "") + '><span class="nav-icon">' + iconOverview + '</span><span class="label">Overview</span></a>';
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

  function pageHead(title, action) {
    return '<div class="page-head"><h1>' + esc(title) + "</h1>" + (action || "") + "</div>";
  }

  function copyRow(label, value) {
    if (!value) return "";
    return '<div class="copy-row"><span class="copy-k">' + esc(label) + "</span><code>" + esc(value) +
      '</code><button type="button" class="btn-quiet copy-btn" data-copy="' + esc(value) + '"><span>Copy</span></button></div>';
  }

  function copyList(pairs) {
    const rows = pairs.map(function (pair) { return copyRow(pair[0], pair[1]); }).join("");
    return rows ? '<div class="copy-list">' + rows + "</div>" : "";
  }

  async function showOverview() {
    main.innerHTML = '<p class="loading">Loading overview…</p>';
    const status = await api("/api/status");
    const cameras = (await api("/api/cameras")).cameras;
    renderNav(cameras, { name: "overview" });
    const ha = status.ha;
    const discovery = status.discovery || { bindings: [] };
    const haKind = ha.connected ? "live" : ha.mode === "off" ? "still" : "bad";
    const haLabel = ha.connected ? "Connected" : ha.mode === "off" ? "Not configured" : "Disconnected";
    const haDetail = ha.error || (ha.mode === "supervisor" ? "Using the Supervisor token." : ha.url || "Add a Home Assistant URL and token to use camera entities.");
    const discoveryKind = discovery.ok ? "live" : "still";
    const discoveryLabel = discovery.ok ? "Ready" : "Check bindings";
    const binds = (discovery.bindings || []).map(function (item) {
      return item.ip + (item.error ? " — " + item.error : " — listening");
    });
    const discoveryDetail = binds[0] || discovery.note || "No discovery sockets yet.";
    const engine = status.engine || {};
    const engineKind = engine.running ? "live" : engine.binary ? "bad" : "still";
    const engineLabel = engine.running ? "Running" : engine.binary ? "Not responding" : "Binary not found";
    const engineDetail = engine.error || (engine.running ? "Publishing the active picture on the stable RTSP URL." : "ONVIF addresses are still advertised.");

    function tile(title, kind, label, detail) {
      return '<section class="card"><div class="stat-top"><span class="dot ' + kind + '"></span><div class="stat-copy"><strong>' + esc(title) +
        '</strong><span class="state">' + esc(label) + "</span></div></div><p class=\"hint\">" + esc(detail) + "</p></section>";
    }

    const cards = cameras.length
      ? '<div class="camera-list">' + cameras.map(function (camera) {
          const state = cameraState(camera);
          const runtime = camera.runtime || {};
          const urls = camera.urls || {};
          return '<article class="card"><div class="entity-top"><div class="entity-copy"><div class="entity-name">' + esc(camera.name) +
            '</div><p class="hint">' + esc(runtime.source_name || "No active source") + " · " + esc(outputSummary(camera)) + "</p></div>" +
            '<span class="state"><span class="dot ' + state.kind + '"></span>' + esc(state.label) + "</span></div>" +
            copyList([["RTSP", urls.rtsp], ["ONVIF", urls.onvif]]) +
            '<div class="row"><a class="btn-quiet" href="#/cameras/' + camera.id + '">Edit</a>' +
            '<a class="btn-quiet" href="#/cameras/' + camera.id + '/status">Status</a></div></article>';
        }).join("") + "</div>"
      : '<div class="card empty"><h2>No virtual cameras</h2><p class="hint">Add a camera to publish a stable ONVIF device.</p><a class="btn" href="#/cameras/new">Add camera</a></div>';

    main.innerHTML = pageHead("Overview", '<a class="btn" href="#/cameras/new">Add camera</a>') +
      '<div class="stat-grid">' +
      tile("Home Assistant", haKind, haLabel, haDetail) +
      tile("Discovery", discoveryKind, discoveryLabel, discoveryDetail) +
      tile("Stream engine", engineKind, engineLabel, engineDetail) +
      "</div>" + cards;
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

  function outputSummary(camera) {
    const output = outputOf(camera);
    return "ONVIF profile main · " + output.width + "×" + output.height + " · " + output.h264_profile + " · " + output.framerate + " fps";
  }

  function profileFacts(camera) {
    return [
      ["Profile name", camera.name || "Uses the camera name"],
      ["Token", "main"],
      ["Fixed", "Yes — clients cannot add or remove profiles"],
      ["ONVIF profile", "Profile S"],
    ];
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
    return '<section class="card" id="onvif-profile">' +
      '<p class="eyebrow">ONVIF profile</p>' +
      '<div class="profile-head"><h2>main</h2><span class="state"><span class="dot live"></span>Profile S</span></div>' +
      '<p class="hint">This is the media profile ONVIF clients receive. The token stays main, so the stream URL does not change when the picture source changes.</p>' +
      factList(profileFacts(camera)) +
      '<h3 class="fixed-title">Video encoder</h3>' +
      '<p class="hint">These settings are stored on profile main and encoded into the RTSP stream.</p>' +
      '<div class="fields two">' +
      '<label class="field">Resolution<select name="out_size">' + sizeOptions + "</select></label>" +
      '<label class="field">H.264 codec profile<select name="out_profile">' + profileOptions + "</select></label>" +
      '<label class="field">Frame rate<input name="out_fps" type="number" min="1" max="30" required value="' + esc(output.framerate) + '"></label>' +
      '<label class="field">Bitrate (kbps)<input name="out_bitrate" type="number" min="128" max="8192" required value="' + esc(output.bitrate_kbps) + '"></label>' +
      '<label class="field">Keyframe interval (seconds)<input name="out_gop" type="number" min="1" max="4" required value="' + esc(keyframeSeconds(output)) + '"></label>' +
      '<p class="hint">UniFi Protect adopts H.264 Main at a 2 second keyframe interval, about 2,000–4,000 kbps, over RTSP TCP.</p>' +
      '</div><h3 class="fixed-title">Not editable</h3>' + factList(encoderFixed(camera)) + "</section>";
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
      source_interface: "",
      source_ip: "",
      username: "",
      password: "",
      rules_enabled: false,
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
    const option = form.iface.selectedOptions[0];
    camera.interface = option ? option.getAttribute("data-name") || "" : camera.interface;
    const sourceOption = form.source_iface.selectedOptions[0];
    camera.source_ip = sourceOption ? sourceOption.value : "";
    camera.source_interface = sourceOption ? sourceOption.getAttribute("data-name") || "" : "";
    camera.username = form.username.value;
    camera.password = form.password.value;
    camera.rules_enabled = form.rules_enabled.checked;
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

  function sourceBlock(source, index) {
    const types = ["rtsp", "ha_camera", "onvif", "image"].map(function (type) {
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
    } else {
      extra = '<label class="field">Image file in the data directory<input name="spath" data-i="' + index + '" value="' + esc(source.image_path) + '" placeholder="images/standby.jpg"></label>' +
        '<label class="field">Or image URL<input name="simg" data-i="' + index + '" value="' + esc(source.image_url) + '" placeholder="https://example.com/standby.jpg"></label>' +
        '<label class="field">Upload still<input name="supload" data-i="' + index + '" type="file" accept="image/jpeg,image/png,image/webp"></label>';
    }
    return '<article class="card"><div class="card-head"><h3>' + esc(source.name || "Source") + "</h3>" +
      '<div class="quiet-actions"><button type="button" class="btn-quiet" data-act="up" data-i="' + index + '">Move up</button>' +
      '<button type="button" class="btn-quiet" data-act="down" data-i="' + index + '">Move down</button>' +
      '<button type="button" class="btn-quiet btn-danger" data-act="remove-source" data-i="' + index + '">Remove</button></div></div>' +
      '<div class="fields"><label class="field">Name<input name="sname" data-i="' + index + '" value="' + esc(source.name) + '"></label>' +
      '<label class="field">Type<select name="stype" data-i="' + index + '">' + types + "</select></label>" + extra + "</div></article>";
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
      '<div class="quiet-actions"><button type="button" class="btn-quiet" data-act="rule-up" data-i="' + index + '" aria-label="Move rule up">Up</button>' +
      '<button type="button" class="btn-quiet" data-act="rule-down" data-i="' + index + '" aria-label="Move rule down">Down</button>' +
      '<button type="button" class="btn-quiet btn-danger" data-act="remove-rule" data-i="' + index + '">Remove</button></div></div>';
  }

  async function showEditor(id) {
    main.innerHTML = '<p class="loading">Loading camera…</p>';
    const interfaces = (await api("/api/interfaces")).interfaces;
    const entityPayload = await api("/api/entities");
    const cameras = (await api("/api/cameras")).cameras;
    const camera = id ? await api("/api/cameras/" + id) : blankCamera();
    renderNav(cameras, { name: "edit", id: id });

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
      const assignedText = loopbackBroadcast
        ? "Loopback publishes only on this machine."
        : (keepAddress
          ? "Assigned address " + camera.listen_ip + ". ONVIF clients use http://" + camera.listen_ip + "/onvif/device_service."
          : "A free address on this network is reserved when you save. ONVIF clients use http://that-address/onvif/device_service.");
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
      const urls = camera.urls || {};
      const addressRows = [];
      if (camera.virtual_ip && camera.listen_ip) addressRows.push(["Address", camera.listen_ip]);
      if (urls.rtsp) addressRows.push(["RTSP", urls.rtsp], ["ONVIF", urls.onvif]);
      const addresses = addressRows.length
        ? '<section class="card"><h2>Addresses</h2><p class="hint">These stay the same when the picture source changes.</p>' +
          copyList(addressRows) + "</section>"
        : "";
      main.innerHTML =
        pageHead(id ? "Edit camera" : "Add camera", "") +
        (entityPayload.connected ? "" : '<div class="banner info">Home Assistant entities are unavailable. RTSP, ONVIF, and image sources can still be saved. ' + esc(entityPayload.error || "") + "</div>") +
        '<form id="camera-form" class="stack">' +
        '<datalist id="camera-entities">' + cameraList + "</datalist>" +
        '<datalist id="all-entities">' + allList + "</datalist>" +
        '<section class="card"><h2>Camera</h2><div class="fields"><label class="field">Name<input name="name" required value="' + esc(camera.name) + '"></label>' +
        '<label class="switch"><input type="checkbox" name="enabled"' + (camera.enabled ? " checked" : "") + '><span>Enabled</span></label></div></section>' +
        '<section class="card"><h2>Networks</h2>' +
        "<p class=\"hint\">Pull the camera on one network and publish the ONVIF stream on another. The published address stays the same when the picture source changes.</p>" +
        '<h3 class="fixed-title">Listening network</h3>' +
        '<label class="field">Reach the camera from<select name="source_iface">' + sourceOptions + "</select></label>" +
        "<p class=\"hint\">RTSP and remote images open from this address. Follow the host route when the host already has a path to the camera. Home Assistant camera entities are fetched by Home Assistant.</p>" +
        '<h3 class="fixed-title">Broadcast network</h3>' +
        '<label class="field">Publish the stream on<select name="iface">' + ifaceOptions + "</select></label>" +
        '<input type="hidden" name="listen_ip" value="' + esc(broadcastValue) + '">' +
        '<p class="hint" data-assigned>' + esc(assignedText) + "</p></section>" +
        '<section class="card"><h2>ONVIF access</h2><div class="fields two"><label class="field">Username<input name="username" value="' + esc(camera.username || "") + '" autocomplete="off"></label>' +
        '<label class="field">Password<input name="password" type="password" value="' + esc(camera.password || "") + '" autocomplete="off"></label></div>' +
        "<p class=\"hint\">Leave both empty for an open lab device. When set, stream and profile calls require a UsernameToken or HTTP Digest. Time and capabilities stay open.</p></section>" +
        addresses +
        outputFields(camera) +
        '<div><h2 class="block-title">Sources</h2><div class="stack">' + camera.sources.map(sourceBlock).join("") +
        '<div><button type="button" class="btn-quiet" id="add-source">Add source</button></div></div></div>' +
        '<section class="card"><h2>Automation</h2>' +
        '<label class="switch"><input type="checkbox" name="rules_enabled"' + (camera.rules_enabled ? " checked" : "") + '><span>Switch picture from entity state</span></label>' +
        '<p class="hint">Leave this off to keep one fixed source. A practical pair is a printer switch that equals on to show the camera, with a still image as the fallback.</p>' +
        '<div class="fields inset"><label class="field">Fallback source<select name="fallback">' + fallback + "</select></label></div>" +
        '<div class="rules">' +
        (camera.rules.length ? camera.rules.map(function (rule, index) { return ruleBlock(rule, index, camera.sources); }).join("") : '<p class="hint">No rules. The fallback source is used.</p>') +
        "</div>" +
        '<button type="button" class="btn-quiet" id="add-rule">Add rule</button></section>' +
        '<p id="form-error" class="banner error" role="alert" hidden></p>' +
        '<div class="form-actions"><button class="btn" type="submit">Save camera</button>' +
        (id ? '<a class="btn-quiet" href="#/cameras/' + id + '/status">Live status</a><button type="button" class="btn-quiet btn-danger" id="delete-camera">Delete</button>' : "") +
        "</div></form>";

      const form = document.getElementById("camera-form");
      const assignedInterface = camera.virtual_ip ? camera.interface : "";
      const assignedListen = camera.virtual_ip ? camera.listen_ip : "";
      form.iface.addEventListener("change", function () {
        const selected = form.iface.selectedOptions[0];
        const name = selected.getAttribute("data-name") || "";
        const note = form.querySelector("[data-assigned]");
        if (selected.value === "127.0.0.1") {
          form.listen_ip.value = "127.0.0.1";
          camera.virtual_ip = false;
          camera.listen_ip = "127.0.0.1";
          note.textContent = "Loopback publishes only on this machine.";
        } else if (assignedInterface === name && assignedListen) {
          form.listen_ip.value = assignedListen;
          camera.virtual_ip = true;
          camera.listen_ip = assignedListen;
          note.textContent = "Assigned address " + assignedListen + ". ONVIF clients use http://" + assignedListen + "/onvif/device_service.";
        } else {
          form.listen_ip.value = "";
          camera.virtual_ip = false;
          camera.listen_ip = "";
          note.textContent = "A free address on this network is reserved when you save. ONVIF clients use http://that-address/onvif/device_service.";
        }
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
    }

    paint();
  }

  function statusSentence(camera) {
    const runtime = camera.runtime || {};
    if (!camera.enabled || runtime.reason === "disabled") return "This camera is disabled.";
    if (runtime.reason === "no_source" || !runtime.source_name) return "No source is available.";
    if (runtime.source_kind === "image") return "Using the still image.";
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
    main.innerHTML =
      pageHead(camera.name, '<a class="btn" href="#/cameras/' + id + '">Edit</a>') +
      '<section class="card"><div class="hero"><div class="hero-icon ' + state.kind + '">' + iconCamera + "</div><div>" +
      '<p class="hero-title">' + esc(statusSentence(camera)) + "</p>" +
      '<p class="hint">' + esc(statusDetail(camera)) + "</p></div></div>" +
      '<p class="eyebrow">Networks</p>' + factList([
        ["Listening", camera.source_ip ? (camera.source_interface ? camera.source_interface + " (" + camera.source_ip + ")" : camera.source_ip) : "Follow the host route"],
        ["Broadcast", camera.virtual_ip ? camera.listen_ip : (camera.interface ? camera.interface + " (" + camera.listen_ip + ")" : camera.listen_ip)],
      ]) +
      '<p class="eyebrow">ONVIF profile</p><h3>main</h3>' + factList(profileFacts(camera).concat(publishedFacts(camera).slice(0, 5), encoderFixed(camera))) +
      (runtime.error ? '<p class="banner error" role="alert">' + esc(runtime.error) + "</p>" : "") +
      (runtime.engine_error ? '<p class="banner error" role="alert">' + esc(runtime.engine_error) + "</p>" : "") +
      copyList([["RTSP", runtime.rtsp_url], ["ONVIF", runtime.onvif_url]]) +
      '<div class="form-actions"><button type="button" class="btn-quiet" id="refresh">Refresh</button></div></section>';
    document.getElementById("refresh").addEventListener("click", function () { showStatus(id); });
  }

  async function render() {
    const current = route();
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
