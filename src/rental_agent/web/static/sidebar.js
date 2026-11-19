// Sidebar state, kept as classes on <html> that style.css reads:
//   rail   — desktop: the sidebar is a narrow rail of icons. Remembered between pages.
//   drawer — phones: the sidebar is open over the page. Closed again on every page load.
// Loaded in <head> without defer, so a collapsed sidebar does not flash open on each page.
"use strict";

(() => {
  const root = document.documentElement;
  const phone = window.matchMedia("(max-width: 760px)");
  // Storage can be blocked (private windows); the sidebar then just starts open.
  const saved = () => { try { return localStorage.getItem("sidebar"); } catch { return null; } };
  const save = (value) => { try { localStorage.setItem("sidebar", value); } catch { /* not kept */ } };

  root.classList.add("js");
  root.classList.toggle("rail", saved() === "rail");

  const setRail = (on) => {
    root.classList.toggle("rail", on);
    save(on ? "rail" : "open");
  };
  const inRail = () => !phone.matches && root.classList.contains("rail");

  document.addEventListener("DOMContentLoaded", () => {
    const side = document.getElementById("side");
    if (!side) return;

    document.getElementById("side-toggle").addEventListener("click", () => {
      if (phone.matches) root.classList.remove("drawer");
      else setRail(!root.classList.contains("rail"));
    });
    document.getElementById("menu-open").addEventListener("click", () => root.classList.add("drawer"));
    document.getElementById("backdrop").addEventListener("click", () => root.classList.remove("drawer"));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") root.classList.remove("drawer");
    });

    // In the rail a group shows only its icon: a click opens the sidebar with that group open.
    for (const summary of side.querySelectorAll("summary")) {
      summary.addEventListener("click", (e) => {
        if (!inRail()) return;
        e.preventDefault();
        summary.parentElement.open = true;
        setRail(false);
      });
    }
    // The same for search: open the sidebar, then type.
    const search = side.querySelector(".side-search");
    search.addEventListener("click", () => {
      if (inRail()) setRail(false);
      search.querySelector("input").focus();
    });
  });
})();
