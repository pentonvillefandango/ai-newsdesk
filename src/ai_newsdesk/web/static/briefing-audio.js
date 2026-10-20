/* Speed buttons for the briefing audio. The player itself is native and works without this. */
(function () {
  var KEY = "ai-newsdesk-audio-rate";
  var audio = document.querySelector(".listen audio");
  var buttons = document.querySelectorAll(".listen [data-rate]");
  if (!audio || !buttons.length) return;

  function apply(rate) {
    audio.playbackRate = rate;
    buttons.forEach(function (b) {
      b.setAttribute("aria-pressed", parseFloat(b.dataset.rate) === rate ? "true" : "false");
    });
  }

  var saved = 1;
  try {
    var stored = parseFloat(localStorage.getItem(KEY));
    if (stored === 1 || stored === 1.25 || stored === 1.5) saved = stored;
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
