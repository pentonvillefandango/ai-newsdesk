/* Theme toggle: Auto (follow the device), Light or Dark, remembered per browser.
   Loaded synchronously in <head> so the forced theme is set before the first paint.
   No markup is ever written from script; storage may be unavailable, so every access is guarded. */
(function () {
  "use strict";

  var KEY = "newsdesk-theme";
  var CHOICES = ["auto", "light", "dark"];
  var root = document.documentElement;

  function load() {
    var value = null;
    try {
      value = window.localStorage.getItem(KEY);
    } catch (e) {
      value = null;
    }
    return CHOICES.indexOf(value) === -1 ? "auto" : value;
  }

  function save(choice) {
    try {
      if (choice === "auto") window.localStorage.removeItem(KEY);
      else window.localStorage.setItem(KEY, choice);
    } catch (e) {
      /* private mode or blocked storage: the choice lasts for this page only */
    }
  }

  function apply(choice) {
    if (choice === "auto") {
      delete root.dataset.theme;
    } else {
      root.dataset.theme = choice;
    }
  }

  var current = load();
  apply(current);
  root.dataset.themeReady = "";  // reveals the control (it does nothing without this script)

  document.addEventListener("DOMContentLoaded", function () {
    var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-theme-choice]"));

    function sync() {
      buttons.forEach(function (button) {
        button.setAttribute("aria-pressed", String(button.dataset.themeChoice === current));
      });
    }

    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var choice = button.dataset.themeChoice;
        if (CHOICES.indexOf(choice) === -1) {
          return;
        }
        current = choice;
        save(choice);
        apply(choice);
        sync();
      });
    });
    sync();
  });
})();
