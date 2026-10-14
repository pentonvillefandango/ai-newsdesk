/* Theme toggle (Auto, Light or Dark) and accent colour, each remembered per browser.
   Loaded synchronously in <head> so the forced theme and the accent are set before the first
   paint. No markup is ever written from script; storage may be unavailable, so every access is
   guarded. */
(function () {
  "use strict";

  var THEME_KEY = "newsdesk-theme";
  var THEMES = ["auto", "light", "dark"];
  var ACCENT_KEY = "newsdesk-accent";
  var ACCENTS = ["amber", "blue", "green", "violet", "rose", "teal"];
  var root = document.documentElement;

  function load(key, choices) {
    var value = null;
    try {
      value = window.localStorage.getItem(key);
    } catch (e) {
      value = null;
    }
    return choices.indexOf(value) === -1 ? choices[0] : value;
  }

  function save(key, choice, fallback) {
    try {
      if (choice === fallback) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, choice);
    } catch (e) {
      /* private mode or blocked storage: the choice lasts for this page only */
    }
  }

  function applyTheme(choice) {
    if (choice === "auto") {
      delete root.dataset.theme;
    } else {
      root.dataset.theme = choice;
    }
  }

  function applyAccent(choice) {
    root.dataset.accent = choice;
  }

  var theme = load(THEME_KEY, THEMES);
  var accent = load(ACCENT_KEY, ACCENTS);
  applyTheme(theme);
  applyAccent(accent);
  root.dataset.themeReady = "";  // reveals the controls (they do nothing without this script)

  function pressed(buttons, attr, current) {
    buttons.forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.getAttribute(attr) === current));
    });
  }

  function setUpTheme() {
    var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-theme-choice]"));
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var choice = button.dataset.themeChoice;
        if (THEMES.indexOf(choice) === -1) {
          return;
        }
        theme = choice;
        save(THEME_KEY, choice, "auto");
        applyTheme(choice);
        pressed(buttons, "data-theme-choice", theme);
      });
    });
    pressed(buttons, "data-theme-choice", theme);
  }

  /* The accent control: a button that opens a small menu of six swatches. Escape or a click
     outside closes it; arrow keys move between swatches. */
  function setUpAccent() {
    var toggle = document.querySelector("[data-accent-toggle]");
    var menu = document.getElementById("accent-menu");
    if (!toggle || !menu) {
      return;
    }
    var buttons = Array.prototype.slice.call(menu.querySelectorAll("[data-accent-choice]"));

    function open() {
      menu.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      var chosen = buttons.filter(function (b) { return b.dataset.accentChoice === accent; });
      (chosen[0] || buttons[0]).focus();
    }

    function close(refocus) {
      if (menu.hidden) {
        return;
      }
      menu.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      if (refocus) toggle.focus();
    }

    toggle.addEventListener("click", function () {
      if (menu.hidden) open();
      else close(true);
    });

    buttons.forEach(function (button, i) {
      button.addEventListener("click", function () {
        var choice = button.dataset.accentChoice;
        if (ACCENTS.indexOf(choice) === -1) {
          return;
        }
        accent = choice;
        save(ACCENT_KEY, choice, "amber");
        applyAccent(choice);
        pressed(buttons, "data-accent-choice", accent);
        close(true);
      });
      button.addEventListener("keydown", function (event) {
        var step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
        if (step) {
          event.preventDefault();
          buttons[(i + step + buttons.length) % buttons.length].focus();
        }
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") close(true);
    });
    document.addEventListener("click", function (event) {
      if (!menu.hidden && !menu.contains(event.target) && !toggle.contains(event.target)) {
        close(false);
      }
    });
    pressed(buttons, "data-accent-choice", accent);
  }

  document.addEventListener("DOMContentLoaded", function () {
    setUpTheme();
    setUpAccent();
  });
})();
