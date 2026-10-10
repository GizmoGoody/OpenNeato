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
  const CARD = "openneato-replay-card";
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
    for (const key of Object.keys(localStorage)) if (key.startsWith("openneato-replay-card")) localStorage.removeItem(key);
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
  function mountReplay(config = {}) {
    const el = document.createElement(hasReplay() ? "openneato-replay" : CARD);
    el.setConfig({ type: "custom:openneato-replay", height: 240, ...config });
    el.context = { entity_id: VACUUM };
    el.hass = hass();
    stage().append(el);
    return el;
  }
  const picker = (el) => el.shadowRoot.querySelector(".picker");
  const optionTexts = (el) => [...picker(el).options].map((o) => o.textContent);
  const readyPicker = (el) => until(() => picker(el).options.length === 3 && picker(el), "the session list");
  const statsText = (el) => el.shadowRoot.querySelector(".stats").textContent;

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
    await until(() => statsText(el).includes("Duration"), "the chosen session to load");
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
    const el = mountReplay({ show_picker: false, session: "4020.jsonl" });
    await until(() => statsText(el).includes("Date unknown"), "the stats row");
    expect(!statsText(el).includes("1970"), "a 1970 date is shown");
  });

  test("stats: with the picker hidden, the map shows the latest run, and says which", hasReplay, async () => {
    clearStage();
    const el = mountReplay({ show_picker: false });
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
    const all = await shown({});
    for (const word of ["Distance", "Duration", "Battery"]) expect(all.includes(word), `${word} missing by default: ${all}`);
    for (const [option, word] of [["show_distance", "Distance"], ["show_duration", "Duration"], ["show_battery", "Battery"]]) {
      const text = await shown({ [option]: false });
      expect(!text.includes(word), `${option}: false still shows ${word}: ${text}`);
      for (const other of ["Distance", "Duration", "Battery"].filter((w) => w !== word)) {
        expect(text.includes(other), `${option}: false also hid ${other}: ${text}`);
      }
    }
    const none = await shown({ show_distance: false, show_duration: false, show_battery: false });
    expectEqual(none, "", "the stats row with all three off");
  });

  /* ── the card: a tile card, and what is under it ─────────────────────── */

  const hasCard = () => source().includes("withReadouts");
  const tileOf = (el) => el.shadowRoot.querySelector("hui-tile-card");
  const chevron = (el) => el.shadowRoot.querySelector(".chevron");
  const tileInfo = (el) => findDeep(tileOf(el).shadowRoot, "ha-tile-info");
  const tileText = (el) => deepText(tileOf(el).shadowRoot);
  const tileReady = (el) => until(() => tileOf(el) && tileText(el).includes("Test Robot") && tileInfo(el), "the tile card");
  const replayIn = (el) => findDeep(el.shadowRoot, "openneato-replay");
  const controlsIn = (el) => findDeep(el.shadowRoot, "openneato-controls");
  const configIn = (el) => findDeep(el.shadowRoot, "openneato-configuration");
  const keysOf = (feature) => [...feature.shadowRoot.querySelectorAll("[data-key]")].map((b) => b.dataset.key);
  const keyed = (feature, key) => feature.shadowRoot.querySelector(`[data-key="${key}"]`);

  function mountCard(config = {}, h = hass()) {
    const el = document.createElement(CARD);
    el.setConfig({ type: `custom:${CARD}`, ...config });
    el.hass = h;
    stage().append(el);
    return el;
  }

  test("card: the tile shows the robot, with State, Charging and Battery level by default", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM });
    await tileReady(el);
    try {
      await until(() => tileText(el).includes("Charging") && tileText(el).includes("85"), "the sensors on the tile");
    } catch (err) {
      throw new Error(`${err.message}; the tile says: ${tileText(el).replace(/\s+/g, " ").slice(0, 300)}; state_content=${JSON.stringify(tileOf(el)._config?.state_content)}`);
    }
    const text = tileText(el);
    expect(/docked/i.test(text), `the state is missing: ${text}`);
    expect(!text.includes("ST_C_Standby") && !text.includes("UIMGR"), `a raw state is shown: ${text}`);
    expect(!text.includes("-55"), `the Wi-Fi signal is shown without being asked for: ${text}`);
  });

  test("card: State content can show any sensor of the robot", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, state_content: ["state", "wifi_signal", "uptime"] });
    await tileReady(el);
    await until(() => tileText(el).includes("-55") && tileText(el).includes("29"), "the chosen sensors");
    expect(!tileText(el).includes("Charging"), "a sensor that was not chosen is shown");
  });

  test("card: the robot's sensors reach the tile as attributes, without the raw states, and are reused", hasCard, async () => {
    clearStage();
    const el = mountCard({ entity: VACUUM });
    await tileReady(el);
    const first = el._tileHass().states[VACUUM];
    const names = Object.keys(first.attributes);
    for (const wanted of ["battery_level", "charging", "wifi_signal", "uptime", "fan_speed"]) expect(names.includes(wanted), `no ${wanted}: ${names}`);
    for (const unwanted of ["robot_state", "ui_state"]) expect(!names.includes(unwanted), `${unwanted} is offered`);
    expect(el._tileHass().states[VACUUM] === first, "the state was made again with nothing changed, so the tile would redraw");
  });

  test("card: closed on load, with nothing created and nothing asked of the robot", hasCard, async () => {
    clearStage();
    forgetOpenState();
    countWsCalls();
    const before = openneatoCalls();
    const el = mountCard({ entity: VACUUM, controls: ["start"] });
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
    const el = mountCard({ entity: VACUUM });
    await tileReady(el);
    await press(tileInfo(el));
    const replay = await until(() => replayIn(el), "the replay to appear");
    await until(() => replay.shadowRoot.querySelector(".picker")?.options.length === 3, "the session list");
    expect(openneatoCalls() > before, "no calls to the integration after opening");
    expectEqual(chevron(el).getAttribute("aria-expanded"), "true", "aria-expanded");
    await until(() => replay.shadowRoot.querySelector(".stats").textContent.includes("Duration"), "the session to load");
    expect(replay.shadowRoot.querySelector("canvas").getBoundingClientRect().height > 100, "the map has no height");
    expect(visible(tileInfo(el)), "the tile went away when it opened");
  });

  test("card: a second tap closes it, and the features go with their polling", hasCard, async () => {
    clearStage();
    forgetOpenState();
    countWsCalls();
    const el = mountCard({ entity: VACUUM });
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
    const el = mountCard({ entity: VACUUM });
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
    const first = mountCard({ entity: VACUUM, expanded: true });
    await tileReady(first);
    await until(() => replayIn(first), "an expanded card to open by itself");
    await press(tileInfo(first));
    await until(() => !replayIn(first), "the card to close");
    clearStage();
    const second = mountCard({ entity: VACUUM, expanded: true });
    await tileReady(second);
    await sleep(500);
    expect(!replayIn(second), "the card forgot that it was closed");
    forgetOpenState();
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

  test("controls: the buttons are the ones chosen, in the order chosen", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, expanded: true, controls: ["dock", "start", "fan_speed"] });
    const controls = await until(() => controlsIn(el), "the controls");
    await until(() => keysOf(controls).length === 5, "the buttons");
    expectEqual(keysOf(controls), ["dock", "start", "fan_eco", "fan_normal", "fan_intense"], "the buttons");
    expectEqual(keyed(controls, "fan_normal").getAttribute("aria-pressed"), "true", "the current suction");
  });

  test("controls: a button does what it says, through Home Assistant", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, expanded: true, controls: ["start", "fan_speed"] });
    const controls = await until(() => controlsIn(el), "the controls");
    await until(() => keyed(controls, "start"), "the Start button");
    await press(keyed(controls, "start"));
    await until(() => /cleaning/i.test(tileText(el)), "the robot to start");
    await press(keyed(controls, "fan_intense"));
    await until(() => keyed(controls, "fan_intense").getAttribute("aria-pressed") === "true", "the suction to change");
    expectEqual(keyed(controls, "fan_normal").getAttribute("aria-pressed"), "false", "the old suction");
  });

  test("configuration: switches toggle and choices change, in the order chosen", hasCard, async () => {
    clearStage();
    forgetOpenState();
    const el = mountCard({ entity: VACUUM, expanded: true, configuration: ["stealth_led", "eco_mode", "navigation_mode"] });
    const config = await until(() => configIn(el), "the configuration");
    await until(() => keysOf(config).length === 3, "the controls");
    expectEqual(keysOf(config), ["stealth_led", "eco_mode", "navigation_mode"], "the order");
    expectEqual(keyed(config, "eco_mode").textContent.trim(), "Eco mode", "the label is the name on the device");
    expectEqual(keyed(config, "eco_mode").getAttribute("aria-pressed"), "false", "eco mode before");
    await press(keyed(config, "eco_mode"));
    await until(() => keyed(config, "eco_mode").getAttribute("aria-pressed") === "true", "eco mode to turn on");
    const select = keyed(config, "navigation_mode");
    expectEqual([...select.options].map((o) => o.value), ["normal", "gentle", "deep"], "the choices");
    select.value = "gentle";
    select.dispatchEvent(new Event("change"));
    await until(() => hass().states["select.test_robot_navigation_mode"].state === "gentle", "the choice to change");
  });

  test("features: they work in any tile card for the robot", hasCard, async () => {
    clearStage();
    const helpers = await window.loadCardHelpers();
    const tile = await helpers.createCardElement({
      type: "tile", entity: VACUUM, features: [{ type: "custom:openneato-controls", items: ["start", "dock"] }],
    });
    tile.hass = hass();
    stage().append(tile);
    const controls = await until(() => findDeep(tile.shadowRoot, "openneato-controls"), "the controls in a plain tile card");
    await until(() => keysOf(controls).length === 2, "the buttons");
    expectEqual(keysOf(controls), ["start", "dock"], "the buttons");
  });

  test("features: Home Assistant offers them only for the OpenNeato robot", hasCard, async () => {
    const features = window.customCardFeatures.filter((f) => f.type.startsWith("openneato-"));
    expectEqual(features.map((f) => f.type).sort(), ["openneato-configuration", "openneato-controls", "openneato-replay"], "the features");
    for (const f of features) {
      expect(f.configurable, `${f.type} cannot be edited`);
      expect(f.isSupported(hass(), { entity_id: VACUUM }), `${f.type} is not offered for the robot`);
      expect(!f.isSupported(hass(), { entity_id: "sensor.test_robot_battery_level" }), `${f.type} is offered for a sensor`);
    }
  });

  /* ── the editor ──────────────────────────────────────────────────────── */

  const EDITOR = "openneato-editor";
  const hasEditor = () => source().includes('"openneato-editor"');

  // An editor as the dashboard hosts it: every saved configuration is handed
  // back to it, and kept here for the test to check
  async function hostedEditor(config, context) {
    const el = document.createElement(EDITOR);
    el.hass = hass();
    if (context) el.context = context;
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
  const fieldNames = (schema) => schema.flatMap((item) => (item.schema ? fieldNames(item.schema) : [item.name]));
  const sections = (form) => form.schema.filter((item) => item.type === "expandable");
  const field = (form, name) => form.schema.flatMap((i) => (i.schema ? i.schema : [i])).find((i) => i.name === name);
  const edit = (form, changes) => form.dispatchEvent(new CustomEvent("value-changed", { detail: { value: { ...form.data, ...changes } } }));
  const last = (saved) => saved[saved.length - 1];
  const cardConfig = { type: `custom:${CARD}`, entity: VACUUM };

  test("editor: the card offers it, and each feature its own", hasEditor, async () => {
    expectEqual(customElements.get(CARD).getConfigElement().tagName.toLowerCase(), EDITOR, "the card's editor");
    for (const tag of ["openneato-replay", "openneato-controls", "openneato-configuration"]) {
      expectEqual(customElements.get(tag).getConfigElement().tagName.toLowerCase(), EDITOR, `${tag}'s editor`);
    }
  });

  test("editor: Robot and State content first, then Replay map, Controls and Configuration", hasEditor, async () => {
    clearStage();
    const { form } = await hostedEditor(cardConfig);
    expectEqual(form.schema.map((i) => i.name || i.title), ["entity", "state_content", "Replay map", "Controls", "Configuration"], "the form's parts");
    expectEqual(form.schema[1].selector.ui_state_content.entity_id, VACUUM, "State content is for the robot");
    for (const text of ["Replay map", "Controls", "Configuration"]) {
      await until(() => deepText(form.shadowRoot).includes(text), `the ${text} section`);
    }
    expect(!deepText(form.shadowRoot).includes("Parts of the card"), "the old Parts of the card section is still there");
  });

  test("editor: Replay map has Expanded on load first, then the options in the order they show on the card", hasEditor, async () => {
    clearStage();
    const { form } = await hostedEditor(cardConfig);
    expectEqual(fieldNames(sections(form)[0].schema),
      ["expanded", "show_picker", "show_distance", "show_duration", "show_battery", "units", "height", "fit", "grid", "trail", "autoplay", "speed"],
      "the Replay map options");
    expectEqual(fieldNames(form.schema).includes("show_stats"), false, "the old show_stats switch");
  });

  test("editor: labels are plain, and Units uses Home Assistant's own words", hasEditor, async () => {
    clearStage();
    const { form } = await hostedEditor(cardConfig);
    const label = (name) => form.computeLabel({ name });
    expectEqual(label("expanded"), "Expanded on load", "expanded");
    expectEqual(label("speed"), "Starting playback speed", "speed");
    expectEqual(["show_picker", "show_distance", "show_duration", "show_battery"].map(label), ["Session picker", "Distance", "Duration", "Battery"], "the parts");
    expectEqual(field(form, "units").selector.select.options.map((o) => o.label), ["Auto (from Home Assistant)", "Metric", "US customary"], "the units");
    expect(form.computeHelper({ name: "show_picker" }).includes("latest run"), "no note on what the map shows without the picker");
  });

  test("editor: State content is offered the robot's sensors, not its raw states", hasEditor, async () => {
    clearStage();
    const { form } = await hostedEditor(cardConfig);
    const names = Object.keys(form.hass.states[VACUUM].attributes);
    for (const wanted of ["battery_level", "charging", "wifi_signal"]) expect(names.includes(wanted), `no ${wanted} to pick: ${names}`);
    for (const unwanted of ["robot_state", "ui_state"]) expect(!names.includes(unwanted), `${unwanted} is offered`);
    expectEqual(form.data.state_content, ["state", "charging", "battery_level"], "the default State content");
  });

  test("editor: the defaults are filled in, and a value set back to its default is dropped", hasEditor, async () => {
    clearStage();
    const { form, saved } = await hostedEditor(cardConfig);
    expectEqual([form.data.show_picker, form.data.show_distance, form.data.units, form.data.expanded], [true, true, "auto", false], "the defaults");
    edit(form, { units: "imperial", speed: 16, show_distance: false });
    expectEqual(last(saved), { ...cardConfig, units: "imperial", speed: 16, show_distance: false }, "after the change");
    edit(form, { units: "auto", show_distance: true });
    expectEqual(last(saved), { ...cardConfig, speed: 16 }, "back to the defaults");
  });

  test("editor: what the form does not know stays exactly as it was", hasEditor, async () => {
    clearStage();
    const features = [{ type: "custom:something" }];
    const { form, saved } = await hostedEditor({ ...cardConfig, session: "4020.jsonl", name: "Alfred", features });
    edit(form, { units: "imperial" });
    expectEqual(last(saved), { ...cardConfig, session: "4020.jsonl", name: "Alfred", features, units: "imperial" }, "the saved configuration");
  });

  test("editor: Controls and Configuration are sortable lists of what the robot has", hasEditor, async () => {
    clearStage();
    const { form, saved } = await hostedEditor(cardConfig);
    const controls = field(form, "controls").selector.select;
    expect(controls.multiple && controls.reorder, "Controls is not a sortable multiple choice");
    expectEqual(controls.options.map((o) => o.value), ["start", "pause", "stop", "dock", "spot", "locate", "fan_speed"], "the controls");
    const settings = field(form, "configuration").selector.select;
    expect(settings.multiple && settings.reorder, "Configuration is not a sortable multiple choice");
    expectEqual(settings.options.map((o) => o.label), ["Eco mode", "Navigation mode", "Stealth LED"], "the settings, by name, without the schedule's days");
    edit(form, { controls: ["dock", "start"] });
    expectEqual(last(saved).controls, ["dock", "start"], "the order chosen");
    edit(form, { controls: [] });
    expect(!("controls" in last(saved)), "an empty list stays in the YAML");
  });

  test("editor: each feature has its own form, with the same options and order", hasEditor, async () => {
    clearStage();
    const replay = await hostedEditor({ type: "custom:openneato-replay", units: "imperial" }, { entity_id: VACUUM });
    expectEqual(fieldNames(replay.form.schema),
      ["show_picker", "show_distance", "show_duration", "show_battery", "units", "height", "fit", "grid", "trail", "autoplay", "speed"], "the replay's options");
    edit(replay.form, { show_picker: false });
    expectEqual(last(replay.saved), { type: "custom:openneato-replay", units: "imperial", show_picker: false }, "the replay's saved options");
    const controls = await hostedEditor({ type: "custom:openneato-controls", items: ["start"] }, { entity_id: VACUUM });
    expectEqual(fieldNames(controls.form.schema), ["items"], "the controls' fields");
    edit(controls.form, { items: ["dock", "start"] });
    expectEqual(last(controls.saved).items, ["dock", "start"], "the controls' order");
  });

  test("editor: an edited configuration works in the card", hasEditor, async () => {
    clearStage();
    forgetOpenState();
    const { form, saved } = await hostedEditor(cardConfig);
    edit(form, { expanded: true, controls: ["start"], state_content: ["state", "wifi_signal"] });
    const el = mountCard(last(saved));
    await tileReady(el);
    await until(() => tileText(el).includes("-55"), "the chosen sensor on the tile");
    await until(() => controlsIn(el) && keysOf(controlsIn(el)).includes("start"), "the chosen control");
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
  // controls and settings, and the editor
  window.openneatoUiGallery = async () => {
    clearStage();
    forgetOpenState();
    if (!hasCard()) {
      const el = mountReplay();
      await readyPicker(el);
    } else {
      const closed = mountCard({ entity: VACUUM });
      await tileReady(closed);
      forgetOpenState();
      const replay = mountCard({ entity: VACUUM, expanded: true });
      await tileReady(replay);
      await until(() => replayIn(replay)?.shadowRoot.querySelector(".stats").textContent.includes("Duration"), "the replay");
      const more = mountCard({
        entity: VACUUM, expanded: true, state_content: ["state", "wifi_signal", "uptime"],
        controls: ["start", "pause", "dock", "locate", "fan_speed"], configuration: ["eco_mode", "stealth_led", "navigation_mode"],
      });
      await tileReady(more);
      await until(() => controlsIn(more) && keysOf(controlsIn(more)).length >= 5 && configIn(more) && keysOf(configIn(more)).length === 3, "the controls");
    }
    if (hasEditor()) {
      const { form } = await hostedEditor(cardConfig);
      for (const text of ["Replay map", "Controls", "Configuration"]) await until(() => deepText(form.shadowRoot).includes(text), text);
    }
    await sleep(800);
    const r = stage().getBoundingClientRect();
    return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
  };
})();
