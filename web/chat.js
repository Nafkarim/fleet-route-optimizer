/* Data assistant chat panel */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const panel = $("#chatPanel"), fab = $("#chatFab"), log = $("#chatLog"), form = $("#chatForm"), input = $("#chatInput");
  const sendBtn = $("#chatSend");

  const SUGGESTIONS = [
    "Give me the big picture: how good is this plan?",
    "Why are so many loads uncovered?",
    "Which home base has the most empty miles, and why?",
    "Walk me through the busiest truck's week",
    "Where is the most empty repositioning happening?",
    "What would happen if I lowered the empty-mile penalty?",
  ];

  const C = { id: null, busy: false, configured: null };

  // ---------------------------------------------------------------- open / close
  function setOpen(open) {
    panel.classList.toggle("open", open);
    panel.setAttribute("aria-hidden", String(!open));
    fab.setAttribute("aria-expanded", String(open));
    fab.hidden = open;
    if (open) {
      checkStatus();
      setTimeout(() => input.focus(), 120);
    }
  }
  fab.addEventListener("click", () => setOpen(true));
  $("#chatClose").addEventListener("click", () => setOpen(false));
  $("#chatNew").addEventListener("click", newChat);
  addEventListener("keydown", (e) => {
    if (e.key === "Escape" && panel.classList.contains("open") && document.activeElement && panel.contains(document.activeElement)) setOpen(false);
  });

  async function checkStatus() {
    if (C.configured !== null) return;
    try {
      const st = await (await fetch("/api/chat/status")).json();
      C.configured = st.configured;
    } catch (e) {
      C.configured = false;
    }
    renderEmpty();
  }

  // ---------------------------------------------------------------- rendering
  function renderEmpty() {
    if (log.querySelector(".msg")) return;
    if (C.configured === false) {
      log.innerHTML = `
        <div class="chat-setup">
          <b>Connect the assistant</b>
          <p>The assistant uses Claude and needs an Anthropic API key. Create a file named <code>.env</code> in the project folder containing:</p>
          <pre>ANTHROPIC_API_KEY=your-key-here</pre>
          <p>Then restart the dashboard (<code>./run.sh</code>). The key stays on your machine and <code>.env</code> is never committed to git.</p>
        </div>`;
      setEnabled(false);
      return;
    }
    log.innerHTML = `
      <div class="chat-welcome">
        <div class="chat-welcome-title">Ask anything about this week's plan</div>
        <p>I can look up trucks, loads, cities and lanes, explain why loads were left uncovered, and compare the optimized plan with naive dispatch.</p>
        <div class="chips">${SUGGESTIONS.map((q) => `<button type="button" class="chip">${escapeHtml(q)}</button>`).join("")}</div>
      </div>`;
    setEnabled(true);
  }

  function setEnabled(on) {
    input.disabled = !on || C.busy;
    sendBtn.disabled = !on || C.busy;
  }

  log.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) { send(chip.textContent); return; }
    const ent = e.target.closest("a.ent");
    if (ent) {
      e.preventDefault();
      openEntity(ent.dataset.kind, ent.dataset.id);
    }
  });

  async function openEntity(kind, id) {
    if (!window.FRO) return;
    if (kind === "truck") {
      window.FRO.openTruck(id);
    } else {
      try {
        const r = await fetch(`/api/load/${encodeURIComponent(id)}`);
        if (!r.ok) return;
        const info = await r.json();
        if (info.truck_id) window.FRO.openTruck(info.truck_id);
        else window.FRO.openUncovered(id);
      } catch (err) { /* ignore */ }
    }
    if (matchMedia("(max-width: 820px)").matches) setOpen(false);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderMarkdown(text) {
    const html = window.marked ? marked.parse(text, { gfm: true, breaks: false }) : escapeHtml(text).replace(/\n/g, "<br>");
    return window.DOMPurify ? DOMPurify.sanitize(html) : escapeHtml(text);
  }

  // turn T0123 / LD01234 in text nodes into links that open the dashboard view
  function linkify(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement.closest("a, code, pre") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    const re = /\b(T\d{4}|LD\d{5})\b/g;
    for (const node of nodes) {
      const t = node.nodeValue;
      if (!re.test(t)) continue;
      re.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0, m;
      while ((m = re.exec(t))) {
        frag.append(t.slice(last, m.index));
        const a = document.createElement("a");
        a.href = "#";
        a.className = "ent";
        a.dataset.kind = m[1].startsWith("T") ? "truck" : "load";
        a.dataset.id = m[1];
        a.title = a.dataset.kind === "truck" ? "Show this truck's route" : "Show this load";
        a.textContent = m[1];
        frag.append(a);
        last = m.index + m[1].length;
      }
      frag.append(t.slice(last));
      node.replaceWith(frag);
    }
  }

  function addUser(text) {
    const el = document.createElement("div");
    el.className = "msg user";
    el.textContent = text;
    log.append(el);
    scrollDown(true);
  }

  function addAssistant() {
    const el = document.createElement("div");
    el.className = "msg bot";
    el.innerHTML = `<div class="steps"></div><div class="body"><span class="typing"><i></i><i></i><i></i></span></div>`;
    log.append(el);
    scrollDown(true);
    return el;
  }

  function addNotice(text) {
    const el = document.createElement("div");
    el.className = "chat-notice";
    el.textContent = text;
    log.append(el);
  }

  function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 80; }
  function scrollDown(force) { if (force || nearBottom()) log.scrollTop = log.scrollHeight; }

  // ---------------------------------------------------------------- sending
  form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
  });
  input.addEventListener("input", autosize);
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 140) + "px";
  }

  async function send(raw) {
    const text = (raw || "").trim();
    if (!text || C.busy || C.configured === false) return;
    if (log.querySelector(".chat-welcome")) log.innerHTML = "";
    input.value = "";
    autosize();
    addUser(text);
    const bubble = addAssistant();
    const body = bubble.querySelector(".body"), steps = bubble.querySelector(".steps");
    C.busy = true;
    setEnabled(true);
    let full = "", pending = false, gotText = false;

    const paint = () => {
      pending = false;
      const stick = nearBottom();
      body.innerHTML = renderMarkdown(full);
      linkify(body);
      if (stick) scrollDown(true);
    };

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversation_id: C.id }),
      });
      if (!res.ok) {
        let msg = `Request failed (${res.status}).`;
        try { msg = (await res.json()).detail || msg; } catch (e) { /* ignore */ }
        throw new Error(msg);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === "conversation") {
            if (ev.reset) addNoticeBefore(bubble, "The plan was re-optimized, so I started a fresh conversation about the new plan.");
            C.id = ev.id;
          } else if (ev.type === "text") {
            gotText = true;
            full += ev.text;
            if (!pending) { pending = true; requestAnimationFrame(paint); }
          } else if (ev.type === "tool") {
            const s = document.createElement("div");
            s.className = "step";
            s.textContent = ev.label + "…";
            steps.append(s);
            if (!gotText) body.innerHTML = `<span class="typing"><i></i><i></i><i></i></span>`;
            scrollDown();
          } else if (ev.type === "error") {
            full += (full ? "\n\n" : "") + `⚠ ${ev.message}`;
            paint();
          }
        }
      }
      if (!full) full = "_(no answer)_";
      paint();
    } catch (err) {
      body.innerHTML = `<p class="chat-error">⚠ ${escapeHtml(err.message || "Something went wrong.")}</p>`;
    } finally {
      steps.classList.add("done");
      C.busy = false;
      setEnabled(true);
      input.focus();
    }
  }

  function addNoticeBefore(el, text) {
    const n = document.createElement("div");
    n.className = "chat-notice";
    n.textContent = text;
    el.before(n);
  }

  function newChat() {
    if (C.busy) return;
    C.id = null;
    log.innerHTML = "";
    renderEmpty();
    input.focus();
  }

  // when the plan is re-optimized, the next question starts a fresh conversation server-side
  let firstPlan = true;
  addEventListener("fro:plan", () => {
    if (firstPlan) { firstPlan = false; return; }
    if (log.querySelector(".msg")) addNotice("New plan loaded. Your next question will be answered using the new numbers.");
  });
})();
