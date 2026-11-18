/* Shared helpers: geolocation, sidebar nav state, order-form totals. */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ *
   * 1. Location helper
   *    Asks the browser for the driver's position, POSTs it to the
   *    backend (so it is remembered), then reloads the page with
   *    ?lat=&lng= so the server can sort by distance.
   * ------------------------------------------------------------------ */
  var LocationHelper = {
    primaryUrl: null,
    button: null,
    status: null,

    init: function (options) {
      this.primaryUrl = (options && options.primaryUrl) || "/driver/location";
      this.button = document.querySelector("[data-geolocate]");
      this.status = document.querySelector("[data-geo-status]");
      if (!this.button) return;

      var self = this;
      this.button.addEventListener("click", function () {
        self.request();
      });

      // If the page has no coordinates yet, try once silently.
      if (this.button.dataset.autorequest === "1") {
        this.request(true);
      }
    },

    setStatus: function (message, tone) {
      if (!this.status) return;
      this.status.textContent = message;
      this.status.className = "muted";
      if (tone) this.status.style.color = tone;
    },

    request: function (silent) {
      var self = this;
      if (!navigator.geolocation) {
        this.setStatus("This browser does not support geolocation. Enter coordinates manually.", "#b42318");
        return;
      }
      this.button.disabled = true;
      this.setStatus(silent ? "Detecting your location…" : "Getting your GPS position…");

      navigator.geolocation.getCurrentPosition(
        function (position) {
          self.save(position.coords.latitude, position.coords.longitude);
        },
        function (error) {
          self.button.disabled = false;
          var reasons = {
            1: "Location permission denied. Allow access or enter coordinates manually.",
            2: "Position unavailable. Try again in a moment.",
            3: "Location request timed out. Try again."
          };
          self.setStatus(reasons[error.code] || "Could not get your location.", "#b42318");
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
      );
    },

    save: function (lat, lng) {
      var self = this;
      this.setStatus("Location found — searching nearby…");

      // CSRF: forms get a hidden field, but a fetch() call must send the
      // token itself. Without this the POST is rejected with 400/403.
      var meta = document.querySelector('meta[name="csrf-token"]');
      var headers = { "Content-Type": "application/json" };
      if (meta && meta.content) headers["X-CSRF-Token"] = meta.content;

      fetch(this.primaryUrl, {
        method: "POST",
        headers: headers,
        credentials: "same-origin",
        body: JSON.stringify({ latitude: lat, longitude: lng })
      })
        .then(function (res) { return res.json().then(function (b) { return { ok: res.ok, body: b }; }); })
        .then(function (result) {
          if (!result.ok) throw new Error(result.body && result.body.error);
          self.setStatus("Location saved.", "#106b3c");
          window.location.href = result.body.redirect;
        })
        .catch(function (err) {
          self.button.disabled = false;
          self.setStatus(err.message || "Could not save your location.", "#b42318");
        });
    }
  };
  window.LocationHelper = LocationHelper;

  /* ------------------------------------------------------------------ *
   * 2. Fill the "coordinates" inputs from the browser and submit the
   *    surrounding plain GET form — used by filters on listing pages.
   * ------------------------------------------------------------------ */
  function initFormGeolocate() {
    document.querySelectorAll("[data-geo-form]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var form = document.getElementById(btn.dataset.geoForm);
        if (!form || !navigator.geolocation) return;
        btn.disabled = true;
        btn.textContent = "Locating…";
        navigator.geolocation.getCurrentPosition(
          function (pos) {
            form.querySelector("[name=lat]").value = pos.coords.latitude;
            form.querySelector("[name=lng]").value = pos.coords.longitude;
            form.submit();
          },
          function () {
            btn.disabled = false;
            btn.textContent = "Use my location";
            window.alert("Could not get your location. Please allow location access and try again.");
          },
          { enableHighAccuracy: true, timeout: 10000 }
        );
      });
    });
  }

  /* ------------------------------------------------------------------ *
   * 2b. GeoLocate — fill coordinate inputs from the browser's position.
   *
   *    Adapted from the standard getCurrentPosition snippet: the same
   *    three-branch error switch (denied / unavailable / timeout), the
   *    same "not supported" fallback, and the same high-accuracy
   *    options. Instead of console.log it writes into the page, so the
   *    result is visible without opening devtools.
   *
   *    Declarative use:
   *      <button data-geo-locate data-geo-target="lat,lng">Use my position</button>
   *      <span data-geo-status></span>
   * ------------------------------------------------------------------ */
  function GeoLocate(options) {
    this.button = options.button;
    this.latInput = options.latInput;
    this.lngInput = options.lngInput;
    // Optional <span> for feedback; falls back to the button's help text.
    this.status = options.status;
    // Optional: after a GPS fix, fill this text input with a readable name.
    this.nameInput = options.nameInput;
    this.filledText = this.button.textContent;
    this.bind();
  }

  GeoLocate.MESSAGES = {
    unsupported: "This browser does not support geolocation. Enter the coordinates by hand.",
    denied: "Location permission denied. Allow access in your browser, or type the coordinates.",
    unavailable: "Location information is unavailable right now. Try again in a moment.",
    timeout: "The request for your location timed out. Try again, or type the coordinates.",
    unknown: "Could not read your location. Please type the coordinates."
  };

  GeoLocate.prototype = {
    setStatus: function (message, tone) {
      if (this.status) {
        this.status.textContent = message;
        this.status.className = "help";
        if (tone) this.status.style.color = tone;
        return;
      }
      // No dedicated element: reuse the button as the message surface.
      this.button.textContent = message;
    },

    reset: function () {
      this.button.disabled = false;
      this.button.textContent = this.filledText;
    },

    // Optional nicety: turn the fix into "Maitama District, Abuja" via our own
    // /api/places/reverse proxy. Failure is silent — coordinates already work.
    describe: function (latitude, longitude) {
      var self = this;
      if (!this.nameInput) return;
      fetch("/api/places/reverse?lat=" + latitude + "&lng=" + longitude, {
        credentials: "same-origin",
        headers: { "Accept": "application/json" }
      })
        .then(function (res) { return res.json(); })
        .then(function (body) {
          var place = body && body.place;
          if (!place || !place.label) return;
          if (self.nameInput.value.trim() && self.nameInput.value.trim() !== place.label) return;
          self.nameInput.value = place.detail ? place.label + ", " + place.detail : place.label;
        })
        .catch(function () { /* coordinates are already set; name is optional */ });
    },

    bind: function () {
      var self = this;
      this.button.addEventListener("click", function () {
        self.request();
      });
    },

    request: function () {
      var self = this;

      // Guard: either the property is absent, or it exists and is undefined
      // (which would throw on .getCurrentPosition). Check both.
      if (!("geolocation" in navigator) || !navigator.geolocation) {
        this.setStatus(GeoLocate.MESSAGES.unsupported, "#d4756a");
        return;
      }

      this.button.disabled = true;
      this.button.textContent = "Locating…";
      this.setStatus("Waiting for your browser's location permission…", null);

      navigator.geolocation.getCurrentPosition(
        function (position) {
          var latitude = position.coords.latitude;
          var longitude = position.coords.longitude;

          // Round to 6dp: plenty for street-level accuracy, and it stops
          // 17-digit floats landing in the input.
          self.latInput.value = latitude.toFixed(6);
          self.lngInput.value = longitude.toFixed(6);
          self.reset();

          var accuracy = position.coords.accuracy;
          var note = "Location captured";
          if (accuracy) note += " (accurate to about " + Math.round(accuracy) + " m)";
          self.setStatus(note + ".", "#7fb98a");

          // Name the place, so the address box is not left blank or stale.
          self.describe(latitude, longitude);

          // Let the page react (e.g. recalculate distances).
          self.button.dispatchEvent(new CustomEvent("geo:located", {
            bubbles: true,
            detail: { latitude: latitude, longitude: longitude }
          }));
        },
        function (error) {
          self.reset();
          var message;
          switch (error.code) {
            case error.PERMISSION_DENIED:
              message = GeoLocate.MESSAGES.denied;
              break;
            case error.POSITION_UNAVAILABLE:
              message = GeoLocate.MESSAGES.unavailable;
              break;
            case error.TIMEOUT:
              message = GeoLocate.MESSAGES.timeout;
              break;
            default:
              message = GeoLocate.MESSAGES.unknown;
          }
          self.setStatus(message, "#d4756a");
        },
        {
          enableHighAccuracy: true, // prefer GPS where available
          timeout: 10000,           // give up rather than hang forever
          maximumAge: 60000         // a position under a minute old is fine
        }
      );
    }
  };

  // Wire any [data-geo-locate] button to its coordinate inputs.
  function initGeoLocate() {
    document.querySelectorAll("[data-geo-locate]").forEach(function (btn) {
      var targets = (btn.dataset.geoTarget || "").split(",");
      if (targets.length !== 2) return;

      var lat = document.getElementById(targets[0].trim());
      var lng = document.getElementById(targets[1].trim());
      if (!lat || !lng) return;

      // Status line: an explicit data-geo-status-target wins, else the nearest
      // [data-geo-status] inside this fieldset/field/help.
      var scope = btn.closest("fieldset, .field, .help, form") || document;
      var status = btn.dataset.geoStatusTarget
        ? document.querySelector(btn.dataset.geoStatusTarget)
        : scope.querySelector("[data-geo-status]");

      // Optional: after a GPS fix, write a readable place name into this input.
      var nameInput = btn.dataset.geoFillName
        ? document.querySelector(btn.dataset.geoFillName)
        : null;

      new GeoLocate({
        button: btn,
        latInput: lat,
        lngInput: lng,
        status: status,
        nameInput: nameInput
      });
    });
  }

  /* ------------------------------------------------------------------ *
   * 2c. PlaceSearch — type-ahead address box that fills coordinates.
   *
   *    Declarative use:
   *      <input data-place-search
   *             data-place-target="latitude,longitude"
   *             data-place-summary="#some-status-element">
   *
   *    Talks to /api/places (our own server, which proxies Photon), so the
   *    CSP stays connect-src 'self' and the free provider is rate limited.
   * ------------------------------------------------------------------ */
  var PlaceSearch = {
    MIN_CHARS: 2,
    DELAY: 320,       // debounce: wait for a pause in typing
    cache: {},

    attach: function (input) {
      var self = this;
      var targets = (input.dataset.placeTarget || "").split(",");
      if (targets.length !== 2) return;
      var latInput = document.getElementById(targets[0].trim());
      var lngInput = document.getElementById(targets[1].trim());
      if (!latInput || !lngInput) return;

      var summary = input.dataset.placeSummary
        ? document.querySelector(input.dataset.placeSummary)
        : null;

      // The listbox must sit under the input, so give it a positioned parent.
      var wrap = document.createElement("div");
      wrap.className = "place-search";
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);

      var list = document.createElement("ul");
      list.className = "place-results";
      list.setAttribute("role", "listbox");
      list.hidden = true;
      wrap.appendChild(list);

      var timer = null;
      var active = -1;

      function close() {
        list.hidden = true;
        list.innerHTML = "";
        active = -1;
        input.setAttribute("aria-expanded", "false");
      }

      function choose(item) {
        latInput.value = Number(item.latitude).toFixed(6);
        lngInput.value = Number(item.longitude).toFixed(6);
        input.value = item.label + (item.detail ? ", " + item.detail : "");
        if (summary) {
          summary.textContent = "Coordinates set from “" + item.label + "”.";
          summary.style.color = "#7fb98a";
        }
        close();
        // Let the page react (distance recalculation etc.).
        input.dispatchEvent(new CustomEvent("place:selected", {
          bubbles: true, detail: item
        }));
      }

      function render(results) {
        list.innerHTML = "";
        if (!results.length) { close(); return; }
        results.forEach(function (item) {
          var li = document.createElement("li");
          li.className = "place-result";
          li.setAttribute("role", "option");
          li.tabIndex = -1;

          var name = document.createElement("span");
          name.className = "place-name";
          name.textContent = item.label;

          var detail = document.createElement("span");
          detail.className = "place-detail";
          detail.textContent = item.detail || "";

          li.appendChild(name);
          if (item.detail) li.appendChild(detail);
          li.addEventListener("mousedown", function (ev) {
            ev.preventDefault();   // keep focus in the input
            choose(item);
          });
          list.appendChild(li);
        });
        list.hidden = false;
        input.setAttribute("aria-expanded", "true");
        active = -1;
      }

      function lookup(term) {
        if (term.length < self.MIN_CHARS) { close(); return; }

        if (self.cache[term]) { render(self.cache[term]); return; }

        fetch("/api/places?q=" + encodeURIComponent(term), {
          credentials: "same-origin",
          headers: { "Accept": "application/json" }
        })
          .then(function (res) { return res.json(); })
          .then(function (body) {
            var results = (body && body.results) || [];
            self.cache[term] = results;
            // Ignore stale responses for text the user has moved past.
            if (input.value.trim() !== term) return;
            render(results);
            if (summary && !results.length && body.detail) {
              summary.textContent = body.detail;
              summary.style.color = "";
            }
          })
          .catch(function () { close(); });
      }

      input.addEventListener("input", function () {
        var term = input.value.trim();
        clearTimeout(timer);
        if (term.length < self.MIN_CHARS) { close(); return; }
        timer = setTimeout(function () { lookup(term); }, self.DELAY);
      });

      input.addEventListener("keydown", function (ev) {
        var items = list.querySelectorAll(".place-result");
        if (list.hidden || !items.length) return;
        if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          ev.preventDefault();
          active += ev.key === "ArrowDown" ? 1 : -1;
          if (active < 0) active = items.length - 1;
          if (active >= items.length) active = 0;
          items.forEach(function (el, i) {
            el.classList.toggle("is-active", i === active);
          });
        } else if (ev.key === "Enter" && active >= 0) {
          ev.preventDefault();
          items[active].dispatchEvent(new MouseEvent("mousedown"));
        } else if (ev.key === "Escape") {
          close();
        }
      });

      input.addEventListener("blur", function () {
        setTimeout(close, 120);   // let a click land first
      });
      input.setAttribute("aria-expanded", "false");
      input.setAttribute("autocomplete", "off");
      input.setAttribute("role", "combobox");
    },

    init: function () {
      var self = this;
      document.querySelectorAll("[data-place-search]").forEach(function (input) {
        self.attach(input);
      });
    }
  };

  // Escape closes any open suggestion list.
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    document.querySelectorAll(".place-results").forEach(function (list) {
      list.hidden = true;
    });
  });

  /* ------------------------------------------------------------------ *
   * 3. Order form: live total = quantity × unit price of the chosen fuel.
   * ------------------------------------------------------------------ */
  function initOrderForms() {
    document.querySelectorAll("[data-order-form]").forEach(function (form) {
      var select = form.querySelector("[name=fuel_type]");
      var qty = form.querySelector("[name=quantity_litres]");
      var output = form.querySelector("[data-total]");
      if (!select || !qty || !output) return;

      function recalc() {
        var option = select.options[select.selectedIndex];
        var price = option ? parseFloat(option.dataset.price || "0") : 0;
        var litres = parseFloat(qty.value || "0");
        var currency = output.dataset.currency || "";
        if (isNaN(price) || isNaN(litres) || price <= 0 || litres <= 0) {
          output.textContent = "—";
          return;
        }
        output.textContent = currency + " " + (price * litres).toLocaleString(undefined, {
          minimumFractionDigits: 2, maximumFractionDigits: 2
        });
      }
      select.addEventListener("change", recalc);
      qty.addEventListener("input", recalc);
      recalc();
    });
  }

  /* ------------------------------------------------------------------ *
   * 4. Mark the active top-nav link.
   * ------------------------------------------------------------------ */
  function initActiveNav() {
    var path = window.location.pathname;

    // Pick the single best match: "/admin/" is a prefix of "/admin/users", so
    // naively marking every prefix would light up two links at once.
    var best = null;
    document.querySelectorAll(".topnav a[data-match]").forEach(function (link) {
      var match = link.dataset.match;
      var hits = match === "/" ? path === "/" : path.indexOf(match) === 0;
      if (!hits) return;
      if (!best || match.length > best.dataset.match.length) best = link;
    });
    if (best) {
      best.classList.add("active");
      best.setAttribute("aria-current", "page");
    }
  }

  function initResponsiveNav() {
    var menu = document.querySelector(".nav-menu");
    if (!menu) return;

    var toggle = menu.querySelector("summary");
    var mobile = window.matchMedia("(max-width: 1050px)");
    function syncExpanded() {
      if (toggle) toggle.setAttribute("aria-expanded", String(menu.open));
    }
    function syncMenu(event) {
      menu.open = !event.matches;
      syncExpanded();
    }

    syncMenu(mobile);
    menu.addEventListener("toggle", syncExpanded);
    mobile.addEventListener("change", syncMenu);

    document.addEventListener("click", function (event) {
      if (mobile.matches && menu.open && !menu.contains(event.target)) {
        menu.open = false;
        syncExpanded();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (mobile.matches && event.key === "Escape" && menu.open) {
        menu.open = false;
        syncExpanded();
        if (toggle) toggle.focus();
      }
    });
  }

  function initPasswordToggles() {
    document.querySelectorAll('input[type="password"]').forEach(function (input) {
      var wrap = document.createElement("div");
      wrap.className = "password-input";
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);

      var button = document.createElement("button");
      button.type = "button";
      button.className = "password-toggle";
      button.textContent = "Show";
      button.setAttribute("aria-label", "Show password");
      button.setAttribute("aria-pressed", "false");

      button.addEventListener("click", function () {
        var showing = input.type === "text";
        input.type = showing ? "password" : "text";
        button.textContent = showing ? "Show" : "Hide";
        button.setAttribute("aria-label", showing ? "Show password" : "Hide password");
        button.setAttribute("aria-pressed", String(!showing));
      });

      wrap.appendChild(button);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initPasswordToggles();
    initGeoLocate();
    initFormGeolocate();
    PlaceSearch.init();
    initOrderForms();
    initActiveNav();
    initResponsiveNav();
  });
})();
