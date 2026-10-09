(function () {
  "use strict";

  function closeMenu(trigger, panel, restoreFocus) {
    panel.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
  }

  window.initActionMenus = function (scope) {
    (scope || document).querySelectorAll("[data-action-menu]").forEach(function (root) {
      if (root.dataset.actionMenuReady === "true") return;
      var trigger = root.querySelector("[data-action-menu-trigger]");
      var panel = root.querySelector("[data-action-menu-panel]");
      if (!trigger || !panel) return;
      root.dataset.actionMenuReady = "true";
      if (!panel.id) {
        panel.id = "action-menu-" + Math.random().toString(36).slice(2, 10);
      }
      trigger.setAttribute("aria-controls", panel.id);
      trigger.setAttribute("aria-expanded", "false");
      panel.hidden = true;

      trigger.addEventListener("click", function (event) {
        event.stopPropagation();
        var opening = panel.hidden;
        document.querySelectorAll("[data-action-menu-panel]:not([hidden])").forEach(function (openPanel) {
          var openRoot = openPanel.closest("[data-action-menu]");
          var openTrigger = openRoot && openRoot.querySelector("[data-action-menu-trigger]");
          if (openTrigger && openPanel !== panel) closeMenu(openTrigger, openPanel, false);
        });
        panel.hidden = !opening;
        trigger.setAttribute("aria-expanded", String(opening));
        if (opening) {
          var first = panel.querySelector("a, button, input, select, textarea, [tabindex]:not([tabindex='-1'])");
          if (first) first.focus();
        }
      });

      panel.addEventListener("click", function (event) { event.stopPropagation(); });
      root.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !panel.hidden) closeMenu(trigger, panel, true);
      });
      document.addEventListener("click", function () {
        if (!panel.hidden) closeMenu(trigger, panel, false);
      });
    });
  };

  document.addEventListener("DOMContentLoaded", function () {
    window.initActionMenus(document);
  });
})();