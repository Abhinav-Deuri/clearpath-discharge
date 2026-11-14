// Patient discharge tracker: refresh the page content in place every few seconds.
// The server renders everything; this script only swaps in the newer markup when something changed.
(function () {
    "use strict";
    var REFRESH_MS = 4000;
    var paying = false;

    function app() {
        return document.getElementById("cp-app");
    }

    function refresh() {
        if (paying || document.hidden) {
            return;
        }
        // Drop the one-time ?notice= so a refreshed copy does not keep repeating it.
        fetch(window.location.pathname, { cache: "no-store", credentials: "omit" })
            .then(function (response) {
                return response.ok ? response.text() : null;
            })
            .then(function (html) {
                if (!html) {
                    return;
                }
                var next = new DOMParser().parseFromString(html, "text/html").getElementById("cp-app");
                var current = app();
                if (next && current && next.dataset.sig !== current.dataset.sig) {
                    current.replaceWith(next);
                    next.classList.add("cp-updated");
                    wire();
                }
            })
            .catch(function () {
                /* offline for a moment: keep showing the last known state */
            });
    }

    // Short "processing" beat before the form posts, so the simulated payment reads as a payment.
    function wire() {
        var form = document.querySelector(".cp-pay-form");
        if (!form) {
            return;
        }
        form.addEventListener("submit", function (ev) {
            if (paying) {
                return;
            }
            ev.preventDefault();
            paying = true;
            var button = ev.submitter || form.querySelector("button");
            form.classList.add("cp-processing");
            button.classList.add("cp-busy");
            button.textContent = "Processing…";
            setTimeout(function () {
                form.submit();
            }, 1100);
        });
    }

    wire();
    if (window.location.search) {
        window.history.replaceState(null, "", window.location.pathname);
    }
    setInterval(refresh, REFRESH_MS);
    document.addEventListener("visibilitychange", refresh);
})();
