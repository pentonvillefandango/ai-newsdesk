/* Speed buttons for the briefing audio. The player itself is native and works without this. */
(function () {
  // A new key (feedback-5): the old one could hold 1, and the default is now 1.25.
  var KEY = "ai-newsdesk-audio-rate-2";
  var RATES = [1, 1.1, 1.15, 1.2, 1.25, 1.3, 1.4, 1.5];
  var audio = document.querySelector(".listen audio");
  var buttons = document.querySelectorAll(".listen [data-rate]");
  if (!audio || !buttons.length) return;

  function apply(rate) {
    audio.playbackRate = rate;
    buttons.forEach(function (b) {
      b.setAttribute("aria-pressed", parseFloat(b.dataset.rate) === rate ? "true" : "false");
    });
  }

  var saved = 1.25;
  try {
    var stored = parseFloat(localStorage.getItem(KEY));
    if (RATES.indexOf(stored) !== -1) saved = stored;
  } catch (e) {}
  apply(saved);
  audio.addEventListener("loadedmetadata", function () { apply(saved); });

  buttons.forEach(function (b) {
    b.addEventListener("click", function () {
      saved = parseFloat(b.dataset.rate);
      apply(saved);
      try { localStorage.setItem(KEY, String(saved)); } catch (e) {}
    });
  });
})();
