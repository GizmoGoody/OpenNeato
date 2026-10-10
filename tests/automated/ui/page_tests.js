/**
 * Runs in Home Assistant's own page, with its real frontend components: the
 * OpenNeato card (a tile card with tile features under it), the features
 * themselves, and the editor, against a fake robot and fake sessions.
 *
 * Each test checks that the branch has the feature first, and is reported as
 * skipped when it does not, so every branch can run all of them. The card's
 * source is in window.openneatoCardSource.
 */
(() => {
  const VACUUM = "vacuum.test_robot";
  const CARD = "openneato-card";
  const hass = () => document.querySelector("home-assistant").hass;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const source = () => window.openneatoCardSource || "";

  async function until(check, what, ms = 10000) {
    const end = Date.now() + ms;
    let last;
    while (Date.now() < end) {
      try {
        const value = await check();
        if (value) return value;
      } catch (err) {
        last = err;
      }
      await sleep(50);
    }
    throw new Error(`Timed out waiting for ${what}${last ? ` (${last})` : ""}`);
  }
  // Same keys and values, whatever order the keys are in
  const canon = (v) => (Array.isArray(v) ? v.map(canon) : v && typeof v === "object"
    ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v);
  function expectEqual(actual, expected, what) {
    if (JSON.stringify(canon(actual)) !== JSON.stringify(canon(expected))) {
      throw new Error(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    }
  }
  function expect(condition, what) {
    if (!condition) throw new Error(what);
  }

  // Every call the card makes to the integration, counted from here on
  const wsCalls = [];
  function countWsCalls() {
    const conn = hass().connection;
    if (conn.__openneatoCounting) return;
    conn.__openneatoCounting = true;
    const original = conn.sendMessagePromise.bind(conn);
    conn.sendMessagePromise = (message) => {
      if (String(message?.type).startsWith("openneato/")) wsCalls.push(message.type);
      return original(message);
    };
  }
  const openneatoCalls = () => wsCalls.length;

  function stage() {
    let el = document.getElementById("openneato-stage");
    if (!el) {
      el = document.createElement("div");
      el.id = "openneato-stage";
      Object.assign(el.style, {
        position: "absolute", left: "0", top: "0", width: "560px", padding: "16px", zIndex: "10000",
        display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: "16px", boxSizing: "border-box",
        background: "var(--primary-background-color)", color: "var(--primary-text-color)",
      });
      document.body.append(el);
    }
    return el;
  }
  const clearStage = () => stage().replaceChildren();
  const forgetOpenState = () => {
    for (const key of Object.keys(localStorage)) if (key.startsWith("openneato-card")) localStorage.removeItem(key);
  };

  function deepText(root, depth = 12) {
    let text = root.textContent ?? "";
    if (depth === 0) return text;
    for (const el of root.querySelectorAll?.("*") ?? []) {
      if (el.shadowRoot) text += ` ${deepText(el.shadowRoot, depth - 1)}`;
    }
    return text;
  }
  function findDeep(root, selector, depth = 12) {
    const found = root.querySelector?.(selector);
    if (found || depth === 0) return found;
    for (const el of root.querySelectorAll?.("*") ?? []) {
      const hit = el.shadowRoot && findDeep(el.shadowRoot, selector, depth - 1);
      if (hit) return hit;
    }
    return null;
  }
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";

  async function press(el) {
    const r = el.getBoundingClientRect();
    await window.cdpClick(r.left + r.width / 2, r.top + r.height / 2);
    await sleep(150);
  }

  // Home Assistant gives every card in a dashboard a new hass whenever a state
  // changes; a card on this page gets the same
  function follow(el) {
    const timer = setInterval(() => {
      if (!el.isConnected) clearInterval(timer);
      else el.hass = hass();
    }, 250);
  }

  const dateOf = (epoch) => new Date(epoch * 1000).toLocaleString(undefined, {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });

  const tests = [];
  const test = (name, requires, run) => tests.push({ name, requires, run });

  /* ── the replay map (a tile feature) ─────────────────────────────────── */

  const hasReplay = () => source().includes('"openneato-replay"');
  const hasDateLabel = () => hasReplay() || source().includes("CLOCK_SET_MIN_EPOCH");

  // The replay as a tile card gives it to its feature: configured, then given
  // hass and the robot
  function mountReplay(config = {}, into = stage()) {
    // The replay feature, or on an older branch the replay-only card
    const el = document.createElement(["openneato-replay", "openneato-replay-card"].find((t) => customElements.get(t)));
    el.setConfig({ type: "custom:openneato-replay", height: 240, ...config });
    el.context = { entity_id: VACUUM };
    el.hass = hass();
    into.append(el);
    follow(el);
    return el;
  }
  const picker = (el) => el.shadowRoot.querySelector(".picker");
  const optionTexts = (el) => [...picker(el).options].map((o) => o.textContent);
  const readyPicker = (el) => until(() => picker(el).options.length === 3 && picker(el), "the session list");
  const statsText = (el) => el.shadowRoot.querySelector(".stats").textContent;
  // The figures' names: in the text when they show, in the hover title when hidden
  const statsNames = (el) => [...el.shadowRoot.querySelectorAll(".stats span")].map((s) => s.title || s.textContent).join(" ");

  test("picker: closed, it shows only the date of the session on screen", hasDateLabel, async () => {
    clearStage();
    const el = mountReplay();
    await readyPicker(el);
    const texts = optionTexts(el);
    for (const text of texts) expect(!text.includes("—"), `closed option still has the full line: ${text}`);
    const real = Number(picker(el).options[0].value.split(".")[0]);
    expectEqual(texts[0], dateOf(real), "the real session's date");
    expectEqual(picker(el).selectedOptions[0].textContent, dateOf(real), "the shown date");
  });

  test("picker: a clock that was never set reads Date unknown, not a day in 1970", hasDateLabel, async () => {
    clearStage();
    const el = mountReplay();
    await readyPicker(el);
    expectEqual(optionTexts(el).slice(1), ["Date unknown", "Date unknown"], "the unset sessions");
  });

  test("picker: open, every session shows its full line", hasDateLabel, async () => {
    clearStage();
    const el = mountReplay();
    await readyPicker(el);
    picker(el).dispatchEvent(new Event("pointerdown", { bubbles: true }));
    const texts = optionTexts(el);
    expect(texts.every((t) => t.includes("—")), `not all full lines: ${JSON.stringify(texts)}`);
    expect(texts[0].includes("House Clean"), `first line lacks the mode: ${texts[0]}`);
    expect(texts[2].includes("Spot Clean"), `last line lacks the mode: ${texts[2]}`);
    expect(/ft²|m²/.test(texts[0]), `first line lacks the area: ${texts[0]}`);
    expect(texts[1].startsWith("Date unknown —"), `unset line: ${texts[1]}`);
  });

  test("picker: the header does not move when the list opens", hasDateLabel, async () => {
    clearStage();
    const el = mountReplay();
    await readyPicker(el);
    const closed = picker(el).getBoundingClientRect().width;
    picker(el).dispatchEvent(new Event("pointerdown", { bubbles: true }));
    const open = picker(el).getBoundingClientRect().width;
    expect(Math.abs(open - closed) < 1, `width ${closed} closed, ${open} open`);
    picker(el).dispatchEvent(new Event("blur"));
    const after = picker(el).getBoundingClientRect().width;
    expect(Math.abs(after - closed) < 1, `width ${closed} before, ${after} after`);
  });

  test("picker: choosing a session closes the list and shows only its date", hasDateLabel, async () => {
    clearStage();
    const el = mountReplay();
    await readyPicker(el);
    picker(el).dispatchEvent(new Event("pointerdown", { bubbles: true }));
    picker(el).value = picker(el).options[2].value;
    picker(el).dispatchEvent(new Event("change", { bubbles: true }));
    expectEqual(optionTexts(el).slice(1), ["Date unknown", "Date unknown"], "closed again");
    await until(() => statsNames(el).includes("Duration"), "the chosen session to load");
    expectEqual(picker(el).selectedOptions[0].textContent, "Date unknown", "the chosen session");
  });

  test("picker: losing focus closes the list, Escape too", hasDateLabel, async () => {
    clearStage();
    const el = mountReplay();
    await readyPicker(el);
    for (const close of [() => picker(el).dispatchEvent(new Event("blur")),
      () => picker(el).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))]) {
      picker(el).dispatchEvent(new Event("pointerdown", { bubbles: true }));
      expect(optionTexts(el).every((t) => t.includes("—")), "did not open");
      close();
      expect(optionTexts(el).every((t) => !t.includes("—")), "did not close");
    }
  });

  test("stats: with the picker hidden, an unset clock still says Date unknown", hasDateLabel, async () => {
    clearStage();
    const el = mountReplay({ header: { selector: false } });
    await readyPicker(el);
    await el._selectSession("4020.jsonl");
    await until(() => statsText(el).includes("Date unknown"), "the stats row");
    expect(!statsText(el).includes("1970"), "a 1970 date is shown");
  });

  test("stats: with the picker hidden, the map shows the latest run, and says which", hasReplay, async () => {
    clearStage();
    const el = mountReplay({ header: { selector: false } });
    await readyPicker(el);
    const newest = Number(picker(el).options[0].value.split(".")[0]);
    await until(() => statsText(el).includes(dateOf(newest)), "the newest run's date");
    expect(statsText(el).includes("House Clean"), `the mode is missing: ${statsText(el)}`);
  });

  test("stats: distance, duration and battery are each their own option", hasReplay, async () => {
    const shown = async (config, expectAny = true) => {
      clearStage();
      const el = mountReplay(config);
      await until(() => el._session, "the session to load");
      await sleep(300);
      return statsText(el);
    };
    const all = await shown({ header: { hide_labels: false } });
    for (const word of ["Distance", "Duration", "Battery"]) expect(all.includes(word), `${word} missing by default: ${all}`);
    for (const [option, word] of [["distance", "Distance"], ["duration", "Duration"], ["battery", "Battery"]]) {
      const text = await shown({ header: { [option]: false, hide_labels: false } });
      expect(!text.includes(word), `${option}: false still shows ${word}: ${text}`);
      for (const other of ["Distance", "Duration", "Battery"].filter((w) => w !== word)) {
        expect(text.includes(other), `${option}: false also hid ${other}: ${text}`);
      }
    }
    const none = await shown({ header: { distance: false, duration: false, battery: false } });
    expectEqual(none, "", "the stats row with all three off");
  });

  test("stats: the figures show without their names by default, the names on hover", hasReplay, async () => {
    clearStage();
    const el = mountReplay();
    await until(() => el._session && statsNames(el).includes("Duration"), "the session to load");
    const text = statsText(el);
    for (const word of ["Distance", "Duration", "Battery"]) expect(!text.includes(word), `${word} is shown: ${text}`);
    expect(text.includes("1:47") && text.includes("142"), `the figures are missing: ${text}`);
    const span = el.shadowRoot.querySelector('.stats span[title="Duration"]');
    expect(span && span.getAttribute("aria-label").startsWith("Duration "), "no name for hover and screen readers");
    clearStage();
    const labelled = mountReplay({ header: { hide_labels: false } });
    await until(() => labelled._session && statsText(labelled).includes("Duration"), "the names to show");
  });

  test("map: the live badge sits on the map, in the corner chosen, and stays there as it redraws", () => hasFeaturesList(), async () => {
    clearStage();
    const corner = async (config, expected) => {
      const el = mountReplay(config);
      await until(() => el._session, "the session to load");
      expect(!el.shadowRoot.querySelector(".controls button.live"), "the live badge is still in the controls row");
      // A live clean: the badge shows and redraws
      el._setLive(true);
      el._renderChip();
      el._renderChip();
      const badge = el.shadowRoot.querySelector(".stage .livebox button.live");
      expect(badge && visible(badge), "the live badge does not show on the map");
      const b = badge.getBoundingClientRect();
      const m = el.shadowRoot.querySelector(".stage").getBoundingClientRect();
      const insets = {
        top_left: [b.left - m.left, b.top - m.top],
        top_right: [m.right - b.right, b.top - m.top],
        bottom_left: [b.left - m.left, m.bottom - b.bottom],
        bottom_right: [m.right - b.right, m.bottom - b.bottom],
      }[expected].map(Math.round);
      expectEqual(insets, [8, 8], `the badge's distance from the two edges of its corner (${expected})`);
      el._setLive(false);
    };
    await corner({}, "bottom_right");
    await corner({ live_position: "top_left" }, "top_left");
  });

  test("map: on a narrow card the controls row stays inside the card", () => hasFeaturesList(), async () => {
    clearStage();
    const box = document.createElement("div");
    box.style.width = "280px";
    stage().append(box);
    const el = mountReplay({}, box);
    await until(() => el._session, "the session to load");
    const row = el.shadowRoot.querySelector(".controls");
    const edge = row.getBoundingClientRect().right;
    for (const child of row.children) {
      if (!visible(child)) continue;
      const right = child.getBoundingClientRect().right;
      expect(right <= edge + 0.5, `${child.className || child.tagName} ends at ${right}, past the row's edge at ${edge}`);
    }
  });

  test("stats: the figures above the map are spaced evenly when they fit", () => hasFeaturesList(), async () => {
    clearStage();
    const el = mountReplay({ header: { distance: false } });
    await until(() => el._session && el.shadowRoot.querySelectorAll(".stats span").length === 2, "the two figures");
    const row = el.shadowRoot.querySelector(".stats").getBoundingClientRect();
    const [one, two] = [...el.shadowRoot.querySelectorAll(".stats span")].map((s) => s.getBoundingClientRect());
    const gaps = [one.left - row.left, two.left - one.right, row.right - two.right].map(Math.round);
    expect(Math.max(...gaps) - Math.min(...gaps) <= 2, `uneven spacing: ${gaps}`);
  });

  test("stats: a live clean shows its distance so far, in the same order as after it", () => hasFeaturesList(), async () => {
    clearStage();
    const el = mountReplay();
    await until(() => el._session && statsNames(el).includes("Distance"), "the session to load");
    const names = () => statsNames(el).split(" ").filter((w) => ["Distance", "Duration", "Battery"].includes(w));
    const after = names();
    el._setLive(true);
    el._renderStats();
    const live = names();
    el._setLive(false);
    expectEqual(live, after, "the figures' order, live and after");
    expectEqual(live, ["Distance", "Duration", "Battery"], "the figures");
  });

  test("map: a floorplan of black walls on a transparent background is found, for Whole floorplan", () => hasFeaturesList(), async () => {
    clearStage();
    const el = mountReplay({ fit: "plan" });
    // As the integration draws it: black walls, everything else transparent
    const c = document.createElement("canvas");
    c.width = 100;
    c.height = 80;
    const ctx = c.getContext("2d");
    ctx.fillStyle = "rgba(0, 0, 0, 1)";
    ctx.fillRect(20, 10, 60, 4);
    ctx.fillRect(20, 10, 4, 50);
    const img = new Image();
    await new Promise((resolve) => {
      img.onload = resolve;
      img.src = c.toDataURL("image/png");
    });
    const bounds = el._planContentBounds(img, { scale: 20, originX: 0, originY: 0 });
    expect(bounds, "no floorplan found in the image");
    expectEqual([bounds.minX, bounds.maxX].map((v) => Math.round(v * 10) / 10), [1, 4], "the floorplan's width in metres");
  });

  test("speed: the button on the map remembers its choice for the next time", hasReplay, async () => {
    localStorage.removeItem("openneato-card:speed");
    clearStage();
    const first = mountReplay();
    const button = first.shadowRoot.querySelector(".speed");
    await until(() => first._session && !button.disabled, "the map to load");
    expectEqual(button.textContent.trim(), "8×", "the speed at first");
    await press(button);
    expectEqual(button.textContent.trim(), "16×", "after one press");
    clearStage();
    const second = mountReplay();
    expectEqual(second.shadowRoot.querySelector(".speed").textContent.trim(), "16×", "the speed on the next load");
    // From 16, the steps are 32, 64, 128 and 256, then round to 1 again
    const seen = [];
    await until(() => second._session && !second.shadowRoot.querySelector(".speed").disabled, "the second map to load");
    for (let i = 0; i < 5; i++) await press(second.shadowRoot.querySelector(".speed"));
    expectEqual(second.shadowRoot.querySelector(".speed").textContent.trim(), "1×", "after wrapping round");
    localStorage.removeItem("openneato-card:speed");
  });

  /* ── the card: a tile card, and its features ─────────────────────────── */

  const hasCard = () => source().includes("withReadouts");
  const hasFeaturesList = () => source().includes("openneato-feature-editor");
  const REPLAY = { type: "custom:openneato-replay" };
  const tileOf = (el) => el.shadowRoot.querySelector("hui-tile-card");
  const chevron = (el) => el.shadowRoot.querySelector(".chevron");
  const tileInfo = (el) => findDeep(tileOf(el).shadowRoot, "ha-tile-info");
  const tileText = (el) => deepText(tileOf(el).shadowRoot);
  const tileReady = (el) => until(() => tileOf(el) && tileText(el).includes("Test Robot") && tileInfo(el), "the tile card");
  const replayIn = (el) => findDeep(el.shadowRoot, "openneato-replay");
  const controlsIn = (el) => findDeep(el.shadowRoot, "openneato-controls");
  const suctionIn = (el) => findDeep(el.shadowRoot, "openneato-suction");
  const configIn = (el) => findDeep(el.shadowRoot, "openneato-configuration");
  const keysOf = (feature) => [...feature.shadowRoot.querySelectorAll("[data-key]")].map((b) => b.dataset.key);
  const keyed = (feature, key) => feature.shadowRoot.querySelector(`[data-key="${key}"]`);
  // The test frontend has no backend translations, so a binary sensor reads "on", not "Charging"
  const CHARGING = /Charging|(^|[^A-Za-z])on([^A-Za-z]|$)/;

  function mountCard(config = {}, h = hass()) {
    const el = document.createElement(CARD);
    el.setConfig({ type: `custom:${CARD}`, ...config });
    el.hass = h;
    stage().append(el);
    if (h === hass()) follow(el);
    return el;
  }

  test("card: the tile shows the robot, with State, Charging and Battery level by default", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, features: [REPLAY] });
    await tileReady(el);
    await until(() => CHARGING.test(tileText(el)) && tileText(el).includes("85"), "the sensors on the tile");
    const text = tileText(el);
    expect(/docked/i.test(text), `the state is missing: ${text}`);
    expect(!text.includes("ST_C_Standby") && !text.includes("UIMGR"), `a raw state is shown: ${text}`);
    expect(!text.includes("-55"), `the Wi-Fi signal is shown without being asked for: ${text}`);
    expect(text.indexOf("85") > text.search(CHARGING), `the order is not State, Charging, Battery level: ${text}`);
  });

  test("card: State content can show any sensor of the robot", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, state_content: ["state", "wifi_signal", "uptime"] });
    await tileReady(el);
    await until(() => tileText(el).includes("-55") && tileText(el).includes("29"), "the chosen sensors");
    expect(!CHARGING.test(tileText(el).replace("Test Robot", "")), "a sensor that was not chosen is shown");
  });

  test("card: a sensor with nothing to show is left out of the tile, but still offered in State content", hasFeaturesList, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, state_content: ["state", "error_message", "battery"] });
    await tileReady(el);
    expect(!("error_message" in el._tileHass().states[VACUUM].attributes), "the empty error message reaches the tile");
    const { tileEditor } = await cardEditor(cardConfig);
    expect("error_message" in tileEditor.hass.states[VACUUM].attributes, "the State content picker does not offer the error message");
  });

  test("card: the robot's sensors reach the tile as attributes, without the raw states, and are reused", hasCard, async () => {
    clearStage();
    const el = mountCard({ entity: VACUUM });
    await tileReady(el);
    const first = el._tileHass().states[VACUUM];
    const names = Object.keys(first.attributes);
    for (const wanted of ["battery", "charging", "wifi_signal", "uptime", "fan_speed"]) expect(names.includes(wanted), `no ${wanted}: ${names}`);
    for (const unwanted of ["robot_state", "ui_state"]) expect(!names.includes(unwanted), `${unwanted} is offered`);
    expect(el._tileHass().states[VACUUM] === first, "the state was made again with nothing changed, so the tile would redraw");
  });

  test("card: closed on load, with no feature created and nothing asked of the robot", hasCard, async () => {
    clearStage();
    forgetOpenState();
    countWsCalls();
    const before = openneatoCalls();
    const el = mountCard({ entity: VACUUM, features: [REPLAY, { type: "custom:openneato-controls", items: ["start"] }] });
    await tileReady(el);
    await sleep(1200);
    expect(!replayIn(el) && !controlsIn(el), "a feature was created while closed");
    expectEqual(openneatoCalls() - before, 0, "calls to the integration while closed");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "false", "aria-expanded");
  });

  test("card: a tap on the tile opens it, and the replay loads", hasCard, async () => {
    clearStage();
    forgetOpenState();
    countWsCalls();
    const before = openneatoCalls();
    const el = mountCard({ entity: VACUUM, features: [REPLAY] });
    await tileReady(el);
    await press(tileInfo(el));
    const replay = await until(() => replayIn(el), "the replay to appear");
    await until(() => replay.shadowRoot.querySelector(".picker")?.options.length === 3, "the session list");
    expect(openneatoCalls() > before, "no calls to the integration after opening");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "true", "aria-expanded");
    await until(() => statsNames(replay).includes("Duration"), "the session to load");
    expect(replay.shadowRoot.querySelector("canvas").getBoundingClientRect().height > 100, "the map has no height");
    expect(visible(tileInfo(el)), "the tile went away when it opened");
  });

  test("card: a second tap closes it, and the features go with their polling", hasCard, async () => {
    clearStage();
    forgetOpenState();
    countWsCalls();
    const el = mountCard({ entity: VACUUM, features: [REPLAY] });
    await tileReady(el);
    await press(tileInfo(el));
    await until(() => replayIn(el)?.shadowRoot.querySelector(".picker")?.options.length === 3, "the replay");
    await press(tileInfo(el));
    await until(() => !replayIn(el), "the replay to go");
    const calls = openneatoCalls();
    await sleep(1500);
    expectEqual(openneatoCalls(), calls, "calls to the integration while closed");
  });

  test("card: the arrow opens and closes it too, and says which", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, features: [REPLAY] });
    await tileReady(el);
    await press(chevron(el));
    await until(() => replayIn(el), "the replay to appear");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "true", "open");
    await press(chevron(el));
    await until(() => !replayIn(el), "the replay to go");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "false", "closed");
    expect(chevron(el).getAttribute("aria-label"), "the arrow has no label");
  });

  test("card: the choice is remembered, and Expanded on load only sets the start", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const first = mountCard({ entity: VACUUM, expanded: true, features: [REPLAY] });
    await tileReady(first);
    await until(() => replayIn(first), "an expanded card to open by itself");
    await press(tileInfo(first));
    await until(() => !replayIn(first), "the card to close");
    clearStage();
    const second = mountCard({ entity: VACUUM, expanded: true, features: [REPLAY] });
    await tileReady(second);
    await sleep(500);
    expect(!replayIn(second), "the card forgot that it was closed");
    forgetOpenState();
  });

  test("card: the replay map is optional, like any feature", hasFeaturesList, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, expanded: true, features: [{ type: "custom:openneato-controls", items: ["start"] }] });
    await until(() => controlsIn(el), "the controls");
    expect(!replayIn(el), "a replay map that was not in the features list");
  });

  test("card: without a robot in the options it finds the OpenNeato robot itself", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({});
    await tileReady(el);
    expect(tileText(el).includes("Test Robot"), "the tile does not show the robot");
  });

  test("card: with no robot to find, it says so, and has no arrow", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({}, { ...hass(), entities: {} });
    await sleep(300);
    expect(visible(el.shadowRoot.querySelector(".notice")), "no notice");
    expect(!visible(chevron(el)), "an arrow without a robot");
  });

  test("card picker: a preview of a representative card, and no description", hasFeaturesList, async () => {
    const entry = window.customCards.find((c) => c.type === CARD);
    expectEqual(entry, { type: CARD, name: "OpenNeato", preview: true }, "the card picker entry");
    expectEqual(document.createElement(CARD).getGridOptions(), { columns: "full", rows: "auto", min_columns: 6 }, "full width by default");
    expectEqual(customElements.get(CARD).getStubConfig(hass()), {
      type: `custom:${CARD}`, entity: VACUUM, state_content: ["state", "charging", "battery"],
      features: [REPLAY, { type: "custom:openneato-controls", items: ["start", "pause", "dock"] }],
    }, "the stub");
  });

  /* ── the features ────────────────────────────────────────────────────── */

  test("controls: the buttons are the ones chosen, in the order chosen, and do what they say", hasFeaturesList, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, expanded: true, features: [{ type: "custom:openneato-controls", items: ["dock", "start"] }] });
    const controls = await until(() => controlsIn(el), "the controls");
    await until(() => keysOf(controls).length === 2, "the buttons");
    expectEqual(keysOf(controls), ["dock", "start"], "the buttons");
    await press(keyed(controls, "start"));
    await until(() => hass().states[VACUUM].state === "cleaning", "the robot to start");
    await until(() => /cleaning/i.test(tileText(el)), "the tile to show it");
  });

  test("suction: its own feature, every level by default, the current one pressed", hasFeaturesList, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, expanded: true, features: [{ type: "custom:openneato-suction" }] });
    const suction = await until(() => suctionIn(el), "the suction");
    await until(() => keysOf(suction).length === 3, "the levels");
    expectEqual(keysOf(suction), ["eco", "normal", "intense"], "the levels");
    expectEqual(keyed(suction, "normal").getAttribute("aria-pressed"), "true", "the current level");
    await press(keyed(suction, "intense"));
    await until(() => keyed(suction, "intense").getAttribute("aria-pressed") === "true", "the level to change");
    expectEqual(keyed(suction, "normal").getAttribute("aria-pressed"), "false", "the old level");
  });

  test("suction: customized levels show in the order chosen", hasFeaturesList, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, expanded: true, features: [{ type: "custom:openneato-suction", fan_speeds: ["intense", "eco"] }] });
    const suction = await until(() => suctionIn(el), "the suction");
    await until(() => keysOf(suction).length === 2, "the levels");
    expectEqual(keysOf(suction), ["intense", "eco"], "the levels");
  });

  test("configuration: switches and choices in the order chosen, without the suction switches", hasFeaturesList, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({
      entity: VACUUM, expanded: true,
      features: [{ type: "custom:openneato-configuration", items: ["stealth_led", "eco_mode", "navigation_mode"] }],
    });
    const config = await until(() => configIn(el), "the configuration");
    await until(() => keysOf(config).length === 2, "the settings");
    expectEqual(keysOf(config), ["stealth_led", "navigation_mode"], "the settings (Eco mode is suction, so it is left out)");
    expectEqual(keyed(config, "stealth_led").textContent.trim(), "Stealth LED", "the label is the name on the device");
    expectEqual(keyed(config, "stealth_led").getAttribute("aria-pressed"), "true", "before");
    await press(keyed(config, "stealth_led"));
    await until(() => keyed(config, "stealth_led").getAttribute("aria-pressed") === "false", "the switch to turn off");
    const choice = keyed(config, "navigation_mode");
    expectEqual([...choice.options].map((o) => o.value), ["normal", "gentle", "deep"], "the choices");
    choice.value = "gentle";
    choice.dispatchEvent(new Event("change"));
    await until(() => hass().states["select.test_robot_navigation_mode"].state === "gentle", "the choice to change");
  });

  test("features: they work in any tile card for the robot", hasFeaturesList, async () => {
    clearStage();
    const helpers = await window.loadCardHelpers();
    const tile = await helpers.createCardElement({
      type: "tile", entity: VACUUM,
      features: [{ type: "custom:openneato-controls", items: ["start", "dock"] }, { type: "custom:openneato-suction" }],
    });
    tile.hass = hass();
    stage().append(tile);
    const controls = await until(() => findDeep(tile.shadowRoot, "openneato-controls"), "the controls in a plain tile card");
    await until(() => keysOf(controls).length === 2, "the buttons");
    expectEqual(keysOf(controls), ["start", "dock"], "the buttons");
    expect(findDeep(tile.shadowRoot, "openneato-suction"), "the suction in a plain tile card");
  });

  test("features: Home Assistant offers the four of them only for the OpenNeato robot", hasFeaturesList, async () => {
    const features = window.customCardFeatures.filter((f) => f.type.startsWith("openneato-"));
    expectEqual(features.map((f) => f.type).sort(),
      ["openneato-configuration", "openneato-controls", "openneato-replay", "openneato-suction"], "the features");
    for (const f of features) {
      expect(f.configurable, `${f.type} cannot be edited`);
      expect(f.isSupported(hass(), { entity_id: VACUUM }), `${f.type} is not offered for the robot`);
      expect(!f.isSupported(hass(), { entity_id: "sensor.test_robot_battery_level" }), `${f.type} is offered for a sensor`);
    }
  });

  /* ── the editors ─────────────────────────────────────────────────────── */

  // An editor as the dashboard hosts it: every saved configuration is handed
  // back to it, and kept here for the test to check
  function hosted(tag, config, context) {
    const el = document.createElement(tag);
    const saved = [];
    el.addEventListener("config-changed", (ev) => {
      saved.push(ev.detail.config);
      el.setConfig(ev.detail.config);
    });
    el.hass = hass();
    if (context) el.context = context;
    el.setConfig(config);
    stage().append(el);
    return { el, saved };
  }
  const last = (saved) => saved[saved.length - 1];
  const flat = (schema) => schema.flatMap((item) => (Array.isArray(item.schema) ? flat(item.schema) : [item]));
  const names = (schema) => flat(schema).filter((i) => i.name).map((i) => i.name);
  const cardConfig = { type: `custom:${CARD}`, entity: VACUUM, features: [REPLAY] };

  async function cardEditor(config) {
    const { el, saved } = hosted("openneato-card-editor", config);
    const tileEditor = await until(() => el.shadowRoot.querySelector("openneato-tile-card-editor"), "the tile card's editor");
    const tileForm = await until(() => tileEditor.shadowRoot?.querySelector("ha-form")?.schema && tileEditor.shadowRoot.querySelector("ha-form"),
      "the tile card editor's form");
    return { el, saved, tileEditor, tileForm, own: el.shadowRoot.getElementById("form") };
  }

  async function featureEditor(config) {
    const { el, saved } = hosted("openneato-feature-editor", config, { entity_id: VACUUM });
    const form = await until(() => el.querySelector("ha-form")?.schema && el.querySelector("ha-form"), "the feature's form");
    return { el, saved, form };
  }
  const editFeature = (form, changes) => form.dispatchEvent(new CustomEvent("value-changed", { detail: { value: { ...form.data, ...changes } } }));

  test("card editor: it is the tile card's own editor, with its features list, and without Tap behavior", hasFeaturesList, async () => {
    clearStage();
    const { tileEditor, tileForm } = await cardEditor(cardConfig);
    expect(tileEditor instanceof customElements.get("hui-tile-card-editor"), "not the tile card's editor");
    const fields = names(tileForm.schema);
    expect(fields.includes("entity") && fields.includes("state_content"), `the tile card's fields are missing: ${fields}`);
    expect(!fields.includes("tap_action"), "Tap behavior is offered, but a tap shows and hides the features");
    expect(fields.includes("icon_tap_action"), "the icon's tap behavior is missing");
    expectEqual(flat(tileForm.schema).find((i) => i.name === "entity").selector, { entity: { include_entities: [VACUUM] } }, "the robot picker");
    await until(() => findDeep(tileEditor.shadowRoot, "hui-card-features-editor"), "the tile card's features list");
  });

  test("card editor: one panel of its own, with Expanded on load", hasFeaturesList, async () => {
    clearStage();
    const { el, saved, own } = await cardEditor(cardConfig);
    expectEqual(el.shadowRoot.querySelector('[slot="header"]').textContent.trim(), "OpenNeato", "the panel");
    expectEqual(names(own.schema), ["expanded"], "the card's own options");
    expectEqual(own.computeLabel(own.schema[0]), "Expanded on load", "the label");
    own.dispatchEvent(new CustomEvent("value-changed", { detail: { value: { expanded: true } } }));
    expectEqual(last(saved), { ...cardConfig, expanded: true }, "on");
    own.dispatchEvent(new CustomEvent("value-changed", { detail: { value: { expanded: false } } }));
    expectEqual(last(saved), { ...cardConfig, expanded: false }, "off, and kept");
  });

  test("card editor: a change in the tile card's editor keeps Expanded on load", hasFeaturesList, async () => {
    clearStage();
    const { saved, tileEditor } = await cardEditor({ ...cardConfig, expanded: true });
    tileEditor.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config: { type: "tile", entity: VACUUM, name: "Robbie", features: [REPLAY] } }, bubbles: true, composed: true,
    }));
    expectEqual(last(saved), { type: `custom:${CARD}`, entity: VACUUM, name: "Robbie", features: [REPLAY], expanded: true }, "the saved card");
  });

  test("card editor: an option the tile card does not know shows its message, not an empty editor", hasFeaturesList, async () => {
    clearStage();
    // As the earlier OpenNeato card saved it, with its controls at the top
    const { el } = hosted("openneato-card-editor", { ...cardConfig, controls: ["start", "fan_speed"] });
    const alert = el.shadowRoot.querySelector("ha-alert");
    await until(() => !alert.hidden, "the error to show");
    expect(alert.textContent.includes("controls"), `the message does not name the option: ${alert.textContent}`);
  });

  test("card editor: State content is offered the robot's sensors by their names, not its raw states", hasFeaturesList, async () => {
    clearStage();
    const { tileEditor } = await cardEditor(cardConfig);
    const state = tileEditor.hass.states[VACUUM];
    const offered = Object.keys(state.attributes);
    for (const wanted of ["battery", "charging", "wifi_signal"]) expect(offered.includes(wanted), `no ${wanted} to pick: ${offered}`);
    for (const unwanted of ["robot_state", "ui_state", "battery_level"]) expect(!offered.includes(unwanted), `${unwanted} is offered`);
    const name = (a) => tileEditor.hass.formatEntityAttributeName(state, a);
    expectEqual([name("battery"), name("charging"), name("wifi_signal")], ["Battery level", "Charging", "WiFi signal"], "the names");
  });

  test("feature editor: Replay map lists its fields in the order they show on the card", hasFeaturesList, async () => {
    clearStage();
    const { form } = await featureEditor(REPLAY);
    expectEqual(names(form.schema), [
      "selector", "distance", "duration", "battery", "hide_labels", "autoplay", "units", "height", "fit", "live_position", "straighten", "degrees",
      "floorplan", "floorplan_tiled", "floorplan_opacity", "grid", "trail", "coverage_tiled",
    ], "the fields");
    const label = (name) => form.computeLabel(flat(form.schema).find((i) => i.name === name));
    expectEqual([label("selector"), label("straighten"), label("degrees"), label("grid"), label("coverage_tiled")],
      ["Session selector", "Straighten against the walls", "Rotation", "Background grid", "Tiled coverage and trail"], "the labels");
    expect(form.computeHelper(form.schema[0]).includes("latest run"), "no note on what the map shows without the selector");
    expectEqual(flat(form.schema).find((i) => i.name === "units").selector.select.options.map((o) => o.label),
      ["Auto", "Metric", "US customary"], "the units");
    expectEqual([form.data.straighten, form.data.degrees, form.data.floorplan, form.data.floorplan_tiled, form.data.grid],
      [true, 0, true, true, true], "the defaults");
  });

  test("feature editor: Replay map saves what is set, nested, default or not, and nothing else", hasFeaturesList, async () => {
    clearStage();
    const { form, saved } = await featureEditor(REPLAY);
    editFeature(form, { units: "us_customary", degrees: 90, floorplan_tiled: false, selector: false });
    expectEqual(last(saved), {
      ...REPLAY, units: "us_customary", header: { selector: false }, rotation: { degrees: 90 }, floorplan: { tiled: false },
    }, "the changes");
    editFeature(form, { degrees: 0, floorplan_tiled: true, selector: true });
    expectEqual(last(saved), {
      ...REPLAY, units: "us_customary", header: { selector: true }, rotation: { degrees: 0 }, floorplan: { tiled: true },
    }, "set back to the defaults, and kept");
    editFeature(form, { straighten: false, degrees: 180.5, grid: false, trail: false });
    expectEqual(last(saved), {
      ...REPLAY, units: "us_customary", header: { selector: true }, rotation: { straighten: false, degrees: 180.5 },
      floorplan: { tiled: true }, grid: false, coverage: { trail: false },
    }, "a fixed angle, no grid, no trail");
    expectEqual(flat(form.schema).find((i) => i.name === "degrees").helper, "The map's angle.", "the rotation's helper without straightening");
  });

  test("feature editor: Home Assistant's dividers set apart the options kept together in YAML", hasFeaturesList, async () => {
    clearStage();
    const { form } = await featureEditor(REPLAY);
    const layout = flat(form.schema).map((i) => (i.type === "divider" ? "|" : i.name)).join(" ");
    expectEqual(layout, "selector distance duration battery hide_labels | autoplay units height fit live_position | straighten degrees | "
      + "floorplan floorplan_tiled floorplan_opacity | grid | trail coverage_tiled", "the fields and dividers");
    await until(() => form.shadowRoot?.querySelector("ha-form-divider"), "a divider on the form");
  });

  test("feature editor: a group deleted from the YAML stays deleted when another option changes", hasFeaturesList, async () => {
    clearStage();
    const { el, form, saved } = await featureEditor({ ...REPLAY, header: { selector: false } });
    el.setConfig({ ...REPLAY });
    expectEqual(form.data.selector, true, "the selector back to its default in the form");
    editFeature(form, { autoplay: true });
    expectEqual(last(saved), { ...REPLAY, autoplay: true }, "the saved options");
  });

  test("feature editor: Tiled floorplan and Floorplan opacity show only with the floorplan", hasFeaturesList, async () => {
    clearStage();
    const { form, saved } = await featureEditor(REPLAY);
    editFeature(form, { floorplan: false });
    expectEqual(last(saved), { ...REPLAY, floorplan: { show: false } }, "the floorplan off");
    await until(() => !names(form.schema).includes("floorplan_tiled"), "the floorplan's fields to go");
    expect(!names(form.schema).includes("floorplan_opacity"), "Floorplan opacity is still shown");
  });

  test("rotation: straightened against the walls plus the degrees, or the degrees alone", hasFeaturesList, async () => {
    clearStage();
    const straight = mountReplay({ rotation: { degrees: 90 } });
    await until(() => straight._session, "the session to load");
    straight._floorplan = { viewRotation: 12 };
    expectEqual(straight._baseRotationDeg(), 102, "straightened, plus 90");
    const fixed = mountReplay({ rotation: { straighten: false, degrees: 90 } });
    await until(() => fixed._session, "the session to load");
    fixed._floorplan = { viewRotation: 12 };
    expectEqual(fixed._baseRotationDeg(), 90, "the degrees alone");
    fixed._updateAutoQuarter(400, 200, { minX: 0, maxX: 1, minY: 0, maxY: 10 });
    expectEqual(fixed._autoQuarter, 0, "no quarter turn without straightening");
  });

  test("feature editor: Controls is a sortable list, without the suction", hasFeaturesList, async () => {
    clearStage();
    const { form, saved } = await featureEditor({ type: "custom:openneato-controls", items: ["start"] });
    const selector = form.schema[0].selector.select;
    expect(selector.multiple && selector.reorder, "not a sortable list");
    expectEqual(selector.options.map((o) => o.value), ["start", "pause", "stop", "dock", "spot", "locate"], "the controls");
    editFeature(form, { items: ["dock", "start"] });
    expectEqual(last(saved), { type: "custom:openneato-controls", items: ["dock", "start"] }, "the order chosen");
    expectEqual(customElements.get("openneato-controls").getStubConfig(hass(), { entity_id: VACUUM }),
      { type: "custom:openneato-controls", items: ["start", "pause", "dock"] }, "what a new Controls feature starts with");
  });

  test("feature editor: Suction shows every level until customized, as Home Assistant's own editors do", hasFeaturesList, async () => {
    clearStage();
    const { form, saved } = await featureEditor({ type: "custom:openneato-suction" });
    expectEqual(names(form.schema), ["customize_fan_speeds"], "not customized");
    editFeature(form, { customize_fan_speeds: true });
    expectEqual(last(saved), { type: "custom:openneato-suction", fan_speeds: ["eco", "normal", "intense"] }, "customized");
    await until(() => names(form.schema).includes("fan_speeds"), "the levels list");
    editFeature(form, { customize_fan_speeds: false });
    expectEqual(last(saved), { type: "custom:openneato-suction" }, "not customized again");
  });

  test("feature editor: Configuration offers the robot's settings by name, without suction or the schedule", hasFeaturesList, async () => {
    clearStage();
    const { form } = await featureEditor({ type: "custom:openneato-configuration", items: [] });
    expectEqual(form.schema[0].selector.select.options.map((o) => o.label), ["Navigation mode", "Stealth LED"], "the settings");
  });

  test("editors: each feature opens the same feature editor", hasFeaturesList, async () => {
    for (const tag of ["openneato-replay", "openneato-controls", "openneato-suction", "openneato-configuration"]) {
      expectEqual(customElements.get(tag).getConfigElement().tagName.toLowerCase(), "openneato-feature-editor", `${tag}'s editor`);
    }
    expectEqual(customElements.get(CARD).getConfigElement().tagName.toLowerCase(), "openneato-card-editor", "the card's editor");
  });

  /* ── run ─────────────────────────────────────────────────────────────── */

  window.openneatoUiTests = async () => {
    const results = [];
    for (const { name, requires, run } of tests) {
      if (!requires()) {
        results.push({ name, ok: true, detail: "skipped: this branch does not have it" });
        continue;
      }
      try {
        await run();
        results.push({ name, ok: true, detail: "" });
      } catch (err) {
        results.push({ name, ok: false, detail: String(err?.stack ?? err) });
      }
    }
    forgetOpenState();
    return results;
  };

  // What the screenshots show: the card closed, open with the replay, open with
  // the other features, the card's editor, and the Replay map's editor
  window.openneatoUiGallery = async () => {
    clearStage();
    forgetOpenState();
    if (!hasFeaturesList()) {
      const el = mountReplay();
      await readyPicker(el);
    } else {
      const closed = mountCard({ entity: VACUUM, features: [REPLAY] });
      await tileReady(closed);
      forgetOpenState();
      const replay = mountCard({ entity: VACUUM, expanded: true, features: [REPLAY] });
      await tileReady(replay);
      await until(() => replayIn(replay) && statsNames(replayIn(replay)).includes("Duration"), "the replay");
      const more = mountCard({
        entity: VACUUM, expanded: true, state_content: ["state", "wifi_signal", "uptime"],
        features: [
          { type: "custom:openneato-controls", items: ["start", "pause", "dock", "locate"] },
          { type: "custom:openneato-suction" },
          { type: "custom:openneato-configuration", items: ["stealth_led", "navigation_mode"] },
        ],
      });
      await tileReady(more);
      await until(() => controlsIn(more) && suctionIn(more) && configIn(more) && keysOf(configIn(more)).length === 2, "the features");
      const { tileEditor } = await cardEditor(cardConfig);
      await until(() => findDeep(tileEditor.shadowRoot, "hui-card-features-editor"), "the features list");
      await featureEditor(REPLAY);
    }
    await sleep(1000);
    const r = stage().getBoundingClientRect();
    return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
  };
})();
