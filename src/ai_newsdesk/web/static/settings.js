/* Settings, Models rows: optional progressive enhancement. Without it every control still
   submits and the server picks (a filled "Other model" box wins over the list).
   With it: a row's model list shows only its provider's models, the text box autocompletes
   from that provider's list, and picking from the list clears the text box.
   No markup is ever written from script. */
(function () {
  "use strict";

  function rowParts(job) {
    return {
      provider: document.querySelector('[data-provider-for="' + job + '"]'),
      models: document.querySelector('[data-models-for="' + job + '"]'),
      other: document.querySelector('[data-other-for="' + job + '"]')
    };
  }

  function showProvider(parts, keepChoice) {
    var provider = parts.provider.value;
    var groups = parts.models.querySelectorAll("optgroup");
    var selected = parts.models.options[parts.models.selectedIndex];
    var selectedGroup = selected ? selected.parentElement : null;
    var firstUsable = null;
    Array.prototype.forEach.call(groups, function (group) {
      var match = group.dataset.provider === provider;
      // Never disable the group holding the current choice: it would drop out of the form.
      var keep = match || group === selectedGroup;
      group.hidden = !keep;
      group.disabled = !keep;
      if (match && firstUsable === null) {
        firstUsable = group.querySelector("option:not([disabled])");
      }
    });
    parts.other.setAttribute("list", "models-" + provider);
    if (keepChoice) {
      return;
    }
    // A new provider: the old choice belongs to another provider's list.
    parts.other.value = "";
    if (selectedGroup && selectedGroup.tagName === "OPTGROUP" &&
        selectedGroup.dataset.provider !== provider) {
      if (firstUsable) {
        firstUsable.selected = true;
      } else {
        parts.models.value = "";
      }
      showProvider(parts, true);  // now the old provider's group can be hidden too
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var providers = document.querySelectorAll("[data-provider-for]");
    Array.prototype.forEach.call(providers, function (select) {
      var parts = rowParts(select.dataset.providerFor);
      if (!parts.models || !parts.other) {
        return;
      }
      showProvider(parts, true);
      select.addEventListener("change", function () {
        showProvider(parts, false);
      });
      parts.models.addEventListener("change", function () {
        if (parts.models.value !== "") {
          parts.other.value = "";
        }
      });
    });
  });
})();
