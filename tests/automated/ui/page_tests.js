/**
 * Runs in Home Assistant's own page, with its real frontend components: the
 * replay card, and the tile card it can wear, against fake sessions.
 *
 * Each test checks that the branch's card has the feature first, and is
 * reported as skipped when it does not, so every branch can run all of them.
 * The card's source is in window.openneatoCardSource.
 */
(() => {
  const VACUUM = "vacuum.test_robot";
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
  const canon = (v) => (v && typeof v === "object" && !Array.isArray(v)
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
  const connection = () => hass().connection;
  function countWsCalls() {
    const conn = connection();
    if (conn.__openneatoCounting) return;
    conn.__openneatoCounting = true;
    const original = conn.sendMessagePromise.bind(conn);
    conn.sendMessagePromise = (message) => {
      if (String(message?.type).startsWith("openneato/")) wsCalls.push(message.type);
      return original(message);
    };
  }

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

  // A card as the dashboard hosts it: configured, then given hass
  function mount(config, into = stage()) {
    const el = document.createElement("openneato-replay-card");
    el.setConfig({ type: "custom:openneato-replay-card", height: 240, ...config });
    el.hass = hass();
    into.append(el);
    return el;
  }
  const clearStage = () => stage().replaceChildren();
  const forgetOpenState = () => {
    for (const key of Object.keys(localStorage)) if (key.startsWith("openneato-replay-card")) localStorage.removeItem(key);
  };

  const picker = (el) => el.shadowRoot.querySelector(".picker");
  const optionTexts = (el) => [...picker(el).options].map((o) => o.textContent);
  const readyPicker = (el) => until(() => picker(el).options.length === 3 && picker(el), "the session list");
  const dateOf = (epoch) => new Date(epoch * 1000).toLocaleString(undefined, {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });

  function deepText(root, depth = 10) {
    let text = root.textContent ?? "";
    if (depth === 0) return text;
    for (const el of root.querySelectorAll?.("*") ?? []) {
      if (el.shadowRoot) text += ` ${deepText(el.shadowRoot, depth - 1)}`;
    }
    return text;
  }
  function findDeep(root, selector, depth = 10) {
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

  /* ── session picker: the date, and only the date, while closed ───────── */

  const hasDateLabel = () => source().includes("CLOCK_SET_MIN_EPOCH");

  const tests = [];
  const test = (name, requires, run) => tests.push({ name, requires, run });

  test("picker: closed, it shows only the date of the session on screen", hasDateLabel, async () => {
    clearStage();
    const el = mount({});
    await readyPicker(el);
    const texts = optionTexts(el);
    for (const text of texts) expect(!text.includes("—"), `closed option still has the full line: ${text}`);
    const real = Number(picker(el).options[0].value.split(".")[0]);
    expectEqual(texts[0], dateOf(real), "the real session's date");
    expectEqual(picker(el).selectedOptions[0].textContent, dateOf(real), "the shown date");
  });

  test("picker: a clock that was never set reads Date unknown, not a day in 1970", hasDateLabel, async () => {
    clearStage();
    const el = mount({});
    await readyPicker(el);
    expectEqual(optionTexts(el).slice(1), ["Date unknown", "Date unknown"], "the unset sessions");
  });

  test("picker: open, every session shows its full line", hasDateLabel, async () => {
    clearStage();
    const el = mount({});
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
    const el = mount({});
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
    const el = mount({});
    await readyPicker(el);
    picker(el).dispatchEvent(new Event("pointerdown", { bubbles: true }));
    picker(el).value = picker(el).options[2].value;
    picker(el).dispatchEvent(new Event("change", { bubbles: true }));
    expectEqual(optionTexts(el), [optionTexts(el)[0], "Date unknown", "Date unknown"], "closed again");
    await until(() => deepText(el.shadowRoot).includes("Duration"), "the chosen session to load");
    expectEqual(picker(el).selectedOptions[0].textContent, "Date unknown", "the chosen session");
  });

  test("picker: losing focus closes the list, Escape too", hasDateLabel, async () => {
    clearStage();
    const el = mount({});
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
    const el = mount({ show_picker: false, session: "4020.jsonl" });
    await until(() => el.shadowRoot.querySelector(".stats")?.textContent.includes("Date unknown"), "the stats row");
    expect(!el.shadowRoot.querySelector(".stats").textContent.includes("1970"), "a 1970 date is shown");
  });

  /* ── tile summary that expands to the replay ─────────────────────────── */

  const hasTile = () => source().includes("collapsible");
  const drawer = (el) => el.shadowRoot.querySelector(".drawer");
  const tileOf = (el) => el.shadowRoot.querySelector("hui-tile-card");
  const chevron = (el) => el.shadowRoot.querySelector(".chevron");
  const tileInfo = (el) => findDeep(tileOf(el).shadowRoot, "ha-tile-info");
  const tileReady = (el) => until(() => tileOf(el) && deepText(tileOf(el).shadowRoot).includes("Test Robot") && tileInfo(el),
    "the tile card");
  const openneatoCalls = () => wsCalls.length;

  test("tile: without the option, the card is the replay alone, as before", hasTile, async () => {
    clearStage();
    forgetOpenState();
    const el = mount({});
    await readyPicker(el);
    expect(!tileOf(el), "a tile card was created");
    expect(visible(drawer(el)), "the replay is hidden");
    expect(!visible(el.shadowRoot.querySelector(".summary")), "the summary is shown");
  });

  test("tile: collapsed, it shows the tile and loads nothing from the robot", hasTile, async () => {
    clearStage();
    forgetOpenState();
    countWsCalls();
    const before = openneatoCalls();
    const el = mount({ collapsible: true, entity: VACUUM });
    await tileReady(el);
    await sleep(1200);
    expect(visible(el.shadowRoot.querySelector(".summary")), "the summary is hidden");
    expect(!visible(drawer(el)), "the replay is showing");
    expect(!visible(el.shadowRoot.querySelector("canvas")), "the canvas is showing");
    expectEqual(openneatoCalls() - before, 0, "calls to the integration while collapsed");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "false", "aria-expanded");
    expectEqual(el._pollTimer, null, "the poll timer");
  });

  test("tile: a tap on the tile opens the replay, and loads the sessions", hasTile, async () => {
    clearStage();
    forgetOpenState();
    countWsCalls();
    const before = openneatoCalls();
    const el = mount({ collapsible: true, entity: VACUUM });
    await tileReady(el);
    await press(tileInfo(el));
    await until(() => visible(drawer(el)), "the replay to show");
    await readyPicker(el);
    expect(openneatoCalls() > before, "no calls to the integration after opening");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "true", "aria-expanded");
    await until(() => deepText(el.shadowRoot).includes("Duration"), "the session to load");
    const canvas = el.shadowRoot.querySelector("canvas");
    expect(canvas.getBoundingClientRect().height > 100, "the canvas has no height");
    expect(visible(el.shadowRoot.querySelector(".summary")), "the tile went away when it opened");
  });

  test("tile: a second tap collapses it and stops all the work", hasTile, async () => {
    clearStage();
    forgetOpenState();
    countWsCalls();
    const el = mount({ collapsible: true, entity: VACUUM });
    await tileReady(el);
    await press(tileInfo(el));
    await until(() => visible(drawer(el)), "the replay to show");
    await readyPicker(el);
    await press(tileInfo(el));
    await until(() => !visible(drawer(el)), "the replay to hide");
    expectEqual(el._pollTimer, null, "the poll timer");
    expectEqual(el._raf, null, "the animation frame");
    const calls = openneatoCalls();
    await sleep(1500);
    expectEqual(openneatoCalls(), calls, "calls to the integration while collapsed");
  });

  test("tile: the chevron opens and closes it too, and says which", hasTile, async () => {
    clearStage();
    forgetOpenState();
    const el = mount({ collapsible: true, entity: VACUUM });
    await tileReady(el);
    await press(chevron(el));
    await until(() => visible(drawer(el)), "the replay to show");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "true", "open");
    await press(chevron(el));
    await until(() => !visible(drawer(el)), "the replay to hide");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "false", "closed");
    expect(chevron(el).getAttribute("aria-label"), "the chevron has no label");
  });

  test("tile: the choice is remembered for the next time the card shows", hasTile, async () => {
    clearStage();
    forgetOpenState();
    const first = mount({ collapsible: true, entity: VACUUM });
    await tileReady(first);
    await press(tileInfo(first));
    await until(() => visible(drawer(first)), "the replay to show");
    clearStage();
    const second = mount({ collapsible: true, entity: VACUUM });
    await tileReady(second);
    expect(visible(drawer(second)), "the card forgot it was open");
    await readyPicker(second);
    clearStage();
    forgetOpenState();
  });

  test("tile: expanded: true starts open until the person chooses otherwise", hasTile, async () => {
    clearStage();
    forgetOpenState();
    const el = mount({ collapsible: true, expanded: true, entity: VACUUM });
    await tileReady(el);
    expect(visible(drawer(el)), "not open");
    await readyPicker(el);
  });

  test("tile: with no entity in the options, it finds the OpenNeato robot itself", hasTile, async () => {
    clearStage();
    forgetOpenState();
    const el = mount({ collapsible: true });
    await tileReady(el);
    expect(deepText(tileOf(el).shadowRoot).includes("Test Robot"), "the tile does not show the robot");
  });

  test("tile: with no robot to find, a plain title row stands in for the tile", hasTile, async () => {
    clearStage();
    forgetOpenState();
    const el = document.createElement("openneato-replay-card");
    el.setConfig({ type: "custom:openneato-replay-card", height: 240, collapsible: true, title: "Robot cleans" });
    el.hass = { ...hass(), entities: {} };
    stage().append(el);
    await until(() => visible(el.shadowRoot.querySelector(".summary")), "the summary");
    expect(!tileOf(el), "a tile was made without an entity");
    expect(deepText(el.shadowRoot.querySelector(".summary")).includes("Robot cleans"), "the title is missing");
    await press(chevron(el));
    await until(() => visible(drawer(el)), "the replay to show");
  });

  /* ── visual editor ───────────────────────────────────────────────────── */

  const hasEditor = () => source().includes("openneato-replay-card-editor");
  const EDITOR = "openneato-replay-card-editor";

  // An editor as the dashboard hosts it: every saved configuration is handed
  // back to it, and kept here for the test to check
  async function hostedEditor(config) {
    const el = document.createElement(EDITOR);
    el.hass = hass();
    el.setConfig(config);
    const saved = [];
    el.addEventListener("config-changed", (ev) => {
      saved.push(ev.detail.config);
      el.setConfig(ev.detail.config);
    });
    stage().append(el);
    const form = await until(() => {
      const f = el.shadowRoot.querySelector("ha-form");
      return f?.schema?.length && f;
    }, "the editor's form");
    return { el, form, saved };
  }
  const fieldNames = (form) => form.schema.flatMap((item) => (item.schema ? item.schema.map((x) => x.name) : [item.name]));
  const edit = (form, changes) => form.dispatchEvent(new CustomEvent("value-changed", { detail: { value: { ...form.data, ...changes } } }));
  const last = (saved) => saved[saved.length - 1];

  test("editor: the card offers it to the dashboard", hasEditor, async () => {
    const card = customElements.get("openneato-replay-card");
    expectEqual(card.getConfigElement().tagName.toLowerCase(), EDITOR, "the editor element");
    expect(customElements.get(EDITOR), "the editor is not defined");
  });

  test("editor: the form shows labelled options, with the defaults filled in", hasEditor, async () => {
    clearStage();
    const { form } = await hostedEditor({ type: "custom:openneato-replay-card", entity: VACUUM });
    // The selectors load on demand, so each label shows up when its own part has loaded
    for (const label of ["Tile that opens the replay", "Title", "Map", "Parts of the card"]) {
      await until(() => deepText(form.shadowRoot).includes(label), `the label ${label}`);
    }
    // The entity picker draws its label inside a component of its own
    expectEqual(form.computeLabel({ name: "entity" }), "Robot (vacuum)", "the picker's label");
    expectEqual(form.data.units, "auto", "the default units");
    expectEqual(form.data.collapsible, false, "the default for the tile");
    expectEqual(form.data.show_picker, true, "the default for the picker");
  });

  test("editor: the robot picker is on screen", hasEditor, async () => {
    clearStage();
    const { form } = await hostedEditor({ type: "custom:openneato-replay-card", entity: VACUUM });
    await until(() => deepText(form.shadowRoot).includes("Tile that opens the replay"), "the form");
    const selector = form.shadowRoot.querySelector("ha-selector");
    const describe = () => {
      const inner = selector.shadowRoot?.firstElementChild;
      return `ha-selector ${Math.round(selector.getBoundingClientRect().height)}px, selector=${JSON.stringify(selector.selector)}, `
        + `inner=${inner?.tagName?.toLowerCase()} ${Math.round(inner?.getBoundingClientRect().height ?? -1)}px, `
        + `inner shadow=${inner?.shadowRoot?.innerHTML?.slice(0, 300)}, value=${JSON.stringify(selector.value)}, `
        + `defined: ${["ha-selector-entity", "ha-entity-picker", "ha-entity-combo-box", "ha-combo-box"].map((n) => `${n}=${!!customElements.get(n)}`).join(" ")}, `
        + `hass.entities=${Object.keys(hass().entities ?? {}).length}`;
    };
    try {
      await until(() => selector.getBoundingClientRect().height > 20, "the robot picker to show");
    } catch (err) {
      throw new Error(`${err.message}: ${describe()}`);
    }
  });

  test("editor: start state and state content appear only with the tile", hasEditor, async () => {
    clearStage();
    const { form } = await hostedEditor({ type: "custom:openneato-replay-card", entity: VACUUM });
    expect(!fieldNames(form).includes("expanded"), "expanded shown without the tile");
    expect(!fieldNames(form).includes("state_content"), "state content shown without the tile");
    edit(form, { collapsible: true });
    await until(() => fieldNames(form).includes("expanded") && fieldNames(form).includes("state_content"), "the tile's fields");
  });

  test("editor: what the form does not know stays exactly as it was", hasEditor, async () => {
    clearStage();
    const features = [{ type: "custom:something" }];
    const { form, saved } = await hostedEditor({
      type: "custom:openneato-replay-card", session: "4020.jsonl", height: "fill", name: "Alfred", features,
    });
    edit(form, { units: "imperial" });
    expectEqual(last(saved), {
      type: "custom:openneato-replay-card", session: "4020.jsonl", height: "fill", name: "Alfred", features, units: "imperial",
    }, "the saved configuration");
  });

  test("editor: a value set back to its default is dropped", hasEditor, async () => {
    clearStage();
    const { form, saved } = await hostedEditor({ type: "custom:openneato-replay-card" });
    edit(form, { units: "imperial", speed: 16 });
    expectEqual(last(saved), { type: "custom:openneato-replay-card", units: "imperial", speed: 16 }, "after the change");
    edit(form, { units: "auto" });
    expectEqual(last(saved), { type: "custom:openneato-replay-card", speed: 16 }, "back to auto");
  });

  test("editor: turning the tile on and off writes and drops collapsible", hasEditor, async () => {
    clearStage();
    const { form, saved } = await hostedEditor({ type: "custom:openneato-replay-card", entity: VACUUM });
    edit(form, { collapsible: true });
    expectEqual(last(saved).collapsible, true, "on");
    edit(form, { collapsible: false });
    expect(!("collapsible" in last(saved)), "collapsible stays in the YAML when off");
    expectEqual(last(saved).entity, VACUUM, "the robot");
  });

  test("editor: an edited configuration works in the card", hasEditor, async () => {
    clearStage();
    forgetOpenState();
    const { form, saved } = await hostedEditor({ type: "custom:openneato-replay-card" });
    edit(form, { collapsible: true, entity: VACUUM, state_content: ["state", "fan_speed"] });
    const el = mount(last(saved));
    await tileReady(el);
    await until(() => deepText(tileOf(el).shadowRoot).includes("normal") || deepText(tileOf(el).shadowRoot).includes("Normal"),
      "the fan speed on the tile");
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

  // What the screenshots show: the card with its picker closed, and the tile
  // card collapsed and open
  window.openneatoUiGallery = async () => {
    clearStage();
    forgetOpenState();
    const cards = [];
    cards.push(mount({}));
    await readyPicker(cards[0]);
    if (hasTile()) {
      const collapsed = mount({ collapsible: true, entity: VACUUM });
      await tileReady(collapsed);
      forgetOpenState();
      const opened = mount({ collapsible: true, expanded: true, entity: VACUUM });
      await tileReady(opened);
      await readyPicker(opened);
      await until(() => deepText(opened.shadowRoot).includes("Duration"), "the session to load");
    }
    if (hasEditor()) {
      const { form } = await hostedEditor({ type: "custom:openneato-replay-card", collapsible: true, entity: VACUUM });
      await until(() => deepText(form.shadowRoot).includes("Tile that opens the replay"), "the editor's labels");
    }
    await sleep(800);
    const r = stage().getBoundingClientRect();
    return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
  };
})();
