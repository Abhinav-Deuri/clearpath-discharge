/** @odoo-module **/
// ClearPath UI effects: click ripples, a cursor-following glow, 3D tilt on tiles, and confetti when a
// discharge packet is released.
//
// Everything here is decoration. It never adds, removes or re-classes nodes inside Odoo's rendered views
// (the framework owns those); it only sets CSS custom properties on hovered elements and draws in its own
// overlay layer on <body>. All of it is skipped when the user asks for reduced motion.
import { registry } from "@web/core/registry";

const GLOW = ".o_cp_panel, .o_cp_subpanel, .o_cp_banner, .o_cp_hero, .oe_stat_button.o_cp_stat, .o_cp_tile, .o_cp_card";
const TILT = ".oe_stat_button.o_cp_stat, .o_cp_banner, .o_cp_tile";
const RIPPLE = ".btn, .oe_stat_button, .o_notebook .nav-link, .o_main_navbar .o_nav_entry, .o_main_navbar .dropdown-toggle";
const RELEASE_BUTTON = 'button[name="action_coordinator_release"]';
const CONFETTI_COLOURS = ["#1976d2", "#64b5f6", "#0b2a6f", "#ffffff", "#2ecc71", "#bbdefb"];

function overlay() {
    let layer = document.querySelector(".o_cp_fx_layer");
    if (!layer) {
        layer = document.createElement("div");
        layer.className = "o_cp_fx_layer";
        document.body.appendChild(layer);
    }
    return layer;
}

// A ripple is drawn in the overlay, clipped to the button's box, so the button itself is never touched.
function ripple(ev) {
    const target = ev.target.closest?.(RIPPLE);
    if (!target || target.disabled) {
        return;
    }
    const rect = target.getBoundingClientRect();
    const style = getComputedStyle(target);
    const clip = document.createElement("div");
    clip.className = "o_cp_ripple_clip";
    Object.assign(clip.style, {
        left: rect.left + "px",
        top: rect.top + "px",
        width: rect.width + "px",
        height: rect.height + "px",
        borderRadius: style.borderRadius,
    });
    const size = Math.max(rect.width, rect.height) * 2.2;
    const wave = document.createElement("span");
    wave.className = "o_cp_ripple";
    // Dark ink on light buttons, light ink on filled ones.
    const rgb = (style.backgroundColor.match(/\d+(\.\d+)?/g) || []).map(Number);
    const filled = rgb.length >= 3 && (rgb[3] === undefined || rgb[3] > 0.5) && rgb[0] + rgb[1] + rgb[2] < 480;
    wave.classList.toggle("o_cp_ripple_light", filled);
    Object.assign(wave.style, {
        width: size + "px",
        height: size + "px",
        left: ev.clientX - rect.left - size / 2 + "px",
        top: ev.clientY - rect.top - size / 2 + "px",
    });
    clip.appendChild(wave);
    overlay().appendChild(clip);
    wave.addEventListener("animationend", () => clip.remove(), { once: true });
    setTimeout(() => clip.remove(), 900);
}

let hovered = null;
function resetHover() {
    if (hovered) {
        for (const prop of ["--cp-mx", "--cp-my", "--cp-rx", "--cp-ry"]) {
            hovered.style.removeProperty(prop);
        }
        hovered = null;
    }
}

let frame = 0;
function pointerMove(ev) {
    if (frame) {
        return;
    }
    frame = requestAnimationFrame(() => {
        frame = 0;
        const el = ev.target.closest?.(GLOW);
        if (el !== hovered) {
            resetHover();
            hovered = el || null;
        }
        if (!el) {
            return;
        }
        const rect = el.getBoundingClientRect();
        const x = ev.clientX - rect.left;
        const y = ev.clientY - rect.top;
        el.style.setProperty("--cp-mx", x + "px");
        el.style.setProperty("--cp-my", y + "px");
        if (el.matches(TILT)) {
            const strength = el.matches(".o_cp_banner") ? 2.5 : 9;
            el.style.setProperty("--cp-ry", ((x / rect.width - 0.5) * strength).toFixed(2) + "deg");
            el.style.setProperty("--cp-rx", ((0.5 - y / rect.height) * strength).toFixed(2) + "deg");
        }
    });
}

function confetti() {
    const canvas = document.createElement("canvas");
    canvas.className = "o_cp_confetti";
    canvas.width = innerWidth;
    canvas.height = innerHeight;
    overlay().appendChild(canvas);
    const ctx = canvas.getContext("2d");
    const pieces = Array.from({ length: 160 }, (_, i) => {
        const fromLeft = i % 2 === 0;
        const angle = (fromLeft ? -Math.PI / 3 : (-2 * Math.PI) / 3) + (Math.random() - 0.5) * 0.9;
        const speed = 9 + Math.random() * 11;
        return {
            x: fromLeft ? 0 : canvas.width,
            y: canvas.height * 0.72,
            vx: Math.cos(angle) * speed,
            vy: Math.sin(angle) * speed,
            w: 6 + Math.random() * 7,
            h: 4 + Math.random() * 5,
            spin: Math.random() * Math.PI,
            vs: (Math.random() - 0.5) * 0.4,
            colour: CONFETTI_COLOURS[i % CONFETTI_COLOURS.length],
        };
    });
    const started = performance.now();
    const DURATION = 3200;
    (function draw(now) {
        const age = now - started;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.globalAlpha = Math.max(0, 1 - Math.max(0, age - DURATION * 0.6) / (DURATION * 0.4));
        for (const p of pieces) {
            p.vy += 0.28;
            p.vx *= 0.99;
            p.x += p.vx;
            p.y += p.vy;
            p.spin += p.vs;
            ctx.save();
            ctx.translate(p.x, p.y);
            ctx.rotate(p.spin);
            ctx.fillStyle = p.colour;
            ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h * Math.abs(Math.cos(p.spin * 2)) + 1);
            ctx.restore();
        }
        if (age < DURATION) {
            requestAnimationFrame(draw);
        } else {
            canvas.remove();
        }
    })(started);
}

// The release is confirmed by the server, so wait for the "Released" ribbon before celebrating.
function watchRelease(ev) {
    if (!ev.target.closest?.(RELEASE_BUTTON)) {
        return;
    }
    const deadline = Date.now() + 12000;
    const timer = setInterval(() => {
        const ribbon = document.querySelector(".o_clearpath_discharge_form .o_widget_web_ribbon");
        if (ribbon && /released/i.test(ribbon.textContent)) {
            clearInterval(timer);
            confetti();
        } else if (Date.now() > deadline) {
            clearInterval(timer);
        }
    }, 250);
}

export const clearpathFxService = {
    start() {
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
            return;
        }
        document.addEventListener("pointerdown", ripple, { capture: true, passive: true });
        document.addEventListener("pointermove", pointerMove, { passive: true });
        document.addEventListener("pointerleave", resetHover, { passive: true });
        document.addEventListener("click", watchRelease, { capture: true, passive: true });
    },
};

registry.category("services").add("clearpath_fx", clearpathFxService);
