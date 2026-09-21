(function () {
  "use strict";

  function addPasswordToggle() {
    const password = document.querySelector('input[type="password"]');

    if (!password || password.dataset.toggleReady) {
      return;
    }

    password.dataset.toggleReady = "1";

    const wrapper = password.parentElement;

    if (!wrapper) {
      return;
    }

    wrapper.style.position = "relative";

    const toggle = document.createElement("button");

    toggle.type = "button";
    toggle.textContent = "Show";
    toggle.setAttribute("aria-label", "Show password");

    toggle.style.cssText =
      "position:absolute;right:8px;bottom:8px;min-height:30px;padding:4px 9px;" +
      "border:0;border-radius:8px;background:#eef2ff;color:#4338ca;" +
      "font-size:11px;font-weight:800;cursor:pointer;";

    toggle.addEventListener("click", function () {

      const visible = password.type === "text";

      password.type = visible ? "password" : "text";

      toggle.textContent = visible ? "Show" : "Hide";

      toggle.setAttribute(
        "aria-label",
        visible ? "Show password" : "Hide password"
      );

    });

    wrapper.appendChild(toggle);
  }

  function addSubmitFeedback() {

    document.querySelectorAll("form").forEach(function (form) {

      form.addEventListener("submit", function () {

        const buttons = form.querySelectorAll(
          'button[type="submit"],input[type="submit"]'
        );

        buttons.forEach(function (button) {

          button.dataset.originalText =
            button.textContent || button.value || "";

          button.disabled = true;

          if ("value" in button) {
            button.value = "Processing…";
          } else {
            button.textContent = "Processing…";
          }

        });

      });

    });
  }

  function enhanceCards() {

    document
      .querySelectorAll(
        ".card,.section,.stat-card,.info-card,.table-card"
      )
      .forEach(function (el) {

        el.addEventListener("mouseenter", function () {

          if (
            window.matchMedia(
              "(prefers-reduced-motion: reduce)"
            ).matches
          ) {
            return;
          }

          el.style.transform = "translateY(-2px)";
          el.style.transition = "transform .18s ease";

        });

        el.addEventListener("mouseleave", function () {
          el.style.transform = "";
        });

      });
  }

  document.addEventListener("DOMContentLoaded", function () {

    addPasswordToggle();

    addSubmitFeedback();

    enhanceCards();

  });

})();