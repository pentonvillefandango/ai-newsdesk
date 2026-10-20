/* Play sample on Settings: points the audio element at the same-origin sample proxy. */
(function () {
  var button = document.getElementById("voice-sample-play");
  var select = document.getElementById("narration_voice_id");
  var audio = document.getElementById("voice-sample-audio");
  if (!button || !select || !audio) return;
  button.disabled = false;
  button.addEventListener("click", function () {
    audio.src = "/settings/voice-sample/" + encodeURIComponent(select.value);
    var started = audio.play();
    if (started && started.catch) started.catch(function () {});
  });
})();
